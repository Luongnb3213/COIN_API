from __future__ import annotations

import json
import re
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src import config
from src.utils.logger import get_logger


FUYOURA_DEFAULT_URL = "https://fuyoura.com/api/otp/v1"
log = get_logger("fuyoura")

# Trạng thái order đã kết thúc mà không có mã -> dừng chờ ngay, không poll tiếp.
_TERMINAL_FAIL = {
    "cancelled",
    "canceled",
    "expired",
    "timeout",
    "timedout",
    "void",
    "voided",
    "refunded",
    "failed",
    "error",
}


class FuyouraError(RuntimeError):
    pass


class FuyouraTemporaryError(FuyouraError):
    pass


def normalize_phone(value: str) -> str:
    """Chuẩn hoá số Nhật của Fuyoura về dạng local COIN cần: '0XXXXXXXXXX'.

    Fuyoura có thể trả nhiều dạng cho jpn:
      - '9059678270'   (di động, bỏ số 0 đầu)      -> '09059678270'
      - '819059678270' (kèm mã quốc gia 81)         -> '09059678270'
      - '00819059678270' (mã quốc tế 00)            -> '09059678270'
      - '09059678270'  (đã đúng local)              -> giữ nguyên
    """
    number = re.sub(r"\D", "", str(value or ""))
    if number.startswith("00"):
        number = number[2:]
    if number.startswith("81") and not number.startswith("0"):
        number = number[2:]
    if number and not number.startswith("0"):
        number = "0" + number
    return number


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


