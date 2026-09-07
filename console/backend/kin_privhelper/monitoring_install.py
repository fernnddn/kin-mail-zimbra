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
        yield proto.event_done(1)
        return

    hostname = this_hostname()
    try:
        inventory = local_inventory(hostname)
    except ValueError as exc:
        release_maintenance_lock(lock_fh)
        yield proto.event_stderr(f"{exc} (hostname={hostname!r})\n")
        yield proto.event_done(2)
        return

    yield await _emit(f"=== install monitoring on {hostname} ===")
    try:
        work = _write_work_files(inventory, MONITORING_WORK_DIR)
        playbook = _playbook_path(PLAYBOOK)
        binary = _ansible_bin()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        release_maintenance_lock(lock_fh)
        yield proto.event_stderr(f"{exc}\n")
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
    yield proto.event_done(exit_code)
