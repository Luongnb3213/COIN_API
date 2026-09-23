from __future__ import annotations

import gzip
import json
import random
import re
import uuid
from dataclasses import dataclass
from html import unescape
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener, urlopen

from src import config
from src.core.fuyoura_client import FuyouraClient, FuyouraError
from src.utils.logger import get_logger
from src.utils.proxy_health import urllib_proxy_url


log = get_logger("coin_api_client")

PROFILE_HEADER_KEYS = {
    "accept",
    "accept-language",
    "priority",
    "user-agent",
    "x-sgc-app-version",
    "x-sgc-client-id",
    "x-sgc-device-model",
    "x-sgc-device-os",
}

IOS_VERSIONS = [
    "26.0",
    "26.0.1",
    "26.1",
    "26.2",
    "26.3",
    "26.4",
    "26.5",
    "26.6",
]

# Map mã lỗi nghiệp vụ của COIN -> thông báo tiếng Việt cho error_details.
COIN_ERROR_MESSAGES = {
    # この電話番号はすでに他のアカウントで使われています。
    "10085": "Số điện thoại này đã được sử dụng bởi một tài khoản khác",
}


class CoinApiError(RuntimeError):
    def __init__(self, step_status: str, message: str, *, final_status: str = "FAILED") -> None:
        super().__init__(message)
        self.step_status = step_status
        self.final_status = final_status


class DeviceProfileError(RuntimeError):
    pass


@dataclass
class CoinRegisterResult:
    coin_id: str
    customer_status: str
    pcard_status: str
    phone_number: str = ""
    gift1_status: str = ""
    gift2_status: str = ""
    card_number: str = ""
    card_name: str = ""
    card_expiry: str = ""
    security_code: str = ""
    card_url: str = ""


@dataclass
class CoinCardInfo:
    card_number: str = ""
    card_name: str = ""
    card_expiry: str = ""
    security_code: str = ""
    card_url: str = ""


def build_register_payload(account: dict) -> dict[str, Any]:
    return {
        "password": str(account.get("password") or "").strip(),
        "katakanaFirstName": str(account.get("katakana_first_name") or "").strip(),
        "katakanaLastName": str(account.get("katakana_last_name") or "").strip(),
        "screenId": config.COIN_SCREEN_ID,
        "phoneNumber": str(account.get("phone") or "").strip(),
        "simpleAuthenticationCode": str(account.get("pin") or "").strip(),
        "dateOfBirth": str(account.get("date_of_birth") or "").strip(),
        "cmsTermsOfServiceSetId": config.COIN_CMS_TERMS_OF_SERVICE_SET_ID,
    }


def _load_device_profiles() -> list[dict[str, Any]]:
    path = Path(str(config.COIN_DEVICE_PROFILES_PATH)).expanduser()
    if not path.is_absolute():
        path = config.ROOT_DIR / path

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_profiles = json.load(f)
    except FileNotFoundError:
        raise DeviceProfileError(f"Không thấy file device profile: {path}") from None
    except Exception as exc:
        raise DeviceProfileError(f"Không đọc được file device profile {path}: {exc}") from exc

    profiles: list[dict[str, Any]] = []
    if isinstance(raw_profiles, dict):
        raw_profiles = raw_profiles.get("profiles", [])
    if isinstance(raw_profiles, list):
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            device_id = str(item.get("device_id") or item.get("deviceId") or "").strip()
            global_setting = str(item.get("globalSetting") or item.get("global_setting") or "").strip()
            if device_id and global_setting:
                profile: dict[str, Any] = {"device_id": device_id, "globalSetting": global_setting}
                raw_headers = item.get("headers")
                if isinstance(raw_headers, dict):
                    headers = {
                        str(key).lower(): str(value)
                        for key, value in raw_headers.items()
                        if str(key).lower() in PROFILE_HEADER_KEYS and value not in (None, "")
                    }
                    if headers:
                        profile["headers"] = headers
                profiles.append(profile)

    if not profiles:
        raise DeviceProfileError(
            f"File device profile {path} không có cặp hợp lệ. "
            "Mỗi item cần có device_id và globalSetting."
        )
    return profiles


def validate_device_profiles() -> None:
    _load_device_profiles()


def pick_device_profile() -> dict[str, Any]:
    return dict(random.choice(_load_device_profiles()))


