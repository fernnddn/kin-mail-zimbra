"""Whether the mail stack has been deployed on this host.

Used by the console API and privhelperd so pre-deployment wizard can run
without a logged-in user, while post-deploy keeps full login + RBAC.

Important: the Zimbra installer creates /opt/zimbra mid-run. Treat an active
full-install as still "setup" so operators are not bounced to /login and lose
the Deploy progress view.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("kin_privhelper.deploy_state")

# Override only for tests / unusual layouts - production uses /opt/zimbra.
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

# World-readable companion to TOPOLOGY in /etc/kin-mail/config (0600 root:root).
# The unprivileged console cannot read that config; this marker is 0644.
TOPOLOGY_MARKER = Path(
    os.environ.get("KIN_TOPOLOGY_MARKER", "/etc/kin-mail/topology")
)
SERVER_ID_FILE = Path(os.environ.get("KIN_SERVER_ID_FILE", "/etc/kin-mail/server-id"))
LICENSE_TOKEN_FILE = Path(
    os.environ.get(
        "KIN_LICENSE_TOKEN_FILE", "/var/lib/kin-mail-console/license.token"
    )
)

# Topology for the login gate: marker first, then applied config, then draft.
KIN_MAIL_CONFIG = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
# Traversable by kin-console. Sensitive files inside stay 0600; do not use umask.
KIN_MAIL_DIR_MODE = 0o755
WIZARD_DRAFT_FILE = Path(
    os.environ.get("KIN_CONSOLE_DRAFT", "/var/lib/kin-mail-console/wizard-draft.json")
)


def demote_wizard_draft_after_remove_host() -> list[str]:
    """After Remove Host, clear peer fields so stale 2vm draft cannot block UI.

    Keeps observability_vm_ip and cluster_vip_ip (still live on the survivor).
    Best-effort: missing or unreadable draft is not a demote failure.
    """
    notes: list[str] = []
    path = WIZARD_DRAFT_FILE
    if not path.is_file():
        return notes
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        notes.append("wizard draft unreadable; left unchanged")
        return notes
    if not isinstance(raw, dict):
        return notes
    changed = False
    if str(raw.get("topology") or "").strip() != "1vm":
        raw["topology"] = "1vm"
        changed = True
    for key in ("peer_host_ip", "peer_host_name"):
        if str(raw.get(key) or "").strip():
            raw[key] = ""
            changed = True
    if not changed:
        return notes
    raw["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        notes.append(
            "Demoted wizard draft to topology=1vm and cleared peer fields "
            "(VIP and Observability IP kept)"
        )
    except OSError as exc:
        notes.append(f"could not demote wizard draft: {exc}")
    return notes


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

# Written for the whole HA orchestration (disk prep, peer SSH install, Ansible).
# last-log / setup status cannot see the daemon lock, so the wizard used to
# show Idle while ansible-playbook was still writing the transcript.
HA_ORCH_RUNNING_MARKER = Path(
    os.environ.get(
        "KIN_HA_ORCH_RUNNING_MARKER",
        "/var/lib/kin-mail-console/ha-orchestration.running",
    )
)

UNEXPECTED_STOP_LINE = (
    "[FAIL] Install stopped unexpectedly -- process ended without a success "
    "or failure marker. Check journalctl -u kin-mail-privhelperd for crashes/OOM.\n"
)

# Redacted tmux pipe-pane capture from 03-install-zimbra.sh. Not the same as
# kin-mail.sh stdout - zmsetup.pl detail lands only here. Console SSE follows it.
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
    r"/05-healthcheck\.sh\b|"
    r"install\.sh --platform-override|zmsetup\.pl)",
    re.I,
)

_MAILBOXD_PROC_RE = re.compile(r"/opt/zimbra/.*mailboxd", re.I)


def ensure_kin_mail_dir(path: Path | None = None) -> None:
    """Create the config dir at 0755 so kin-console can traverse to 0644 markers.

    Individual files keep their own modes (config 0600, markers 0644). A 0750
    leftover from LUKS keyfile setup, or a umask without other-execute, makes
    Path.is_file() raise PermissionError even when the marker itself is 0644.
    """
    target = path if path is not None else KIN_MAIL_CONFIG.parent
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, KIN_MAIL_DIR_MODE)
    except OSError:
        log.warning("could not chmod %s to 0755 (console needs directory execute)", target)


def path_is_file(path: Path) -> bool:
    """Like Path.is_file(), but never raise. PermissionError is treated as absent."""
    try:
        return path.is_file()
    except PermissionError:
        log.warning(
            "cannot stat %s: permission denied. The directory must be 0755 so "
            "the unprivileged console can read 0644 markers; files inside stay 0600.",
            path,
        )
        return False
    except OSError:
        return False


def path_exists(path: Path) -> bool:
    """Like Path.exists(), but never raise."""
    try:
        return path.exists()
    except PermissionError:
        log.warning("cannot stat %s: permission denied", path)
        return False
    except OSError:
        return False


def full_install_in_progress() -> bool:
    """True when kin-mail full-install (or stage 03 / zmsetup) appears to be running."""
    return _ps_matches(_INSTALL_PROC_RE)


def ha_orchestration_in_progress() -> bool:
    """True while Build HA pair holds its running marker.

    Ansible and the peer SSH install are not matched by full_install_in_progress().
    """
    return path_is_file(HA_ORCH_RUNNING_MARKER)


def pipeline_in_progress() -> bool:
    """True when Deploy or Build HA pair is still running on this host."""
    return full_install_in_progress() or ha_orchestration_in_progress()


def mailboxd_running() -> bool:
    """True when mailboxd looks alive - not merely that /opt/zimbra exists."""
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


def topology_marker_body(topology: str) -> str:
    """Single-line body for /etc/kin-mail/topology. Empty if topology is unknown."""
    normalized = _normalize_topology(topology)
    if not normalized:
        return ""
    return f"{normalized}\n"


def _first_nonempty_line(text: str) -> str:
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line
    return (text or "").strip()


def _topology_from_marker() -> str:
    try:
        text = TOPOLOGY_MARKER.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _normalize_topology(_first_nonempty_line(text))


def _topology_from_config() -> str:
    try:
        text = KIN_MAIL_CONFIG.read_text(encoding="utf-8")
    except PermissionError:
        log.warning(
            "cannot read %s: permission denied. The unprivileged console cannot "
            "use this file for topology; %s is the intended source.",
            KIN_MAIL_CONFIG,
            TOPOLOGY_MARKER,
        )
        return ""
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

    /etc/kin-mail/topology is preferred (world-readable, no secrets). Applied
    /etc/kin-mail/config and the wizard draft remain fallbacks for hosts that
    have not picked up the marker yet.
    """
    for reader in (_topology_from_marker, _topology_from_config, _topology_from_draft):
        found = reader()
        if found:
            return found
    return ""


