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

# Written only after kin-mail.sh full-install succeeds (all selected stages exited 0).
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
    r"(?:kin-mail\.sh\s+--full-install|/03-install-zimbra\.sh\b|"
    r"install\.sh --platform-override|zmsetup\.pl)",
    re.I,
)

_MAILBOXD_PROC_RE = re.compile(r"/opt/zimbra/.*mailboxd", re.I)


def full_install_in_progress() -> bool:
    """True when kin-mail full-install (or stage 03 / zmsetup) appears to be running."""
    return _ps_matches(_INSTALL_PROC_RE)


def mailboxd_running() -> bool:
    """True when mailboxd looks alive — not merely that /opt/zimbra exists."""
    pid_candidates = [
        Path(os.environ.get("KIN_MAILBOXD_PID", str(ZIMBRA_ROOT / "log" / "zmmailboxd_pid"))),
        ZIMBRA_ROOT / "log" / "zmmailboxd.pid",
        ZIMBRA_ROOT / "mailboxd" / "zmmailboxd.pid",
    ]
    extra = os.environ.get("KIN_MAILBOXD_PID_EXTRA", "").strip()
    if extra:
        pid_candidates.append(Path(extra))
    for pidf in pid_candidates:
        if _pidfile_alive(pidf):
            return True
    return _ps_matches(_MAILBOXD_PROC_RE)


def _pidfile_alive(pidf: Path) -> bool:
    try:
        raw = pidf.read_text(encoding="utf-8", errors="replace").strip().split()[0]
        pid = int(raw)
    except (OSError, ValueError, IndexError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ps_matches(pattern: re.Pattern[str]) -> bool:
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
        if pattern.search(line):
            return True
    return False


def is_mail_deployed() -> bool:
    """True when the console should require login (setup finished).

    A failed/partial install creates /opt/zimbra long before zmsetup finishes.
    Directory existence is not a completion signal.
    """
    if full_install_in_progress():
        return False
    if SETUP_COMPLETE_MARKER.is_file():
        return True
    if not ZIMBRA_ROOT.exists():
        return False
    # Legacy hosts (CLI install before the marker existed): only if mailboxd
    # is actually running. A menus-timeout leftover tree must not flip the gate.
    return mailboxd_running()
