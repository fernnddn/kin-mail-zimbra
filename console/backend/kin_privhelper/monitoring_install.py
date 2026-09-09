"""Install the built-in metrics stack on THIS node.

Prometheus, node_exporter and the mail-flow collector are what fill the
Monitoring tab and every figure on the Reports tab. Until now the only thing
that installed them was `mail_monitoring`, a step inside the Build HA pair
pipeline, so a single-server appliance never got them at all: both tabs were
permanently empty and the Reports empty state told the operator to "re-run the
monitoring step of the deploy", which on 1vm does not exist (live QA,
7 Sep 2026).

This runs the same playbook against localhost only. It needs no SSH, no
credentials and no peer: privhelperd is already root on the node being
configured. It is additive and safe to repeat, which is why it is also allowed
to run on a healthy HA pair to repair a soft-failed `mail_monitoring` step.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto

PLAYBOOK = "playbooks/mail-monitoring.yml"
# Never the shared orchestration directory. See the comment in the command.
MONITORING_WORK_DIR = Path(
    os.environ.get(
        "KIN_MONITORING_WORK_DIR",
        "/var/lib/kin-mail-privhelper/monitoring",
    )
)
# Same shape as an inventory hostname elsewhere in the tree: a DNS label or
# FQDN, nothing that could carry shell or YAML meaning.
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252})$")
# Where the outcome of the detached run is recorded.
#
# The install is deliberately detached (see run_after_install), which means
# nobody is awaiting it and its exit code has nowhere to go. Before this file
# existed the result went into the transcript and then nowhere: the deploy had
# already reported success, so a failed or timed-out metrics install left an
# appliance that looked finished and healthy with permanently empty charts, and
# the Reports tab counted a month of zeroes as though that were the answer.
#
# Under /var/lib/kin-mail-console, NOT /var/lib/kin-mail-privhelper.
#
# privhelperd writes this and the unprivileged console reads it, so it has to
# live where the console can reach it. The privhelper directory is that
# daemon's HOME and its secrets vault: provisioning-secrets.json and
# observability-secrets.json sit there at 0600, it is created by whichever
# code path touches it first with a bare mkdir, and nothing guarantees it is
# traversable by kin-console. A directory the console cannot enter makes this
# file unreadable no matter what mode the file itself has, which this repo has
# already been bitten by once (see ensure_kin_mail_dir in deploy_state: a 0750
# leftover made 0644 markers raise PermissionError). Reading it then fails,
# the failure looks like "no record", and a no-record state is deliberately
# silent, so a failed metrics install would warn nowhere at all. The
# kin-mail-console directory is the one that already exists for markers the
# console reads.
METRICS_STATE_FILE = Path(
    os.environ.get(
        "KIN_METRICS_STATE_FILE",
        "/var/lib/kin-mail-console/metrics-state.json",
    )
)

# running: started and not yet finished. ok: the playbook exited 0.
# failed:  the playbook exited non-zero. timeout: it hit INSTALL_TIMEOUT_SEC.
# error:   it could not be started at all.
METRICS_PHASES = ("running", "ok", "failed", "timeout", "error")


def record_metrics_state(
    phase: str,
    *,
    exit_code: int | None = None,
    reason: str = "",
    attempts: int | None = None,
) -> bool:
    """Persist how the detached metrics install is going. Never raises.

    Best effort on purpose: this is called from a background task and from a
    finally-ish path, and a full disk must not turn a metrics problem into a
    deploy crash. A missing state file reads back as "unknown", which the
    console already treats as "not proven installed".
    """
    if phase not in METRICS_PHASES:
        return False
    if attempts is None:
        # Carry the previous count forward. Only an explicit value changes it,
        # so recording an outcome never silently resets the budget that stops
        # a hopeless install being retried on every daemon start.
        attempts = int(read_metrics_state().get("auto_attempts") or 0)
    body = {
        "phase": phase,
        "exit_code": exit_code,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "auto_attempts": max(0, int(attempts)),
    }
    try:
        # Create if absent, but never widen it. bootstrap.sh installs this
        # directory as kin-console:kin-console 0750 on purpose: admin.hash,
        # session.secret and tls/key.pem live here. The console owns it, so it
        # can already traverse and read; forcing 0755 to "make sure" would
        # publish the console's private material to every local user, which is
        # a far worse bug than the one being fixed.
        METRICS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = METRICS_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        # Replace, so a reader never sees a half-written file.
        tmp.replace(METRICS_STATE_FILE)
        # 0644: this carries a phase, an exit code and an English sentence.
        # No secret has any business in it, and the console must be able to
        # read it without being in any particular group.
        os.chmod(METRICS_STATE_FILE, 0o644)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _state_age_seconds(at: str, *, now: datetime | None = None) -> float | None:
    """Seconds since a recorded timestamp, or None if it cannot be read."""
    try:
        when = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - when).total_seconds()


def read_metrics_state(*, now: datetime | None = None) -> dict[str, Any]:
    """What the last detached metrics install did. Never raises.

    Three read failures that are NOT the same thing, and used to be:

    unknown     no file. A node that never ran one, or an appliance deployed
                before this record existed. Callers treat it as unproven but
                do not warn on it, because warning on every pre-existing box
                is how a warning stops being read.
    unreadable  the file is there and cannot be read, almost always because
                the directory is not traversable by the console user. This
                MUST NOT collapse into "unknown": that is the silent path
                where a failed metrics install warns nowhere at all.
    timeout     a "running" record too old to still be running. The task that
                would have finished it died with privhelperd, so believing
                "running" for ever leaves the console waiting on something
                that is never coming back.
    """
    unknown: dict[str, Any] = {
        "phase": "unknown",
        "exit_code": None,
        "reason": "",
        "at": "",
        "auto_attempts": 0,
        "installed": False,
    }
    try:
        text = METRICS_STATE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return unknown
    except PermissionError:
        return {
            "phase": "unreadable",
            "exit_code": None,
            "at": "",
            "auto_attempts": 0,
            "installed": False,
            "reason": (
                "The console cannot read the monitoring install record at "
                f"{METRICS_STATE_FILE}. Its directory must be readable and "
                "executable by the console user. Until that is fixed there is "
                "no way to tell whether metrics collection is working, so "
                "treat the figures on this page as unverified."
            ),
        }
    except OSError:
        return unknown
    try:
        raw = json.loads(text)
    except ValueError:
        return unknown
    if not isinstance(raw, dict):
        return unknown
    phase = str(raw.get("phase") or "")
    if phase not in METRICS_PHASES:
        return unknown
    exit_code = raw.get("exit_code")
    at = str(raw.get("at") or "")
    reason = str(raw.get("reason") or "")

    # A "running" record older than the ceiling the task itself honours cannot
    # still be running: nothing would have left it in that state except
    # privhelperd going away mid-install, which takes the detached task with
    # it and leaves no one to write the outcome.
    if phase == "running":
        age = _state_age_seconds(at, now=now)
        if age is not None and age > INSTALL_TIMEOUT_SEC:
            return {
                "phase": "timeout",
                "exit_code": None,
                "at": at,
                "auto_attempts": int(raw.get("auto_attempts") or 0),
                "installed": False,
                "reason": (
                    "A monitoring install has been recorded as running since "
                    f"{at} and cannot still be in progress. It was most likely "
                    "interrupted. Mail is unaffected. Use Install monitoring "
                    "on the Monitoring tab to run it again."
                ),
            }

    return {
        "phase": phase,
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "reason": reason,
        "at": at,
        "auto_attempts": int(raw.get("auto_attempts") or 0),
        # Only a completed, zero-exit run counts. "running" is not yet proof,
        # and treating it as proof is how an in-flight install reads as done.
        "installed": phase == "ok",
    }


# Ceiling for the detached run. The playbook is allowed to wait 900s on an
# apt lock, plus package download and service start, so this is generous;
# it exists only so a stuck run cannot hold the maintenance lock for ever.
INSTALL_TIMEOUT_SEC = 30 * 60


def local_inventory(hostname: str) -> str:
    """Inventory naming this node only, over a local connection.

    `ansible_connection: local` on purpose: the playbook's one role installs
    packages and writes config on the host it runs on, and going out over SSH
    to reach ourselves would need credentials this command does not have and
    does not want.
    """
    name = (hostname or "").strip()
    if not HOSTNAME_RE.match(name):
        raise ValueError("refusing to build an inventory for a non-hostname")
    return "\n".join(
        [
            "all:",
            "  vars:",
            "    ansible_connection: local",
            "    ansible_become: true",
            "  children:",
            "    mail_nodes:",
            "      hosts:",
            f"        {name}:",
            "",
        ]
    )


async def _emit(text: str, *, err: bool = False) -> dict[str, Any]:
    from .maintenance import _emit as maint_emit

    return await maint_emit(text, err=err)


# Tasks spawned by run_after_install. Held so the event loop's only reference
# is not a weak one: a task nobody keeps can be collected mid-run.
_BACKGROUND: set[Any] = set()


def run_after_install() -> str:
    """Start the metrics install detached from whoever asked for it.

    This has to outlive the request that triggers it. The deploy streams over
    SSE, and when that connection drops (a proxy reaping an idle socket during
    the long package upgrade, or the operator closing the tab) the console's
    generator is closed, GeneratorExit is thrown into the command, and anything
    written after the streaming loop never runs. That is exactly how a deploy
    on 8 Sep 2026 finished cleanly and still had no metrics: the operator saw
    the log drop and reconnect at "upgrading and updating package", and the
    tail died with the first connection.

    An asyncio task is owned by the loop, not by the generator that created it,
    so it keeps running once the client is gone. Output goes to the deploy
    transcript, which is what View logs reads.
    """
    import asyncio

    async def _run() -> None:
        try:
            # Bounded, because this holds the maintenance lock for its whole
            # run and nothing is watching it. The playbook waits up to 900s on
            # an apt lock by design, so the ceiling has to clear that; what it
            # must not do is sit there for ever, because a stuck metrics
            # install would block Enter Maintenance, Remove Host and every
            # other cluster operation with an explanation nobody can act on.
            # Cancelling raises into the generator, whose finally releases the
            # lock.
            exit_code = await asyncio.wait_for(_drain(), timeout=INSTALL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            record_metrics_state(
                "timeout",
                reason=(
                    "The metrics install was still running after "
                    f"{INSTALL_TIMEOUT_SEC // 60} minutes and was given up on. "
                    "Mail is unaffected. Use Install monitoring on the "
                    "Monitoring tab to try again."
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001 - a detached task must not raise
            record_metrics_state(
                "error",
                reason=(
                    f"The metrics install could not run ({exc.__class__.__name__}). "
                    "Mail is unaffected. Use Install monitoring on the "
                    "Monitoring tab to try again."
                ),
            )
            return
        # ok / failed are recorded by cmd_install_monitoring itself, so the
        # manual Install monitoring button updates the same state and a repair
        # actually clears a previous failure. Only the two outcomes it cannot
        # see from the inside, timeout and could-not-start, are recorded here.
        del exit_code

    async def _drain() -> int:
        exit_code = 1
        async for ev in cmd_install_monitoring({}):
            # Events are written to the transcript by _stream_redacted.
            # Nothing is listening to them here, and that is the point; the
            # exit code is the one thing that has to come back out.
            if ev.get("type") == "done":
                raw = ev.get("exit_code")
                exit_code = 1 if raw is None else int(raw)
        return exit_code

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        record_metrics_state(
            "error",
            reason=(
                "The metrics install was never started (no event loop). Mail "
                "is unaffected. Use Install monitoring on the Monitoring tab."
            ),
        )
        return "no event loop, metrics not started"
    # Written before the task is created, so a privhelperd restart mid-install
    # leaves "running" behind rather than nothing at all. "running" is not
    # treated as installed anywhere, so a state that never advances reads as
    # unproven, which is the truthful answer.
    record_metrics_state("running")
    task = loop.create_task(_run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return "started"


# How many times the daemon will start a metrics install by itself before it
# stops and leaves it to the operator. Three is enough to ride out a transient
# apt lock, a slow mirror or a reboot in the middle of the first attempt, and
# small enough that a genuinely broken install cannot hold the maintenance lock
# for half an hour on every restart of a crash-looping daemon.
MAX_AUTO_ATTEMPTS = 3


def auto_install_decision() -> tuple[bool, str]:
    """Should this daemon start a metrics install by itself, and why not.

    Monitoring is meant to arrive with the first deploy and never need a second
    visit. It did not: the role used a module the appliance's ansible-core
    cannot resolve, so the automatic install failed, and from then on the only
    way to get metrics was for a person to press Install monitoring, which
    failed in exactly the same way. Fixing the module fixes new deployments.
    Appliances that already have the failure recorded need something to pick it
    up again, or that operator is pressing a button forever.

    Deliberately narrow. It never runs before the deploy has finished, never
    while an install is in flight, never on a node that already has metrics,
    and never more than MAX_AUTO_ATTEMPTS times without a success in between.
    """
    from .deploy_state import is_full_install_complete

    if not is_full_install_complete():
        return False, "the first deploy has not finished yet"

    state = read_metrics_state()
    phase = str(state.get("phase") or "")
    if phase == "ok":
        return False, "metrics are already installed"
    if phase == "running":
        return False, "an install is already running"
    if phase == "unreadable":
        # privhelperd is root, so this means something stranger than a
        # permission problem. Do not act blind.
        return False, "the metrics record could not be read"

    attempts = int(state.get("auto_attempts") or 0)
    if attempts >= MAX_AUTO_ATTEMPTS:
        return False, (
            f"already tried {attempts} times without success; use Install "
            "monitoring on the Monitoring tab once the cause is fixed"
        )
    return True, f"last state was {phase or 'unrecorded'}, attempt {attempts + 1}"


def resume_after_restart() -> str:
    """Start a metrics install on daemon startup if one is owed. Never raises."""
    try:
        go, why = auto_install_decision()
        if not go:
            return f"skipped: {why}"
        attempts = int(read_metrics_state().get("auto_attempts") or 0)
        # Counted before the run, not after. A daemon that dies mid-install
        # must still consume its budget, or a crash loop retries for ever.
        record_metrics_state("running", attempts=attempts + 1)
        return f"starting: {why} ({run_after_install()})"
    except Exception as exc:  # noqa: BLE001 - startup must not fail over this
        return f"skipped: {exc.__class__.__name__}"


async def cmd_install_monitoring(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run mail-monitoring.yml against this node and stream the output."""
    from .deploy_state import DEPLOY_LAST_LOG
    from .maintenance import (
        release_maintenance_lock,
        this_hostname,
        try_lock_maintenance,
    )
    from .orchestration import (
        ANSIBLE_DIR,
        _ansible_bin,
        _playbook_path,
        _stream_redacted,
        _write_work_files,
        kin_mail_deploy_dir,
    )

    args = args or {}
    check_only = bool(args.get("check"))

    # Two separate guards, because neither is sufficient alone.
    #
    # The lock stops this overlapping Add/Remove Host and Add/Remove
    # Observability, which all take it. Build HA pair takes NO lock, so the
    # lock cannot stop that one; the private work directory does. Build HA pair
    # writes its inventory once and runs roughly fifteen playbooks against that
    # file, so writing the shared inventory.yml here mid-build would silently
    # repoint the rest of the build at this node alone.
    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield proto.event_stderr(
            "Refusing: another cluster operation is already running. Metrics "
            "are additive and can wait; try again when it finishes.\n"
        )
        # No state written: the lock refusal changed nothing on this node,
        # and a node already collecting metrics must not be downgraded to
        # "failed" because someone pressed the button at a busy moment.
        yield proto.event_stdout("KIN_METRICS_END exit=1 reason=locked\n")
        yield proto.event_done(1)
        return

    hostname = this_hostname()
    try:
        inventory = local_inventory(hostname)
    except ValueError as exc:
        release_maintenance_lock(lock_fh)
        yield proto.event_stderr(f"{exc} (hostname={hostname!r})\n")
        yield proto.event_stdout("KIN_METRICS_END exit=2 reason=bad-hostname\n")
        yield proto.event_done(2)
        return

    # Greppable markers. Metrics were missing after a deploy on 8 Sep 2026 and
    # the transcript said nothing either way, so the cause had to be guessed
    # at. KIN_METRICS_BEGIN / KIN_METRICS_END with an exit code means the next
    # one is read, not inferred.
    yield await _emit(f"KIN_METRICS_BEGIN host={hostname}")
    yield await _emit(f"=== install monitoring on {hostname} ===")
    try:
        work = _write_work_files(inventory, MONITORING_WORK_DIR)
        playbook = _playbook_path(PLAYBOOK)
        binary = _ansible_bin()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        release_maintenance_lock(lock_fh)
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_stdout("KIN_METRICS_END exit=2 reason=setup\n")
        yield proto.event_done(2)
        return

    argv = [binary, "-i", str(work / "inventory.yml"), str(playbook)]
    if check_only:
        argv.append("--check")
    env = {
        "HOME": str(MONITORING_WORK_DIR),
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "KIN_MAIL_DEPLOY_DIR": kin_mail_deploy_dir(),
    }
    yield await _emit(f"ansible-playbook {PLAYBOOK}" + (" --check" if check_only else ""))

    exit_code = 1
    try:
        async for ev in _stream_redacted(
            argv,
            cwd=ANSIBLE_DIR,
            extra_env=env,
            secrets=[],
            transcript=Path(DEPLOY_LAST_LOG),
        ):
            if ev.get("type") == "done":
                exit_code = int(
                    ev.get("exit_code") if ev.get("exit_code") is not None else 1
                )
            else:
                yield ev
    finally:
        # BaseException too: closing the browser tab throws GeneratorExit in
        # here, and a lock left held would block every later cluster operation.
        release_maintenance_lock(lock_fh)

    # State is written only for a real run. A --check run converges nothing,
    # and stamping it would mark an appliance as collecting metrics on the
    # strength of a rehearsal. The messages below are unchanged either way.
    if exit_code == 0:
        if not check_only:
            # attempts=0: a success clears the automatic-retry budget, so a
            # box that recovers is not carrying an old failure count around.
            record_metrics_state("ok", exit_code=0, attempts=0)
        yield await _emit(
            "Monitoring installed. Prometheus and node_exporter are on loopback; "
            "charts fill in from now, and figures start from this moment rather "
            "than being back-filled."
        )
    else:
        if not check_only:
            record_metrics_state(
                "failed",
                exit_code=exit_code,
                reason=(
                    f"The metrics install failed (exit {exit_code}). Mail is "
                    "unaffected, but the Monitoring and Reports tabs have "
                    "nothing to count until it succeeds. Use Install "
                    "monitoring on the Monitoring tab to try again."
                ),
            )
        yield await _emit(
            f"Monitoring install failed (exit {exit_code}). Mail is unaffected: "
            "this step only adds metrics collection.",
            err=True,
        )
    yield proto.event_stdout(f"KIN_METRICS_END exit={exit_code}\n")
    yield proto.event_done(exit_code)
