from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit


def urllib_proxy_url(proxy: dict) -> str:
    server = str(proxy.get("server") or "").strip()
    if not server:
        raise ValueError("Proxy thiếu server")
    if "://" not in server:
        server = "http://" + server
    parsed = urlsplit(server)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return urlunsplit((parsed.scheme or "http", auth + hostname + port, "", "", ""))


def proxy_group_key(proxy: dict) -> str:
    server = str(proxy.get("server") or "").strip().lower()
    username = str(proxy.get("username") or "").strip().lower()
    selector_positions = [
        position for token in (
            "-cc-", "-country-", "-city-", "-state-", "-region-",
            "-session-", "-sessid-", "-sticky-",
        )
        if (position := username.find(token)) >= 0
    ]
    pool_name = username[:min(selector_positions)] if selector_positions else username
    password = str(proxy.get("password") or "")
    return "\x1f".join((server, pool_name, password))


def proxy_group_label(proxy: dict) -> str:
    server = str(proxy.get("server") or "").replace("http://", "").replace("https://", "")
    username = str(proxy.get("username") or "").strip().lower()
    positions = [
        position for token in ("-cc-", "-country-", "-city-", "-state-", "-region-")
        if (position := username.find(token)) >= 0
    ]
    pool_name = username[:min(positions)] if positions else username
    return f"{server} | {pool_name or 'no-auth'}"


def is_quota_or_auth_error(detail: str, http_status: int | None = None) -> bool:
    text = str(detail or "").lower()
    return http_status in (401, 402, 407, 429) or any(token in text for token in (
        "407", "proxy authentication", "authentication required", "quota",
        "bandwidth", "traffic limit", "insufficient balance", "account disabled",
    ))
