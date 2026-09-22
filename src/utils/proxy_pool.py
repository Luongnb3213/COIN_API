from __future__ import annotations

import threading
from urllib.parse import urlsplit

from src import config
from src.utils.logger import get_logger
from src.utils.proxy_health import proxy_group_key, proxy_group_label


log = get_logger("proxy_pool")


class ProxyPool:
    def __init__(self, proxy_list: list) -> None:
        self.proxies = []
        self.lock = threading.Lock()
        self.index = 0
        self.retired_indices = set()
        self.dead_indices = set()
        self.in_use_indices = set()
        self.claimed_indices = set()
        self.group_failures = {}
        self.blocked_groups = set()
        self.consecutive_failures = 0
        self.circuit_open = False
        self.circuit_reason = ""
        self.usage_counts = {}
        self.load_from_list(proxy_list)

    def load_from_list(self, proxy_list: list) -> None:
        if not proxy_list:
            log.warning("Danh sách proxy trống. Chạy không dùng proxy.")
            return
        for position, item in enumerate(proxy_list, start=1):
            if isinstance(item, dict):
                raw = str(item.get("raw") or item.get("proxy") or "").strip()
                proxy_id = str(item.get("proxy_id") or f"PX-{position:06d}").strip()
                sheet_row = item.get("sheet_row")
            else:
                raw = str(item or "").strip()
                proxy_id = f"PX-{position:06d}"
                sheet_row = None
            parsed = self.parse_proxy_string(raw)
            if not parsed:
                continue
            parsed["proxy_id"] = proxy_id
            parsed["sheet_row"] = sheet_row
            parsed["group_key"] = proxy_group_key(parsed)
            parsed["group_label"] = proxy_group_label(parsed)
            self.proxies.append(parsed)
        log.info("Loaded %s proxies vào ProxyPool.", len(self.proxies))

    @staticmethod
    def parse_proxy_string(proxy_str: str) -> dict | None:
        try:
            if "://" in proxy_str:
                parsed = urlsplit(proxy_str)
                if not parsed.hostname or not parsed.port:
                    log.warning("Định dạng proxy không hợp lệ: %s", proxy_str)
                    return None
                result = {
                    "server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}",
                    "raw": proxy_str,
                }
                if parsed.username:
                    result["username"] = parsed.username
                if parsed.password:
                    result["password"] = parsed.password
                return result
            if "@" in proxy_str:
                auth_part, host_part = proxy_str.split("@", 1)
                username, password = auth_part.split(":", 1)
                host, port = host_part.split(":", 1)
                return {
                    "server": f"http://{host}:{port}",
                    "username": username,
                    "password": password,
                    "raw": proxy_str,
                }
            parts = proxy_str.split(":")
            if len(parts) == 2:
                return {"server": f"http://{parts[0]}:{parts[1]}", "raw": proxy_str}
            if len(parts) == 4:
                return {
                    "server": f"http://{parts[0]}:{parts[1]}",
                    "username": parts[2],
                    "password": parts[3],
                    "raw": proxy_str,
                }
            log.warning("Định dạng proxy không hợp lệ: %s", proxy_str)
            return None
        except Exception as exc:
            log.error("Lỗi parse proxy string %r: %s", proxy_str, exc)
            return None

    def get_next_proxy(self) -> tuple[dict | str | None, int | None]:
        if not self.proxies:
            return None, None
        with self.lock:
            if self.circuit_open:
                log.error("Circuit breaker proxy đang mở: %s", self.circuit_reason)
                return None, None
            start_index = self.index
            while True:
                curr_idx = self.index
                self.index = (self.index + 1) % len(self.proxies)
                group_key = self.proxies[curr_idx].get("group_key", "")
                if (
                    curr_idx not in self.retired_indices
                    and curr_idx not in self.dead_indices
                    and curr_idx not in self.in_use_indices
                    and group_key not in self.blocked_groups
                ):
                    proxy = dict(self.proxies[curr_idx])
                    self.in_use_indices.add(curr_idx)
                    log.info("Reserve proxy index=%s | %s", curr_idx, proxy.get("server"))
                    return proxy, curr_idx
                if self.index == start_index:
                    eligible_in_use = any(
                        idx in self.in_use_indices
                        and idx not in self.dead_indices
                        and idx not in self.retired_indices
                        and self.proxies[idx].get("group_key", "") not in self.blocked_groups
                        for idx in range(len(self.proxies))
                    )
                    return ("WAIT", -1) if eligible_in_use else (None, None)

    def release_proxy(self, proxy_index: int) -> None:
        if proxy_index is None or proxy_index < 0:
            return
        with self.lock:
            self.in_use_indices.discard(proxy_index)

    def mark_used(self, proxy_index: int) -> None:
        if proxy_index is None or proxy_index < 0:
            return
        with self.lock:
            self.consecutive_failures = 0
            self.usage_counts[proxy_index] = self.usage_counts.get(proxy_index, 0) + 1

    def set_permanent_count(self, proxy_ref, count: int) -> None:
        return

    def mark_failed(self, proxy_index: int, reason: str = "", fatal_group: bool = False) -> None:
        if proxy_index is None or proxy_index < 0:
            return
        with self.lock:
            self.dead_indices.add(proxy_index)
            self.in_use_indices.discard(proxy_index)
            self.consecutive_failures += 1
            group_key = self.proxies[proxy_index].get("group_key", "")
            if fatal_group:
                self.blocked_groups.add(group_key)
                for idx, proxy in enumerate(self.proxies):
                    if proxy.get("group_key", "") == group_key:
                        self.dead_indices.add(idx)
                        self.in_use_indices.discard(idx)
            threshold = int(getattr(config, "PROXY_FAILURE_THRESHOLD", 3))
            if self.consecutive_failures >= threshold:
                self.circuit_open = True
                self.circuit_reason = f"{self.consecutive_failures} lỗi proxy liên tiếp; lỗi gần nhất: {reason or 'không rõ'}"
                config.PROXY_CIRCUIT_OPEN = True
                config.PROXY_CIRCUIT_REASON = self.circuit_reason

    def get_proxy_ref(self, proxy_index: int) -> dict:
        if proxy_index is None or proxy_index < 0:
            return {}
        with self.lock:
            proxy = self.proxies[proxy_index]
            return {
                "proxy_id": str(proxy.get("proxy_id") or ""),
                "raw": str(proxy.get("raw") or ""),
                "sheet_row": proxy.get("sheet_row"),
            }

    def count(self) -> int:
        return len(self.proxies)