def generate_headers(device_id: str, profile_headers: dict[str, Any] | None = None) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "x-sgc-app-version": "1.76.0",
        "x-sgc-client-id": "00001",
        "x-sgc-device-id": device_id,
        "x-sgc-device-os": str(config.COIN_DEVICE_OS or f"iOS/{random.choice(IOS_VERSIONS)}"),
        "x-sgc-device-model": "iPhone",
        "accept-language": "vi-VN;q=1.0",
        "user-agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Mobile/15E148 - SGCAPP"
        ),
    }
    if isinstance(profile_headers, dict):
        headers.update(
            {
                str(key).lower(): str(value)
                for key, value in profile_headers.items()
                if str(key).lower() in PROFILE_HEADER_KEYS and value not in (None, "")
            }
        )
    headers["x-sgc-device-id"] = device_id
    return headers


def build_sms_verify_payload(otp_code: str, sms_authentication_id: str) -> dict[str, Any]:
    return {
        "smsAuthenticationCode": otp_code,
        "smsAuthenticationId": sms_authentication_id,
    }


def mask_phone(phone: str) -> str:
    text = str(phone or "")
    return f"{text[:3]}******{text[-2:]}" if len(text) >= 7 else "***"


def _loggable_headers(headers: dict[str, str]) -> dict[str, str]:
    output = dict(headers)
    auth = output.get("Authorization") or output.get("authorization")
    if auth and len(auth) > 24:
        key = "Authorization" if "Authorization" in output else "authorization"
        output[key] = auth[:18] + "..." + auth[-6:]
    return output


def _loggable_body(text: str, *, limit: int = 3000) -> str:
    clean = text.replace("\n", "\\n")
    return clean if len(clean) <= limit else clean[:limit] + "...<truncated>"


