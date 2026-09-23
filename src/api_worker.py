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

    def _lock_proxy(self):
        if not getattr(config, "USE_PROXY", False):
            return None, -1, "Direct"
        while not config.STOP_FLAG:
            proxy, proxy_idx = self.proxy_pool.get_next_proxy()
            if proxy == "WAIT":
                time.sleep(2)
                continue
            if not proxy:
                raise RuntimeError("PROXY_ERROR: Không còn proxy khả dụng.")
            return proxy, proxy_idx, str(proxy.get("raw") or proxy.get("server") or "")
        raise RuntimeError("Dừng trước khi lock proxy.")

    def run(self) -> None:
        log.info("Worker %s started.", self.worker_id)
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
                proxy, proxy_idx, proxy_label = self._lock_proxy()
                if proxy_idx >= 0:
                    proxy_ref = self.proxy_pool.get_proxy_ref(proxy_idx)
                self._start_processing(row)
                self.sheets_manager.update_account(row, {"proxy_used": proxy_label, "proxy_id": proxy.get("proxy_id", "") if proxy else ""})
                client = CoinApiClient(proxy=proxy)

                def progress(step: str) -> None:
                    nonlocal step_status
                    step_status = step
                    self.sheets_manager.update_account(row, {"step_status": step})

                result = client.register_account(account, progress)
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
                if proxy_idx >= 0:
                    self.proxy_pool.mark_used(proxy_idx)
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
                if proxy_idx >= 0:
                    if final_status != "SUCCESS":
                        self.proxy_pool.mark_used(proxy_idx)
                    self.proxy_pool.release_proxy(proxy_idx)
                self._finish(final_status)
                self.account_queue.task_done()
                cooldown = int(config.ACCOUNT_COOLDOWN_SECONDS or 0)
                if cooldown and not config.STOP_FLAG:
                    time.sleep(cooldown)

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
