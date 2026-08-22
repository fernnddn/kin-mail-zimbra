"""Best-effort public IPv4 for wizard DNS paste (A / SPF).

Never returns RFC1918, loopback, link-local, or CGNAT. The SMTP sending
address can still differ (NAT); the UI prefers a later install-log measurement
when one exists.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.error
import urllib.request

_CACHE_TTL_SEC = 120.0
_cache_at = 0.0
_cache_ip = ""

_SOURCES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)


def reset_public_ip_cache() -> None:
    global _cache_at, _cache_ip
    _cache_at = 0.0
    _cache_ip = ""


def is_public_ipv4(value: str) -> bool:
    text = (value or "").strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    return bool(addr.is_global) and not addr.is_multicast


def _read_ipv4(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "kin-mail-console/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(64).decode("ascii", errors="ignore").strip()
    return raw.split()[0] if raw else ""


def probe_public_ipv4(*, timeout: float = 3.0, now: float | None = None) -> str:
    """Return a public IPv4 or empty. Cached briefly so the wizard can poll."""
    global _cache_at, _cache_ip
    ts = time.monotonic() if now is None else now
    if _cache_ip and (ts - _cache_at) < _CACHE_TTL_SEC:
        return _cache_ip
    found = ""
    for url in _SOURCES:
        try:
            candidate = _read_ipv4(url, timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
        if is_public_ipv4(candidate):
            found = candidate
            break
    if found:
        _cache_ip = found
        _cache_at = ts
    return found
