from __future__ import annotations

import json
import random
import re
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

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
        if self.proxy:
            proxy_url = urllib_proxy_url(self.proxy)
            self.opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
            log.info("Dùng proxy cho flow: %s", self.proxy.get("server") or self.proxy.get("raw"))
        log.info(
            "Tạo header flow: device_id=%s os=%s cookie=%s",
            self.headers.get("x-sgc-device-id", "")[:8] + "...",
            self.headers.get("x-sgc-device-os", ""),
            self.headers.get("cookie", "<chưa có>"),
        )

    def register_account(self, account: dict, progress) -> CoinRegisterResult:
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
        )

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
            message = CoinApiClient._pick(data, "message", "error", "errorMessage", "detail") or text
            final_status = "RETRY" if status_code in {408, 429, 500, 502, 503, 504} else "FAIL_NO_RETRY"
            raise CoinApiError(
                step_status,
                f"HTTP {status_code}: {message}",
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