def write_topology_marker(topology: str) -> bool:
    """Write /etc/kin-mail/topology at 0644. Companion to TOPOLOGY in config.

    Idempotent: skips rewrite when the file already holds the same value.
    Returns False when topology is not 1vm/2vm (does not write).
    """
    body = topology_marker_body(topology)
    if not body:
        return False
    if path_is_file(TOPOLOGY_MARKER):
        try:
            existing = TOPOLOGY_MARKER.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if _normalize_topology(_first_nonempty_line(existing)) == _normalize_topology(body):
            return True
    ensure_kin_mail_dir(TOPOLOGY_MARKER.parent)
    tmp = TOPOLOGY_MARKER.with_name(TOPOLOGY_MARKER.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(TOPOLOGY_MARKER)
    return True


def read_server_id() -> str:
    if not path_is_file(SERVER_ID_FILE):
        return ""
    try:
        return _first_nonempty_line(SERVER_ID_FILE.read_text(encoding="utf-8"))
    except OSError:
        return ""


def ensure_server_id() -> str:
    """Stable per-install ID. Generated once; never rotated."""
    existing = read_server_id()
    if existing:
        return existing
    ensure_kin_mail_dir(SERVER_ID_FILE.parent)
    new_id = str(uuid.uuid4())
    tmp = SERVER_ID_FILE.with_name(SERVER_ID_FILE.name + ".tmp")
    tmp.write_text(new_id + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(SERVER_ID_FILE)
    return new_id


def read_license_token() -> str:
    if not LICENSE_TOKEN_FILE.is_file():
        return ""
    try:
        return LICENSE_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_license_token(token: str) -> None:
    import grp

    LICENSE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LICENSE_TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(token.strip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        os.chown(tmp, 0, grp.getgrnam("kin-console").gr_gid)
    except (KeyError, PermissionError, OSError):
        pass
    tmp.replace(LICENSE_TOKEN_FILE)
    try:
        os.chmod(LICENSE_TOKEN_FILE, 0o640)
        os.chown(LICENSE_TOKEN_FILE, 0, grp.getgrnam("kin-console").gr_gid)
    except (KeyError, PermissionError, OSError):
        pass



def backfill_topology_marker_from_config() -> bool:
    """Write the topology marker from config when the marker is missing or stale.

    privhelperd runs as root, so it can read 0600 /etc/kin-mail/config. The
    unprivileged console cannot. Returns True when a write happened.
    """
    topology = _topology_from_config()
    if not topology:
        return False
    if _topology_from_marker() == topology:
        return False
    return write_topology_marker(topology)


def _complete_stamp() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"complete {stamp}\n"


def marker_already_complete(text: str | None) -> bool:
    """True when a marker file already holds the durable complete stamp."""
    if text is None:
        return False
    return text.lstrip().startswith("complete ")


def mark_ha_setup_complete() -> bool:
    """Durable completion stamp. Caller must have reached genuine ORCH_DONE apply.

    Returns True when the marker exists afterwards. Skips rewrite when the file
    already starts with 'complete '.
    """
    if path_is_file(HA_SETUP_COMPLETE_MARKER):
        try:
            existing = HA_SETUP_COMPLETE_MARKER.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if marker_already_complete(existing):
            return True
    ensure_kin_mail_dir(HA_SETUP_COMPLETE_MARKER.parent)
    tmp = HA_SETUP_COMPLETE_MARKER.with_name(HA_SETUP_COMPLETE_MARKER.name + ".tmp")
    tmp.write_text(_complete_stamp(), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(HA_SETUP_COMPLETE_MARKER)
    return True


def record_ha_orchestration_success(*, join_mode: str) -> bool:
    """Write the HA marker only for a successful join_mode=apply run.

    Returns True if the marker was written. --check / any other mode is a no-op.
    """
    if str(join_mode or "").strip().lower() != "apply":
        return False
    mark_ha_setup_complete()
    write_topology_marker("2vm")
    return True


def plan_peer_ha_console_state(
    *,
    config_text: str,
    ha_marker_present: bool,
    ha_marker_text: str = "",
    setup_marker_present: bool,
    topology_marker_text: str = "",
) -> dict[str, Any]:
    """Decide which peer console files to write after a successful HA apply.

    Idempotent: already-2vm config and existing complete markers are left alone.
    setup-complete is required by is_mail_deployed() for topology 2vm; write it
    only when the file is missing (peer_os_prep normally already created it).
    The world-readable topology marker is written whenever it is missing or
    not already 2vm, even if config and completion markers are already right.

    Refuses a TOPOLOGY-only skeleton when the peer config is missing required
    identity keys (SERVER_IP / MAIL_HOST / MAIL_DOMAIN) - callers must stage a
    full peer config first (Build HA peer_os_prep or add-host attach).
    """
    from .apply_config import ensure_topology_2vm, format_config, parse_config

    values = parse_config(config_text or "")
    new_values, config_changed = ensure_topology_2vm(values)
    required = ("SERVER_IP", "MAIL_HOST", "MAIL_DOMAIN")
    incomplete = not all(str(new_values.get(k) or "").strip() for k in required)
    # Refuse whenever identity keys are missing - even if TOPOLOGY is already
    # 2vm - so we never stamp ha-setup-complete onto a skeleton peer config.
    refuse_incomplete = incomplete
    write_ha = not (ha_marker_present and marker_already_complete(ha_marker_text))
    write_setup = not setup_marker_present
    write_topo = _normalize_topology(_first_nonempty_line(topology_marker_text)) != "2vm"
    stamp = _complete_stamp()
    return {
        "write_config": bool(config_changed and not refuse_incomplete),
        "config_body": (
            format_config(new_values) if config_changed and not refuse_incomplete else ""
        ),
        "refuse_incomplete_config": refuse_incomplete,
        "write_ha_marker": write_ha,
        "write_setup_marker": write_setup,
        "write_topology_marker": write_topo,
        "ha_marker_body": stamp if write_ha else "",
        "setup_marker_body": stamp if write_setup else "",
        "topology_marker_body": topology_marker_body("2vm") if write_topo else "",
        "noop": (
            not config_changed
            and not write_ha
            and not write_setup
            and not write_topo
            and not refuse_incomplete
        ),
    }


def is_full_install_complete() -> bool:
    """True when kin-mail.sh wrote /etc/kin-mail/setup-complete.

    Independent of last-log text (HA orchestration truncates that file) and
    independent of is_mail_deployed() (2vm stays false until HA apply succeeds).
    """
    return path_is_file(SETUP_COMPLETE_MARKER)


def is_ha_setup_complete() -> bool:
    """True when join_mode=apply reached ORCH_DONE and wrote ha-setup-complete."""
    return path_is_file(HA_SETUP_COMPLETE_MARKER)


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
        # Do not use mailboxd as a proxy - it is already running after the
        # single-node install, which is exactly when HA has not started yet.
        return path_is_file(SETUP_COMPLETE_MARKER) and path_is_file(
            HA_SETUP_COMPLETE_MARKER
        )

    if path_is_file(SETUP_COMPLETE_MARKER):
        if topology == "1vm":
            return True
        # Topology unknown: fail open (pre-deploy). Locking the operator out
        # here would block Build HA pair if config/draft were unreadable on a
        # 2vm host. 1vm CLI labs without TOPOLOGY stay login-optional until
        # they sign in from the wizard; that is the smaller gap.
        return False

    if not path_exists(ZIMBRA_ROOT):
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


def mark_ha_orchestration_started() -> None:
    HA_ORCH_RUNNING_MARKER.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    HA_ORCH_RUNNING_MARKER.write_text(f"started {stamp}\n", encoding="utf-8")
    try:
        os.chmod(HA_ORCH_RUNNING_MARKER, 0o640)
    except OSError:
        pass


def mark_ha_orchestration_finished() -> None:
    try:
        HA_ORCH_RUNNING_MARKER.unlink()
    except FileNotFoundError:
        return


def append_unexpected_stop_if_needed(log_path: Path | None = None) -> bool:
    """Stamp the transcript if it has no success/fail marker. Returns True if written."""
    path = log_path or DEPLOY_LAST_LOG
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path_is_file(path) else ""
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
    if not path_is_file(FULL_INSTALL_RUNNING_MARKER):
        return False
    if full_install_in_progress():
        return False
    append_unexpected_stop_if_needed()
    mark_full_install_finished()
    return True


def reclaim_stale_ha_orchestration_marker() -> bool:
    """If HA left a running marker across a helper restart, stamp ORCH_FAILED.

    Only call on privhelperd startup. last-log must not reclaim: peer Zimbra
    install holds this marker for a long time with no ansible-playbook on A.
    """
    if not path_is_file(HA_ORCH_RUNNING_MARKER):
        return False
    try:
        text = (
            DEPLOY_LAST_LOG.read_text(encoding="utf-8", errors="replace")
            if path_is_file(DEPLOY_LAST_LOG)
            else ""
        )
    except OSError:
        text = ""
    if not re.search(r"\bORCH_(DONE|FAILED)\b", text):
        try:
            DEPLOY_LAST_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DEPLOY_LAST_LOG.open("a", encoding="utf-8") as fh:
                fh.write(
                    "ORCH_FAILED step=interrupted join_mode=unknown\n"
                    "[FAIL] HA orchestration stopped unexpectedly -- helper "
                    "restarted without ORCH_DONE or ORCH_FAILED. Check "
                    "journalctl -u kin-mail-privhelperd.\n"
                )
            try:
                os.chmod(DEPLOY_LAST_LOG, 0o640)
            except OSError:
                pass
        except OSError:
            pass
    mark_ha_orchestration_finished()
    return True


def setup_status_payload() -> dict[str, object]:
    """Body for GET /api/setup/status. Never raises; degrades to not-deployed."""
    try:
        installing = pipeline_in_progress()
        return {
            "deployed": is_mail_deployed(),
            "full_install_complete": is_full_install_complete(),
            "ha_setup_complete": is_ha_setup_complete(),
            "busy": installing,
            "install_in_progress": installing,
            "marker": str(ZIMBRA_ROOT),
            "zimbra_tree_present": path_exists(ZIMBRA_ROOT),
        }
    except Exception:
        log.exception("setup_status_payload failed; reporting not-deployed")
        return {
            "deployed": False,
            "full_install_complete": False,
            "ha_setup_complete": False,
            "busy": False,
            "install_in_progress": False,
            "marker": str(ZIMBRA_ROOT),
            "zimbra_tree_present": False,
        }
