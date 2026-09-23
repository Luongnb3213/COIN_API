from __future__ import annotations

import argparse
import threading
from queue import Queue

from src import config
from src.api_worker import CoinRegistrationWorker
from src.connections.xlsx_connection import XlsxConnection
from src.core.coin_api_client import validate_device_profiles
from src.utils.logger import add_file_handler, get_logger
from src.utils.proxy_pool import ProxyPool


LOG_FILE = config.ROOT_DIR / "logs" / "coin_api.log"
add_file_handler(str(LOG_FILE))
log = get_logger("api_main")


def main(limit: int = 0) -> dict:
    config.STOP_FLAG = False
    validate_device_profiles()
    manager = XlsxConnection(str(config.XLSX_PATH))
    if not manager.is_connected():
        raise RuntimeError("Không thể mở file XLSX. Chạy tools/create_template.py hoặc kiểm tra xlsx_path.")

    manager.reset_interrupted_to_pending()
    requested = int(limit or 0)
    accounts = manager.get_pending_accounts(batch_size=requested if requested > 0 else 1_000_000)
    if requested > 0 and len(accounts) < requested:
        raise RuntimeError(f"Không đủ account runnable: yêu cầu {requested}, còn {len(accounts)}.")
    if not accounts:
        log.warning("Không có account PENDING/FAILED/RETRY hoặc status trống.")
        return {"queued": 0, "stats": dict(config.SESSION_STATS)}

    proxies = manager.get_active_proxies() if getattr(config, "USE_PROXY", False) else []
    if getattr(config, "USE_PROXY", False) and not proxies:
        raise RuntimeError("Đã bật proxy nhưng sheet Proxies không có proxy ACTIVE.")
    proxy_pool = ProxyPool(proxies)

    config.SESSION_STATS.clear()
    config.SESSION_STATS.update(
        {"PENDING": len(accounts), "PROCESSING": 0, "SUCCESS": 0, "FAILED": 0, "RETRY": 0, "FAIL_NO_RETRY": 0}
    )

    queue: Queue = Queue()
    for account in accounts:
        queue.put(account)

    worker_count = max(1, min(3, int(config.WORKER_COUNT or 1), len(accounts)))
    log.info("Chạy COIN_API với %s worker.", worker_count)

    threads: list[threading.Thread] = []
    for worker_id in range(1, worker_count + 1):
        worker = CoinRegistrationWorker(worker_id, queue, manager, proxy_pool)
        thread = threading.Thread(target=worker.run, name=f"coin-worker-{worker_id}", daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()

    log.info("Kết thúc COIN_API: %s", config.SESSION_STATS)
    return {"queued": len(accounts), "stats": dict(config.SESSION_STATS)}


def cli() -> None:
    parser = argparse.ArgumentParser(description="COIN_API Excel batch runner")
    parser.add_argument("--limit", type=int, default=None, help="0 = chạy hết runnable rows")
    args = parser.parse_args()
    limit = config.RUN_LIMIT if args.limit is None else args.limit
    main(limit)


if __name__ == "__main__":
    cli()
