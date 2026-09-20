"""Console-driven appliance settings (AD, admin IPs, license, TLS).

section=seats still exists on the privhelper for internal/bootstrap writes.
The customer Settings page no longer exposes a manual seat field; seat count
comes from applying a signed license (which writes CONTRACTED_SEATS).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto
from .apply_config import update_config_keys
from .license_sync import push_license_to_peer
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
HYBRID_AUTH_CANDIDATES = (
    "install/06-hybrid-auth.sh",
    "06-hybrid-auth.sh",
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


# Where the mailbox keeps the stage that writes its own firewall. It is not
# pushed from here: that script sources 00-config.sh and lib/, so it is the
# copy the build installed, not a single file that can travel.
MAILBOX_FIREWALL = "/opt/kin-mail-deploy/install/10-host-firewall.sh"


def _config_value(key: str) -> str:
    """One key out of /etc/kin-mail/config, or "" when absent/unreadable."""
    try:
        from .apply_config import CONF_FILE, parse_config

        if not CONF_FILE.is_file():
            return ""
        return str(parse_config(CONF_FILE.read_text(encoding="utf-8")).get(key) or "").strip()
    except OSError:
        return ""


def _license_state(token: str, server_id: str) -> dict[str, Any]:
    from kin_console.license import license_view

    return license_view(token, server_id)


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
    if section == "cloudflare_token":
        async for ev in _set_cloudflare_token(args):
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
    yield _emit(
        "section must be seats, ad, firewall, cloudflare_token, license, "
        "tls_status, tls_renew, or status",
        err=True,
    )
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
        if state.get("status") == "invalid":
            yield _emit(
                "A license file is present but not valid. Apply a signed license "
                "before changing seats. CONTRACTED_SEATS is not writable while "
                "the token is invalid.",
                err=True,
            )
            yield proto.event_done(2)
            return
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
    if code != 0:
        yield proto.event_done(code)
        return
    ok, sync_lines = await push_license_to_peer(seats=raw, token="", server_id=sid)
    for line in sync_lines:
        yield _emit(line, err=not ok)
    if not ok:
        yield _emit(
            "The seat count is set on this node. Re-apply it once the other "
            "node is reachable so the pair agrees.",
            err=True,
        )
        yield proto.event_done(3)
        return
    yield proto.event_done(0)


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
    # 06-hybrid-auth.sh refuses to run without a directory account it can bind
    # as a real user, so Settings has to be able to supply them too. Blank
    # means "leave what is stored", same as the bind password.
    test_user = str(args.get("test_user") or "").strip()
    test_pass = str(args.get("test_pass") or "")
    if test_user:
        updates["AD_TEST_USER"] = test_user
    if test_pass:
        updates["AD_TEST_PASS"] = test_pass
    code, lines = update_config_keys(updates)
    for line in lines:
        yield _emit(line, err=code != 0)
    if code != 0:
        yield proto.event_done(code)
        return

    if not enabled:
        yield _emit(
            "Directory sign-in is off. Console falls back to local accounts; "
            "Zimbra's own directory settings are left untouched."
        )
        yield proto.event_done(0)
        return

    # Writing the config only configures the CONSOLE. Zimbra authenticates
    # through its own zimbraAuthLdap* domain attributes, which are set by
    # 06-hybrid-auth.sh via zmprov - so without this the operator would enable
    # AD, see console sign-in start working, and have no idea mail sign-in was
    # still purely local (live Phase 6 QA asked for both halves to work).
    missing = [
        k
        for k in ("AD_TEST_USER", "AD_TEST_PASS")
        if not str(_config_value(k) or "").strip()
    ]
    if missing:
        yield _emit(
            "Console sign-in is configured. Zimbra was NOT reconfigured: "
            f"{' and '.join(missing)} is not set, and 06-hybrid-auth.sh needs a "
            "real directory account to verify the bind before it changes mail "
            "authentication. Fill in the directory test account and save again.",
            err=True,
        )
        yield proto.event_done(0)
        return

    script = resolve_under_deploy(HYBRID_AUTH_CANDIDATES, "06-hybrid-auth.sh")
    yield _emit(f"Applying directory settings to Zimbra via {script}")
    from .commands import _stream_subprocess

    hybrid_code = 0
    async for ev in _stream_subprocess([str(script)]):
        if ev.get("type") == "done":
            hybrid_code = int(ev.get("exit_code") or 0)
        else:
            yield ev
    if hybrid_code != 0:
        yield _emit(
            f"Zimbra directory setup exited {hybrid_code}. Console sign-in is "
            "configured, but mail sign-in is unchanged - see the output above.",
            err=True,
        )
    else:
        yield _emit("Zimbra now authenticates against the same directory")
    yield proto.event_done(hybrid_code)


CF_CREDS_PATH = Path("/etc/letsencrypt/cloudflare.ini")


def cloudflare_token_error(token: str) -> str | None:
    """Reject anything that clearly is not a Cloudflare API token."""
    body = (token or "").strip()
    if not body:
        return "Paste the Cloudflare API token."
    if any(c.isspace() for c in body):
        return "That token contains spaces or line breaks - paste just the token."
    if "PASTE" in body.upper():
        return "That is still the placeholder text, not a real token."
    # Cloudflare tokens are 40 URL-safe characters today; accept a range rather
    # than pinning an exact length, but catch an obviously truncated paste.
    if len(body) < 20:
        return "That token looks too short - copy the whole value."
    if len(body) > 200:
        return "That does not look like a Cloudflare API token."
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+", body):
        return "That token has characters a Cloudflare API token does not use."
    return None


async def _set_cloudflare_token(args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Write /etc/letsencrypt/cloudflare.ini so DNS-01 can run unattended.

    certbot's Cloudflare plugin reads the token from a file, and 04-tls-dkim.sh
    refuses to prompt for it when it has no terminal - which is every run
    driven from this console. Without somewhere to put the token, choosing
    Cloudflare meant the operator had to SSH in and hand-write the file, which
    is precisely the admin access this console exists to replace.

    The token never touches the wizard draft: it goes straight to a root-owned
    0600 file, the same treatment as the host provisioning passwords.
    """
    token = str(args.get("token") or "").strip()
    problem = cloudflare_token_error(token)
    if problem:
        yield _emit(problem, err=True)
        yield proto.event_done(2)
        return
    try:
        CF_CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Create with the final mode rather than widening it afterwards, so the
        # token is never briefly world-readable on disk.
        fd = os.open(
            str(CF_CREDS_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            os.write(fd, f"dns_cloudflare_api_token = {token}\n".encode())
        finally:
            os.close(fd)
        os.chmod(CF_CREDS_PATH, 0o600)
        os.chown(CF_CREDS_PATH, 0, 0)
    except OSError as exc:
        yield _emit(f"Could not write {CF_CREDS_PATH}: {exc}", err=True)
        yield proto.event_done(1)
        return
    # Never echo the token back, not even a prefix.
    yield _emit(f"Cloudflare API token stored at {CF_CREDS_PATH} (root only, mode 600)")
    yield _emit("Certificate issuance and automatic renewal can now run unattended.")
    yield proto.event_done(0)


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

    async for ev in _set_firewall_on_mailbox(joined):
        yield ev


async def _set_firewall_on_mailbox(joined: str) -> AsyncIterator[dict[str, Any]]:
    """Carry the trusted IPs to the other machine of a multi deployment.

    This setting's whole purpose is admin access, and on a split the Zimbra
    admin console is on the MAILBOX - port 7071, served by mailboxd, which the
    edge does not run. Applying it only here left that port open to the edge
    and to nobody else, so the operator added their laptop, watched it apply,
    and still could not reach the one page they wanted (20 Sep 2026).

    Best effort. The edge's own firewall is already applied by the time this
    runs; a mailbox that cannot be reached is worth a warning, not an undo.
    """
    from .commands import (
        _mailbox_ssh_password,
        _appliance_config,
        mailbox_ssh_argv,
        _stream_subprocess,
    )

    cfg = _appliance_config()
    if cfg.get("TOPOLOGY") != "split":
        return
    ip = (cfg.get("MAILBOX_IP") or "").strip()
    user = (cfg.get("MAILBOX_SSH_USER") or cfg.get("KIN_OS_USER") or "kin").strip()
    password = _mailbox_ssh_password(cfg)
    if not ip or not password:
        yield _emit(
            "The mailbox address or its stored password is missing, so its "
            "firewall still allows only this machine. The Zimbra admin console "
            "on that node will stay unreachable.",
            err=True,
        )
        return
    if shutil.which("sshpass") is None:
        yield _emit("sshpass is not installed here, so the mailbox was not updated.", err=True)
        return

    # The value is interpolated, and it is safe to interpolate: parse_admin_ips
    # has already refused anything that is not an IPv4 address or a CIDR, so it
    # holds digits, dots, slashes and spaces and nothing a shell or sed would
    # read as syntax. Written to the config as well as passed in the
    # environment, because an environment variable does not survive the next
    # time somebody re-runs that stage by hand.
    conf = "/etc/kin-mail/config"
    remote = (
        f"sed -i 's|^KIN_ADMIN_IPS=.*|KIN_ADMIN_IPS=\"{joined}\"|' {conf}"
        f" && grep -q '^KIN_ADMIN_IPS=' {conf}"
        f" || printf 'KIN_ADMIN_IPS=\"{joined}\"\\n' >> {conf}; "
        f"KIN_ADMIN_IPS=\"{joined}\" {MAILBOX_FIREWALL} apply"
        # Cancelled on the same proof the build uses: this SSH connection is
        # still open after the rules went live, so the rules did not cut it.
        # Nobody is sitting at that machine to press a button.
        f" && {MAILBOX_FIREWALL} cancel-deadman"
    )
    yield _emit(f"Applying the same trusted IPs on the mailbox ({ip}).")
    rc = 0
    async for ev in _stream_subprocess(
        mailbox_ssh_argv(user, ip, remote),
        extra_env={"SSHPASS": password},
        secrets=[password],
        stdin_text=password + "\n",
    ):
        if ev.get("type") == "done":
            rc = int(ev.get("exit_code") or 0)
            continue
        yield ev
    if rc == 0:
        yield _emit("Mailbox firewall updated; its Zimbra admin console is reachable from those IPs.")
    else:
        yield _emit(
            "The mailbox firewall was NOT updated. This machine is firewalled "
            "correctly; the Zimbra admin console on the mailbox will still "
            "refuse everything but this node.",
            err=True,
        )


async def _set_license(args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    from kin_console.license import verify_license

    # Collapse whitespace the same way verify does, so what we store matches
    # what Settings pasted (including accidental newlines from a wrapped copy).
    token = "".join(str(args.get("token") or "").split())
    sid = read_server_id() or ensure_server_id()
    try:
        verified = verify_license(token, server_id=sid)
    except ValueError as exc:
        detail = str(exc)
        # Name both IDs. "different Email Server ID" alone gave the operator no
        # way to see WHICH id the license carries, so a license issued for the
        # peer node (or for an id captured before a rebuild regenerated it)
        # was indistinguishable from a bad signature (live Phase 4-1).
        if "different email server id" in detail.lower():
            issued_for = ""
            try:
                from kin_console.license import parse_license_string

                payload, _raw, _sig = parse_license_string(token)
                issued_for = str(payload.get("server_id") or "").strip()
            except ValueError:
                issued_for = ""
            if issued_for:
                detail = (
                    f"{detail}: this server_id is {sid}, "
                    f"the license was issued for {issued_for}"
                )
        yield _emit(detail, err=True)
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

    # An HA pair is one licensed appliance. Until this ran, applying a key here
    # left the other node unlicensed, and which one answered depended on where
    # the VIP was pointing.
    ok, sync_lines = await push_license_to_peer(
        seats=str(verified["seats"]), token=token, server_id=sid
    )
    for line in sync_lines:
        yield _emit(line, err=not ok)
    yield _emit("LICENSE_JSON:" + json.dumps(verified))
    if not ok:
        # The local node is licensed correctly, so this is not a failure of the
        # apply; it is a pair that now disagrees, and saying so is the point.
        yield _emit(
            "The license is active on this node. Re-apply it once the other "
            "node is reachable so the pair agrees.",
            err=True,
        )
        yield proto.event_done(3)
        return
    yield proto.event_done(0)


def _mail_host_from_config() -> str:
    try:
        from .apply_config import CONF_FILE, parse_config

        if CONF_FILE.is_file():
            return str(
                parse_config(CONF_FILE.read_text(encoding="utf-8")).get("MAIL_HOST") or ""
            )
    except OSError:
        pass
    return ""


def _tls_method_from_config() -> str:
    try:
        from .apply_config import CONF_FILE, parse_config

        if CONF_FILE.is_file():
            return str(
                parse_config(CONF_FILE.read_text(encoding="utf-8")).get("TLS_METHOD") or ""
            )
    except OSError:
        pass
    return ""


async def _tls_status() -> AsyncIterator[dict[str, Any]]:
    """Report the certificate this host would actually present.

    The reading itself lives in kin_privhelper.tls_status so the Settings page
    and the cluster status poll cannot disagree about which file is the truth.
    """
    from .tls_status import read_certificate

    info = read_certificate(_mail_host_from_config(), _tls_method_from_config())
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
