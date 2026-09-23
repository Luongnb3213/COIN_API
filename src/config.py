from __future__ import annotations

import json
import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT_DIR / os.environ.get("CONFIG_FILE", "config.json")

if CONFIG_FILE.exists():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        _cfg = json.load(f)
else:
    _cfg = {}


def _get(key: str, default=None):
    return _cfg.get(key, default)


DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
(ROOT_DIR / "logs").mkdir(exist_ok=True)

XLSX_PATH = Path(str(_get("xlsx_path", ROOT_DIR / "COIN_API_template.xlsx"))).expanduser()
ACTIVE_SHEET = str(_get("active_sheet", "Accounts")).strip() or "Accounts"
WORKER_COUNT = max(1, int(_get("worker_count", 1) or 1))
RUN_LIMIT = max(0, int(_get("run_limit", 0) or 0))

COIN_BASE_URL = str(_get("coin_base_url", "https://tocapi.coinplus.jp")).rstrip("/")
COIN_SCREEN_ID = str(_get("coin_screen_id", "3")).strip()
COIN_CMS_TERMS_OF_SERVICE_SET_ID = str(_get("coin_cms_terms_of_service_set_id", "sgc-ts-4020")).strip()
COIN_DEVICE_ID = str(os.environ.get("COIN_DEVICE_ID") or _get("coin_device_id", "") or "").strip()
COIN_DEVICE_OS = str(os.environ.get("COIN_DEVICE_OS") or _get("coin_device_os", "") or "").strip()
COIN_DEVICE_PROFILES_PATH = Path(str(_get("coin_device_profiles_path", ROOT_DIR / "coin_device_profiles.json"))).expanduser()
REQUEST_TIMEOUT = max(5, int(_get("request_timeout", 30) or 30))
ACCOUNT_COOLDOWN_SECONDS = max(0, int(_get("account_cooldown_seconds", 0) or 0))
COIN_HTTP_HEADERS = dict(_get("coin_http_headers", {}) or {})
COIN_GIFT_CODES = [
    str(value).strip()
    for value in (_get("gift_codes", []) or [])
    if str(value or "").strip()
]
FUYOURA_API_KEY = str(os.environ.get("FUYOURA_API_KEY") or _get("fuyoura_api_key", "") or "").strip()
FUYOURA_BASE_URL = str(_get("fuyoura_base_url", "https://fuyoura.com/api/otp/v1") or "https://fuyoura.com/api/otp/v1").strip().rstrip("/")
FUYOURA_COUNTRY = str(os.environ.get("FUYOURA_COUNTRY") or _get("fuyoura_country", "japan") or "japan").strip()
FUYOURA_PROJECT = str(os.environ.get("FUYOURA_PROJECT") or _get("fuyoura_project", "70035") or "70035").strip()
OTP_WAIT_TIMEOUT = max(5.0, float(_get("otp_wait_timeout", 120.0) or 120.0))
OTP_POLL_INTERVAL = max(0.5, float(_get("otp_poll_interval", 2.0) or 2.0))
USE_PROXY = bool(_get("use_proxy", True))
MAX_ACCOUNTS_PER_PROXY = 1
PROXY_FAILURE_THRESHOLD = max(1, int(_get("proxy_failure_threshold", 3) or 3))
PROXY_CIRCUIT_OPEN = False
PROXY_CIRCUIT_REASON = ""

STOP_FLAG = False
SESSION_STATS: dict[str, int] = {
    "PENDING": 0,
    "PROCESSING": 0,
    "SUCCESS": 0,
    "FAILED": 0,
    "RETRY": 0,
    "FAIL_NO_RETRY": 0,
}
