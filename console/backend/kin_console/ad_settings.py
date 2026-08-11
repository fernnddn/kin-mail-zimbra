"""Load AD settings from appliance config (same source as 06-hybrid-auth.sh).

The unprivileged console cannot read /etc/kin-mail/config (mode 600, may contain
ADMIN_PASS). Bootstrap / apply_wizard_draft sync only the AD_* keys into
/etc/kin-mail-console/ad.env (mode 640, group kin-console).
"""

from __future__ import annotations

import grp
import os
import re
from dataclasses import dataclass
from pathlib import Path

AD_KEYS = (
    "AD_AUTH_ENABLED",
    "AD_LDAP_URL",
    "AD_SEARCH_BASE",
    "AD_SEARCH_FILTER",
    "AD_SEARCH_BIND_DN",
    "AD_SEARCH_BIND_PASSWORD",
    "AD_BIND_DN_TEMPLATE",
    "AD_TEST_USER",
    # AD_TEST_PASS intentionally omitted from the console mirror — login never
    # needs it; test credentials stay in the root-only appliance config.
)

_KIN_MAIL_CONFIG = Path("/etc/kin-mail/config")
_AD_ENV_DEFAULT = Path("/etc/kin-mail-console/ad.env")

_ASSIGN_RE = re.compile(
    r"""^([A-Za-z_][A-Za-z0-9_]*)=(?:'(.*)'|"(.*)"|(.*))\s*$"""
)


@dataclass(frozen=True)
class AdSettings:
    enabled: bool
    ldap_url: str
    search_base: str
    search_filter: str
    search_bind_dn: str
    search_bind_password: str
    bind_dn_template: str

    def public_summary(self) -> dict[str, object]:
        """Non-secret view for Super Admin UI."""
        return {
            "enabled": self.enabled,
            "ldap_url": self.ldap_url,
            "search_base": self.search_base,
            "search_filter": self.search_filter,
            "search_bind_dn": self.search_bind_dn,
            "bind_dn_template": self.bind_dn_template,
            "search_bind_password_set": bool(self.search_bind_password),
        }


def ad_env_path() -> Path:
    return Path(os.environ.get("KIN_CONSOLE_AD_ENV", str(_AD_ENV_DEFAULT)))


def kin_mail_config_path() -> Path:
    return Path(os.environ.get("KIN_MAIL_CONFIG", str(_KIN_MAIL_CONFIG)))


def _parse_assignments(text: str) -> dict[str, str]:
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
            val = m.group(2).replace("'\\''", "'")
        elif m.group(3) is not None:
            val = m.group(3)
        else:
            val = m.group(4) or ""
        out[key] = val
    return out


def sync_ad_env_from_kin_config(
    *,
    source: Path | None = None,
    dest: Path | None = None,
    group_name: str = "kin-console",
) -> Path:
    """Extract AD_* from appliance config into a console-readable mirror.

    Never logs secret values. Safe to call from root (bootstrap / privhelper).
    """
    source = source or kin_mail_config_path()
    dest = dest or ad_env_path()
    values: dict[str, str] = {}
    if source.is_file():
        values = _parse_assignments(source.read_text(encoding="utf-8"))

    lines = [
        "# Synced from /etc/kin-mail/config for KIN Mail console AD login.",
        "# Do not edit by hand — re-run console/bootstrap.sh or apply_wizard_draft.",
    ]
    for key in AD_KEYS:
        val = values.get(key, "")
        if key in ("AD_SEARCH_FILTER", "AD_SEARCH_BIND_PASSWORD"):
            esc = val.replace("'", "'\\''")
            lines.append(f"{key}='{esc}'")
        else:
            esc = (
                val.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("`", "\\`")
                .replace("$", "\\$")
            )
            lines.append(f'{key}="{esc}"')
    body = "\n".join(lines) + "\n"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        gid = grp.getgrnam(group_name).gr_gid
        os.chown(tmp, 0, gid)
    except (KeyError, PermissionError, OSError):
        # Best-effort ownership when not root / group missing.
        pass
    tmp.replace(dest)
    try:
        os.chmod(dest, 0o640)
    except OSError:
        pass
    return dest


def load_ad_settings() -> AdSettings:
    path = ad_env_path()
    raw: dict[str, str] = {}
    if path.is_file():
        raw = _parse_assignments(path.read_text(encoding="utf-8"))
    enabled = str(raw.get("AD_AUTH_ENABLED", "no")).strip().lower() == "yes"
    return AdSettings(
        enabled=enabled,
        ldap_url=str(raw.get("AD_LDAP_URL") or "").strip(),
        search_base=str(raw.get("AD_SEARCH_BASE") or "").strip(),
        search_filter=str(raw.get("AD_SEARCH_FILTER") or "(sAMAccountName=%u)").strip()
        or "(sAMAccountName=%u)",
        search_bind_dn=str(raw.get("AD_SEARCH_BIND_DN") or "").strip(),
        search_bind_password=str(raw.get("AD_SEARCH_BIND_PASSWORD") or ""),
        bind_dn_template=str(raw.get("AD_BIND_DN_TEMPLATE") or "").strip(),
    )
