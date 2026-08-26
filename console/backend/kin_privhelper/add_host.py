"""Console add-host: attach a blank peer to a live one-node survivor cluster.

After Remove Host the survivor stays TOPOLOGY=1vm with Pacemaker + VIP.
Greenfield Build HA (run_ha_orchestration) must refuse that live CIB.
This command runs mail-add-host.yml instead: prepare the blank peer, pcs
node add, rewrite the DRBD peer, then live-join full sync while the
survivor stays Primary.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto
from .orchestration import (
    OrchHost,
    kin_mail_deploy_dir,
    ssh_password_candidates,
    sync_peer_ha_console_state,
    valid_ipv4,
    valid_name,
)

LAST_REMOVED_PEER_PATH = Path(
    os.environ.get("KIN_LAST_REMOVED_PEER", "/etc/kin-mail/last-removed-peer")
)
_IPV4 = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$"
)


@dataclass(frozen=True)
class AddHostPlan:
    survivor_name: str
    survivor_ip: str
    new_name: str
    new_ip: str
    retired_name: str
    retired_ip: str
    obs_name: str
    obs_ip: str
    vip_ip: str
    data_disk: str
    meta_disk: str
    errors: tuple[str, ...]
    notes: tuple[str, ...]


def read_last_removed_peer(path: Path | None = None) -> dict[str, str]:
    p = path or LAST_REMOVED_PEER_PATH
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    name = str(raw.get("name") or "").strip()
    ip = str(raw.get("ip") or "").strip()
    out: dict[str, str] = {}
    if name:
        out["name"] = name
    if ip and _IPV4.match(ip):
        out["ip"] = ip
    return out


def write_last_removed_peer(name: str, ip: str, path: Path | None = None) -> None:
    p = path or LAST_REMOVED_PEER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"name": (name or "").strip(), "ip": (ip or "").strip()},
        separators=(",", ":"),
    )
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(p)
    try:
        os.chmod(p, 0o640)
    except OSError:
        pass


def clear_last_removed_peer(path: Path | None = None) -> None:
    p = path or LAST_REMOVED_PEER_PATH
    try:
        p.unlink()
    except FileNotFoundError:
        return
    except OSError:
        pass


def attach_peer_eligible(
    *,
    topology: str,
    live_nodes: list[str],
    vip_ip: str = "",
    vip_node: str | None = None,
    promoted: str | None = None,
    package_stub: bool = False,
) -> bool:
    """True only for a real one-node survivor HA, not a fresh single deploy.

    Greenfield Add Second Server (1vm with no live VIP/Promoted, or Debian
    package stub Pacemaker) must use Build HA / run_ha_orchestration.
    Attach (mail-add-host) is only for after Remove Host when VIP + Promoted
    still exist on exactly one live mail node.
    """
    topo = (topology or "").strip().lower()
    if topo not in ("1vm", "1"):
        return False
    if package_stub:
        return False
    online = [str(n).strip() for n in (live_nodes or []) if str(n).strip()]
    if len(online) != 1:
        return False
    if not (vip_ip or "").strip():
        return False
    return bool(vip_node) or bool(promoted)


def plan_add_host(
    *,
    local_host: str,
    local_ip: str,
    new_name: str,
    new_ip: str,
    retired_name: str,
    retired_ip: str,
    obs_name: str,
    obs_ip: str,
    vip_ip: str,
    live_nodes: list[str],
    promoted: str | None,
    standby: list[str],
    data_disk: str,
    meta_disk: str,
) -> AddHostPlan:
    """Refuse unsafe attach-to-survivor cases; otherwise return a runnable plan."""
    errors: list[str] = []
    notes: list[str] = []

    surv_name = (local_host or "").strip()
    surv_ip = (local_ip or "").strip()
    peer_name = (new_name or "").strip()
    peer_ip = (new_ip or "").strip()
    retired_n = (retired_name or "").strip()
    retired_a = (retired_ip or "").strip()
    mon_name = (obs_name or "").strip() or f"obs-{(obs_ip or '').replace('.', '-')}"
    mon_ip = (obs_ip or "").strip()
    vip = (vip_ip or "").strip()
    disk = (data_disk or "").strip() or "/dev/sdb1"
    meta = (meta_disk or "").strip() or "/dev/sdb2"

    if not surv_name or not valid_name(surv_name):
        errors.append("cannot identify the survivor hostname for this console")
    if not surv_ip or not valid_ipv4(surv_ip):
        errors.append("survivor IPv4 is missing from config / cluster status")
    if not peer_name or not valid_name(peer_name):
        errors.append("enter a valid hostname for the new mail server")
    if not peer_ip or not valid_ipv4(peer_ip):
        errors.append("enter a valid IPv4 for the new mail server")
    if not retired_n or not valid_name(retired_n):
        errors.append(
            "previous peer hostname is required (saved at Remove Host, or enter it)"
        )
    if not mon_ip or not valid_ipv4(mon_ip):
        errors.append("Observability IPv4 is required (kept after Remove Host)")
    if not vip or not valid_ipv4(vip):
        errors.append("cluster VIP is required (kept after Remove Host)")

    if peer_name and surv_name and peer_name.lower() == surv_name.lower():
        errors.append("new server hostname must differ from the survivor")
    if peer_name and retired_n and peer_name.lower() == retired_n.lower():
        errors.append("new server hostname must differ from the retired peer")
    if peer_ip and surv_ip and peer_ip == surv_ip:
        errors.append("new server IP must differ from the survivor")
    if peer_ip and retired_a and peer_ip == retired_a:
        errors.append("new server IP must differ from the retired peer address")
    if peer_ip and vip and peer_ip == vip:
        errors.append("new server IP must not be the cluster VIP")
    if peer_ip and mon_ip and peer_ip == mon_ip:
        errors.append("new server IP must not be the Observability VM")
    if surv_ip and vip and surv_ip == vip:
        errors.append("cluster VIP must not equal the survivor node address")

    online = [n for n in (live_nodes or []) if n]
    if len(online) != 1:
        errors.append(
            "add-host requires exactly one live Pacemaker mail node "
            f"(found {len(online)}: {', '.join(online) or 'none'})"
        )
    elif surv_name and online[0].split(".")[0].lower() != surv_name.split(".")[0].lower():
        # Allow FQDN vs short mismatch via short compare.
        from .remove_host import names_match

        if not names_match(online[0], surv_name):
            errors.append(
                f"live cluster node {online[0]} is not this console host ({surv_name})"
            )

    if standby:
        errors.append(
            "exit maintenance before attaching a peer "
            f"(standby={', '.join(standby)})"
        )
    if not promoted:
        errors.append("no stable Promoted node; wait for Pacemaker to settle")
    elif surv_name:
        from .remove_host import names_match

        if not names_match(str(promoted), surv_name):
            errors.append(
                f"Promoted is {promoted}, not this survivor; move Master here first"
            )

    notes.append(
        "Attach blank peer via mail-add-host: packages/fencing on the new host, "
        "pcs node add, DRBD peer rewrite, then full sync while this host stays Primary"
    )
    return AddHostPlan(
        survivor_name=surv_name,
        survivor_ip=surv_ip,
        new_name=peer_name,
        new_ip=peer_ip,
        retired_name=retired_n,
        retired_ip=retired_a,
        obs_name=mon_name,
        obs_ip=mon_ip,
        vip_ip=vip,
        data_disk=disk,
        meta_disk=meta,
        errors=tuple(errors),
        notes=tuple(notes),
    )


def render_add_host_inventory(plan: AddHostPlan) -> str:
    """YAML inventory with env-lookup passwords. Never embed secret values."""
    from .orchestration import _iqn_suffix

    new_iqn = _iqn_suffix(plan.new_name)
    surv_iqn = _iqn_suffix(plan.survivor_name)
    lines = [
        "all:",
        "  vars:",
        "    ansible_user: '{{ lookup(\"env\", \"KIN_ANSIBLE_USER\") }}'",
        "    ansible_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_PASSWORD\") }}'",
        "    ansible_become: true",
        "    ansible_become_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_BECOME_PASSWORD\") }}'",
        "    ansible_ssh_common_args: '-o StrictHostKeyChecking=accept-new "
        "-o ServerAliveInterval=30 -o ServerAliveCountMax=120'",
        f"    cluster_add_host_survivor_name: {plan.survivor_name}",
        f"    cluster_add_host_survivor_address: {plan.survivor_ip}",
        f"    cluster_add_host_retired_name: {plan.retired_name}",
        f"    cluster_add_host_retired_address: {plan.retired_ip or '0.0.0.0'}",
        f"    cluster_add_host_new_name: {plan.new_name}",
        f"    cluster_add_host_new_address: {plan.new_ip}",
        "    cluster_add_host_hacluster_password: "
        "'{{ lookup(\"env\", \"KIN_HACLUSTER_PASSWORD\") }}'",
        "    cluster_node_base_hacluster_password: "
        "'{{ lookup(\"env\", \"KIN_HACLUSTER_PASSWORD\") }}'",
        f'    iscsi_initiator_portal: "{plan.obs_ip}:3260"',
        "    iscsi_target_chap_userid: kin-sbd-chap",
        "    iscsi_target_chap_password: '{{ lookup(\"env\", \"KIN_CHAP_PASSWORD\") }}'",
        "    iscsi_initiator_chap_userid: kin-sbd-chap",
        "    iscsi_initiator_chap_password: '{{ lookup(\"env\", \"KIN_CHAP_PASSWORD\") }}'",
        f"    cluster_add_host_drbd_disk: {plan.data_disk}",
        f"    cluster_add_host_drbd_meta_disk: {plan.meta_disk}",
        f"    drbd_resource_disk: {plan.data_disk}",
        f"    drbd_resource_meta_disk: {plan.meta_disk}",
        "    drbd_resource_luks: true",
        '    kin_mail_deploy_dir: "' + kin_mail_deploy_dir() + '"',
        f"    pacemaker_mail_stack_vip_ip: {plan.vip_ip}",
        "  children:",
        "    monitoring:",
        "      hosts:",
        f"        {plan.obs_name}:",
        f"          ansible_host: {plan.obs_ip}",
        "    mail_survivor:",
        "      hosts:",
        f"        {plan.survivor_name}:",
        f"          ansible_host: {plan.survivor_ip}",
        "          ansible_connection: local",
        f'          iscsi_initiator_name: "iqn.2026-08.example.test:{surv_iqn}"',
        "    mail_new_node:",
        "      hosts:",
        f"        {plan.new_name}:",
        f"          ansible_host: {plan.new_ip}",
        "          ansible_connection: ssh",
        f'          iscsi_initiator_name: "iqn.2026-08.example.test:{new_iqn}"',
        "    mail_nodes:",
        "      children:",
        "        mail_survivor:",
        "        mail_new_node:",
        "",
    ]
    return "\n".join(lines)


async def _emit(text: str, *, err: bool = False) -> dict[str, Any]:
    from .maintenance import _emit as maint_emit

    return await maint_emit(text, err=err)


async def cmd_add_host(args: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    from .apply_config import (
        ensure_topology_2vm,
        parse_config,
        write_config_file,
    )
    from .deploy_state import (
        DEPLOY_LAST_LOG,
        mark_ha_setup_complete,
        write_topology_marker,
    )
    from .ha_disk import DEFAULT_DATA_DISK, DEFAULT_META_DISK
    from .maintenance import (
        gather_status,
        release_maintenance_lock,
        try_lock_maintenance,
    )
    from .orchestration import (
        ANSIBLE_DIR,
        WORK_DIR,
        _ansible_bin,
        _load_config,
        _playbook_path,
        _stream_redacted,
        _write_work_files,
        ssh_password_candidates,
    )
    from .provisioning_secrets import load_secrets

    args = args or {}
    op = str(args.get("op") or "apply").strip().lower()
    if op not in ("probe", "apply"):
        yield proto.event_stderr("op must be probe or apply\n")
        yield proto.event_done(2)
        return

    yield await _emit(f"=== add_host {op} ===")
    st = await gather_status()
    config = _load_config()
    last = read_last_removed_peer()

    local_host = str(st.get("local_host") or config.get("MAIL_HOST") or "").strip()
    local_ip = str(config.get("SERVER_IP") or "").strip()
    addrs = dict(st.get("addrs") or {})
    if local_host and not local_ip:
        local_ip = str(addrs.get(local_host) or "").strip()

    new_name = str(args.get("new_name") or args.get("peer_host_name") or "").strip()
    new_ip = str(args.get("new_ip") or args.get("peer_host_ip") or "").strip()
    retired_name = str(
        args.get("retired_name") or last.get("name") or config.get("PEER_HOST_NAME") or ""
    ).strip()
    retired_ip = str(
        args.get("retired_ip") or last.get("ip") or config.get("PEER_HOST_IP") or ""
    ).strip()
    obs_ip = str(
        args.get("observability_vm_ip")
        or config.get("OBSERVABILITY_VM_IP")
        or ""
    ).strip()
    obs_name = str(args.get("observability_name") or "").strip()
    vip_ip = str(args.get("cluster_vip_ip") or config.get("CLUSTER_VIP_IP") or "").strip()
    data_disk = str(args.get("data_disk") or DEFAULT_DATA_DISK).strip()
    meta_disk = str(args.get("meta_disk") or DEFAULT_META_DISK).strip()

    live_nodes = list(st.get("nodes") or [])
    if not attach_peer_eligible(
        topology=str(st.get("topology") or ""),
        live_nodes=live_nodes,
        vip_ip=vip_ip or str(st.get("vip_ip") or ""),
        vip_node=st.get("vip_node") if isinstance(st.get("vip_node"), str) else None,
        promoted=st.get("promoted") if isinstance(st.get("promoted"), str) else None,
        package_stub=bool(st.get("package_stub")),
    ):
        yield await _emit(
            "Refusing add-host: this console is not a live one-node HA survivor. "
            "For a fresh single-server deploy, use Add Second Server / Build HA pair "
            "(ha_orchestration), not attach peer.",
            err=True,
        )
        yield proto.event_done(1)
        return

    plan = plan_add_host(
        local_host=local_host,
        local_ip=local_ip,
        new_name=new_name,
        new_ip=new_ip,
        retired_name=retired_name,
        retired_ip=retired_ip,
        obs_name=obs_name,
        obs_ip=obs_ip,
        vip_ip=vip_ip,
        live_nodes=live_nodes,
        promoted=st.get("promoted") if isinstance(st.get("promoted"), str) else None,
        standby=list(st.get("standby") or []),
        data_disk=data_disk,
        meta_disk=meta_disk,
    )
    probe = {
        "survivor": plan.survivor_name,
        "new_name": plan.new_name,
        "new_ip": plan.new_ip,
        "retired_name": plan.retired_name,
        "retired_ip": plan.retired_ip,
        "obs_ip": plan.obs_ip,
        "vip_ip": plan.vip_ip,
        "errors": list(plan.errors),
        "notes": list(plan.notes),
    }
    yield await _emit("ADD_HOST_PROBE_JSON:" + json.dumps(probe, separators=(",", ":")))
    for note in plan.notes:
        yield await _emit(note)
    if plan.errors:
        for err in plan.errors:
            yield await _emit(err, err=True)
        yield proto.event_done(2)
        return
    if op == "probe":
        yield proto.event_done(0)
        return

    if not ANSIBLE_DIR.is_dir():
        yield await _emit(f"Refusing: ansible tree missing at {ANSIBLE_DIR}", err=True)
        yield proto.event_done(1)
        return

    secrets_map = load_secrets()
    root_pass = secrets_map.get("host_root_pass") or ""
    kin_pass = secrets_map.get("kin_user_pass") or ""
    if not root_pass or not kin_pass:
        yield await _emit(
            "Refusing: store root and admin passwords for the new server first",
            err=True,
        )
        yield proto.event_done(2)
        return

    hacluster_pass = secrets_map.get("hacluster_pass") or ""
    if not hacluster_pass:
        yield await _emit(
            "Refusing: hacluster password missing from the vault. "
            "Attach peer must reuse the survivor cluster secret; refusing to mint a new one.",
            err=True,
        )
        yield proto.event_done(1)
        return

    chap_pass = secrets_map.get("chap_pass") or ""
    if not chap_pass:
        yield await _emit(
            "Refusing: iSCSI CHAP password missing from the vault. "
            "The live Observability target still expects the original secret; "
            "refusing to mint a new CHAP that would break initiator login.",
            err=True,
        )
        yield proto.event_done(1)
        return

    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield await _emit(
            "Refusing: another maintenance or cluster operation is already running.",
            err=True,
        )
        yield proto.event_done(1)
        return

    try:
        inv = render_add_host_inventory(plan)
        secrets = [p for p in (root_pass, kin_pass, hacluster_pass, chap_pass) if p]
        if any(s and s in inv for s in secrets):
            yield await _emit("internal error: inventory would contain a secret", err=True)
            yield proto.event_done(1)
            return

        _write_work_files(inv)
        inv_path = WORK_DIR / "inventory.yml"
        play = _playbook_path("playbooks/mail-add-host.yml")
        ansible_playbook = _ansible_bin()
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
            "KIN_HACLUSTER_PASSWORD": hacluster_pass,
            "KIN_CHAP_PASSWORD": chap_pass,
            "KIN_MAIL_DEPLOY_DIR": kin_mail_deploy_dir(),
            "ANSIBLE_CONFIG": str(WORK_DIR / "ansible.cfg"),
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
            "ANSIBLE_LOCAL_TEMP": str(WORK_DIR / ".ansible" / "tmp"),
        }
        argv = [ansible_playbook, "-i", str(inv_path), str(play)]
        yield await _emit(
            f"ansible-playbook mail-add-host.yml new={plan.new_name} survivor={plan.survivor_name}"
        )
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
            yield await _emit(f"add-host playbook failed (exit {exit_code})", err=True)
            yield proto.event_done(exit_code)
            return

        # Stage a full peer /etc/kin-mail/config BEFORE console sync / markers.
        # sync_peer_ha_console_state refuses TOPOLOGY-only skeletons.
        from .apply_config import parse_config
        from .orchestration import (
            _push_peer_install_files,
            _ssh_run,
            build_peer_install_payload,
        )

        peer_host = OrchHost(plan.new_name, plan.new_ip, "mail2")
        conf_path = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
        try:
            primary_values = parse_config(
                conf_path.read_text(encoding="utf-8") if conf_path.is_file() else ""
            )
        except OSError as exc:
            yield await _emit(
                f"Could not read survivor config for peer staging: {exc}",
                err=True,
            )
            yield proto.event_done(1)
            return

        ip_code, ip_text = await _ssh_run(
            peer_host,
            ssh_user,
            ssh_pass,
            secrets,
            "ip -4 -o addr show scope global",
            timeout=20,
            stdin_text=ssh_pass,
        )
        if ip_code != 0:
            yield await _emit(
                f"Could not read peer addresses for NET_IFACE (exit {ip_code})",
                err=True,
            )
            yield proto.event_done(1)
            return
        stage_code, stage_lines, payload = build_peer_install_payload(
            primary=primary_values,
            peer=peer_host,
            ip_addr_text=ip_text,
            cloudflare_text=None,
        )
        for line in stage_lines:
            yield await _emit(line, err=stage_code != 0)
        if stage_code != 0 or not payload.get("body"):
            yield await _emit(
                "Refusing peer console sync without a full peer config body.",
                err=True,
            )
            yield proto.event_done(1)
            return
        push_code, push_text = await _push_peer_install_files(
            peer_host, ssh_user, ssh_pass, secrets, payload
        )
        if push_code != 0:
            yield await _emit(
                f"Failed to stage peer /etc/kin-mail/config (exit {push_code}): "
                f"{push_text or 'no detail'}",
                err=True,
            )
            yield proto.event_done(1)
            return
        yield await _emit(
            f"Staged peer config SERVER_IP={payload.get('SERVER_IP')} "
            f"MAIL_HOST={payload.get('MAIL_HOST')}"
        )

        # Same peer console path as Build HA: install :9443 on the new peer.
        # Promote local TOPOLOGY=2vm / ha-setup-complete only AFTER peer sync
        # succeeds so a failed peer console leaves attach_peer_eligible True
        # for retry (survivor stays 1vm).
        yield await _emit("Deploying admin console to the new peer (:9443)")
        peer_ok, peer_notes = await sync_peer_ha_console_state(
            peer_host, ssh_user, ssh_pass, secrets
        )
        for note in peer_notes:
            yield await _emit(note, err=not peer_ok)
        if not peer_ok:
            yield await _emit(
                "Add-host playbook succeeded but peer console deploy failed. "
                "Survivor markers were not promoted (still 1vm) so Add Second "
                "Server can be retried after fixing the peer.",
                err=True,
            )
            yield proto.event_done(1)
            return

        try:
            text = ""
            if conf_path.is_file():
                text = conf_path.read_text(encoding="utf-8")
            values = parse_config(text)
            values, _changed = ensure_topology_2vm(values)
            values["PEER_HOST_NAME"] = plan.new_name
            values["PEER_HOST_IP"] = plan.new_ip
            values["OBSERVABILITY_VM_IP"] = plan.obs_ip
            values["CLUSTER_VIP_IP"] = plan.vip_ip
            write_config_file(values)
            write_topology_marker("2vm")
            mark_ha_setup_complete()
            clear_last_removed_peer()
            yield await _emit(
                f"console config restored to TOPOLOGY=2vm peer={plan.new_name} ({plan.new_ip})"
            )
        except OSError as exc:
            yield await _emit(
                f"peer console OK but local console config update failed: {exc}",
                err=True,
            )
            yield proto.event_done(1)
            return

        after = await gather_status()
        nodes = list(after.get("nodes") or [])
        from .remove_host import names_match

        if not any(names_match(n, plan.new_name) for n in nodes):
            yield await _emit(
                f"playbook finished but {plan.new_name} is not yet in Pacemaker nodelist "
                f"({', '.join(nodes) or 'empty'}); check pcs status / DRBD sync",
                err=True,
            )
            yield proto.event_done(1)
            return

        result = {
            "ok": True,
            "survivor": plan.survivor_name,
            "new_name": plan.new_name,
            "new_ip": plan.new_ip,
        }
        yield await _emit("ADD_HOST_JSON:" + json.dumps(result, separators=(",", ":")))
        yield await _emit(
            "Add host finished. Wait for DRBD UpToDate on the new Secondary, then "
            "use Move Master here if you want mail on that node."
        )
        yield proto.event_done(0)
    finally:
        release_maintenance_lock(lock_fh)
