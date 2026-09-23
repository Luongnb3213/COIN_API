from __future__ import annotations

import time
import traceback
from queue import Empty, Queue

from src import config
from src.connections.xlsx_connection import XlsxConnection
from src.core.coin_api_client import CoinApiClient, CoinApiError
from src.utils.logger import get_logger


log = get_logger("api_worker")


class CoinRegistrationWorker:
    def __init__(self, worker_id: int, account_queue: Queue, sheets_manager: XlsxConnection, proxy_pool=None) -> None:
        self.worker_id = worker_id
        self.account_queue = account_queue
        self.sheets_manager = sheets_manager
        self.proxy_pool = proxy_pool
        self.current_proxy = None
        self.current_proxy_idx = -1
        self.current_proxy_label = "Direct"
        self.current_proxy_count = 0

    def _ensure_proxy(self):
        if not getattr(config, "USE_PROXY", False):
            return None, -1, "Direct"
        if self.current_proxy_idx >= 0:
            return self.current_proxy, self.current_proxy_idx, self.current_proxy_label
        while not config.STOP_FLAG:
            proxy, proxy_idx = self.proxy_pool.get_next_proxy()
            if proxy == "WAIT":
                time.sleep(2)
                continue
            if not proxy:
                raise RuntimeError("PROXY_ERROR: Không còn proxy khả dụng.")
            self.current_proxy = proxy
            self.current_proxy_idx = int(proxy_idx)
            self.current_proxy_label = str(proxy.get("raw") or proxy.get("server") or "")
            self.current_proxy_count = 0
            log.info(
                "Worker %s pick proxy index=%s, sẽ chạy tối đa %s account trước khi đổi.",
                self.worker_id,
                self.current_proxy_idx,
                int(config.MAX_ACCOUNTS_PER_PROXY or 10),
            )
            return self.current_proxy, self.current_proxy_idx, self.current_proxy_label
        raise RuntimeError("Dừng trước khi lock proxy.")

    def _release_current_proxy(self) -> None:
        if self.current_proxy_idx < 0:
            return
        self.proxy_pool.release_proxy(self.current_proxy_idx)
        log.info("Worker %s release proxy index=%s sau %s account.", self.worker_id, self.current_proxy_idx, self.current_proxy_count)
        self.current_proxy = None
        self.current_proxy_idx = -1
        self.current_proxy_label = "Direct"
        self.current_proxy_count = 0

    def _sleep_before_next_proxy(self) -> None:
        rest_seconds = int(getattr(config, "PROXY_ROTATION_REST_SECONDS", 300) or 0)
        if rest_seconds <= 0 or config.STOP_FLAG or self.account_queue.empty():
            return
        log.info("Worker %s nghỉ %ss trước khi bốc proxy mới.", self.worker_id, rest_seconds)
        deadline = time.monotonic() + rest_seconds
        while not config.STOP_FLAG:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _mark_proxy_account_done(self, proxy_idx: int) -> bool:
        if proxy_idx < 0:
            return False
        self.proxy_pool.mark_used(proxy_idx)
        if proxy_idx == self.current_proxy_idx:
            self.current_proxy_count += 1
            if self.current_proxy_count >= int(config.MAX_ACCOUNTS_PER_PROXY or 10):
                self._release_current_proxy()
                self._sleep_before_next_proxy()
                return True
        return False

    def run(self) -> None:
        log.info("Worker %s started.", self.worker_id)
        try:
            while not config.STOP_FLAG:
                try:
                    account = self.account_queue.get(timeout=2)
                except Empty:
                    return

                row = int(account.get("_row") or 0)
                label = account.get("phone") or f"row#{row}"
                final_status = "FAILED"
                step_status = ""
                error_details = ""
                proxy = None
                proxy_idx = -1
                proxy_label = "Direct"
                proxy_ref = {}

                try:
                    proxy, proxy_idx, proxy_label = self._ensure_proxy()
                    if proxy_idx >= 0:
                        proxy_ref = self.proxy_pool.get_proxy_ref(proxy_idx)
                    self._start_processing(row)
                    self.sheets_manager.update_account(row, {"proxy_used": proxy_label, "proxy_id": proxy.get("proxy_id", "") if proxy else ""})
                    client = CoinApiClient(proxy=proxy)

                    def progress(step: str) -> None:
                        nonlocal step_status
                        step_status = step
                        self.sheets_manager.update_account(row, {"step_status": step})

                    def report_phone(number: str) -> None:
                        # Fuyoura vừa cấp số -> ghi ngay vào cột phone của dòng này,
                        # để cả khi register lỗi vẫn biết số đã thuê.
                        self.sheets_manager.update_account(row, {"phone": number})

                    result = client.register_account(account, progress, report_phone)
                    final_status = "SUCCESS"
                    registered_at = self.sheets_manager.success_stamp()
                    success_data = {
                        **account,
                        "status": final_status,
                        "step_status": "ACCOUNT_ACTIVE",
                        "error_details": "",
                        "proxy_used": proxy_label,
                        "proxy_id": proxy.get("proxy_id", "") if proxy else "",
                        "phone": result.phone_number or account.get("phone", ""),
                        "coin_id": result.coin_id,
                        "customer_status": result.customer_status,
                        "pcard_status": result.pcard_status,
                        "gift1_status": result.gift1_status,
                        "gift2_status": result.gift2_status,
                        "card_number": result.card_number,
                        "card_name": result.card_name,
                        "card_expiry": result.card_expiry,
                        "security_code": result.security_code,
                        "card_url": result.card_url,
                        "registered_at": registered_at,
                    }
                    self.sheets_manager.update_account(row, success_data)
                    self.sheets_manager.append_success_account(success_data)
                    log.info("Row %s OK: %s -> %s", row, label, result.coin_id)
                except CoinApiError as exc:
                    final_status = exc.final_status
                    step_status = exc.step_status
                    error_details = str(exc)
                    self.sheets_manager.update_account(
                        row,
                        {
                            "status": final_status,
                            "step_status": step_status,
                            "error_details": error_details[:1500],
                        },
                    )
                    log.warning("Row %s %s: %s", row, final_status, error_details)
                except Exception as exc:
                    error_details = str(exc)
                    final_status = "RETRY" if self._looks_temporary(error_details) else "FAILED"
                    self.sheets_manager.update_account(
                        row,
                        {
                            "status": final_status,
                            "step_status": step_status or "FAILED_UNKNOWN",
                            "error_details": error_details[:1500],
                        },
                    )
                    log.error("Row %s lỗi: %s", row, error_details)
                    log.debug(traceback.format_exc())
                finally:
                    rotated_proxy = self._mark_proxy_account_done(proxy_idx)
                    self._finish(final_status)
                    self.account_queue.task_done()
                    cooldown = int(config.ACCOUNT_COOLDOWN_SECONDS or 0)
                    if cooldown and not rotated_proxy and not config.STOP_FLAG:
                        time.sleep(cooldown)
        finally:
            self._release_current_proxy()

    def _start_processing(self, row: int) -> None:
        config.SESSION_STATS["PENDING"] = max(0, config.SESSION_STATS.get("PENDING", 0) - 1)
        config.SESSION_STATS["PROCESSING"] = config.SESSION_STATS.get("PROCESSING", 0) + 1
        self.sheets_manager.update_account(
            row,
            {
                "status": "PROCESSING",
                "step_status": "PROCESSING",
                "error_details": "",
            },
        )

    @staticmethod
    def _looks_temporary(text: str) -> bool:
        upper = str(text or "").upper()
        return any(marker in upper for marker in ("TIMEOUT", "NETWORK", "PROXY", "429", "RATE"))

    @staticmethod
    def _finish(status: str) -> None:
        config.SESSION_STATS["PROCESSING"] = max(0, config.SESSION_STATS.get("PROCESSING", 0) - 1)
        bucket = status if status in config.SESSION_STATS else "FAILED"
        config.SESSION_STATS[bucket] = config.SESSION_STATS.get(bucket, 0) + 1
