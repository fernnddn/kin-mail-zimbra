"""Whether the mail stack has been deployed on this host.

Used by the console API and privhelperd so pre-deployment wizard can run
without a logged-in user, while post-deploy keeps full login + RBAC.

Important: the Zimbra installer creates /opt/zimbra mid-run. Treat an active
full-install as still "setup" so operators are not bounced to /login and lose
the Deploy progress view.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Override only for tests / unusual layouts — production uses /opt/zimbra.
ZIMBRA_ROOT = Path(os.environ.get("KIN_ZIMBRA_ROOT", "/opt/zimbra"))

# Written by a successful console/CLI full install (future); optional legacy signal.
SETUP_COMPLETE_MARKER = Path(
    os.environ.get("KIN_SETUP_COMPLETE_MARKER", "/etc/kin-mail/setup-complete")
)

# Last deploy transcript (also used by console last-log API).
DEPLOY_LAST_LOG = Path(
    os.environ.get("KIN_DEPLOY_LAST_LOG", "/var/log/kin-mail/deploy-last.log")
)

# Redacted tmux pipe-pane capture from 03-install-zimbra.sh. Not the same as
# kin-mail.sh stdout — zmsetup.pl detail lands only here. Console SSE follows it.
ZIMBRA_INSTALL_LOG = Path(
    os.environ.get("KIN_ZIMBRA_INSTALL_LOG", "/var/log/kin-mail-install.log")
)

# Audit / privhelper identity when wizard runs without a session (pre-deploy only).
SETUP_USERNAME = "setup"

# Commands the anonymous setup identity may run before setup is complete.
SETUP_ALLOWED_COMMANDS = frozenset(
    {
        "get_status",
        "run_script:09-hardening.sh --status",
        "apply_wizard_draft",
        "run_hardening",
        "run_full_install",
        "cancel_firewall_deadman",
        "get_deploy_log",
        "store_provisioning_secrets",
        "run_ha_orchestration",
        "ha_disk_preflight",
    }
)

_INSTALL_PROC_RE = re.compile(
    r"(?:kin-mail\.sh\s+--full-install|/03-install-zimbra\.sh\b)",
    re.I,
)


def full_install_in_progress() -> bool:
    """True when kin-mail full-install (or stage 03) appears to be running."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "args="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in (proc.stdout or "").splitlines():
        if _INSTALL_PROC_RE.search(line):
            return True
    return False


def is_mail_deployed() -> bool:
    """True when the console should require login (setup finished).

    /opt/zimbra alone is not enough — the installer creates that tree while
    full-install is still running. While that job is active, stay in setup mode.
    """
    if SETUP_COMPLETE_MARKER.is_file():
        return True
    if not ZIMBRA_ROOT.exists():
        return False
    if full_install_in_progress():
        return False
    return True
