"""Whether the mail stack has been deployed on this host.

Used by the console API and privhelperd so pre-deployment wizard can run
without a logged-in user, while post-deploy keeps full login + RBAC.

Important: the Zimbra installer creates /opt/zimbra mid-run. Treat an active
full-install as still "setup" so operators are not bounced to /login and lose
the Deploy progress view.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Override only for tests / unusual layouts — production uses /opt/zimbra.
ZIMBRA_ROOT = Path(os.environ.get("KIN_ZIMBRA_ROOT", "/opt/zimbra"))

# Written only after kin-mail.sh full-install succeeds (all selected stages exited 0).
SETUP_COMPLETE_MARKER = Path(
    os.environ.get("KIN_SETUP_COMPLETE_MARKER", "/etc/kin-mail/setup-complete")
)

# Written only after run_ha_orchestration reaches ORCH_DONE with join_mode=apply.
# Not written on --check, ORCH_FAILED, or any early refuse.
HA_SETUP_COMPLETE_MARKER = Path(
    os.environ.get("KIN_HA_SETUP_COMPLETE_MARKER", "/etc/kin-mail/ha-setup-complete")
)

# Topology for the login gate: applied config first, then the wizard draft.
KIN_MAIL_CONFIG = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
WIZARD_DRAFT_FILE = Path(
    os.environ.get("KIN_CONSOLE_DRAFT", "/var/lib/kin-mail-console/wizard-draft.json")
)

# Last deploy transcript (also used by console last-log API).
DEPLOY_LAST_LOG = Path(
    os.environ.get("KIN_DEPLOY_LAST_LOG", "/var/log/kin-mail/deploy-last.log")
)

# Written when console full-install starts; removed only after a terminal
# outcome (success, Pipeline stopped, or explicit unexpected-stop stamp).
# Survives a privhelperd crash so the next start can tell the operator.
FULL_INSTALL_RUNNING_MARKER = Path(
    os.environ.get(
        "KIN_FULL_INSTALL_RUNNING_MARKER",
        "/var/lib/kin-mail-console/full-install.running",
    )
)

UNEXPECTED_STOP_LINE = (
    "[FAIL] Install stopped unexpectedly -- process ended without a success "
    "or failure marker. Check journalctl -u kin-mail-privhelperd for crashes/OOM.\n"
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


def _normalize_topology(raw: str) -> str:
    t = (raw or "").strip().lower()
    if t in ("1vm", "1"):
        return "1vm"
    if t in ("2vm", "2"):
        return "2vm"
    return ""


def _topology_from_config() -> str:
    try:
        text = KIN_MAIL_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("TOPOLOGY="):
            val = line.split("=", 1)[1].strip().strip("'").strip('"')
            return _normalize_topology(val)
    return ""


def _topology_from_draft() -> str:
    try:
        data = json.loads(WIZARD_DRAFT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return _normalize_topology(str(data.get("topology") or ""))


def saved_wizard_topology() -> str:
    """Return '1vm', '2vm', or '' if topology cannot be determined.

    Applied /etc/kin-mail/config wins (what Deploy actually used). The wizard
    draft is the fallback when config is missing or has no TOPOLOGY.
    """
    for reader in (_topology_from_config, _topology_from_draft):
        found = reader()
        if found:
            return found
    return ""


def mark_ha_setup_complete() -> None:
    """Durable completion stamp — caller must have reached genuine ORCH_DONE apply."""
    HA_SETUP_COMPLETE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = HA_SETUP_COMPLETE_MARKER.with_name(HA_SETUP_COMPLETE_MARKER.name + ".tmp")
    tmp.write_text(f"complete {stamp}\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(HA_SETUP_COMPLETE_MARKER)


def record_ha_orchestration_success(*, join_mode: str) -> bool:
    """Write the HA marker only for a successful join_mode=apply run.

    Returns True if the marker was written. --check / any other mode is a no-op.
    """
    if str(join_mode or "").strip().lower() != "apply":
        return False
    mark_ha_setup_complete()
    return True


def is_full_install_complete() -> bool:
    """True when kin-mail.sh wrote /etc/kin-mail/setup-complete.

    Independent of last-log text (HA orchestration truncates that file) and
    independent of is_mail_deployed() (2vm stays false until HA apply succeeds).
    """
    return SETUP_COMPLETE_MARKER.is_file()


def is_ha_setup_complete() -> bool:
    """True when join_mode=apply reached ORCH_DONE and wrote ha-setup-complete."""
    return HA_SETUP_COMPLETE_MARKER.is_file()


def is_mail_deployed() -> bool:
    """True when the console should require login (setup finished).

    A failed/partial install creates /opt/zimbra long before zmsetup finishes.
    Directory existence is not a completion signal.

    2-server topology is not finished until HA orchestration also completes
    (ha-setup-complete). setup-complete alone is the end of single-node
    kin-mail.sh --full-install, which is when Build HA pair still needs the
    anonymous wizard identity.
    """
    if full_install_in_progress():
        return False

    topology = saved_wizard_topology()
    if topology == "2vm":
        # Do not use mailboxd as a proxy — it is already running after the
        # single-node install, which is exactly when HA has not started yet.
        return SETUP_COMPLETE_MARKER.is_file() and HA_SETUP_COMPLETE_MARKER.is_file()

    if SETUP_COMPLETE_MARKER.is_file():
        if topology == "1vm":
            return True
        # Topology unknown: fail open (pre-deploy). Locking the operator out
        # here would block Build HA pair if config/draft were unreadable on a
        # 2vm host. 1vm CLI labs without TOPOLOGY stay login-optional until
        # they sign in from the wizard; that is the smaller gap.
        return False

    if not ZIMBRA_ROOT.exists():
        return False
    # Legacy hosts (CLI install before the marker existed): only if mailboxd
    # is actually running. A menus-timeout leftover tree must not flip the gate.
    return mailboxd_running()


def transcript_has_terminal_outcome(text: str) -> bool:
    """True if the deploy transcript already records success or a clear failure."""
    if not text:
        return False
    if re.search(r"Full install complete", text, re.I):
        return True
    if re.search(r"All selected pipeline stages exited 0", text, re.I):
        return True
    if re.search(r"Pipeline stopped at ", text, re.I):
        return True
    if re.search(r"Install stopped unexpectedly", text, re.I):
        return True
    if re.search(r"\[FAIL\]", text):
        return True
    return False


def mark_full_install_started() -> None:
    FULL_INSTALL_RUNNING_MARKER.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    FULL_INSTALL_RUNNING_MARKER.write_text(f"started {stamp}\n", encoding="utf-8")
    try:
        os.chmod(FULL_INSTALL_RUNNING_MARKER, 0o640)
    except OSError:
        pass


def mark_full_install_finished() -> None:
    try:
        FULL_INSTALL_RUNNING_MARKER.unlink()
    except FileNotFoundError:
        return


def append_unexpected_stop_if_needed(log_path: Path | None = None) -> bool:
    """Stamp the transcript if it has no success/fail marker. Returns True if written."""
    path = log_path or DEPLOY_LAST_LOG
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        text = ""
    if transcript_has_terminal_outcome(text):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(UNEXPECTED_STOP_LINE)
        try:
            os.chmod(path, 0o640)
        except OSError:
            pass
    except OSError:
        return False
    return True


def reclaim_stale_full_install_marker() -> bool:
    """If a previous full-install left a marker and nothing is running, stamp FAIL.

    Call on privhelperd startup and when serving the last-log API so an operator
    opening View logs after a crash never sees Idle with no explanation.
    """
    if not FULL_INSTALL_RUNNING_MARKER.is_file():
        return False
    if full_install_in_progress():
        return False
    append_unexpected_stop_if_needed()
    mark_full_install_finished()
    return True