class CoinApiClient:
    """HTTP client for the Coin registration flow."""

    def __init__(self, proxy: dict | None = None) -> None:
        self.device_profile = pick_device_profile()
        self.headers = generate_headers(self.device_profile["device_id"], self.device_profile.get("headers"))
        self.headers.update(config.COIN_HTTP_HEADERS)
        self.proxy = proxy or {}
        self.opener = None
        self.web_cookie_jar = CookieJar()
        self.web_opener = None
        if self.proxy:
            proxy_url = urllib_proxy_url(self.proxy)
            proxy_handler = ProxyHandler({"http": proxy_url, "https": proxy_url})
            self.opener = build_opener(proxy_handler)
            self.web_opener = build_opener(proxy_handler, HTTPCookieProcessor(self.web_cookie_jar))
            log.info("Dùng proxy cho flow: %s", self.proxy.get("server") or self.proxy.get("raw"))
        else:
            self.web_opener = build_opener(HTTPCookieProcessor(self.web_cookie_jar))
        log.info(
            "Tạo header flow: device_id=%s os=%s cookie=%s",
            self.headers.get("x-sgc-device-id", "")[:8] + "...",
            self.headers.get("x-sgc-device-os", ""),
            self.headers.get("cookie", "<chưa có>"),
        )

    def register_account(self, account: dict, progress, on_phone=None) -> CoinRegisterResult:
        payload = build_register_payload(account)
        self._validate_account(account, payload)
        log.info("Bắt đầu flow register (Fuyoura thuê số).")
        otp_client = self._otp_client()

        # Fuyoura cấp số điện thoại (rent-a-number): số do Fuyoura trả về, không
        # lấy từ Excel. Phải thuê số trước khi gọi SMS request.
        try:
            rented = otp_client.get_number()
        except FuyouraError as exc:
            raise CoinApiError("SMS_REQUEST_FAILED", f"FUYOURA_ERROR: {exc}") from exc
        order_id = rented["order"]
        payload["phoneNumber"] = rented["number"]
        # Ghi số vừa thuê ra Excel NGAY (trước SMS request). Nhờ vậy dù flow sau
        # đó lỗi/bị dừng, số vẫn được lưu lại để biết số nào đã dùng.
        if on_phone:
            try:
                on_phone(rented["number"])
            except Exception:
                log.debug("on_phone callback lỗi, bỏ qua.", exc_info=True)
        masked_phone = mask_phone(payload["phoneNumber"])
        log.info(
            "[%s] Fuyoura cấp số (order=%s, country=%s, project=%s).",
            masked_phone,
            order_id,
            config.FUYOURA_COUNTRY,
            config.FUYOURA_PROJECT,
        )

        self._prepare_sms_session(masked_phone)

        try:
            log.info("[%s] Gửi SMS request.", masked_phone)
            sms_request_data = self._post_json(
                "/v2/authentications/sms-new-entry/request",
                {"phoneNumber": payload["phoneNumber"]},
                step_status="SMS_REQUEST_FAILED",
                update_cookie=False,
            )
            sms_authentication_id = self._pick(sms_request_data, "smsAuthenticationId")
            if not sms_authentication_id:
                raise CoinApiError("SMS_REQUEST_FAILED", "API không trả về smsAuthenticationId.")
            progress("SMS_REQUEST_OK")
            log.info("[%s] SMS request OK, smsAuthenticationId=%s, chờ Fuyoura lấy mã.", masked_phone, sms_authentication_id)

            otp_code = otp_client.wait_for_code(order_id)
        except FuyouraError as exc:
            # Chưa có mã -> trả số về cho Fuyoura (không bị tính phí).
            otp_client.safe_cancel(order_id)
            raise CoinApiError("SMS_VERIFY_FAILED", f"FUYOURA_ERROR: {exc}") from exc
        except BaseException:
            # SMS request lỗi / bị dừng trước khi có mã -> trả số về.
            otp_client.safe_cancel(order_id)
            raise
        log.info("[%s] Fuyoura đã lấy được OTP: %s", masked_phone, otp_code)
        log.info(
            "[%s] Verify SMS OTP payload: smsAuthenticationCode=%s smsAuthenticationId=%s",
            masked_phone,
            otp_code,
            sms_authentication_id,
        )
        sms_verify_data = self._post_json(
            "/authentications/sms-new-entry/verify",
            build_sms_verify_payload(otp_code, str(sms_authentication_id)),
            step_status="SMS_VERIFY_FAILED",
            update_cookie=False,
        )
        authentication_token = self._pick(sms_verify_data, "authenticationToken")
        if not authentication_token:
            raise CoinApiError("SMS_VERIFY_FAILED", "API không trả về authenticationToken.")
        progress("SMS_VERIFY_OK")
        log.info("[%s] SMS verify OK.", masked_phone)

        log.info("[%s] Check PIN.", masked_phone)
        self._post_json(
            "/authentications/pin/validity-check",
            {"simpleAuthenticationCode": payload["simpleAuthenticationCode"]},
            bearer_token=str(authentication_token),
            step_status="PIN_CHECK_FAILED",
        )
        progress("PIN_CHECK_OK")
        log.info("[%s] PIN check OK.", masked_phone)

        log.info("[%s] Register customer.", masked_phone)
        register_data = self._post_json(
            "/v3/customers/register",
            payload,
            bearer_token=str(authentication_token),
            step_status="REGISTER_FAILED",
        )
        register_token = self._pick(register_data, "authenticationToken") or authentication_token
        progress("REGISTER_OK")
        log.info("[%s] Register OK.", masked_phone)

        gift_statuses = self._grant_gift_codes(account, str(register_token), progress, masked_phone)
        card_info = self._fetch_card_info(str(register_token), payload["simpleAuthenticationCode"], progress, masked_phone)

        log.info("[%s] Check customer status.", masked_phone)
        status_data = self._get_json(
            "/v2/customers/status",
            bearer_token=str(register_token),
            step_status="ACCOUNT_CHECK_FAILED",
        )
        log.info("[%s] Get customer profile.", masked_phone)
        customer_data = self._get_json(
            "/v4/customers?configurationMode=0",
            bearer_token=str(register_token),
            step_status="ACCOUNT_CHECK_FAILED",
        )
        progress("ACCOUNT_ACTIVE")

        coin_id = self._pick(customer_data, "coinId", "coin_id", "customerId", "customer_id", "id")
        customer_status = self._pick(status_data, "customerStatus", "customer_status", "status") or "ACTIVE"
        pcard_status = self._pick(customer_data, "pcardStatus", "pcard_status", "prepaidCardStatus") or ""
        if not coin_id:
            raise CoinApiError(
                "ACCOUNT_CHECK_FAILED",
                "API đăng ký thành công nhưng response không có coin_id/customerId/id.",
                final_status="FAILED",
            )
        log.info("[%s] Account active: coin_id=%s status=%s pcard=%s.", masked_phone, coin_id, customer_status, pcard_status)

        return CoinRegisterResult(
            coin_id=str(coin_id),
            customer_status=str(customer_status),
            pcard_status=str(pcard_status),
            phone_number=str(payload["phoneNumber"]),
            gift1_status=gift_statuses[0],
            gift2_status=gift_statuses[1],
            card_number=card_info.card_number,
            card_name=card_info.card_name,
            card_expiry=card_info.card_expiry,
            security_code=card_info.security_code,
            card_url=card_info.card_url,
        )

    def _grant_gift_codes(self, account: dict, bearer_token: str, progress, masked_phone: str) -> tuple[str, str]:
        codes = self._gift_codes_for_account(account)
        statuses = ["SKIPPED_NO_CODE", "SKIPPED_NO_CODE"]
        if not any(codes[:2]):
            log.info("[%s] Không có gift_code_1/gift_code_2, bỏ qua grant gift code.", masked_phone)
            return statuses[0], statuses[1]
        for index, gift_code in enumerate(codes[:2], start=1):
            if not gift_code:
                continue
            log.info("[%s] Grant gift code #%s.", masked_phone, index)
            self._post_json(
                "/transactions/gift-codes/grant",
                {"giftCode": gift_code},
                bearer_token=bearer_token,
                step_status=f"GIFT_CODE_{index}_FAILED",
            )
            statuses[index - 1] = "OK"
            progress(f"GIFT_CODE_{index}_OK")
            log.info("[%s] Gift code #%s OK.", masked_phone, index)
        return statuses[0], statuses[1]

    @staticmethod
    def _gift_codes_for_account(account: dict) -> list[str]:
        codes = [
            str(account.get("gift_code_1") or "").strip(),
            str(account.get("gift_code_2") or "").strip(),
        ]
        fallback = list(getattr(config, "COIN_GIFT_CODES", []) or [])
        for index in range(2):
            if not codes[index] and index < len(fallback):
                codes[index] = str(fallback[index] or "").strip()
        return codes

    def _fetch_card_info(self, register_token: str, pin: str, progress, masked_phone: str) -> CoinCardInfo:
        log.info("[%s] Verify PIN for PCARD webview.", masked_phone)
        pin_data = self._post_json(
            "/authentications/pin/verify",
            {"functionCode": "PCARD", "simpleAuthenticationCode": str(pin)},
            bearer_token=register_token,
            step_status="PCARD_PIN_VERIFY_FAILED",
        )
        pcard_token = self._pick(pin_data, "authenticationToken")
        if not pcard_token:
            raise CoinApiError("PCARD_PIN_VERIFY_FAILED", "API không trả về PCARD authenticationToken.")
        progress("PCARD_PIN_VERIFY_OK")

        log.info("[%s] Issue PCARD webview OTP.", masked_phone)
        otp_data = self._post_empty_json(
            "/authentications/pcard-webview-otp/issue",
            bearer_token=str(pcard_token),
            step_status="PCARD_WEBVIEW_OTP_FAILED",
        )
        otp_code = str(self._pick(otp_data, "otpCode") or "").strip()
        login_url = str(self._pick(otp_data, "pcardWebViewUrl") or "https://web.coinplus-prepaid.jp/login").strip()
        redirect_url = str(self._pick(otp_data, "redirectUrl") or "/topMenu").strip()
        if not otp_code:
            raise CoinApiError("PCARD_WEBVIEW_OTP_FAILED", "API không trả về otpCode.")
        progress("PCARD_WEBVIEW_OTP_OK")

        log.info("[%s] Webview login.", masked_phone)
        login_body = urlencode({"redirectUrl": redirect_url, "otpCode": otp_code}).encode("utf-8")
        login_status, login_html = self._web_request(
            login_url,
            method="POST",
            body=login_body,
            headers=self._webview_headers(
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                content_type="application/x-www-form-urlencoded",
                origin="null",
                sec_fetch_site="none",
                sec_fetch_mode="navigate",
                sec_fetch_dest="document",
            ),
            step_status="PCARD_WEBVIEW_LOGIN_FAILED",
            log_body=False,
        )
        if not 200 <= login_status < 300:
            raise CoinApiError("PCARD_WEBVIEW_LOGIN_FAILED", f"HTTP {login_status}: login webview thất bại.")
        form_fields = self._extract_form_inputs(login_html)
        if not form_fields:
            raise CoinApiError("PCARD_WEBVIEW_LOGIN_FAILED", "Không parse được form authenticate từ HTML login.")
        if not any(name == "otpCode" and value == otp_code for name, value in form_fields):
            form_fields.append(("otpCode", otp_code))

        authenticate_url = urljoin(login_url, "/authenticate")
        log.info("[%s] Webview authenticate.", masked_phone)
        auth_status, _ = self._web_request(
            authenticate_url,
            method="POST",
            body=urlencode(form_fields).encode("utf-8"),
            headers=self._webview_headers(
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                content_type="application/x-www-form-urlencoded",
                origin="https://web.coinplus-prepaid.jp",
                referer=login_url,
                sec_fetch_site="same-origin",
                sec_fetch_mode="navigate",
                sec_fetch_dest="document",
            ),
            step_status="PCARD_WEBVIEW_AUTH_FAILED",
            log_body=False,
        )
        if not 200 <= auth_status < 400:
            raise CoinApiError("PCARD_WEBVIEW_AUTH_FAILED", f"HTTP {auth_status}: authenticate webview thất bại.")
        progress("PCARD_WEBVIEW_AUTH_OK")

        log.info("[%s] Get card URL from webview.", masked_phone)
        card_url_status, card_url_text = self._web_request(
            "https://web.coinplus-prepaid.jp/getCardNum",
            method="GET",
            headers=self._webview_headers(
                accept="*/*",
                referer="https://web.coinplus-prepaid.jp/topMenu",
                sec_fetch_site="same-origin",
                sec_fetch_mode="cors",
                sec_fetch_dest="empty",
            ),
            step_status="PCARD_CARD_URL_FAILED",
            log_body=False,
        )
        if not 200 <= card_url_status < 300:
            raise CoinApiError("PCARD_CARD_URL_FAILED", f"HTTP {card_url_status}: getCardNum thất bại.")
        try:
            card_url = str(json.loads(card_url_text).get("url") or "").strip()
        except ValueError as exc:
            raise CoinApiError("PCARD_CARD_URL_FAILED", f"getCardNum không trả JSON hợp lệ: {card_url_text[:300]}") from exc
        if not card_url:
            raise CoinApiError("PCARD_CARD_URL_FAILED", "getCardNum không trả url.")

        log.info("[%s] Fetch Paycierge card HTML.", masked_phone)
        card_status, card_html = self._web_request(
            card_url,
            method="GET",
            headers=self._paycierge_headers(),
            step_status="PCARD_CARD_HTML_FAILED",
            log_body=False,
        )
        if not 200 <= card_status < 300:
            raise CoinApiError("PCARD_CARD_HTML_FAILED", f"HTTP {card_status}: lấy HTML card thất bại.")
        card_info = self._parse_card_html(card_html, card_url)
        if not card_info.card_number or not card_info.security_code:
            raise CoinApiError("PCARD_CARD_HTML_FAILED", "HTML card không có card_number/security_code.")
        progress("PCARD_CARD_INFO_OK")
        log.info(
            "[%s] Card info OK: number_len=%s expiry=%s name=%s.",
            masked_phone,
            len(card_info.card_number.replace(" ", "")),
            card_info.card_expiry,
            card_info.card_name,
        )
        return card_info

    @staticmethod
    def _otp_client() -> FuyouraClient:
        return FuyouraClient(
            config.FUYOURA_API_KEY,
            timeout=config.OTP_WAIT_TIMEOUT,
            poll_interval=config.OTP_POLL_INTERVAL,
            base_url=config.FUYOURA_BASE_URL,
            country=config.FUYOURA_COUNTRY,
            project=config.FUYOURA_PROJECT,
        )

    def _prepare_sms_session(self, masked_phone: str) -> None:
        log.info("[%s] Preflight session trước SMS: devices/global-setting.", masked_phone)
        self.headers.pop("cookie", None)
        global_setting_query = urlencode({"globalSetting": self.device_profile["globalSetting"]})
        self._request(
            self._url(f"/devices/global-setting?{global_setting_query}"),
            method="GET",
            step_status="SMS_REQUEST_FAILED",
        )

        if "cookie" not in self.headers:
            fallback = str(uuid.uuid4())
            self.headers["cookie"] = f"XSRF-TOKEN={fallback}"
            log.warning("[%s] Preflight không lấy được XSRF từ Set-Cookie; fallback XSRF-TOKEN=%s.", masked_phone, fallback)

        log.info("[%s] Preflight session trước SMS: campaign/banners/info.", masked_phone)
        self._request(
            self._url("/campaign/banners/info"),
            method="GET",
            step_status="SMS_REQUEST_FAILED",
            update_cookie=False,
        )
        log.info(
            "[%s] Preflight xong, dùng %s với device_id=%s os=%s cho SMS request/verify.",
            masked_phone,
            self.headers.get("cookie"),
            self.headers.get("x-sgc-device-id"),
            self.headers.get("x-sgc-device-os"),
        )

    @staticmethod
    def _url(path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(config.COIN_BASE_URL + "/", path.lstrip("/"))

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        bearer_token: str = "",
        step_status: str,
        update_cookie: bool = True,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status_code, response_text = self._request(
            self._url(path),
            method="POST",
            body=body,
            bearer_token=bearer_token,
            step_status=step_status,
            update_cookie=update_cookie,
        )
        return self._parse_response(status_code, response_text, step_status)

    def _get_json(self, path: str, *, bearer_token: str, step_status: str, update_cookie: bool = True) -> dict[str, Any]:
        status_code, response_text = self._request(
            self._url(path),
            method="GET",
            bearer_token=bearer_token,
            step_status=step_status,
            update_cookie=update_cookie,
        )
        return self._parse_response(status_code, response_text, step_status)

    def _post_empty_json(self, path: str, *, bearer_token: str, step_status: str) -> dict[str, Any]:
        status_code, response_text = self._request(
            self._url(path),
            method="POST",
            bearer_token=bearer_token,
            step_status=step_status,
        )
        return self._parse_response(status_code, response_text, step_status)

    def _web_request(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        step_status: str,
        body: bytes | None = None,
        log_body: bool = True,
    ) -> tuple[int, str]:
        request = Request(url, data=body, headers=headers, method=method)
        label = self._web_log_label(url)
        log.info("WEB %s %s bắt đầu.", method, label)
        log.info("WEB %s %s request headers: %s", method, label, json.dumps(headers, ensure_ascii=False))
        if body is not None:
            log.info("WEB %s %s request body: %s", method, label, body.decode("utf-8", errors="replace"))
        else:
            log.info("WEB %s %s request body: <empty>", method, label)
        try:
            opener = self.web_opener or build_opener(HTTPCookieProcessor(self.web_cookie_jar))
            with opener.open(request, timeout=config.REQUEST_TIMEOUT) as response:
                text = self._read_response_text(response)
                log.info("WEB %s %s -> %s.", method, label, response.status)
                log.info("WEB %s %s response headers: %s", method, label, json.dumps(dict(response.headers.items()), ensure_ascii=False))
                if log_body:
                    log.info("WEB %s %s response body: %s", method, label, _loggable_body(text) if text else "<empty>")
                else:
                    log.info("WEB %s %s response body: <hidden len=%s>", method, label, len(text))
                return response.status, text
        except HTTPError as exc:
            text = self._read_response_text(exc)
            log.warning("WEB %s %s -> %s.", method, label, exc.code)
            log.warning("WEB %s %s response headers: %s", method, label, json.dumps(dict(exc.headers.items()), ensure_ascii=False))
            if log_body:
                log.warning("WEB %s %s response body: %s", method, label, _loggable_body(text) if text else "<empty>")
            else:
                log.warning("WEB %s %s response body: <hidden len=%s>", method, label, len(text))
            return exc.code, text
        except URLError as exc:
            log.warning("WEB %s %s lỗi mạng: %s", method, label, exc.reason)
            raise CoinApiError(step_status, f"NETWORK_ERROR: {exc.reason}") from exc
        except OSError as exc:
            log.warning("WEB %s %s lỗi mạng: %s", method, label, exc)
            raise CoinApiError(step_status, f"NETWORK_ERROR: {exc}") from exc

    @staticmethod
    def _web_log_label(url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc == "prepaidcube-multi.paycierge.com" and parsed.path.endswith("/card"):
            return parsed.netloc + parsed.path + "?<redacted>"
        return parsed.netloc + parsed.path + (f"?{parsed.query}" if parsed.query else "")

    @staticmethod
    def _read_response_text(response) -> str:
        raw = response.read()
        encoding = str(response.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        charset = "utf-8"
        content_type = str(response.headers.get("Content-Type") or "")
        match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
        if match:
            charset = match.group(1).strip("\"'")
        return raw.decode(charset, errors="replace")

    def _webview_headers(
        self,
        *,
        accept: str,
        content_type: str = "",
        origin: str = "",
        referer: str = "",
        sec_fetch_site: str = "",
        sec_fetch_mode: str = "",
        sec_fetch_dest: str = "",
    ) -> dict[str, str]:
        headers = {
            "User-Agent": self._webview_user_agent(),
            "Accept": accept,
            "Accept-Language": "vi-VN,vi;q=0.9",
            "Priority": "u=0, i" if sec_fetch_mode == "navigate" else "u=3, i",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if origin:
            headers["Origin"] = origin
        if referer:
            headers["Referer"] = referer
        if sec_fetch_site:
            headers["Sec-Fetch-Site"] = sec_fetch_site
        if sec_fetch_mode:
            headers["Sec-Fetch-Mode"] = sec_fetch_mode
        if sec_fetch_dest:
            headers["Sec-Fetch-Dest"] = sec_fetch_dest
        return headers

    def _paycierge_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._webview_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://web.coinplus-prepaid.jp/",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Accept-Language": "vi-VN,vi;q=0.9",
            "Priority": "u=0, i",
        }

    def _webview_user_agent(self) -> str:
        user_agent = str(self.headers.get("user-agent") or self.headers.get("User-Agent") or "")
        if "SGCAPP-Webview" in user_agent:
            return user_agent
        if "Mobile/15E148 - SGCAPP" in user_agent:
            return user_agent.replace("Mobile/15E148 - SGCAPP", "- SGCAPP-Webview")
        return (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) - SGCAPP-Webview"
        )

    @staticmethod
    def _extract_form_inputs(html_text: str) -> list[tuple[str, str]]:
        fields: list[tuple[str, str]] = []
        for tag_match in re.finditer(r"<input\b[^>]*>", html_text, flags=re.I):
            attrs = {
                key.lower(): unescape(value)
                for key, value in re.findall(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["']([^"']*)["']""", tag_match.group(0))
            }
            name = attrs.get("name")
            if name:
                fields.append((name, attrs.get("value", "")))
        return fields

    @staticmethod
    def _parse_card_html(html_text: str, card_url: str) -> CoinCardInfo:
        def extract_span(span_id: str) -> str:
            match = re.search(
                rf"""<span\b[^>]*id=["']{re.escape(span_id)}["'][^>]*>(.*?)</span>""",
                html_text,
                flags=re.I | re.S,
            )
            if not match:
                return ""
            return CoinApiClient._clean_html_text(match.group(1))

        card_number = extract_span("copyNumber")
        card_name = extract_span("copyCard")
        card_expiry = extract_span("copyYkk")
        security_code = extract_span("copySct")

        if not card_number:
            match = re.search(r"(\d{4}(?:&nbsp;|\s)+\d{4}(?:&nbsp;|\s)+\d{4}(?:&nbsp;|\s)+\d{4})", html_text)
            card_number = CoinApiClient._clean_html_text(match.group(1)) if match else ""
        if not card_expiry:
            match = re.search(r"\b(\d{2}/\d{2})\b", html_text)
            card_expiry = match.group(1) if match else ""
        if not security_code:
            match = re.search(r"""id=["']copySct["'][^>]*>\s*(\d{3,4})\s*<""", html_text, flags=re.I)
            security_code = match.group(1) if match else ""

        return CoinCardInfo(
            card_number=card_number,
            card_name=card_name,
            card_expiry=card_expiry,
            security_code=security_code,
            card_url=card_url,
        )

    @staticmethod
    def _clean_html_text(text: str) -> str:
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = unescape(clean).replace("\xa0", " ")
        return re.sub(r"\s+", " ", clean).strip()

    def _request(
        self,
        url: str,
        *,
        method: str,
        body: bytes | None = None,
        bearer_token: str = "",
        authorization_header: str = "",
        step_status: str,
        update_cookie: bool = True,
    ) -> tuple[int, str]:
        headers = dict(self.headers)
        if body is None:
            headers.pop("content-type", None)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif authorization_header:
            headers["Authorization"] = authorization_header
        request = Request(url, data=body, headers=headers, method=method)
        parsed = urlparse(url)
        label = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        log.info("HTTP %s %s bắt đầu.", method, label)
        log.info("HTTP %s %s request headers: %s", method, label, json.dumps(_loggable_headers(headers), ensure_ascii=False))
        if body is not None:
            log.info("HTTP %s %s request body: %s", method, label, body.decode("utf-8", errors="replace"))
        else:
            log.info("HTTP %s %s request body: <empty>", method, label)
        try:
            open_func = self.opener.open if self.opener is not None else urlopen
            with open_func(request, timeout=config.REQUEST_TIMEOUT) as response:
                text = response.read().decode("utf-8", errors="replace")
                if update_cookie:
                    self._update_xsrf_from_response(response.headers.get_all("Set-Cookie", []), label)
                log.info("HTTP %s %s -> %s.", method, label, response.status)
                log.info("HTTP %s %s response headers: %s", method, label, json.dumps(dict(response.headers.items()), ensure_ascii=False))
                log.info("HTTP %s %s response body: %s", method, label, _loggable_body(text) if text else "<empty>")
                return response.status, text
        except HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            if update_cookie:
                self._update_xsrf_from_response(exc.headers.get_all("Set-Cookie", []), label)
            log.warning("HTTP %s %s -> %s.", method, label, exc.code)
            log.warning("HTTP %s %s response headers: %s", method, label, json.dumps(dict(exc.headers.items()), ensure_ascii=False))
            log.warning("HTTP %s %s response body: %s", method, label, _loggable_body(text) if text else "<empty>")
            return exc.code, text
        except URLError as exc:
            log.warning("HTTP %s %s lỗi mạng: %s", method, label, exc.reason)
            raise CoinApiError(step_status, f"NETWORK_ERROR: {exc.reason}") from exc
        except OSError as exc:
            log.warning("HTTP %s %s lỗi mạng: %s", method, label, exc)
            raise CoinApiError(step_status, f"NETWORK_ERROR: {exc}") from exc

    @staticmethod
    def _parse_response(status_code: int, response_text: str, step_status: str) -> dict[str, Any]:
        text = response_text.strip()
        try:
            data = json.loads(text) if text else {}
        except ValueError as exc:
            raise CoinApiError(
                step_status,
                f"API trả về không phải JSON (HTTP {status_code}): {text[:500]}",
                final_status="FAILED",
            ) from exc

        if not 200 <= status_code < 300:
            code = str(CoinApiClient._pick(data, "code", "errorCode") or "").strip()
            message = CoinApiClient._pick(data, "message", "error", "errorMessage", "detail") or text
            final_status = "RETRY" if status_code in {408, 429, 500, 502, 503, 504} else "FAIL_NO_RETRY"
            # Ưu tiên thông báo tiếng Việt đã map theo mã lỗi nghiệp vụ.
            raise CoinApiError(
                step_status,
                COIN_ERROR_MESSAGES.get(code) or f"HTTP {status_code}: {message}",
                final_status=final_status,
            )

        if isinstance(data, dict):
            success = CoinApiClient._pick(data, "success", "ok", "result")
            if success is False:
                message = CoinApiClient._pick(data, "message", "error", "errorMessage", "detail") or "API báo đăng ký thất bại."
                raise CoinApiError(step_status, str(message), final_status="FAIL_NO_RETRY")
            nested = CoinApiClient._pick(data, "data", "customer", "account")
            return nested if isinstance(nested, dict) else data

        raise CoinApiError(step_status, "API trả về JSON không đúng dạng object.", final_status="FAILED")

    def _update_xsrf_from_response(self, set_cookie_headers: list[str], label: str) -> None:
        for header in set_cookie_headers:
            cookie = SimpleCookie()
            try:
                cookie.load(header)
            except Exception:
                continue
            morsel = cookie.get("XSRF-TOKEN")
            if not morsel or not morsel.value:
                continue
            current = str(self.headers.get("cookie", "")).replace("XSRF-TOKEN=", "")
            if morsel.value != current:
                self.headers["cookie"] = f"XSRF-TOKEN={morsel.value}"
                log.info("HTTP %s Set-Cookie: cập nhật XSRF-TOKEN=%s cho request sau.", label, morsel.value)
            return

    @staticmethod
    def _pick(data: Any, *keys: str) -> Any:
        if not isinstance(data, dict):
            return None
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = CoinApiClient._pick(value, *keys)
            if found not in (None, ""):
                return found
        return None

    @staticmethod
    def _validate_account(account: dict, payload: dict[str, Any]) -> None:
        # Không còn yêu cầu "phoneNumber": số điện thoại do Fuyoura cấp ở bước
        # get_number() trong register_account, không lấy từ Excel.
        required = [
            "password",
            "katakanaFirstName",
            "katakanaLastName",
            "simpleAuthenticationCode",
            "dateOfBirth",
            "screenId",
            "cmsTermsOfServiceSetId",
        ]
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise CoinApiError(
                "VALIDATION_FAILED",
                f"Thiếu field bắt buộc: {', '.join(missing)}",
                final_status="FAIL_NO_RETRY",
            )

        if not config.FUYOURA_API_KEY:
            raise CoinApiError(
                "SMS_VERIFY_FAILED",
                "Chưa cấu hình fuyoura_api_key để tự thuê số & lấy OTP.",
                final_status="FAIL_NO_RETRY",
            )
        if not config.FUYOURA_PROJECT:
            raise CoinApiError(
                "SMS_VERIFY_FAILED",
                "Chưa cấu hình fuyoura_project (mã project của docking).",
                final_status="FAIL_NO_RETRY",
            )

        pin = str(payload["simpleAuthenticationCode"])
        if not re.fullmatch(r"\d{4}", pin):
            raise CoinApiError(
                "PIN_CHECK_FAILED",
                "pin/simpleAuthenticationCode phải gồm đúng 4 chữ số.",
                final_status="FAIL_NO_RETRY",
            )

        dob = str(payload["dateOfBirth"])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            raise CoinApiError(
                "VALIDATION_FAILED",
                "date_of_birth phải theo format YYYY-MM-DD.",
                final_status="FAIL_NO_RETRY",
            )