class FuyouraClient:
    """Client cho Fuyoura OTP API (mô hình thuê số: getNumber -> getCode -> cancel)."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float,
        poll_interval: float,
        base_url: str = FUYOURA_DEFAULT_URL,
        country: str = "",
        project: str = "",
    ) -> None:
        self._api_key = api_key.strip()
        if not self._api_key:
            raise FuyouraError("Chưa có API key Fuyoura.")
        self._base_url = (base_url or FUYOURA_DEFAULT_URL).strip().rstrip("/") or FUYOURA_DEFAULT_URL
        self._country = str(country or "").strip()
        self._project = str(project or "").strip()
        self.timeout = timeout
        self.poll_interval = poll_interval

    @staticmethod
    def _check_cancelled() -> None:
        if config.STOP_FLAG:
            raise FuyouraError("Đã dừng theo yêu cầu người dùng.")

    def _post(self, action: str, *, request_timeout: float = 15.0, **params: int | str) -> dict:
        self._check_cancelled()
        body = {"key": self._api_key, "action": action}
        body.update({key: value for key, value in params.items() if value not in (None, "")})
        data = urlencode(body).encode("utf-8")
        req = Request(
            self._base_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=request_timeout) as response:
                payload = json.load(response)
        except HTTPError as exc:
            status = exc.code
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            exc.close()
            if status in (401, 403):
                raise FuyouraError("API key Fuyoura sai hoặc chưa được cấp quyền.") from None
            if status == 429 or status >= 500:
                raise FuyouraTemporaryError(f"Fuyoura trả HTTP {status}.") from None
            raise FuyouraError(f"Fuyoura trả HTTP {status}. {detail}".strip()) from None
        except (URLError, TimeoutError, OSError):
            raise FuyouraTemporaryError("Không kết nối được Fuyoura. Kiểm tra mạng và thử lại.") from None
        except (ValueError, UnicodeError):
            raise FuyouraError("Fuyoura trả dữ liệu JSON không hợp lệ.") from None

        self._check_cancelled()
        if not isinstance(payload, dict):
            raise FuyouraError("Fuyoura trả dữ liệu không đúng định dạng.")
        # Response lỗi của Fuyoura/Laravel là {"error": ...} hoặc {"message": ...}.
        error = payload.get("error") or payload.get("message")
        if error and payload.get("orders") is None and "requested" not in payload and "activated" not in payload:
            raise FuyouraError(f"Fuyoura: {error}")
        return payload

    def get_number(self) -> dict:
        """Thuê 1 số. Trả về {order, number(local), raw_number, price, expires_in}."""
        if not self._project:
            raise FuyouraError("Chưa cấu hình project Fuyoura (fuyoura_project).")
        data = self._post("getNumber", country=self._country, project=self._project, qty=1)
        orders = data.get("orders")
        if not isinstance(orders, list) or not orders:
            raise FuyouraError("Fuyoura không tạo được order (orders rỗng).")
        entry = orders[0]
        if not isinstance(entry, dict):
            raise FuyouraError("Fuyoura trả order không hợp lệ.")
        order = entry.get("order")
        raw_number = str(entry.get("number") or "")
        number = normalize_phone(raw_number)
        if order is None or not number:
            raise FuyouraError(f"Order Fuyoura thiếu số hoặc order id: {entry}.")
        if not re.fullmatch(r"0\d{9,10}", number):
            raise FuyouraError(
                f"Số Fuyoura trả về không đúng dạng Nhật (local): raw={raw_number} -> {number}."
            )
        log.info("Fuyoura thuê số order=%s number=%s price=%s.", order, number, entry.get("price"))
        return {
            "order": order,
            "number": number,
            "raw_number": raw_number,
            "price": entry.get("price"),
            "expires_in": entry.get("expires_in"),
        }

    @staticmethod
    def _find_order(data: dict, order: object) -> dict | None:
        orders = data.get("orders")
        if not isinstance(orders, list):
            return None
        for item in orders:
            if isinstance(item, dict) and str(item.get("order")) == str(order):
                return item
        return orders[0] if orders and isinstance(orders[0], dict) else None

    @staticmethod
    def _extract_code(entry: dict) -> str | None:
        # Ưu tiên tách mã COIN+ từ nội dung SMS; nếu không khớp, dùng field `code`
        # mà Fuyoura đã tách sẵn.
        code = _coin_otp_from_content(str(entry.get("message") or ""))
        if code:
            return code
        raw = re.sub(r"\D", "", str(entry.get("code") or ""))
        if 4 <= len(raw) <= 8:
            return raw
        return None

    def wait_for_code(self, order: object, *, timeout: float | None = None) -> str:
        wait_timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + wait_timeout
        attempt = 0
        last_error = ""
        last_wait_log = 0.0
        log.info("Fuyoura bắt đầu chờ OTP cho order=%s trong %.0fs.", order, wait_timeout)
        while (remaining := deadline - time.monotonic()) > 0:
            self._check_cancelled()
            try:
                attempt += 1
                data = self._post("getCode", orders=str(order), request_timeout=min(15.0, remaining))
                last_error = ""
                entry = self._find_order(data, order)
                if entry is not None:
                    code = self._extract_code(entry)
                    if code:
                        log.info("Fuyoura nhận OTP cho order=%s ở lần poll #%s.", order, attempt)
                        return code
                    status = str(entry.get("status") or "").lower()
                    if status in _TERMINAL_FAIL:
                        raise FuyouraError(
                            f"Order {order} kết thúc ở trạng thái '{status}' mà không có mã."
                        )
                now = time.monotonic()
                if now - last_wait_log >= 15:
                    elapsed = wait_timeout - remaining
                    log.info("Fuyoura vẫn đang chờ OTP order=%s... %.0fs/%.0fs.", order, elapsed, wait_timeout)
                    last_wait_log = now
            except FuyouraTemporaryError as exc:
                last_error = str(exc)
                log.warning("Fuyoura poll #%s order=%s lỗi tạm thời: %s", attempt, order, last_error)
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        raise FuyouraError(
            f"Hết {wait_timeout:g}s chờ OTP từ Fuyoura cho order={order}."
            + (f" {last_error}" if last_error else "")
        )

    def cancel(self, order: object) -> None:
        self._post("cancel", order=str(order))

    def safe_cancel(self, order: object) -> None:
        """Huỷ order để trả số về, nuốt lỗi để không che lỗi gốc của flow."""
        try:
            self.cancel(order)
            log.info("Fuyoura đã huỷ order=%s, trả số về.", order)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            log.warning("Fuyoura huỷ order=%s thất bại: %s", order, exc)
