"""Whether the mail stack has been deployed on this host.

Used by the console API and privhelperd so pre-deployment wizard can run
without a logged-in user, while post-deploy keeps full login + RBAC.
"""

from __future__ import annotations

import os
from pathlib import Path

# Override only for tests / unusual layouts — production uses /opt/zimbra.
ZIMBRA_ROOT = Path(os.environ.get("KIN_ZIMBRA_ROOT", "/opt/zimbra"))

# Audit / privhelper identity when wizard runs without a session (pre-deploy only).
SETUP_USERNAME = "setup"

# Commands the anonymous setup identity may run before /opt/zimbra exists.
SETUP_ALLOWED_COMMANDS = frozenset(
    {
        "get_status",
        "run_script:09-hardening.sh --status",
        "apply_wizard_draft",
        "run_hardening",
        "run_full_install",
        "cancel_firewall_deadman",
        "get_deploy_log",
    }
)


def is_mail_deployed() -> bool:
    """True once Zimbra tree exists on this host (successful / in-progress install)."""
    return ZIMBRA_ROOT.exists()
