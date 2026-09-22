from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src import config
from src.utils.logger import get_logger


OTPBASE_OTP_URL = "https://www.otpbase.space/api/v1/otp"
log = get_logger("otpbase")


class OTPBaseError(RuntimeError):
    pass


class OTPBaseTemporaryError(OTPBaseError):
    pass


def normalize_phone(value: str) -> str:
    number = re.sub(r"\D", "", str(value or ""))
    if number.startswith("81") and len(number) == 12:
        number = "0" + number[2:]
    return number


def _seq(value: object) -> int:
    return int(str(value)) if str(value).isascii() and str(value).isdigit() else -1


def _message_phone(message: dict) -> str:
    for key in ("phone", "tel", "number", "to", "recipient"):
        value = str(message.get(key) or "").strip()
        if value:
            return value
    return ""


def _message_content(message: object) -> str:
    if isinstance(message, str):
        return unicodedata.normalize("NFKC", message)
    if isinstance(message, list):
        parts = [_message_content(item) for item in message]
        return "\n".join(part for part in parts if part)
    if not isinstance(message, dict):
        return ""

    for key in (
        "content",
        "message",
        "sms",
        "text",
        "body",
        "sms_content",
        "smsContent",
        "message_content",
        "messageContent",
        "raw",
        "raw_message",
        "rawMessage",
    ):
        value = str(message.get(key) or "")
        if value:
            return unicodedata.normalize("NFKC", value)

    # OTPBase đôi khi để nội dung SMS trong object/list lồng sâu, trong khi
    # field `otp` ở top-level lại rỗng hoặc là dấu gạch ngang. Gom toàn bộ
    # string lồng nhau để tránh dừng nhầm ở field số điện thoại trước SMS.
    parts = [_message_content(value) for value in message.values()]
    return "\n".join(part for part in parts if part)


def _coin_otp_from_content(content: str) -> str | None:
    text = unicodedata.normalize("NFKC", str(content or ""))
    patterns = (
        r"COIN\s*\+\s*認証コード\s*[:：]\s*([0-9]{4,8})(?![0-9])",
        r"認証コード\s*[:：]\s*([0-9]{4,8})(?![0-9])",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return None


@dataclass(frozen=True)
class OTPBaseCursor:
    seq: int = -1


class OTPBaseClient:
    def __init__(self, api_key: str, *, timeout: float, poll_interval: float) -> None:
        self._api_key = api_key.strip()
        if not self._api_key:
            raise OTPBaseError("Chưa có API key OTPBase.")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.last_otp_source = ""
        self.last_seq = -1

    @staticmethod
    def _check_cancelled() -> None:
        if config.STOP_FLAG:
            raise OTPBaseError("Đã dừng chờ OTP theo yêu cầu người dùng.")

    def _request(self, phone: str, *, request_timeout: float = 15.0, **params: int | str) -> dict:
        self._check_cancelled()
        query = urlencode({"phone": normalize_phone(phone), **params})
        req = Request(f"{OTPBASE_OTP_URL}?{query}", headers={"x-api-key": self._api_key})
        try:
            with urlopen(req, timeout=request_timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401:
                raise OTPBaseError("API key OTPBase không đúng hoặc đã bị thu hồi.") from None
            if status == 403:
                raise OTPBaseError("API key OTPBase chưa được cấp quyền cho số điện thoại này.") from None
            error = OTPBaseTemporaryError if status == 429 or status >= 500 else OTPBaseError
            raise error(f"OTPBase trả HTTP {status}.") from None
        except (URLError, TimeoutError, OSError):
            raise OTPBaseTemporaryError("Không kết nối được OTPBase. Kiểm tra mạng và thử lại.") from None
        except (ValueError, UnicodeError):
            raise OTPBaseError("OTPBase trả dữ liệu JSON không hợp lệ.") from None
        self._check_cancelled()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise OTPBaseError("OTPBase không chấp nhận yêu cầu. Kiểm tra API key và quyền của số điện thoại.")
        return data

    def _otp(self, phone: str, *, request_timeout: float = 15.0, **params: int | str) -> dict:
        data = self._request(phone, request_timeout=request_timeout, **params)
        if "found" not in data:
            raise OTPBaseError("OTPBase /otp không trả field `found` hợp lệ.")
        return data

    def capture_cursor(self, phone: str) -> OTPBaseCursor:
        log.info("OTPBase capture cursor cho %s.", normalize_phone(phone))
        data = self._otp(phone)
        cursor = max(_seq(data.get("seq")), _seq(data.get("last_seq")))
        log.info("OTPBase cursor=%s.", cursor)
        return OTPBaseCursor(seq=cursor)

    def _code_from_otp_response(self, data: dict, phone: str, cursor: OTPBaseCursor) -> str | None:
        self.last_otp_source = ""
        if data.get("found") is not True:
            return None
        seq = max(_seq(data.get("seq")), _seq(data.get("last_seq")))
        if cursor.seq >= 0:
            if seq <= cursor.seq:
                return None
        else:
            return None
        if normalize_phone(_message_phone(data)) != normalize_phone(phone):
            return None

        content = _message_content(data)
        code = _coin_otp_from_content(content)
        if code:
            self.last_otp_source = "message"
            self.last_seq = seq
            otp = re.sub(r"\D", "", str(data.get("otp") or ""))
            if 4 <= len(otp) <= 8 and otp != code:
                log.warning("OTPBase field otp=%s khác mã trong nội dung SMS=%s; ưu tiên nội dung SMS.", otp, code)
            elif not (4 <= len(otp) <= 8):
                log.info("OTPBase field otp rỗng; đã tách OTP từ nội dung SMS.")
            return code

        otp = re.sub(r"\D", "", str(data.get("otp") or ""))
        if 4 <= len(otp) <= 8:
            self.last_otp_source = "otp"
            self.last_seq = seq
            return otp
        return None

    def wait_for_otp(self, phone: str, after_seq: OTPBaseCursor | int, *, timeout: float | None = None) -> str:
        cursor = after_seq if isinstance(after_seq, OTPBaseCursor) else OTPBaseCursor(seq=int(after_seq))
        wait_timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + wait_timeout
        last_error = ""
        attempt = 0
        last_wait_log = 0.0
        log.info("OTPBase bắt đầu chờ OTP mới trong %.0fs.", wait_timeout)
        while (remaining := deadline - time.monotonic()) > 0:
            self._check_cancelled()
            try:
                attempt += 1
                params: dict[str, int] = {}
                if cursor.seq >= 0:
                    params["after_seq"] = cursor.seq
                data = self._otp(phone, request_timeout=min(15.0, remaining), **params)
                last_error = ""
                code = self._code_from_otp_response(data, phone, cursor)
                if code:
                    log.info(
                        "OTPBase nhận OTP mới ở lần poll #%s, seq=%s, source=%s.",
                        attempt,
                        self.last_seq,
                        self.last_otp_source or "otp",
                    )
                    return code
                now = time.monotonic()
                if now - last_wait_log >= 15:
                    elapsed = wait_timeout - remaining
                    log.info("OTPBase vẫn đang chờ OTP mới... %.0fs/%.0fs.", elapsed, wait_timeout)
                    last_wait_log = now
            except OTPBaseTemporaryError as exc:
                last_error = str(exc)
                log.warning("OTPBase poll #%s lỗi tạm thời: %s", attempt, last_error)
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        raise OTPBaseError(
            f"Hết {wait_timeout:g}s chờ OTP COIN+ mới cho số điện thoại đã nhập."
            + (f" {last_error}" if last_error else "")
        )
