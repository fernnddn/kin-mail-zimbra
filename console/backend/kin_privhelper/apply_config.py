"""Apply wizard draft → /etc/kin-mail/config (merge + timestamped backup).

File write only — does not run install stages or restart services.

On a fresh appliance (no /etc/kin-mail/config yet), creates a base config first
using auto-detected SERVER_IP / NET_IFACE (same idea as install/00-config.sh
detect_defaults) plus safe defaults, then merges the wizard draft on top.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

_IPV4_RE = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$"
)

# zmsetup.pl checkPasswordStrength rejects $ & | < > / ; ` and whitespace
# ("Invalid metacharater used."). Also block ! * and quotes that break tmux.
_ZIMBRA_ADMIN_PASS_META_RE = re.compile(r"""[\s!$&*|<>/;`'"\\]""")


def valid_ipv4(value: str) -> bool:
    return bool(_IPV4_RE.match((value or "").strip()))


def cloudflare_creds_error(path: str, text: str | None) -> str | None:
    """Non-interactive Cloudflare TLS cannot prompt for a token."""
    dest = (path or "/etc/letsencrypt/cloudflare.ini").strip()
    body = (text or "").strip()
    if not body:
        return (
            f"TLS_METHOD=cloudflare needs a real API token in {dest} "
            "(the wizard does not store it). Write dns_cloudflare_api_token = ... "
            "there with mode 600, or choose Manual / Customer TLS."
        )
    if "PASTE" in body:
        return (
            f"{dest} still contains PASTE; replace it with a real Cloudflare API token "
            "or choose Manual / Customer TLS."
        )
    return None


CONF_DIR = Path("/etc/kin-mail")
CONF_FILE = CONF_DIR / "config"
DRAFT_FILE = Path(
    os.environ.get("KIN_CONSOLE_DRAFT", "/var/lib/kin-mail-console/wizard-draft.json")
)

# Keys written with single-quoted values (may contain #, spaces, etc.).
_SINGLE_QUOTE_KEYS = frozenset(
    {
        "ADMIN_PASS",
        "HOST_ROOT_PASS",
        "KIN_USER_PASS",
        "AD_SEARCH_FILTER",
        "AD_SEARCH_BIND_PASSWORD",
        "AD_TEST_PASS",
    }
)

# Preferred key order matching 00-config.sh wizard output (+ TOPOLOGY).
_KEY_ORDER = [
    "TOPOLOGY",
    "PEER_HOST_IP",
    "PEER_HOST_NAME",
    "OBSERVABILITY_VM_IP",
    "CLUSTER_VIP_IP",
    "KIN_OS_USER",
    "HOST_ROOT_PASS",
    "KIN_USER_PASS",
    "MAIL_DOMAIN",
    "MAIL_HOST",
    "SERVER_IP",
    "NET_IFACE",
    "TIMEZONE",
    "ZIMBRA_TZ_NAME",
    "DNS_UPSTREAM_1",
    "DNS_UPSTREAM_2",
    "INTERNAL_ZONE",
    "INTERNAL_DNS",
    "ZCS_VERSION",
    "ZCS_FILE",
    "ZCS_BASE",
    "ZCS_SRC",
    "ADMIN_PASS",
    "LE_EMAIL",
    "TLS_METHOD",
    "CF_CREDS",
    "CF_PROPAGATION",
    "EXTERNAL_TEST_ADDRESS",
    "TEST_USER_1",
    "TEST_PASS_1",
    "TEST_USER_2",
    "TEST_PASS_2",
    "AD_AUTH_ENABLED",
    "AD_LDAP_URL",
    "AD_SEARCH_BASE",
    "AD_SEARCH_FILTER",
    "AD_SEARCH_BIND_DN",
    "AD_SEARCH_BIND_PASSWORD",
    "AD_BIND_DN_TEMPLATE",
    "AD_TEST_USER",
    "AD_TEST_PASS",
    "CONTRACTED_SEATS",
    "KIN_ADMIN_IPS",
    "ZPUSH_ENABLED",
    "KIN_HA_PEER_INSTALL",
]

_ASSIGN_RE = re.compile(
    r"""^([A-Za-z_][A-Za-z0-9_]*)=(?:'(.*)'|"(.*)"|(.*))\s*$"""
)


def parse_config(text: str) -> dict[str, str]:
    """Parse KEY=value lines; ignore comments/blank. Values are unescaped lightly."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        if m.group(2) is not None:
            # single-quoted: only '' → '
            val = m.group(2).replace("'\\''", "'")
        elif m.group(3) is not None:
            val = m.group(3)
        else:
            val = m.group(4) or ""
        out[key] = val
    return out


def _sq(value: str) -> str:
    return value.replace("'", "'\\''")


def _dq(value: str, *, allow_dollar: bool = False) -> str:
    # Preserve literal $ in values like KinTest1-$(hostname -s) when rewriting
    # existing test passwords; escape $ for draft-sourced fields.
    out = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    if not allow_dollar:
        out = out.replace("$", "\\$")
    return out


def format_config(values: dict[str, str]) -> str:
    lines = [
        f"# KIN Mail - updated by kin-mail-privhelperd apply_wizard_draft",
        f"# Change with: sudo ./00-config.sh --reset  OR console apply_wizard_draft",
    ]
    allow_dollar_keys = frozenset({"TEST_PASS_1", "TEST_PASS_2"})
    seen: set[str] = set()
    for key in _KEY_ORDER:
        if key not in values:
            continue
        seen.add(key)
        val = values[key]
        if key in _SINGLE_QUOTE_KEYS:
            lines.append(f"{key}='{_sq(val)}'")
        else:
            lines.append(f'{key}="{_dq(val, allow_dollar=key in allow_dollar_keys)}"')
    for key, val in values.items():
        if key in seen:
            continue
        if key in _SINGLE_QUOTE_KEYS:
            lines.append(f"{key}='{_sq(val)}'")
        else:
            lines.append(f'{key}="{_dq(val, allow_dollar=key in allow_dollar_keys)}"')
    return "\n".join(lines) + "\n"


def ensure_topology_2vm(values: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Set TOPOLOGY=2vm unless the file already records a 2-server topology.

    Does not invent other keys. Caller writes with format_config().
    """
    current = str(values.get("TOPOLOGY") or "").strip().lower()
    if current in ("2vm", "2"):
        return dict(values), False
    out = dict(values)
    out["TOPOLOGY"] = "2vm"
    return out, True


def parse_ip_dash_o_v4(text: str) -> list[tuple[str, str]]:
    """Parse `ip -4 -o addr show scope global` into (ipv4, iface) pairs."""
    rows: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        parts = line.split()
        # e.g. "2: ens33    inet 192.0.2.15/24 brd ..." (RFC 5737; not a lab address)
        if len(parts) >= 4 and parts[2] == "inet":
            iface = parts[1]
            ip = parts[3].split("/", 1)[0]
            if iface and valid_ipv4(ip):
                rows.append((ip, iface))
    return rows


_DEFAULT_DEV_RE = re.compile(r"\bdev\s+(\S+)")


def default_route_iface(route_text: str) -> str:
    """Iface from `ip -4 route show default` (first default line)."""
    for line in (route_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("default") or stripped.startswith("0.0.0.0/0"):
            match = _DEFAULT_DEV_RE.search(stripped)
            if match:
                return match.group(1)
    return ""


def pick_host_network(addr_text: str, route_text: str = "") -> tuple[str, str]:
    """Prefer the default-route NIC; fall back to the first global IPv4."""
    rows = parse_ip_dash_o_v4(addr_text)
    if not rows:
        return "", ""
    want = default_route_iface(route_text)
    if want:
        for ip, iface in rows:
            if iface == want:
                return ip, iface
    return rows[0][0], rows[0][1]


def iface_for_ipv4(text: str, ipv4: str) -> str:
    """Return the iface that owns ipv4 in `ip -4 -o addr` text, else empty."""
    want = (ipv4 or "").strip()
    for ip, iface in parse_ip_dash_o_v4(text):
        if ip == want:
            return iface
    return ""


def detect_host_network() -> tuple[str, str]:
    """Auto-detect (SERVER_IP, NET_IFACE) like install/00-config.sh detect_defaults."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return "", ""
    route = ""
    try:
        route = subprocess.check_output(
            ["ip", "-4", "route", "show", "default"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        route = ""
    return pick_host_network(out, route)


def peer_install_config(
    primary: dict[str, str],
    *,
    peer_host: str,
    peer_ip: str,
    peer_iface: str,
) -> dict[str, str]:
    """Config for a fresh HA peer full-install.

    Domain, seats, ZCS artefact, and admin password are copied from the
    primary. Host identity is not: 02-prepare-os runs hostnamectl set-hostname
    MAIL_HOST and writes SERVER_IP into /etc/hosts, so those must be this
    machine. TLS/Z-Push/AD are forced off: repeating them on mail B is how HA
    join failed after a successful node-A Deploy (second ACME wait, Ondrej PPA,
    AD bind).
    """
    peer_host = (peer_host or "").strip()
    peer_ip = (peer_ip or "").strip()
    peer_iface = (peer_iface or "").strip()
    if not peer_host:
        raise ValueError("peer hostname is required")
    if not valid_ipv4(peer_ip):
        raise ValueError("peer SERVER_IP must be IPv4")
    if not peer_iface or any(ch in peer_iface for ch in ("/", " ", "\n", "\t")):
        raise ValueError("peer NET_IFACE is missing or invalid")
    primary_host = str(primary.get("MAIL_HOST") or "").strip()
    primary_ip = str(primary.get("SERVER_IP") or "").strip()
    if not str(primary.get("MAIL_DOMAIN") or "").strip():
        raise ValueError("primary config missing MAIL_DOMAIN")
    if not str(primary.get("ADMIN_PASS") or "").strip():
        raise ValueError("primary config missing ADMIN_PASS")
    if peer_host == primary_host:
        raise ValueError("peer hostname must differ from the primary MAIL_HOST")
    if peer_ip == primary_ip:
        raise ValueError("peer SERVER_IP must differ from the primary SERVER_IP")
    out = dict(primary)
    out["MAIL_HOST"] = peer_host
    out["SERVER_IP"] = peer_ip
    out["NET_IFACE"] = peer_iface
    out["TOPOLOGY"] = "2vm"
    if primary_ip:
        out["PEER_HOST_IP"] = primary_ip
    if primary_host:
        out["PEER_HOST_NAME"] = primary_host
    # Remote peer install must not re-issue Let's Encrypt for mail2, add a
    # second Z-Push, or bind AD. Those live on the primary DRBD copy; repeating
    # them is how HA join failed after a successful node-A Deploy (manual DNS-01
    # 25-minute wait, Cloudflare without mail2, Ondrej PPA).
    out["TLS_METHOD"] = "customer"
    out["ZPUSH_ENABLED"] = "no"
    out["AD_AUTH_ENABLED"] = "no"
    # 04 must not mint a second DKIM key; 05 must not FAIL when public DNS
    # still has the primary's selector. DRBD later overwrites B's tree anyway.
    out["KIN_HA_PEER_INSTALL"] = "yes"
    return out


def _os_version_id() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION_ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "22.04"


def _zcs_platform() -> tuple[str, str]:
    """Return (plat, tag_prefix) matching install/00-config.sh detect_latest_zcs."""
    version_id = _os_version_id()
    if version_id.startswith("24"):
        return "UBUNTU24_64", "zimbra-foss-build-ubuntu-24.04"
    return "UBUNTU22_64", "zimbra-foss-build-ubuntu-22.04"


def resolve_latest_zcs_artefacts() -> dict[str, str] | None:
    """Query Maldua GitHub releases for the newest .tgz matching this Ubuntu.

    Filenames include a build timestamp (e.g. …UBUNTU24_64.20260801175919.tgz);
    hardcoding the short name 404s and leaves empty stubs under /opt/zcs-src.
    """
    plat, tag = _zcs_platform()
    try:
        import urllib.request

        req = urllib.request.Request(
            "https://api.github.com/repos/maldua/zimbra-foss/releases?per_page=60",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "kin-mail-console"},
        )
        with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310 — fixed HTTPS URL
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None

    best_url = ""
    for rel in payload:
        if not isinstance(rel, dict):
            continue
        for asset in rel.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if (
                plat in name
                and name.endswith(".tgz")
                and f"/{tag}/" in url
                and url
            ):
                # Prefer lexicographic max (timestamps / versions sort usefully).
                if url > best_url:
                    best_url = url
    if not best_url:
        return None
    zcs_file = best_url.rsplit("/", 1)[-1]
    # …/download/{tag}/{version}/{file}
    parts = best_url.split("/")
    try:
        zcs_version = parts[parts.index("download") + 2]
    except (ValueError, IndexError):
        return None
    return {
        "ZCS_VERSION": zcs_version,
        "ZCS_FILE": zcs_file,
        "ZCS_BASE": f"https://github.com/maldua/zimbra-foss/releases/download/{tag}/{zcs_version}",
        "ZCS_SRC": "/opt/zcs-src",
    }


def default_zcs_artefacts() -> dict[str, str]:
    """ZCS download defaults — prefer live GitHub resolve, else last-known good."""
    resolved = resolve_latest_zcs_artefacts()
    if resolved:
        return resolved
    plat, tag = _zcs_platform()
    # Fallback must include the Maldua build-id suffix or downloads 404.
    if plat == "UBUNTU24_64":
        zcs_version = "10.1.18.p1"
        zcs_file = "zcs-10.1.18_GA_4200001.UBUNTU24_64.20260801175919.tgz"
    else:
        zcs_version = "10.1.18.p1"
        zcs_file = "zcs-10.1.18_GA_4200001.UBUNTU22_64.20260801175919.tgz"
    return {
        "ZCS_VERSION": zcs_version,
        "ZCS_FILE": zcs_file,
        "ZCS_BASE": (
            f"https://github.com/maldua/zimbra-foss/releases/download/{tag}/{zcs_version}"
        ),
        "ZCS_SRC": "/opt/zcs-src",
    }


def build_base_config() -> dict[str, str]:
    """Base /etc/kin-mail/config for a brand-new appliance (no prior CONF_FILE)."""
    server_ip, net_iface = detect_host_network()
    if not server_ip or not net_iface:
        raise RuntimeError(
            "cannot auto-detect SERVER_IP/NET_IFACE — check network is up "
            "(ip -4 addr show scope global)"
        )
    base: dict[str, str] = {
        "SERVER_IP": server_ip,
        "NET_IFACE": net_iface,
        "DNS_UPSTREAM_1": "1.1.1.1",
        "DNS_UPSTREAM_2": "8.8.8.8",
        "INTERNAL_ZONE": "",
        "INTERNAL_DNS": "",
        "CF_CREDS": "/etc/letsencrypt/cloudflare.ini",
        "CF_PROPAGATION": "40",
        "EXTERNAL_TEST_ADDRESS": "",
        "CONTRACTED_SEATS": "PLACEHOLDER_UNSET",
        "KIN_ADMIN_IPS": "",
        "ZPUSH_ENABLED": "no",
        "AD_AUTH_ENABLED": "no",
        "AD_LDAP_URL": "",
        "AD_SEARCH_BASE": "",
        "AD_SEARCH_FILTER": "(sAMAccountName=%u)",
        "AD_SEARCH_BIND_DN": "",
        "AD_SEARCH_BIND_PASSWORD": "",
        "AD_BIND_DN_TEMPLATE": "",
        "AD_TEST_USER": "",
        "AD_TEST_PASS": "",
        "TEST_PASS_1": "KinTest1-$(hostname -s)",
        "TEST_PASS_2": "KinTest2-$(hostname -s)",
    }
    base.update(default_zcs_artefacts())
    return base


def zimbra_tz(timezone: str) -> str:
    if timezone in ("Asia/Jakarta", "Asia/Pontianak"):
        return "Asia/Bangkok"
    return timezone


def validate_draft(draft: dict[str, Any], existing: dict[str, str]) -> list[str]:
    errs: list[str] = []
    topology = str(draft.get("topology") or "").strip()
    if topology not in ("1vm", "2vm"):
        errs.append("topology must be 1vm or 2vm")

    peer_ip = str(draft.get("peer_host_ip") or "").strip()
    if topology == "2vm" and not peer_ip:
        errs.append("peer_host_ip is required when topology is 2vm")
    obs_ip = str(draft.get("observability_vm_ip") or existing.get("OBSERVABILITY_VM_IP") or "").strip()
    if topology == "2vm" and not obs_ip:
        errs.append("observability_vm_ip is required when topology is 2vm")
    vip = str(draft.get("cluster_vip_ip") or existing.get("CLUSTER_VIP_IP") or "").strip()
    if topology == "2vm":
        if not vip:
            errs.append("cluster_vip_ip is required when topology is 2vm")
        elif not valid_ipv4(vip):
            errs.append("cluster_vip_ip must be an IPv4 address")
        else:
            local_ip = str(existing.get("SERVER_IP") or "").strip()
            if vip == peer_ip:
                errs.append("cluster_vip_ip must not be the second server IP")
            if vip == obs_ip:
                errs.append("cluster_vip_ip must not be the Observability VM IP")
            if local_ip and vip == local_ip:
                errs.append("cluster_vip_ip must not be this server's own address")

    host_root_pass = str(draft.get("host_root_pass") or "")
    if not host_root_pass:
        try:
            from kin_privhelper.provisioning_secrets import load_secrets

            vault = load_secrets()
            host_root_pass = vault.get("host_root_pass") or ""
        except Exception:  # noqa: BLE001
            host_root_pass = ""
    if host_root_pass:
        if len(host_root_pass) < 8:
            errs.append("host_root_pass must be at least 8 characters")
    elif not existing.get("HOST_ROOT_PASS"):
        errs.append("host_root_pass is required (draft empty, vault empty, and no existing HOST_ROOT_PASS)")

    kin_user_pass = str(draft.get("kin_user_pass") or "")
    if not kin_user_pass:
        try:
            from kin_privhelper.provisioning_secrets import load_secrets

            vault = load_secrets()
            kin_user_pass = vault.get("kin_user_pass") or ""
        except Exception:  # noqa: BLE001
            kin_user_pass = ""
    if kin_user_pass:
        if len(kin_user_pass) < 8:
            errs.append("kin_user_pass must be at least 8 characters")
    elif not existing.get("KIN_USER_PASS"):
        errs.append("kin_user_pass is required (draft empty and no existing KIN_USER_PASS)")

    domain = str(draft.get("mail_domain") or "").strip()
    if not domain or " " in domain or "@" in domain:
        errs.append("mail_domain is required (example: example.co.id)")

    host = str(draft.get("mail_host") or "").strip()
    if not host:
        errs.append("mail_host is required")

    tz = str(draft.get("timezone") or "").strip()
    if not tz:
        errs.append("timezone is required")

    admin_pass = str(draft.get("admin_pass") or "")
    if admin_pass:
        if len(admin_pass) < 8:
            errs.append("admin_pass must be at least 8 characters")
        if _ZIMBRA_ADMIN_PASS_META_RE.search(admin_pass):
            errs.append(
                "admin_pass cannot use characters like ! * & $ | < > / ; ` or spaces"
            )
    elif not existing.get("ADMIN_PASS"):
        errs.append("admin_pass is required (draft empty and no existing ADMIN_PASS)")
    elif _ZIMBRA_ADMIN_PASS_META_RE.search(str(existing.get("ADMIN_PASS") or "")):
        errs.append(
            "admin_pass cannot use characters like ! * & $ | < > / ; ` or spaces"
        )

    le = str(draft.get("le_email") or "").strip()
    if not le:
        errs.append("le_email is required")

    tls = str(draft.get("tls_method") or "").strip()
    if tls not in ("cloudflare", "manual", "customer"):
        errs.append("tls_method must be cloudflare, manual, or customer")

    seats = str(draft.get("contracted_seats") or "").strip() or "PLACEHOLDER_UNSET"
    if seats != "PLACEHOLDER_UNSET" and not seats.isdigit():
        errs.append("contracted_seats must be PLACEHOLDER_UNSET or a non-negative integer")

    if draft.get("ad_auth_enabled"):
        required_ad = [
            ("ad_ldap_url", "AD LDAP URL"),
            ("ad_search_base", "AD search base"),
            ("ad_search_bind_dn", "AD search bind DN"),
            ("ad_search_bind_password", "AD search bind password"),
            ("ad_test_user", "AD test user"),
            ("ad_test_pass", "AD test password"),
        ]
        for key, label in required_ad:
            if str(draft.get(key) or "").strip():
                continue
            if key == "ad_search_bind_password" and existing.get("AD_SEARCH_BIND_PASSWORD"):
                continue
            if key == "ad_test_pass" and existing.get("AD_TEST_PASS"):
                continue
            errs.append(f"{label} required when hybrid AD is enabled")

    # Preserve-critical live fields must already exist when merging
    # (populated by build_base_config() on first create).
    for key in ("SERVER_IP", "NET_IFACE"):
        if not existing.get(key):
            errs.append(f"existing config missing {key} — cannot safely merge draft")

    return errs


