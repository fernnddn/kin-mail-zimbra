"""Wizard draft store — console data dir only; never touches /etc/kin-mail/config."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .settings import settings

DRAFT_VERSION = 1

# Secrets are kept in the draft for review/redeploy later, but never echo'd in logs.
SECRET_KEYS = frozenset(
    {
        "admin_pass",
        "ad_search_bind_password",
        "ad_test_pass",
    }
)


class WizardDraft(BaseModel):
    """Draft wizard answers. Maps conceptually to 00-config.sh fields; not applied."""

    version: int = DRAFT_VERSION
    updated_at: str | None = None
    current_step: str = "topology"

    # a. Topology
    topology: str = ""  # "1vm" | "2vm"

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
    zpush_enabled: bool = True

    # f. Licensing
    contracted_seats: str = "PLACEHOLDER_UNSET"

    # g. Firewall
    kin_admin_ips: str = ""


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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return draft


def public_draft(draft: WizardDraft) -> dict[str, Any]:
    """Return draft for API; secrets included for authenticated operator review only."""
    return draft.model_dump()


class DraftPatch(BaseModel):
    """Partial update from the UI. Unknown keys ignored by pydantic extra=ignore."""

    model_config = {"extra": "ignore"}

    current_step: str | None = None
    topology: str | None = None
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
    data.update(updates)
    return WizardDraft.model_validate(data)


class EulaAcceptBody(BaseModel):
    accepted: bool = Field(description="Must be true to accept")
