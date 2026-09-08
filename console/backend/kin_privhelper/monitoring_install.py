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

import os
import re
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
            await asyncio.wait_for(_drain(), timeout=INSTALL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001 - a detached task must not raise
            pass

    async def _drain() -> None:
        async for _ev in cmd_install_monitoring({}):
            # Events are written to the transcript by _stream_redacted.
            # Nothing is listening here, and that is the point.
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "no event loop, metrics not started"
    task = loop.create_task(_run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return "started"


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

    if exit_code == 0:
        yield await _emit(
            "Monitoring installed. Prometheus and node_exporter are on loopback; "
            "charts fill in from now, and figures start from this moment rather "
            "than being back-filled."
        )
    else:
        yield await _emit(
            f"Monitoring install failed (exit {exit_code}). Mail is unaffected: "
            "this step only adds metrics collection.",
            err=True,
        )
    yield proto.event_stdout(f"KIN_METRICS_END exit={exit_code}\n")
    yield proto.event_done(exit_code)