def merge_draft(draft: dict[str, Any], existing: dict[str, str]) -> dict[str, str]:
    out = dict(existing)
    domain = str(draft.get("mail_domain") or "").strip()
    host = str(draft.get("mail_host") or "").strip()
    tz = str(draft.get("timezone") or "").strip() or "Asia/Jakarta"
    admin_pass = str(draft.get("admin_pass") or "")
    host_root_pass = str(draft.get("host_root_pass") or "")
    kin_user_pass = str(draft.get("kin_user_pass") or "")
    le = str(draft.get("le_email") or "").strip()
    tls = str(draft.get("tls_method") or "").strip()
    seats = str(draft.get("contracted_seats") or "").strip() or "PLACEHOLDER_UNSET"
    if seats != "PLACEHOLDER_UNSET" and not seats.isdigit():
        seats = "PLACEHOLDER_UNSET"

    topology = str(draft.get("topology") or "").strip()
    out["TOPOLOGY"] = topology
    if topology == "2vm":
        out["PEER_HOST_IP"] = str(draft.get("peer_host_ip") or "").strip()
        out["PEER_HOST_NAME"] = str(draft.get("peer_host_name") or "").strip()
        obs = str(draft.get("observability_vm_ip") or "").strip()
        if obs:
            out["OBSERVABILITY_VM_IP"] = obs
        elif "OBSERVABILITY_VM_IP" not in out:
            out["OBSERVABILITY_VM_IP"] = ""
        vip = str(draft.get("cluster_vip_ip") or "").strip()
        if vip:
            out["CLUSTER_VIP_IP"] = vip
        elif "CLUSTER_VIP_IP" not in out:
            out["CLUSTER_VIP_IP"] = ""
    else:
        out["PEER_HOST_IP"] = ""
        out["PEER_HOST_NAME"] = ""
        out["OBSERVABILITY_VM_IP"] = ""
        out["CLUSTER_VIP_IP"] = ""

    # OS admin identity for later host provisioning; passwords preserve-on-empty like ADMIN_PASS.
    out["KIN_OS_USER"] = "kin"
    if not host_root_pass or not kin_user_pass:
        try:
            from kin_privhelper.provisioning_secrets import load_secrets

            vault = load_secrets()
            if not host_root_pass:
                host_root_pass = vault.get("host_root_pass") or ""
            if not kin_user_pass:
                kin_user_pass = vault.get("kin_user_pass") or ""
        except Exception:  # noqa: BLE001
            pass
    if host_root_pass:
        out["HOST_ROOT_PASS"] = host_root_pass
    if kin_user_pass:
        out["KIN_USER_PASS"] = kin_user_pass

    out["MAIL_DOMAIN"] = domain
    out["MAIL_HOST"] = host
    out["TIMEZONE"] = tz
    out["ZIMBRA_TZ_NAME"] = zimbra_tz(tz)
    if admin_pass:
        out["ADMIN_PASS"] = admin_pass
    out["LE_EMAIL"] = le
    out["TLS_METHOD"] = tls
    out["CONTRACTED_SEATS"] = seats
    # Do not wipe existing KIN_ADMIN_IPS when draft leaves the field blank
    # (wizard allows deferred firewall sources). Explicit blanking requires
    # sending whitespace-only only after operator clears it intentionally —
    # still preserve existing if draft value is empty string.
    draft_ips = str(draft.get("kin_admin_ips") or "").strip()
    if draft_ips:
        out["KIN_ADMIN_IPS"] = draft_ips
    elif "KIN_ADMIN_IPS" not in out:
        out["KIN_ADMIN_IPS"] = ""
    out["ZPUSH_ENABLED"] = "yes" if draft.get("zpush_enabled", False) else "no"

    # Keep test users aligned to domain (passwords preserved if already set).
    out["TEST_USER_1"] = f"test1@{domain}"
    out["TEST_USER_2"] = f"test2@{domain}"
    out.setdefault("TEST_PASS_1", existing.get("TEST_PASS_1", "KinTest1-$(hostname -s)"))
    out.setdefault("TEST_PASS_2", existing.get("TEST_PASS_2", "KinTest2-$(hostname -s)"))

    ad_on = bool(draft.get("ad_auth_enabled"))
    out["AD_AUTH_ENABLED"] = "yes" if ad_on else "no"
    out["AD_LDAP_URL"] = str(draft.get("ad_ldap_url") or "").strip()
    out["AD_SEARCH_BASE"] = str(draft.get("ad_search_base") or "").strip()
    out["AD_SEARCH_FILTER"] = (
        str(draft.get("ad_search_filter") or "").strip() or "(sAMAccountName=%u)"
    )
    out["AD_SEARCH_BIND_DN"] = str(draft.get("ad_search_bind_dn") or "").strip()
    # Preserve-on-empty for AD secrets (same discipline as HOST_ROOT_PASS / ADMIN_PASS).
    ad_bind_pw = str(draft.get("ad_search_bind_password") or "")
    if ad_bind_pw:
        out["AD_SEARCH_BIND_PASSWORD"] = ad_bind_pw
    elif "AD_SEARCH_BIND_PASSWORD" not in out:
        out["AD_SEARCH_BIND_PASSWORD"] = ""
    out["AD_BIND_DN_TEMPLATE"] = str(draft.get("ad_bind_dn_template") or "").strip()
    out["AD_TEST_USER"] = str(draft.get("ad_test_user") or "").strip()
    ad_test_pw = str(draft.get("ad_test_pass") or "")
    if ad_test_pw:
        out["AD_TEST_PASS"] = ad_test_pw
    elif "AD_TEST_PASS" not in out:
        out["AD_TEST_PASS"] = ""

    out.setdefault("CF_CREDS", "/etc/letsencrypt/cloudflare.ini")
    out.setdefault("CF_PROPAGATION", "40")
    out.setdefault("ZCS_SRC", "/opt/zcs-src")
    out.setdefault("EXTERNAL_TEST_ADDRESS", "")

    # Until Zimbra exists, always refresh ZCS_* from GitHub so a stale short
    # filename (no build timestamp) cannot 404 on the next Deploy.
    if not Path(os.environ.get("KIN_ZIMBRA_ROOT", "/opt/zimbra")).exists():
        artefacts = default_zcs_artefacts()
        out.update(artefacts)

    return out


