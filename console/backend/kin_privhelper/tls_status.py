"""What certificate is this appliance actually serving, and for how long.

Read the DEPLOYED certificate first, not the one in /etc/letsencrypt.

They are not the same thing. certbot renews into /etc/letsencrypt and a deploy
hook copies it into Zimbra; if that hook fails - and on an HA Secondary it
cannot run at all, because /opt/zimbra is an unmounted mountpoint - then
/etc/letsencrypt holds a fresh 90-day certificate while clients are still being
handed the old one. Reading the wrong file reports a healthy certificate on an
appliance that is about to serve an expired one, which is the same shape of
mistake as calling a dead replication link UpToDate.

Cached, because it is read on the cluster status poll and an expiry date does
not change between polls.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Deployed first. The last entry is where a future upload-a-certificate flow
# would put one; it is checked last so it can never mask what Zimbra serves.
def cert_paths(mail_host: str = "") -> list[Path]:
    host = (mail_host or "").strip()
    paths: list[Path] = [
        Path("/opt/zimbra/ssl/zimbra/commercial/commercial.crt"),
        Path("/opt/zimbra/ssl/zimbra/server/server.crt"),
    ]
    if host:
        paths.append(Path(f"/etc/letsencrypt/live/{host}/cert.pem"))
    paths.append(Path("/var/lib/kin-mail-console/tls/cert.pem"))
    return paths


def parse_openssl_enddate(text: str) -> datetime | None:
    """`notAfter=Oct  1 12:00:00 2026 GMT` -> aware datetime, or None.

    openssl pads a single-digit day with two spaces; strptime tolerates that.
    Anything it cannot read returns None rather than a guess, because a wrong
    expiry date is worse than an unknown one.
    """
    stamp = (text or "").strip()
    if not stamp:
        return None
    stamp = stamp.split("=", 1)[-1].strip()
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            dt = datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc)
    return None


def days_until(expiry: datetime | None, now: datetime | None = None) -> int | None:
    if expiry is None:
        return None
    ref = now or datetime.now(timezone.utc)
    # timedelta normalises so that .days is already the floor in both
    # directions: 30.5 days is 30, and anything past expiry is negative. An
    # extra "correction" for the negative case made an expiry two days ago
    # report as three.
    return (expiry - ref).days


def _read_enddate(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-enddate"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def read_certificate(mail_host: str = "", method: str = "") -> dict[str, Any]:
    """The certificate this host would present. Never raises."""
    info: dict[str, Any] = {
        "path": "",
        "not_after": "",
        "days_left": None,
        "method": method or "",
        "source": "",
    }
    for path in cert_paths(mail_host):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        line = _read_enddate(path)
        if not line:
            continue
        expiry = parse_openssl_enddate(line)
        info["path"] = str(path)
        info["not_after"] = line.split("=", 1)[-1].strip()
        info["days_left"] = days_until(expiry)
        info["source"] = "deployed" if "/opt/zimbra/" in str(path) else "issued"
        break
    return info


_CACHE: dict[str, Any] = {}
_CACHE_TTL = float(os.environ.get("KIN_TLS_STATUS_TTL", "300"))


def cached_certificate(mail_host: str = "", method: str = "") -> dict[str, Any]:
    """read_certificate with a TTL, for the status poll."""
    now = time.monotonic()
    key = f"{mail_host}\0{method}"
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return dict(hit[1])
    info = read_certificate(mail_host, method)
    _CACHE[key] = (now, info)
    return dict(info)


def clear_cache() -> None:
    _CACHE.clear()
