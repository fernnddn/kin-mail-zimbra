"""Console Add/Remove Observability commands (SSE). Distinct from mail-node host ops."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto
from .observability_lifecycle import (
    plan_add_observability,
    plan_remove_observability,
    render_observability_inventory,
    sbd_stop_order,
)
from .orchestration import OrchHost, _iqn_suffix, valid_ipv4


async def _emit(text: str, *, err: bool = False) -> dict[str, Any]:
    from .maintenance import _emit as maint_emit

    return await maint_emit(text, err=err)


def _mail_hosts(st: dict[str, Any], config: dict[str, str]) -> list[OrchHost]:
    addrs = dict(st.get("addrs") or {})
    local_name = str(config.get("MAIL_HOST") or st.get("local_host") or "").strip()
    local_ip = str(config.get("SERVER_IP") or "").strip()
    peer_name = str(config.get("PEER_HOST_NAME") or "").strip()
    peer_ip = str(config.get("PEER_HOST_IP") or "").strip()
    if local_name and local_ip:
        addrs.setdefault(local_name, local_ip)
    if peer_name and peer_ip:
        addrs.setdefault(peer_name, peer_ip)

    names: list[str] = []
    for group in (
        [local_name, peer_name],
        st.get("nodes") or [],
        st.get("offline") or [],
        st.get("stale_peers") or [],
    ):
        for name in group:
            n = str(name or "").strip()
            if n and n not in names:
                names.append(n)

    hosts: list[OrchHost] = []
    seen_ip: set[str] = set()
    for name in names:
        ip = str(addrs.get(name) or "").strip()
        if not valid_ipv4(ip) or ip in seen_ip:
            continue
        seen_ip.add(ip)
        hosts.append(OrchHost(name, ip, _iqn_suffix(name)))
    return hosts


def write_observability_vm_ip(ip: str) -> None:
    from .apply_config import format_config, parse_config

    path = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
    values: dict[str, str] = {}
    if path.is_file():
        values = parse_config(path.read_text(encoding="utf-8"))
    values["OBSERVABILITY_VM_IP"] = (ip or "").strip()
    from .deploy_state import ensure_kin_mail_dir

    ensure_kin_mail_dir(path.parent)
    path.write_text(format_config(values), encoding="utf-8")


def probe_payload(plan: Any, *, reachable: bool, ssh_ok: bool) -> dict[str, Any]:
    return {
        "configured_ip": getattr(plan, "configured_ip", "") or getattr(plan, "new_ip", ""),
        "status": plan.status,
        "reachable": reachable,
        "ssh_ok": ssh_ok,
        "errors": list(plan.errors),
        "notes": list(plan.notes),
    }


async def _ssh_login(host: OrchHost, user_pass_pairs: list[tuple[str, str]]) -> tuple[str, str] | None:
    from .orchestration import _ssh_probe

    if not host.ip:
        return None
    secrets = [p for _, p in user_pass_pairs if p]
    for user, pwd in user_pass_pairs:
        if not pwd:
            continue
        code, _ident = await _ssh_probe(host, user, pwd, secrets)
        if code == 0:
            return user, pwd
    return None


async def _run_playbook(
    *,
    playbook_rel: str,
    inventory: str,
    extra_vars: list[str],
    extra_env: dict[str, str],
    secrets: list[str],
) -> AsyncIterator[dict[str, Any]]:
    from .orchestration import (
        ANSIBLE_DIR,
        WORK_DIR,
        _ansible_bin,
        _playbook_path,
        _stream_redacted,
        _write_work_files,
        kin_mail_deploy_dir,
    )
    from .deploy_state import DEPLOY_LAST_LOG

    work = _write_work_files(inventory)
    playbook = _playbook_path(playbook_rel)
    argv = [
        _ansible_bin(),
        "-i",
        str(work / "inventory.yml"),
        str(playbook),
        *extra_vars,
    ]
    env = {
        "HOME": str(WORK_DIR),
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "KIN_MAIL_DEPLOY_DIR": kin_mail_deploy_dir(),
        **extra_env,
    }
    yield await _emit(f"ansible-playbook {playbook_rel}")
    exit_code = 1
    async for ev in _stream_redacted(
        argv,
        cwd=ANSIBLE_DIR,
        extra_env=env,
        secrets=secrets,
        transcript=DEPLOY_LAST_LOG,
    ):
        if ev.get("type") == "done":
            exit_code = int(ev.get("exit_code") if ev.get("exit_code") is not None else 1)
        else:
            yield ev
    yield {"type": "_playbook_exit", "exit_code": exit_code}


async def cmd_remove_observability(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .maintenance import gather_status, release_maintenance_lock, try_lock_maintenance
    from .orchestration import _load_config, ssh_password_candidates
    from .provisioning_secrets import load_secrets

    args = args or {}
    op = str(args.get("op") or "apply").strip().lower()
    if op not in ("probe", "apply"):
        yield proto.event_stderr("op must be probe or apply\n")
        yield proto.event_done(2)
        return

    yield await _emit(f"=== remove_observability {op} ===")
    st = await gather_status()
    obs = st.get("observability") or {}
    identity = str(obs.get("ip") or "")
    reachable = bool(obs.get("reachable"))
    secrets_map = load_secrets()
    root_pass = secrets_map.get("host_root_pass") or ""
    kin_pass = secrets_map.get("kin_user_pass") or ""
    ssh_ok = False
    if identity and valid_ipv4(identity):
        ssh_ok = (
            await _ssh_login(
                OrchHost("observability", identity, "obs"),
                list(ssh_password_candidates(kin_pass, root_pass)),
            )
            is not None
        )
    plan = plan_remove_observability(
        identity=identity,
        reachable=reachable,
        ssh_ok=ssh_ok,
    )
    yield await _emit(
        "REMOVE_OBS_PROBE_JSON:"
        + json.dumps(probe_payload(plan, reachable=reachable, ssh_ok=ssh_ok), separators=(",", ":"))
    )
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

    config = _load_config()
    mail = _mail_hosts(st, config)
    if len(mail) < 2:
        yield await _emit(
            "Refusing: need both mail nodes in config/corosync to disarm fencing",
            err=True,
        )
        yield proto.event_done(2)
        return

    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield await _emit("Refusing: another cluster operation is already running.", err=True)
        yield proto.event_done(1)
        return

    promoted_before = str(st.get("promoted") or "")
    try:
        local_name = str(st.get("local_host") or config.get("MAIL_HOST") or "")
        inv = render_observability_inventory(
            mail_hosts=[(h.name, h.ip, h.iqn_suffix) for h in mail],
            obs_name="observability-retired",
            obs_ip=identity or "0.0.0.0",
            retired_ip=identity,
            local_mail_name=local_name,
        )
        order = sbd_stop_order(
            promoted=promoted_before,
            hosts=[h.name for h in mail],
        )
        extra = [
            "-e",
            f"observability_promoted={promoted_before}",
            "-e",
            "observability_sbd_stop_order=" + ",".join(order),
        ]
        extra_env = {
            "KIN_ANSIBLE_USER": "kin",
            "KIN_ANSIBLE_PASSWORD": kin_pass,
            "KIN_ANSIBLE_BECOME_PASSWORD": kin_pass or root_pass,
        }
        secrets = [p for p in (root_pass, kin_pass) if p]
        play_exit = 1
        async for ev in _run_playbook(
            playbook_rel="playbooks/mail-remove-observability.yml",
            inventory=inv,
            extra_vars=extra,
            extra_env=extra_env,
            secrets=secrets,
        ):
            if ev.get("type") == "_playbook_exit":
                play_exit = int(ev.get("exit_code") if ev.get("exit_code") is not None else 1)
            else:
                yield ev
        if play_exit != 0:
            yield await _emit(
                f"remove-observability playbook failed (exit {play_exit})",
                err=True,
            )
            yield proto.event_done(play_exit)
            return

        write_observability_vm_ip("")
        yield await _emit("cleared OBSERVABILITY_VM_IP in local config")

        after = await gather_status()
        after_obs = after.get("observability") or {}
        if str(after_obs.get("status") or "") != "absent":
            yield await _emit(
                "cleanup finished but Observability identity is still present "
                f"(status={after_obs.get('status')} ip={after_obs.get('ip')})",
                err=True,
            )
            yield proto.event_done(1)
            return
        promoted_after = str(after.get("promoted") or "")
        if promoted_before and promoted_after and promoted_before != promoted_after:
            yield await _emit(
                f"mail Promoted moved during remove ({promoted_before} -> {promoted_after})",
                err=True,
            )
            yield proto.event_done(1)
            return
        yield await _emit(
            f"verified: Observability absent, Promoted still {promoted_after or promoted_before}"
        )
        yield proto.event_done(0)
    finally:
        release_maintenance_lock(lock_fh)


async def cmd_add_observability(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .maintenance import gather_status, release_maintenance_lock, try_lock_maintenance
    from .observability_secrets import clear_observability_secrets, load_observability_secrets
    from .orchestration import _load_config, ssh_password_candidates
    from .provisioning_secrets import load_secrets
    from .qdevice_status import quorum_votes_match_rebuild

    args = args or {}
    op = str(args.get("op") or "apply").strip().lower()
    if op not in ("probe", "apply"):
        yield proto.event_stderr("op must be probe or apply\n")
        yield proto.event_done(2)
        return

    yield await _emit(f"=== add_observability {op} ===")
    stored = load_observability_secrets()
    new_ip = str(stored.get("ip") or args.get("ip") or "").strip()
    hostname = str(stored.get("hostname") or args.get("hostname") or "").strip()
    obs_root = stored.get("root_pass") or ""
    obs_kin = stored.get("kin_user_pass") or ""
    has_creds = bool(obs_root and obs_kin)

    st = await gather_status()
    obs = st.get("observability") or {}
    plan = plan_add_observability(
        current_identity=str(obs.get("ip") or ""),
        current_reachable=bool(obs.get("reachable")),
        new_ip=new_ip,
        hostname=hostname,
        has_credentials=has_creds,
    )
    yield await _emit(
        "ADD_OBS_PROBE_JSON:"
        + json.dumps(
            {
                "new_ip": plan.new_ip,
                "hostname": plan.hostname,
                "inventory_name": plan.inventory_name,
                "status": plan.status,
                "errors": list(plan.errors),
                "notes": list(plan.notes),
            },
            separators=(",", ":"),
        )
    )
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

    obs_host = OrchHost(plan.inventory_name, plan.new_ip, _iqn_suffix(plan.inventory_name))
    login = await _ssh_login(obs_host, list(ssh_password_candidates(obs_kin, obs_root)))
    if not login:
        yield await _emit(
            "Refusing: could not SSH to the new Observability VM with the supplied credentials",
            err=True,
        )
        yield proto.event_done(2)
        return
    obs_user, obs_pass = login

    config = _load_config()
    mail = _mail_hosts(st, config)
    if len(mail) < 2:
        yield await _emit("Refusing: need both mail nodes to reconnect qdevice and SBD", err=True)
        yield proto.event_done(2)
        return

    mail_secrets = load_secrets()
    mail_root = mail_secrets.get("host_root_pass") or ""
    mail_kin = mail_secrets.get("kin_user_pass") or ""
    if not mail_root or not mail_kin:
        yield await _emit(
            "Refusing: mail-node provisioning vault is empty; store host credentials first",
            err=True,
        )
        yield proto.event_done(2)
        return

    lock_fh = try_lock_maintenance()
    if lock_fh is None:
        yield await _emit("Refusing: another cluster operation is already running.", err=True)
        yield proto.event_done(1)
        return

    promoted_before = str(st.get("promoted") or "")
    try:
        local_name = str(st.get("local_host") or config.get("MAIL_HOST") or "")
        inv = render_observability_inventory(
            mail_hosts=[(h.name, h.ip, h.iqn_suffix) for h in mail],
            obs_name=plan.inventory_name,
            obs_ip=plan.new_ip,
            retired_ip="",
            local_mail_name=local_name,
            force_tls_reinit=True,
            expect_fresh_sbd=True,
        )
        extra = [
            "-e",
            f"observability_promoted={promoted_before}",
            "-e",
            "observability_sbd_stop_order="
            + ",".join(sbd_stop_order(promoted=promoted_before, hosts=[h.name for h in mail])),
        ]
        extra_env = {
            "KIN_ANSIBLE_USER": "kin",
            "KIN_ANSIBLE_PASSWORD": mail_kin,
            "KIN_ANSIBLE_BECOME_PASSWORD": mail_kin or mail_root,
            "KIN_OBS_ANSIBLE_USER": obs_user,
            "KIN_OBS_ANSIBLE_PASSWORD": obs_pass,
            "KIN_OBS_ANSIBLE_BECOME_PASSWORD": obs_pass,
        }
        secrets = [p for p in (mail_root, mail_kin, obs_root, obs_kin) if p]
        play_exit = 1
        async for ev in _run_playbook(
            playbook_rel="playbooks/mail-add-observability.yml",
            inventory=inv,
            extra_vars=extra,
            extra_env=extra_env,
            secrets=secrets,
        ):
            if ev.get("type") == "_playbook_exit":
                play_exit = int(ev.get("exit_code") if ev.get("exit_code") is not None else 1)
            else:
                yield ev
        if play_exit != 0:
            yield await _emit(
                f"add-observability playbook failed (exit {play_exit})",
                err=True,
            )
            yield proto.event_done(play_exit)
            return

        write_observability_vm_ip(plan.new_ip)
        yield await _emit(f"set OBSERVABILITY_VM_IP={plan.new_ip}")

        after = await gather_status()
        after_obs = after.get("observability") or {}
        quorum_txt = str((after.get("raw") or {}).get("quorum") or "")
        if str(after_obs.get("status") or "") != "healthy":
            yield await _emit(
                "rebuild finished but Observability is not healthy "
                f"(status={after_obs.get('status')} reachable={after_obs.get('reachable')})",
                err=True,
            )
            yield proto.event_done(1)
            return
        if not after.get("qdevice_ok"):
            yield await _emit("qdevice is not voting after rebuild", err=True)
            yield proto.event_done(1)
            return
        if not quorum_votes_match_rebuild(quorum_txt):
            yield await _emit(
                "quorum is not expected 3 / total 3 / quorum 2 after rebuild",
                err=True,
            )
            yield proto.event_done(1)
            return
        promoted_after = str(after.get("promoted") or "")
        if promoted_before and promoted_after and promoted_before != promoted_after:
            yield await _emit(
                f"mail Promoted moved during add ({promoted_before} -> {promoted_after})",
                err=True,
            )
            yield proto.event_done(1)
            return
        clear_observability_secrets()
        yield await _emit(
            "verified: qdevice Connected path (votes ok), expected 3 / total 3 / "
            f"quorum 2, Promoted still {promoted_after or promoted_before}"
        )
        yield proto.event_done(0)
    finally:
        release_maintenance_lock(lock_fh)
