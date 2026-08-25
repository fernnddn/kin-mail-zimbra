"""Console-driven appliance settings (AD, admin IPs, license, TLS).

section=seats still exists on the privhelper for internal/bootstrap writes.
The customer Settings page no longer exposes a manual seat field; seat count
comes from applying a signed license (which writes CONTRACTED_SEATS).
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto
from .apply_config import update_config_keys
from .commands import resolve_under_deploy
from .deploy_state import (
    ensure_server_id,
    read_license_token,
    read_server_id,
    write_license_token,
)

_IPV4 = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|[01]?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d?\d)){3}$"
)
_CIDR = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|[01]?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d?\d)){3}"
    r"/(?:3[0-2]|[12]?\d)$"
)

FIREWALL_CANDIDATES = (
    "install/10-host-firewall.sh",
    "10-host-firewall.sh",
)
TLS_CANDIDATES = (
    "install/04-tls-dkim.sh",
    "04-tls-dkim.sh",
)


def _emit(text: str, err: bool = False) -> dict[str, Any]:
    if err:
        return proto.event_stderr(text if text.endswith("\n") else text + "\n")
    return proto.event_stdout(text if text.endswith("\n") else text + "\n")


def parse_admin_ips(raw: str) -> list[str]:
    items: list[str] = []
    for part in raw.replace(",", " ").split():
        token = part.strip()
        if not token:
            continue
        if not (_IPV4.match(token) or _CIDR.match(token)):
            raise ValueError(f"not an IPv4 or CIDR: {token}")
        if token not in items:
            items.append(token)
    return items


def _license_state(token: str, server_id: str) -> dict[str, Any]:
    from kin_console.license import verify_license

    if not token:
        return {
            "present": False,
            "status": "none",
            "provisioning_blocked": False,
            "seats": None,
            "server_id": server_id,
        }
    try:
        verified = verify_license(token, server_id=server_id)
    except ValueError as exc:
        return {
            "present": True,
            "status": "invalid",
            "error": str(exc),
            "provisioning_blocked": False,
            "seats": None,
            "server_id": server_id,
        }
    return {"present": True, "server_id": server_id, **verified}


async def cmd_apply_appliance_settings(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    args = args or {}
    section = str(args.get("section") or "").strip().lower()
    if section == "seats":
        async for ev in _set_seats(args):
            yield ev
        return
    if section == "ad":
        async for ev in _set_ad(args):
            yield ev
        return
    if section == "firewall":
        async for ev in _set_firewall(args):
            yield ev
        return
    if section == "license":
        async for ev in _set_license(args):
            yield ev
        return
    if section == "tls_status":
        async for ev in _tls_status():
            yield ev
        return
    if section == "tls_renew":
        async for ev in _tls_renew():
            yield ev
        return
    if section == "status":
        sid = read_server_id() or ensure_server_id()
        token = read_license_token()
        state = _license_state(token, sid)
        seats_raw = ""
        admin_ips = ""
        try:
            from .apply_config import CONF_FILE, parse_config

            if CONF_FILE.is_file():
                cfg = parse_config(CONF_FILE.read_text(encoding="utf-8"))
                seats_raw = str(cfg.get("CONTRACTED_SEATS") or "")
                admin_ips = str(cfg.get("KIN_ADMIN_IPS") or "")
        except OSError:
            pass
        license_seats = state.get("seats") if state.get("status") not in ("none", "invalid", None) and state.get("present") else None
        yield _emit(
            "SETTINGS_JSON:"
            + json.dumps(
                {
                    "license": state,
                    "server_id": sid,
                    "contracted_seats": str(license_seats) if license_seats else seats_raw,
                    "seats_source": "license" if license_seats else "manual",
                    "admin_ips": admin_ips,
                }
            )
        )
        yield proto.event_done(0)
        return
    yield _emit("section must be seats, ad, firewall, license, tls_status, tls_renew, or status", err=True)
    yield proto.event_done(2)


async def _set_seats(args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    raw = str(args.get("seats") or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        yield _emit("seats must be a positive integer", err=True)
        yield proto.event_done(2)
        return
    token = read_license_token()
    sid = read_server_id() or ensure_server_id()
    if token:
        state = _license_state(token, sid)
        if state.get("present") and state.get("status") not in ("none", "invalid"):
            yield _emit(
                "A signed license is active. Seat count comes from that license, not this field.",
                err=True,
            )
            yield proto.event_done(2)
            return
    code, lines = update_config_keys({"CONTRACTED_SEATS": raw})
    for line in lines:
        yield _emit(line, err=code != 0)
    yield proto.event_done(code)


async def _set_ad(args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    enabled = bool(args.get("enabled"))
    updates = {
        "AD_AUTH_ENABLED": "yes" if enabled else "no",
        "AD_LDAP_URL": str(args.get("ldap_url") or "").strip(),
        "AD_SEARCH_BASE": str(args.get("search_base") or "").strip(),
        "AD_SEARCH_FILTER": str(args.get("search_filter") or "").strip()
        or "(sAMAccountName=%u)",
        "AD_SEARCH_BIND_DN": str(args.get("search_bind_dn") or "").strip(),
        "AD_BIND_DN_TEMPLATE": str(args.get("bind_dn_template") or "").strip(),
    }
    pw = str(args.get("search_bind_password") or "")
    if pw:
        updates["AD_SEARCH_BIND_PASSWORD"] = pw
    code, lines = update_config_keys(updates)
    for line in lines:
        yield _emit(line, err=code != 0)
    yield proto.event_done(code)


async def _set_firewall(args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    try:
        ips = parse_admin_ips(str(args.get("admin_ips") or ""))
    except ValueError as exc:
        yield _emit(str(exc), err=True)
        yield proto.event_done(2)
        return
    client_ip = str(args.get("client_ip") or "").strip()
    if client_ip and _IPV4.match(client_ip) and client_ip not in ips:
        ips.append(client_ip)
        yield _emit(f"Kept this console session reachable from {client_ip}")
    if not ips:
        yield _emit("Need at least one trusted IP or network", err=True)
        yield proto.event_done(2)
        return
    joined = " ".join(ips)
    code, lines = update_config_keys({"KIN_ADMIN_IPS": joined})
    for line in lines:
        yield _emit(line, err=code != 0)
    if code != 0:
        yield proto.event_done(code)
        return
    script = resolve_under_deploy(FIREWALL_CANDIDATES, "10-host-firewall.sh")
    yield _emit(
        f"Applying host firewall via {script}. A safety timer will disable ufw unless you confirm access."
    )
    from .commands import _stream_subprocess

    async for ev in _stream_subprocess([str(script), "apply"], extra_env={"KIN_ADMIN_IPS": joined}):
        yield ev


async def _set_license(args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    from kin_console.license import verify_license

    token = str(args.get("token") or "").strip()
    sid = read_server_id() or ensure_server_id()
    try:
        verified = verify_license(token, server_id=sid)
    except ValueError as exc:
        yield _emit(str(exc), err=True)
        yield proto.event_done(2)
        return
    # Write CONTRACTED_SEATS first, license.token second. A verified token on
    # disk with a stale seat count would read as "licensed" while the quota
    # gate (which only reads CONTRACTED_SEATS) still enforces the old limit -
    # writing the token last means a failure here leaves that combination
    # impossible instead of leaving a valid token paired with stale seats
    # (re-audit, 25 Aug 2026).
    code, lines = update_config_keys({"CONTRACTED_SEATS": str(verified["seats"])})
    for line in lines:
        yield _emit(line, err=code != 0)
    if code != 0:
        yield proto.event_done(code)
        return
    write_license_token(token)
    yield _emit("LICENSE_JSON:" + json.dumps(verified))
    yield proto.event_done(0)


def _cert_paths() -> list[Path]:
    host = ""
    try:
        from .apply_config import CONF_FILE, parse_config

        if CONF_FILE.is_file():
            host = str(parse_config(CONF_FILE.read_text(encoding="utf-8")).get("MAIL_HOST") or "")
    except OSError:
        host = ""
    paths = [
        Path(f"/etc/letsencrypt/live/{host}/cert.pem") if host else None,
        Path("/opt/zimbra/ssl/zimbra/commercial/commercial.crt"),
        Path("/var/lib/kin-mail-console/tls/cert.pem"),
    ]
    return [p for p in paths if p is not None]


async def _tls_status() -> AsyncIterator[dict[str, Any]]:
    import subprocess

    info: dict[str, Any] = {"path": "", "not_after": "", "days_left": None, "method": ""}
    try:
        from .apply_config import CONF_FILE, parse_config

        if CONF_FILE.is_file():
            info["method"] = str(
                parse_config(CONF_FILE.read_text(encoding="utf-8")).get("TLS_METHOD") or ""
            )
    except OSError:
        pass
    for path in _cert_paths():
        if not path.is_file():
            continue
        try:
            proc = subprocess.run(
                ["openssl", "x509", "-in", str(path), "-noout", "-enddate"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if proc.returncode != 0:
            continue
        line = (proc.stdout or "").strip()
        # notAfter=Oct  1 12:00:00 2026 GMT
        stamp = line.split("=", 1)[-1].strip()
        try:
            dt = datetime.strptime(stamp, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
        info["path"] = str(path)
        info["not_after"] = stamp
        if dt is not None:
            info["days_left"] = (dt - datetime.now(timezone.utc)).days
        break
    yield _emit("TLS_JSON:" + json.dumps(info))
    yield proto.event_done(0)


async def _tls_renew() -> AsyncIterator[dict[str, Any]]:
    script = resolve_under_deploy(TLS_CANDIDATES, "04-tls-dkim.sh")
    yield _emit(f"Starting TLS renewal via {script} (DNS poll mode).")
    from .commands import _stream_subprocess

    argv = [str(script)]
    if shutil.which("stdbuf"):
        argv = ["stdbuf", "-oL", "-eL", *argv]
    async for ev in _stream_subprocess(
        argv,
        extra_env={"KIN_MAIL_DNS_POLL": "1", "KIN_TLS_FORCE_RENEW": "1"},
    ):
        yield ev
