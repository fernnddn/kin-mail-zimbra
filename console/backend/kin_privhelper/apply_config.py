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
    for line in out.splitlines():
        parts = line.split()
        # e.g. "2: ens33    inet 10.10.40.15/24 brd ..."
        if len(parts) >= 4 and parts[2] == "inet":
            iface = parts[1]
            ip = parts[3].split("/", 1)[0]
            if iface and ip:
                return ip, iface
    return "", ""


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
        "ZPUSH_ENABLED": "yes",
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

    host_root_pass = str(draft.get("host_root_pass") or "")
    if host_root_pass:
        if len(host_root_pass) < 8:
            errs.append("host_root_pass must be at least 8 characters")
    elif not existing.get("HOST_ROOT_PASS"):
        errs.append("host_root_pass is required (draft empty and no existing HOST_ROOT_PASS)")

    kin_user_pass = str(draft.get("kin_user_pass") or "")
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
    elif not existing.get("ADMIN_PASS"):
        errs.append("admin_pass is required (draft empty and no existing ADMIN_PASS)")

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
    else:
        out["PEER_HOST_IP"] = ""
        out["PEER_HOST_NAME"] = ""

    # Fixed Arcfra-style OS admin identity; passwords preserve-on-empty like ADMIN_PASS.
    out["KIN_OS_USER"] = "kin"
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
    out["ZPUSH_ENABLED"] = "yes" if draft.get("zpush_enabled", True) else "no"

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
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
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
