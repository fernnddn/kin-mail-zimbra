"""Wizard draft store - console data dir only; never touches /etc/kin-mail/config."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .settings import settings

IPV4_RE = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$"
)


def valid_ipv4(value: str) -> bool:
    return bool(IPV4_RE.match((value or "").strip()))

DRAFT_VERSION = 1

# Host provisioning passwords must never sit in the console-readable draft.
HOST_PROVISION_KEYS = frozenset({"host_root_pass", "kin_user_pass"})

# Secrets kept in the draft for apply_config, but GET always returns "".
# Empty PUT values must not wipe a stored value (GET→PUT round-trip).
DRAFT_ECHO_SECRETS = (
    "admin_pass",
    "ad_search_bind_password",
    "ad_test_pass",
)

# Secrets are kept in the draft for review/redeploy later, but never echo'd in logs.
SECRET_KEYS = frozenset(
    {
        "admin_pass",
        "host_root_pass",
        "kin_user_pass",
        "ad_search_bind_password",
        "ad_test_pass",
    }
)

# OS admin username written to config (not shown in the wizard UI).
KIN_OS_USER = "kin"


class WizardDraft(BaseModel):
    """Draft wizard answers. Maps conceptually to 00-config.sh fields; not applied."""

    version: int = DRAFT_VERSION
    updated_at: str | None = None
    current_step: str = "topology"

    # a. Topology
    topology: str = ""  # "1vm" | "2vm"
    peer_host_ip: str = ""
    peer_host_name: str = ""
    observability_vm_ip: str = ""
    cluster_vip_ip: str = ""

    # Host credentials live in the privhelper vault, not in this JSON.
    host_root_pass: str = ""
    kin_user_pass: str = ""
    host_credentials_set: bool = False

    # b. Domain & mail
    mail_domain: str = ""
    mail_host: str = ""
    timezone: str = "Asia/Jakarta"
    admin_pass: str = ""
    le_email: str = ""

    # c. TLS
    tls_method: str = ""  # cloudflare | manual | customer

    # d. Hybrid AD (optional)
    ad_auth_enabled: bool = False
    ad_ldap_url: str = ""
    ad_search_base: str = ""
    ad_search_filter: str = "(sAMAccountName=%u)"
    ad_search_bind_dn: str = ""
    ad_search_bind_password: str = ""
    ad_bind_dn_template: str = ""
    ad_test_user: str = ""
    ad_test_pass: str = ""

    # e. Z-Push
    zpush_enabled: bool = False

    # f. Licensing
    contracted_seats: str = "PLACEHOLDER_UNSET"

    # g. Firewall
    kin_admin_ips: str = ""


def detect_local_ipv4() -> str:
    """Best-effort local NIC IPv4 for VIP collision checks (not a secret)."""
    conf = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
    if conf.is_file():
        try:
            for line in conf.read_text(encoding="utf-8").splitlines():
                if line.startswith("SERVER_IP="):
                    raw = line.split("=", 1)[1].strip().strip("'").strip('"')
                    if valid_ipv4(raw):
                        return raw
        except OSError:
            pass
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.0.2.1", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if valid_ipv4(ip):
            return ip
    except OSError:
        pass
    return ""


def draft_path() -> Path:
    return settings.data_dir / "wizard-draft.json"


def default_draft() -> WizardDraft:
    return WizardDraft()


def load_draft() -> WizardDraft:
    path = draft_path()
    if not path.is_file():
        return default_draft()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return default_draft()
    return WizardDraft.model_validate(raw)


def save_draft(draft: WizardDraft) -> WizardDraft:
    path = draft_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    draft.version = DRAFT_VERSION
    draft.updated_at = datetime.now(timezone.utc).isoformat()
    payload = draft.model_dump()
    for key in HOST_PROVISION_KEYS:
        payload[key] = ""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return draft


def public_draft(draft: WizardDraft) -> dict[str, Any]:
    """Return draft for API. Secrets are never included; flags say whether they exist."""
    data = draft.model_dump()
    for key in DRAFT_ECHO_SECRETS:
        data[f"{key}_set"] = bool((data.get(key) or "").strip())
        data[key] = ""
    for key in HOST_PROVISION_KEYS:
        data[key] = ""
    data["local_host_ip"] = detect_local_ipv4()
    try:
        from kin_privhelper.provisioning_secrets import marker_present

        data["host_credentials_set"] = marker_present()
    except Exception:  # noqa: BLE001
        data["host_credentials_set"] = False
    return data


class DraftPatch(BaseModel):
    """Partial update from the UI. Unknown keys ignored by pydantic extra=ignore."""

    model_config = {"extra": "ignore"}

    current_step: str | None = None
    topology: str | None = None
    peer_host_ip: str | None = None
    peer_host_name: str | None = None
    observability_vm_ip: str | None = None
    cluster_vip_ip: str | None = None
    host_root_pass: str | None = None
    kin_user_pass: str | None = None
    mail_domain: str | None = None
    mail_host: str | None = None
    timezone: str | None = None
    admin_pass: str | None = None
    le_email: str | None = None
    tls_method: str | None = None
    ad_auth_enabled: bool | None = None
    ad_ldap_url: str | None = None
    ad_search_base: str | None = None
    ad_search_filter: str | None = None
    ad_search_bind_dn: str | None = None
    ad_search_bind_password: str | None = None
    ad_bind_dn_template: str | None = None
    ad_test_user: str | None = None
    ad_test_pass: str | None = None
    zpush_enabled: bool | None = None
    contracted_seats: str | None = None
    kin_admin_ips: str | None = None


def apply_patch(draft: WizardDraft, patch: DraftPatch) -> WizardDraft:
    data = draft.model_dump()
    updates = patch.model_dump(exclude_unset=True)
    # Never persist host provisioning passwords in the console-readable draft.
    updates.pop("host_root_pass", None)
    updates.pop("kin_user_pass", None)
    # Empty secrets from GET-stripped clients must not wipe a stored value.
    incoming_secrets: dict[str, str] = {}
    for key in DRAFT_ECHO_SECRETS:
        incoming = updates.pop(key, None)
        if isinstance(incoming, str) and incoming.strip():
            incoming_secrets[key] = incoming
    data.update(updates)
    data.update(incoming_secrets)
    data["host_root_pass"] = ""
    data["kin_user_pass"] = ""
    return WizardDraft.model_validate(data)


class EulaAcceptBody(BaseModel):
    accepted: bool = Field(description="Must be true to accept")
