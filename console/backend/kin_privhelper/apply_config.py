"""Apply wizard draft → /etc/kin-mail/config (merge + timestamped backup).

File write only — does not run install stages or restart services.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
        "AD_SEARCH_FILTER",
        "AD_SEARCH_BIND_PASSWORD",
        "AD_TEST_PASS",
    }
)

# Preferred key order matching 00-config.sh wizard output (+ TOPOLOGY).
_KEY_ORDER = [
    "TOPOLOGY",
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


def zimbra_tz(timezone: str) -> str:
    if timezone in ("Asia/Jakarta", "Asia/Pontianak"):
        return "Asia/Bangkok"
    return timezone


def validate_draft(draft: dict[str, Any], existing: dict[str, str]) -> list[str]:
    errs: list[str] = []
    topology = str(draft.get("topology") or "").strip()
    if topology not in ("1vm", "2vm"):
        errs.append("topology must be 1vm or 2vm")

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
            if not str(draft.get(key) or "").strip():
                errs.append(f"{label} required when hybrid AD is enabled")

    # Preserve-critical live fields must already exist when merging.
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
    le = str(draft.get("le_email") or "").strip()
    tls = str(draft.get("tls_method") or "").strip()
    seats = str(draft.get("contracted_seats") or "").strip() or "PLACEHOLDER_UNSET"
    if seats != "PLACEHOLDER_UNSET" and not seats.isdigit():
        seats = "PLACEHOLDER_UNSET"

    out["TOPOLOGY"] = str(draft.get("topology") or "").strip()
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
    out["AD_SEARCH_BIND_PASSWORD"] = str(draft.get("ad_search_bind_password") or "")
    out["AD_BIND_DN_TEMPLATE"] = str(draft.get("ad_bind_dn_template") or "").strip()
    out["AD_TEST_USER"] = str(draft.get("ad_test_user") or "").strip()
    out["AD_TEST_PASS"] = str(draft.get("ad_test_pass") or "")

    out.setdefault("CF_CREDS", "/etc/letsencrypt/cloudflare.ini")
    out.setdefault("CF_PROPAGATION", "40")
    out.setdefault("ZCS_SRC", "/opt/zcs-src")
    out.setdefault("EXTERNAL_TEST_ADDRESS", "")
    return out


def apply_wizard_draft() -> tuple[int, list[str]]:
    """Returns (exit_code, log_lines)."""
    lines: list[str] = []
    lines.append("=== apply_wizard_draft ===\n")

    if not DRAFT_FILE.is_file():
        lines.append(f"ERROR: draft missing at {DRAFT_FILE}\n")
        return 1, lines
    if not CONF_FILE.is_file():
        lines.append(
            f"ERROR: {CONF_FILE} missing — merge requires an existing config "
            "(run CLI 00-config once, or restore from backup)\n"
        )
        return 1, lines

    try:
        draft = json.loads(DRAFT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        lines.append(f"ERROR: cannot read draft: {exc}\n")
        return 1, lines
    if not isinstance(draft, dict):
        lines.append("ERROR: draft is not a JSON object\n")
        return 1, lines

    try:
        existing_text = CONF_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        lines.append(f"ERROR: cannot read config: {exc}\n")
        return 1, lines
    existing = parse_config(existing_text)
    lines.append(f"Loaded existing config ({len(existing)} keys)\n")
    lines.append(f"Loaded draft from {DRAFT_FILE}\n")

    errs = validate_draft(draft, existing)
    if errs:
        lines.append("ERROR: draft validation failed:\n")
        for e in errs:
            lines.append(f"  - {e}\n")
        return 1, lines
    lines.append("Draft validation OK\n")

    merged = merge_draft(draft, existing)
    stamp = int(time.time())
    backup = CONF_FILE.with_name(f"config.bak.{stamp}")
    try:
        shutil.copy2(CONF_FILE, backup)
        os.chmod(backup, 0o600)
        # Preserve owner of original config on backup.
        st = CONF_FILE.stat()
        os.chown(backup, st.st_uid, st.st_gid)
    except OSError as exc:
        lines.append(f"ERROR: backup failed: {exc}\n")
        return 1, lines
    lines.append(f"Backup written: {backup}\n")

    body = format_config(merged)
    tmp = CONF_FILE.with_suffix(".tmp")
    try:
        st = CONF_FILE.stat()
        tmp.write_text(body, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.chown(tmp, st.st_uid, st.st_gid)
        tmp.replace(CONF_FILE)
    except OSError as exc:
        lines.append(f"ERROR: write failed: {exc}\n")
        return 1, lines

    lines.append(f"Updated {CONF_FILE} (mode 600)\n")
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
    # Summarize non-secret keys for the log viewer.
    for key in (
        "TOPOLOGY",
        "MAIL_DOMAIN",
        "MAIL_HOST",
        "TIMEZONE",
        "TLS_METHOD",
        "ZPUSH_ENABLED",
        "CONTRACTED_SEATS",
        "KIN_ADMIN_IPS",
        "AD_AUTH_ENABLED",
    ):
        lines.append(f"  {key}={merged.get(key, '')}\n")
    lines.append("=== apply_wizard_draft done ===\n")
    return 0, lines