def _config_fingerprint(values: dict[str, str]) -> dict[str, str]:
    """Non-secret keys for change detection / operator-facing logs."""
    keys = (
        "TOPOLOGY",
        "PEER_HOST_IP",
        "PEER_HOST_NAME",
        "OBSERVABILITY_VM_IP",
        "CLUSTER_VIP_IP",
        "KIN_OS_USER",
        "MAIL_DOMAIN",
        "MAIL_HOST",
        "SERVER_IP",
        "NET_IFACE",
        "TIMEZONE",
        "TLS_METHOD",
        "ZPUSH_ENABLED",
        "CONTRACTED_SEATS",
        "KIN_ADMIN_IPS",
        "AD_AUTH_ENABLED",
        "LE_EMAIL",
        "ZCS_FILE",
    )
    return {k: values.get(k, "") for k in keys}


def apply_wizard_draft() -> tuple[int, list[str]]:
    """Returns (exit_code, log_lines)."""
    lines: list[str] = []
    lines.append("=== apply_wizard_draft ===\n")

    if not DRAFT_FILE.is_file():
        lines.append(f"ERROR: draft missing at {DRAFT_FILE}\n")
        return 1, lines

    created_new = False
    existing: dict[str, str] = {}
    if CONF_FILE.is_file():
        try:
            existing_text = CONF_FILE.read_text(encoding="utf-8")
        except OSError as exc:
            lines.append(f"ERROR: cannot read config: {exc}\n")
            return 1, lines
        existing = parse_config(existing_text)
        lines.append(f"Loaded existing config ({len(existing)} keys)\n")
    else:
        created_new = True
        try:
            existing = build_base_config()
        except RuntimeError as exc:
            lines.append(f"ERROR: {exc}\n")
            return 1, lines
        lines.append(
            f"No {CONF_FILE} yet — building base config "
            f"(SERVER_IP={existing.get('SERVER_IP')} NET_IFACE={existing.get('NET_IFACE')})\n"
        )

    try:
        draft = json.loads(DRAFT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        lines.append(f"ERROR: cannot read draft: {exc}\n")
        return 1, lines
    if not isinstance(draft, dict):
        lines.append("ERROR: draft is not a JSON object\n")
        return 1, lines
    lines.append(f"Loaded draft from {DRAFT_FILE}\n")

    errs = validate_draft(draft, existing)
    if errs:
        lines.append("ERROR: draft validation failed:\n")
        for e in errs:
            lines.append(f"  - {e}\n")
        return 1, lines
    lines.append("Draft validation OK\n")

    before_fp = _config_fingerprint(existing) if not created_new else {}
    merged = merge_draft(draft, existing)
    if str(merged.get("TLS_METHOD") or "").strip() == "cloudflare":
        cf_path = str(merged.get("CF_CREDS") or "/etc/letsencrypt/cloudflare.ini").strip()
        try:
            cf_text = Path(cf_path).read_text(encoding="utf-8")
        except OSError:
            cf_text = None
        cf_err = cloudflare_creds_error(cf_path, cf_text)
        if cf_err:
            lines.append(f"ERROR: {cf_err}\n")
            return 1, lines
    after_fp = _config_fingerprint(merged)

    if not created_new:
        stamp = int(time.time())
        backup = CONF_FILE.with_name(f"config.bak.{stamp}")
        try:
            shutil.copy2(CONF_FILE, backup)
            os.chmod(backup, 0o600)
            st = CONF_FILE.stat()
            os.chown(backup, st.st_uid, st.st_gid)
        except OSError as exc:
            lines.append(f"ERROR: backup failed: {exc}\n")
            return 1, lines
        lines.append(f"Backup written: {backup}\n")

    body = format_config(merged)
    from .deploy_state import ensure_kin_mail_dir, write_topology_marker

    try:
        ensure_kin_mail_dir(CONF_DIR)
        tmp = CONF_FILE.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.chmod(tmp, 0o600)
        if CONF_FILE.is_file() and not created_new:
            st = CONF_FILE.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
        else:
            # Fresh file: root:root (privhelperd runs as root).
            os.chown(tmp, 0, 0)
        tmp.replace(CONF_FILE)
        os.chmod(CONF_FILE, 0o600)
    except OSError as exc:
        lines.append(f"ERROR: write failed: {exc}\n")
        return 1, lines

    try:
        if not write_topology_marker(str(merged.get("TOPOLOGY") or "")):
            lines.append("ERROR: could not write topology marker (invalid TOPOLOGY)\n")
            return 1, lines
        lines.append("Wrote topology marker (mode 644)\n")
    except OSError as exc:
        lines.append(f"ERROR: topology marker write failed: {exc}\n")
        return 1, lines

    if created_new:
        lines.append(f"Created new {CONF_FILE} (mode 600)\n")
        lines.append("Apply result: created (first config on this host)\n")
    elif before_fp != after_fp:
        lines.append(f"Updated {CONF_FILE} (mode 600)\n")
        lines.append("Apply result: changed (draft differed from previous config)\n")
    else:
        lines.append(f"Updated {CONF_FILE} (mode 600)\n")
        lines.append(
            "Apply result: unchanged (draft matched existing non-secret settings)\n"
        )

    try:
        from kin_console.ad_settings import sync_ad_env_from_kin_config

        ad_env = sync_ad_env_from_kin_config(source=CONF_FILE)
        lines.append(f"Synced console AD settings → {ad_env} (mode 640, no secrets in this log)\n")
    except Exception as exc:  # noqa: BLE001 — apply still succeeded; console may need bootstrap sync
        lines.append(f"WARN: console AD env sync failed: {exc}\n")
    lines.append(
        "NOTE: No services were restarted. Live Zimbra/Pacemaker still use prior "
        "runtime state until an install stage is run separately.\n"
    )
    for key, val in after_fp.items():
        lines.append(f"  {key}={val}\n")
    lines.append("=== apply_wizard_draft done ===\n")
    return 0, lines
