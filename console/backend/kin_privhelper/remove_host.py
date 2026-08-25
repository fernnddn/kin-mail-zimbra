"""Console remove-host: drain (if reachable), then survivor-side cluster_remove_host.

Graceful: target answers SSH. Forced: SSH fails or the node is already Offline;
survivor cleanup must still succeed so a later Add Host is not blocked.
Departing-node uninstall never undoes a successful survivor-side removal (no
rollback of cluster state on its failure), but it is not silently best-effort
either: a failed uninstall now fails the whole job (ok:false) so the operator
knows the retired host is still fully installed, same as a failed topology
demote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto
from .orchestration import OrchHost, kin_mail_deploy_dir


def names_match(left: str, right: str) -> bool:
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return a.split(".")[0] == b.split(".")[0]


def pick_name(needle: str, pool: list[str]) -> str | None:
    for name in pool:
        if names_match(needle, name):
            return name
    return None


@dataclass(frozen=True)
class RemovePlan:
    mode: str
    target: str
    survivor: str
    local_is_survivor: bool
    local_is_target: bool
    errors: tuple[str, ...]
    notes: tuple[str, ...]


def plan_remove_host(
    *,
    local_host: str,
    target: str,
    members: list[str],
    offline: list[str],
    stale_peers: list[str],
    known_peer: str,
    promoted: str | None,
    target_ssh_ok: bool,
    survivor_ssh_ok: bool,
) -> RemovePlan:
    """Decide graceful vs forced and refuse unsafe Primary ambiguity.

    members = Online + Standby + Maintenance. offline = pcs Offline.
    stale_peers = corosync names not in either list.
    """
    errors: list[str] = []
    notes: list[str] = []
    known: list[str] = []
    for name in [*members, *offline, *stale_peers, known_peer, local_host, target]:
        n = (name or "").strip()
        if n and n not in known:
            known.append(n)

    target_canon = pick_name(target, known) or target
    local_canon = pick_name(local_host, known) or local_host
    local_is_target = names_match(local_canon, target_canon)

    others = [n for n in known if not names_match(n, target_canon)]
    if local_is_target:
        survivor = pick_name(known_peer, others) or (others[0] if others else "")
    else:
        survivor = local_canon

    if not survivor:
        errors.append("cannot identify a surviving mail node to keep serving")
    elif names_match(survivor, target_canon):
        errors.append("survivor and target resolved to the same hostname")

    local_is_survivor = bool(survivor) and names_match(local_canon, survivor)

    if local_is_target and not others:
        errors.append("refusing to remove the last remaining mail node")

    if local_is_target and not survivor_ssh_ok:
        errors.append(
            "this console is on the departing node and the survivor does not "
            "answer SSH; survivor-side cleanup cannot run from here"
        )

    if not local_is_survivor and not survivor_ssh_ok:
        errors.append(
            f"survivor {survivor or '(unknown)'} does not answer SSH; "
            "refusing to mutate cluster state from a non-survivor"
        )

    promoted_is_target = bool(promoted) and names_match(promoted, target_canon)
    local_is_promoted = bool(promoted) and names_match(promoted, local_canon)
    if (
        not target_ssh_ok
        and promoted_is_target
        and not local_is_promoted
        and not local_is_target
    ):
        errors.append(
            "refusing forced remove of the Promoted node while this host is not "
            "Promoted (who is serving mail is ambiguous)"
        )

    mode = "graceful" if target_ssh_ok else "forced"
    if mode == "forced":
        notes.append(
            "target does not answer SSH; taking the forced path "
            "(survivor-side cleanup only, no remote uninstall)"
        )
    else:
        notes.append(
            "target is reachable; drain first if needed, then survivor-side "
            "remove, then uninstall on the departing node"
        )
    return RemovePlan(
        mode=mode,
        target=target_canon,
        survivor=survivor,
        local_is_survivor=local_is_survivor,
        local_is_target=local_is_target,
        errors=tuple(errors),
        notes=tuple(notes),
    )


def render_remove_host_inventory(
    *,
    survivor: OrchHost,
    retired: OrchHost,
    monitoring: OrchHost,
    local_connection: bool,
) -> str:
    """YAML inventory with env-lookup passwords. Never embed secret values."""
    conn = "local" if local_connection else "ssh"
    lines = [
        "all:",
        "  vars:",
        "    ansible_user: '{{ lookup(\"env\", \"KIN_ANSIBLE_USER\") }}'",
        "    ansible_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_PASSWORD\") }}'",
        "    ansible_become: true",
        "    ansible_become_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_BECOME_PASSWORD\") }}'",
        "    ansible_ssh_common_args: '-o StrictHostKeyChecking=accept-new "
        "-o ServerAliveInterval=30 -o ServerAliveCountMax=120'",
        f"    cluster_remove_host_survivor_name: {survivor.name}",
        f"    cluster_remove_host_survivor_address: {survivor.ip}",
        f"    cluster_remove_host_retired_name: {retired.name}",
        f"    cluster_remove_host_retired_address: {retired.ip}",
        f'    cluster_remove_host_qnetd_inventory_host: "{monitoring.name}"',
        '    kin_mail_deploy_dir: "' + kin_mail_deploy_dir() + '"',
        "  children:",
    ]
    if monitoring.name:
        lines.extend(
            [
                "    monitoring:",
                "      hosts:",
                f"        {monitoring.name}:",
                f"          ansible_host: {monitoring.ip}",
            ]
        )
    lines.extend(
        [
            "    mail_survivor:",
            "      hosts:",
            f"        {survivor.name}:",
            f"          ansible_host: {survivor.ip}",
            f"          ansible_connection: {conn}",
            "",
        ]
    )
    return "\n".join(lines)


def uninstall_script() -> Path:
    return Path(kin_mail_deploy_dir()) / "install" / "kin-mail-uninstall.sh"


def probe_payload(plan: RemovePlan, *, target_ssh_ok: bool) -> dict[str, Any]:
    return {
        "mode": plan.mode,
        "target": plan.target,
        "survivor": plan.survivor,
        "reachable": target_ssh_ok,
        "local_is_survivor": plan.local_is_survivor,
        "errors": list(plan.errors),
        "notes": list(plan.notes),
    }


async def _emit(text: str, *, err: bool = False) -> dict[str, Any]:
    from .maintenance import _emit as maint_emit

    return await maint_emit(text, err=err)


async def _ssh_ok_for(host: OrchHost, secrets_map: dict[str, str]) -> bool:
    from .orchestration import _ssh_probe, ssh_password_candidates

    root_pass = secrets_map.get("host_root_pass") or ""
    kin_pass = secrets_map.get("kin_user_pass") or ""
    if not host.ip:
        return False
    secrets = [p for p in (root_pass, kin_pass) if p]
    for user, pwd in ssh_password_candidates(kin_pass, root_pass):
        if not pwd:
            continue
        code, _ident = await _ssh_probe(host, user, pwd, secrets)
        if code == 0:
            return True
    return False


def _host_for(name: str, ip: str) -> OrchHost:
    from .orchestration import _iqn_suffix

    return OrchHost(name, ip, _iqn_suffix(name))


async def _unstandby_best_effort(target: str) -> str:
    """Roll a graceful drain back if remove-host fails before Ansible removes
    the target from the cluster - otherwise a failed attempt leaves a node
    that is still supposed to be in service stuck in Standby indefinitely
    (re-audit, 25 Aug 2026). Best-effort: reports the outcome, never raises.
    """
    from .maintenance import _capture

    c, out, err = await _capture(["pcs", "node", "unstandby", target], timeout=60)
    if c == 0:
        return f"Rolled back: pcs node unstandby {target} (remove-host did not finish)"
    detail = (err or out or f"exit {c}").strip()
    return (
        f"WARNING: pcs node unstandby {target} failed ({detail}) after remove-host "
        f"did not finish - {target} may be stuck in Standby. Run it manually."
    )


async def _demote_survivor_topology_to_1vm(
    *,
    plan: RemovePlan,
    survivor_host: OrchHost,
    ssh_user: str,
    ssh_pass: str,
    secrets: list[str],
) -> tuple[bool, list[str]]:
    """Set TOPOLOGY=1vm and clear PEER_HOST_* on the surviving node's config.

    Keeps CLUSTER_VIP_IP and OBSERVABILITY_VM_IP (still live on Pacemaker /
    qdevice). Clears ha-setup-complete so the console no longer reports a
    finished two-server pair after the peer is gone.

    Returns (ok, notes) - the caller must surface a False here, not just log
    the notes and report the overall job as ok:true.
    """
    from .apply_config import (
        CONF_FILE,
        ensure_topology_1vm,
        format_config,
        parse_config,
        write_config_file,
    )
    from .deploy_state import HA_SETUP_COMPLETE_MARKER, write_topology_marker

    notes: list[str] = []
    if plan.local_is_survivor:
        try:
            text = CONF_FILE.read_text(encoding="utf-8") if CONF_FILE.is_file() else ""
        except OSError as exc:
            notes.append(f"could not read local config to demote topology: {exc}")
            return False, notes
        values = parse_config(text)
        new_values, changed = ensure_topology_1vm(values)
        if changed:
            try:
                write_config_file(new_values)
            except OSError as exc:
                notes.append(f"could not write demoted local config: {exc}")
                return False, notes
        write_topology_marker("1vm")
        if HA_SETUP_COMPLETE_MARKER.is_file():
            try:
                HA_SETUP_COMPLETE_MARKER.unlink()
                notes.append("Cleared local ha-setup-complete (pair setup no longer valid)")
            except OSError as exc:
                notes.append(f"could not clear local ha-setup-complete: {exc}")
                return False, notes
        notes.append(
            "Demoted local /etc/kin-mail/config to TOPOLOGY=1vm, cleared peer fields "
            "(VIP and Observability IP kept)"
        )
        return True, notes

    # Survivor is remote (this console is on the departing node): read/write
    # its config over the same password-SSH path used for peer HA console sync.
    from .orchestration import _push_peer_text_file, _ssh_run

    code, blob = await _ssh_run(
        survivor_host,
        ssh_user,
        ssh_pass,
        secrets,
        "cat /etc/kin-mail/config 2>/dev/null || true",
        timeout=20,
    )
    if code != 0:
        notes.append(f"could not read survivor config over SSH (exit {code}); topology not demoted")
        return False, notes
    values = parse_config(blob)
    new_values, changed = ensure_topology_1vm(values)
    if changed:
        code, _text = await _push_peer_text_file(
            survivor_host,
            ssh_user,
            ssh_pass,
            secrets,
            body=format_config(new_values),
            remote_tmp="/tmp/kin-mail-survivor-config",
            dest="/etc/kin-mail/config",
            mode="600",
        )
        if code != 0:
            notes.append(f"failed to push demoted config to survivor (exit {code})")
            return False, notes
    code, _text = await _push_peer_text_file(
        survivor_host,
        ssh_user,
        ssh_pass,
        secrets,
        body="1vm\n",
        remote_tmp="/tmp/kin-mail-survivor-topology",
        dest="/etc/kin-mail/topology",
        mode="644",
    )
    if code != 0:
        notes.append(f"failed to push /etc/kin-mail/topology=1vm to survivor (exit {code})")
        return False, notes
    code, _text = await _ssh_run(
        survivor_host,
        ssh_user,
        ssh_pass,
        secrets,
        "rm -f /etc/kin-mail/ha-setup-complete",
        timeout=15,
    )
    if code != 0:
        notes.append(
            f"failed to clear survivor ha-setup-complete (exit {code}); "
            "topology demoted but pair marker may still be present"
        )
        return False, notes
    notes.append(
        "Demoted survivor /etc/kin-mail/config and topology marker to TOPOLOGY=1vm "
        "over SSH (VIP and Observability IP kept; ha-setup-complete cleared)"
    )
    return True, notes


async def cmd_remove_host(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    from .maintenance import (
        gather_status,
        release_maintenance_lock,
        run_preflight,
        try_lock_maintenance,
        validate_node_name,
        _capture,
    )
    from .orchestration import (
        _ansible_bin,
        _playbook_path,
        _stream_redacted,
        _write_work_files,
        resolve_topology,
        ssh_password_candidates,
        valid_ipv4,
        ANSIBLE_DIR,
        WORK_DIR,
        _load_config,
        _load_draft,
    )
    from .provisioning_secrets import load_secrets

    args = args or {}
    op = str(args.get("op") or "apply").strip().lower()
    if op not in ("probe", "apply"):
        yield proto.event_stderr("op must be probe or apply\n")
        yield proto.event_done(2)
        return
    try:
        target_raw = validate_node_name(str(args.get("target") or ""))
    except ValueError as exc:
        yield proto.event_stderr(f"{exc}\n")
        yield proto.event_done(2)
        return

    yield await _emit(f"=== remove_host {op} target={target_raw} ===")
    st = await gather_status()
    local_host = str(st.get("local_host") or "")
    members = list(st.get("nodes") or [])
    offline = list(st.get("offline") or [])
    stale = list(st.get("stale_peers") or [])
    addrs = dict(st.get("addrs") or {})
    promoted = st.get("promoted") if isinstance(st.get("promoted"), str) else None

    known_peer = ""
    peer_ip = ""
    local_ip = ""
    monitoring_host = _host_for("", "")
    try:
        draft = _load_draft()
        config = _load_config()
        local_h, peer_h, mon_h = resolve_topology(draft=draft, config=config)
        known_peer = peer_h.name
        peer_ip = peer_h.ip
        local_ip = local_h.ip
        monitoring_host = mon_h
        if not local_host:
            local_host = local_h.name
    except Exception as exc:  # noqa: BLE001
        yield await _emit(f"topology/config note: {exc}")

    allowed = members + offline + stale
    if known_peer:
        allowed.append(known_peer)
    if pick_name(target_raw, allowed) is None:
        yield await _emit(
            f"target {target_raw!r} is not in Pacemaker members, Offline, "
            "corosync, or PEER_HOST_NAME",
            err=True,
        )
        yield proto.event_done(2)
        return

    secrets_map = load_secrets()
    target_ip = addrs.get(pick_name(target_raw, list(addrs)) or "") or ""
    if not target_ip and known_peer and names_match(target_raw, known_peer):
        target_ip = peer_ip
    if not target_ip and names_match(target_raw, local_host):
        target_ip = local_ip
    target_host = _host_for(target_raw, target_ip)

    target_ssh_ok = False
    if names_match(target_raw, local_host):
        target_ssh_ok = True
    elif target_ip and valid_ipv4(target_ip):
        target_ssh_ok = await _ssh_ok_for(target_host, secrets_map)
        yield await _emit(
            f"ssh_probe target={target_raw} ip={target_ip} reachable={target_ssh_ok}"
        )
    else:
        yield await _emit(f"ssh_probe target={target_raw} skipped (no IPv4)")

    plan = plan_remove_host(
        local_host=local_host,
        target=target_raw,
        members=members,
        offline=offline,
        stale_peers=stale,
        known_peer=known_peer,
        promoted=promoted,
        target_ssh_ok=target_ssh_ok,
        survivor_ssh_ok=True,
    )

    survivor_ip = addrs.get(pick_name(plan.survivor, list(addrs)) or "") or ""
    if not survivor_ip and names_match(plan.survivor, local_host):
        survivor_ip = local_ip
    if not survivor_ip and known_peer and names_match(plan.survivor, known_peer):
        survivor_ip = peer_ip
    survivor_host = _host_for(plan.survivor or "unknown", survivor_ip)
    survivor_ssh_ok = True
    if plan.survivor and not names_match(plan.survivor, local_host):
        survivor_ssh_ok = await _ssh_ok_for(survivor_host, secrets_map)
        yield await _emit(
            f"ssh_probe survivor={plan.survivor} ip={survivor_ip} reachable={survivor_ssh_ok}"
        )
        plan = plan_remove_host(
            local_host=local_host,
            target=target_raw,
            members=members,
            offline=offline,
            stale_peers=stale,
            known_peer=known_peer,
            promoted=promoted,
            target_ssh_ok=target_ssh_ok,
            survivor_ssh_ok=survivor_ssh_ok,
        )

    payload = probe_payload(plan, target_ssh_ok=target_ssh_ok)
    yield await _emit("REMOVE_PROBE_JSON:" + json.dumps(payload, separators=(",", ":")))
    for note in plan.notes:
        yield await _emit(note)
    if plan.errors:
        for err in plan.errors:
            yield await _emit(err, err=True)
        yield proto.event_done(1)
        return
    if op == "probe":
        yield proto.event_done(0)
        return

    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield await _emit(
            "Refusing: another maintenance or remove-host operation is already running.",
            err=True,
        )
        yield proto.event_done(1)
        return

    uninstall_warned = False
    ansible_ok = False
    stood_by_target = False
    try:
        if plan.mode == "graceful" and plan.target not in list(st.get("standby") or []):
            yield await _emit(f"drain: reuse maintenance enter for {plan.target}")
            ok, logs, checks = await run_preflight(plan.target)
            for line in logs:
                yield await _emit(line)
            yield await _emit("PREFLIGHT_JSON:" + json.dumps(checks, separators=(",", ":")))
            if not ok:
                yield await _emit(
                    "Refusing remove-host: pre-flight failed (same gate as Enter Maintenance).",
                    err=True,
                )
                yield proto.event_done(1)
                return
            yield await _emit(f"pcs node standby {plan.target}")
            c, out, err = await _capture(["pcs", "node", "standby", plan.target], timeout=120)
            if out:
                yield await _emit(out)
            if err:
                yield await _emit(err, err=True)
            if c != 0:
                yield await _emit(f"pcs node standby failed (exit {c})", err=True)
                yield proto.event_done(c)
                return
            st = await gather_status()
            yield await _emit(
                f"after drain promoted={st.get('promoted')} standby={st.get('standby')}"
            )
            if plan.target not in list(st.get("standby") or []):
                yield await _emit(
                    f"{plan.target} is not listed as Standby after pcs node standby",
                    err=True,
                )
                yield proto.event_done(1)
                return
            # Rolled back on any failure below - this run put it here, so
            # this run is responsible for taking it back out if remove-host
            # does not finish. A target already in maintenance before this
            # run started (the elif branch below) is left alone: that state
            # was not this run's doing.
            stood_by_target = True
        elif plan.mode == "graceful":
            yield await _emit(f"{plan.target} already in maintenance; skipping drain")
        else:
            yield await _emit("forced path: skipping drain (target unreachable)")

        retired_ip = target_ip
        inv = render_remove_host_inventory(
            survivor=survivor_host,
            retired=_host_for(plan.target, retired_ip),
            monitoring=monitoring_host,
            local_connection=plan.local_is_survivor,
        )
        secrets = [p for p in (secrets_map.get("host_root_pass"), secrets_map.get("kin_user_pass")) if p]
        if any(s and s in inv for s in secrets):
            yield await _emit("internal error: inventory would contain a secret", err=True)
            if stood_by_target:
                yield await _emit(await _unstandby_best_effort(plan.target))
            yield proto.event_done(1)
            return
        work = _write_work_files(inv)
        inv_path = work / "inventory.yml"
        play = _playbook_path("playbooks/mail-remove-host.yml")
        ansible_playbook = _ansible_bin()
        root_pass = secrets_map.get("host_root_pass") or ""
        kin_pass = secrets_map.get("kin_user_pass") or ""
        ssh_user, ssh_pass = "kin", kin_pass
        for user, pwd in ssh_password_candidates(kin_pass, root_pass):
            if pwd:
                ssh_user, ssh_pass = user, pwd
                break
        ansible_env = {
            "HOME": str(WORK_DIR),
            "KIN_ANSIBLE_USER": ssh_user,
            "KIN_ANSIBLE_PASSWORD": ssh_pass,
            "KIN_ANSIBLE_BECOME_PASSWORD": ssh_pass,
            "KIN_MAIL_DEPLOY_DIR": kin_mail_deploy_dir(),
            "ANSIBLE_CONFIG": str(WORK_DIR / "ansible.cfg"),
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
            "ANSIBLE_LOCAL_TEMP": str(WORK_DIR / ".ansible" / "tmp"),
        }
        argv = [ansible_playbook, "-i", str(inv_path), str(play)]
        yield await _emit(f"ansible-playbook mail-remove-host.yml survivor={plan.survivor}")
        from .deploy_state import DEPLOY_LAST_LOG

        exit_code = 1
        async for ev in _stream_redacted(
            argv,
            cwd=ANSIBLE_DIR,
            extra_env=ansible_env,
            secrets=secrets,
            transcript=DEPLOY_LAST_LOG,
        ):
            if ev.get("type") == "done":
                exit_code = int(ev.get("exit_code") if ev.get("exit_code") is not None else 1)
            else:
                yield ev
        if exit_code != 0:
            yield await _emit(
                f"survivor-side remove-host playbook failed (exit {exit_code})",
                err=True,
            )
            if stood_by_target:
                yield await _emit(await _unstandby_best_effort(plan.target))
            yield proto.event_done(exit_code)
            return
        ansible_ok = True
        yield await _emit("survivor-side remove-host playbook finished")

        demote_ok, demote_notes = await _demote_survivor_topology_to_1vm(
            plan=plan,
            survivor_host=survivor_host,
            ssh_user=ssh_user,
            ssh_pass=ssh_pass,
            secrets=secrets,
        )
        for line in demote_notes:
            yield await _emit(line, err=not demote_ok)
        if not demote_ok:
            # Unlike the departing-node uninstall below, this is not
            # best-effort cleanup: it is the survivor's own state. Reporting
            # ok:true here would recreate the exact bug remove-host exists to
            # prevent (survivor stuck at TOPOLOGY=2vm, pointing at a retired
            # peer) while telling the operator everything worked.
            yield await _emit(
                "Refusing to report success: survivor topology was not demoted to 1vm. "
                "The cluster-membership steps (Corosync/DRBD/constraints) already "
                "succeeded and are safe to repeat - retry Remove Host to finish the "
                "topology demote, or fix /etc/kin-mail/config by hand "
                "(TOPOLOGY=1vm, clear PEER_HOST_IP/NAME).",
                err=True,
            )
            result = {
                "ok": False,
                "mode": plan.mode,
                "target": plan.target,
                "survivor": plan.survivor,
                "ansible_ok": ansible_ok,
                "demote_ok": False,
            }
            yield await _emit("REMOVE_HOST_JSON:" + json.dumps(result, separators=(",", ":")))
            yield proto.event_done(1)
            return

        script = uninstall_script()
        if plan.mode == "graceful":
            if plan.local_is_target:
                remote_cmd = f"sudo -n {script} --decommission"
                yield await _emit(f"uninstall on departing node: {remote_cmd}")
                c, out, err = await _capture(
                    ["bash", str(script), "--decommission"],
                    timeout=900,
                )
                if out:
                    yield await _emit(out)
                if err:
                    yield await _emit(err, err=True)
                if c != 0:
                    uninstall_warned = True
                    yield await _emit(
                        f"departing-node uninstall exited {c}",
                        err=True,
                    )
            else:
                from .orchestration import _ssh_run, wrap_privileged_remote

                # sudo -n needs passwordless sudo on the departing node's kin
                # account, which is not guaranteed outside Ansible's own
                # become_password path. wrap_privileged_remote falls back to
                # sudo -S (password on stdin, never argv) - without it this
                # uninstall silently no-ops on a plain greenfield kin account
                # and the departing host is left fully installed.
                remote_cmd = wrap_privileged_remote(f"{script} --decommission")
                yield await _emit(f"uninstall on departing node: {script} --decommission")
                code, text = await _ssh_run(
                    target_host,
                    ssh_user,
                    ssh_pass,
                    secrets,
                    remote_cmd,
                    timeout=900,
                    stdin_text=ssh_pass,
                )
                if text:
                    yield await _emit(text)
                if code != 0:
                    uninstall_warned = True
                    yield await _emit(
                        f"departing-node uninstall ssh exit {code}",
                        err=True,
                    )
        else:
            yield await _emit("forced path: skipping departing-node uninstall (unreachable)")

        if uninstall_warned:
            yield await _emit(
                "Refusing to report success: departing-node uninstall failed. "
                "Survivor topology is already 1vm. Retry Remove Host, or run "
                f"{script} --decommission on the retired host by hand.",
                err=True,
            )
            result = {
                "ok": False,
                "mode": plan.mode,
                "target": plan.target,
                "survivor": plan.survivor,
                "ansible_ok": ansible_ok,
                "demote_ok": True,
                "uninstall_ok": False,
                "uninstall_warned": True,
            }
            yield await _emit("REMOVE_HOST_JSON:" + json.dumps(result, separators=(",", ":")))
            yield proto.event_done(1)
            return

        result = {
            "ok": True,
            "mode": plan.mode,
            "target": plan.target,
            "survivor": plan.survivor,
            "ansible_ok": ansible_ok,
            "demote_ok": True,
            "uninstall_ok": True,
            "uninstall_warned": False,
        }
        yield await _emit("REMOVE_HOST_JSON:" + json.dumps(result, separators=(",", ":")))
        yield proto.event_done(0)
    finally:
        release_maintenance_lock(lock_fh)
