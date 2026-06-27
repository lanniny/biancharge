"""Application-level SOCKS5 proxy for autotrader HTTP — ignores OS proxy settings."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    import socks
    from sockshandler import SocksiPyHandler
except ImportError:  # pragma: no cover
    socks = None  # type: ignore[assignment]
    SocksiPyHandler = None  # type: ignore[assignment,misc]

DEFAULT_SOCKS5_URL = ""
DEFAULT_REMOTE_SOCKS5_URL = ""

_proxy_opener: urllib.request.OpenerDirector | None = None
_direct_opener: urllib.request.OpenerDirector | None = None
_active_proxy_url: str | None = None
_bootstrapped = False


def parse_socks5_url(url: str) -> tuple[str, int, str | None, str | None]:
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"socks5", "socks5h"}:
        raise ValueError(f"Unsupported proxy scheme {parsed.scheme!r}; expected socks5")
    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid SOCKS5 URL (missing host): {url!r}")
    port = parsed.port or 1080
    return host, port, parsed.username, parsed.password


def configure_socks5_proxy(*, url: str | None = None, enabled: bool = True) -> dict[str, Any]:
    """Force all autotrader HTTP through SOCKS5 (or disable explicitly)."""
    global _proxy_opener, _active_proxy_url, _bootstrapped
    _bootstrapped = True
    if not enabled or not url:
        _active_proxy_url = None
        _proxy_opener = None
        return {"enabled": False}
    if socks is None or SocksiPyHandler is None:
        raise RuntimeError("PySocks is required for SOCKS5 proxy. Install: pip install pysocks")
    host, port, username, password = parse_socks5_url(url)
    handler = SocksiPyHandler(
        socks.SOCKS5,
        host,
        port,
        username=username,
        password=password,
    )
    _proxy_opener = urllib.request.build_opener(handler)
    _active_proxy_url = url
    return {"enabled": True, "host": host, "port": port}


def configure_socks5_from_dict(raw: dict[str, Any] | None) -> dict[str, Any]:
    if os.environ.get("AUTOTRADER_SOCKS5_DISABLED", "").lower() in {"1", "true", "yes"}:
        return configure_socks5_proxy(enabled=False)
    env_url = os.environ.get("BINANCE_SOCKS5_PROXY", "").strip()
    raw = raw or {}
    enabled = bool(raw.get("enabled", True))
    if "enabled" in raw:
        enabled = bool(raw["enabled"])
    elif env_url:
        enabled = True
    url = str(raw.get("url") or env_url or DEFAULT_SOCKS5_URL).strip()
    if not enabled:
        return configure_socks5_proxy(enabled=False)
    if not url:
        return configure_socks5_proxy(enabled=False)
    return configure_socks5_proxy(url=url, enabled=True)


def proxy_status() -> dict[str, Any]:
    if not _active_proxy_url:
        return {"enabled": False}
    host, port, _, _ = parse_socks5_url(_active_proxy_url)
    return {"enabled": True, "host": host, "port": port, "scheme": "socks5"}


def _ensure_bootstrapped() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    if os.environ.get("AUTOTRADER_SOCKS5_DISABLED", "").lower() in {"1", "true", "yes"}:
        configure_socks5_proxy(enabled=False)
        return
    configure_socks5_from_dict({})


def _get_opener() -> urllib.request.OpenerDirector:
    global _direct_opener
    _ensure_bootstrapped()
    if _proxy_opener is not None:
        return _proxy_opener
    if _direct_opener is None:
        _direct_opener = urllib.request.build_opener()
    return _direct_opener


def urlopen(request: urllib.request.Request, timeout_seconds: int | float = 10):
    return _get_opener().open(request, timeout=timeout_seconds)


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 10,
) -> Any:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urlopen(request, timeout_seconds=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        binance_code = None
        try:
            payload = json.loads(raw_body)
            message = payload.get("msg") or raw_body
            # BIN-003: preserve the numeric Binance error code (-1021, -2019,
            # -2022, -1111, -4015, -4131 ...) so callers can branch on it instead
            # of fragile substring matching. Include it in the message too.
            if isinstance(payload, dict) and "code" in payload:
                try:
                    binance_code = int(payload["code"])
                except (TypeError, ValueError):
                    binance_code = None
        except json.JSONDecodeError:
            message = raw_body or exc.reason
        code_str = f" [code={binance_code}]" if binance_code is not None else ""
        err = RuntimeError(f"HTTP {exc.code}: {message}{code_str}")
        err.binance_code = binance_code  # type: ignore[attr-defined]
        err.http_status = exc.code  # type: ignore[attr-defined]
        raise err from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed: {exc.reason}") from exc
    return json.loads(body) if body else {}
