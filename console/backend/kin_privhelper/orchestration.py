"""Console-driven 2-node HA Ansible sequence (Slice 2).

Reads host passwords from the privhelper vault only. Generated inventory never
contains secrets (passwords travel in env vars for ansible_password). Ansible
stdout/stderr is redacted before it is yielded to the console or written to
the deploy transcript.

join_mode=apply  — greenfield / idempotent resume of the full sequence.
join_mode=check  — per-node package/hardening/TLS install against the wizard
                   peer only; cluster-join playbooks (--check) against the
                   live Pacemaker nodelist. Never adds a non-member peer to
                   the live CIB/DRBD resource.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto
from .deploy_state import DEPLOY_LAST_LOG
from .provisioning_secrets import load_secrets

ANSIBLE_DIR = Path(
    os.environ.get("KIN_ANSIBLE_DIR", "/opt/kin-mail-console/ansible")
)
WORK_DIR = Path(
    os.environ.get(
        "KIN_ORCH_WORK_DIR",
        "/var/lib/kin-mail-privhelper/orchestration",
    )
)
IPV4_RE = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$"
)
NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252})$")


@dataclass(frozen=True)
class OrchHost:
    name: str
    ip: str
    iqn_suffix: str


def valid_ipv4(value: str) -> bool:
    return bool(IPV4_RE.match((value or "").strip()))


def valid_name(value: str) -> bool:
    name = (value or "").strip()
    return bool(NAME_RE.match(name)) and ".." not in name and "/" not in name


def redact_text(text: str, secrets: list[str]) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def _iqn_suffix(hostname: str) -> str:
    short = hostname.split(".")[0].lower()
    short = re.sub(r"[^a-z0-9-]", "", short) or "mail"
    return short[:32]


def render_inventory(
    mail_hosts: list[OrchHost],
    monitoring: OrchHost,
    *,
    vip_ip: str = "",
) -> str:
    """YAML inventory with env-lookup passwords — no secret values in the file."""
    lines = [
        "all:",
        "  vars:",
        "    ansible_connection: ssh",
        "    ansible_user: '{{ lookup(\"env\", \"KIN_ANSIBLE_USER\") }}'",
        "    ansible_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_PASSWORD\") }}'",
        "    ansible_become: true",
        "    ansible_become_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_BECOME_PASSWORD\") }}'",
        "    ansible_ssh_common_args: '-o StrictHostKeyChecking=accept-new'",
        "    corosync_qdevice_qnetd_ip: " + monitoring.ip,
        "    corosync_qdevice_qnetd_inventory_host: " + monitoring.name,
        "    iscsi_initiator_portal: \"" + monitoring.ip + ":3260\"",
        "    cluster_node_base_hacluster_password: '{{ lookup(\"env\", \"KIN_HACLUSTER_PASSWORD\") }}'",
        "    cluster_setup_name: kin-mail",
        # Proven HA layout — not the old loop-meta default. Preflight checks these.
        "    drbd_resource_disk: /dev/sdb1",
        "    drbd_resource_meta_disk: /dev/sdb2",
    ]
    if vip_ip:
        lines.append(f"    pacemaker_mail_stack_vip_ip: {vip_ip}")
    if len(mail_hosts) >= 2:
        lines.extend(
            [
                f"    drbd_resource_node_a_name: {mail_hosts[0].name}",
                f"    drbd_resource_node_a_address: {mail_hosts[0].ip}",
                f"    drbd_resource_node_b_name: {mail_hosts[1].name}",
                f"    drbd_resource_node_b_address: {mail_hosts[1].ip}",
            ]
        )
    lines.extend(
        [
            "  children:",
            "    monitoring:",
            "      hosts:",
            f"        {monitoring.name}:",
            f"          ansible_host: {monitoring.ip}",
            "    mail_nodes:",
            "      hosts:",
        ]
    )
    for host in mail_hosts:
        lines.append(f"        {host.name}:")
        lines.append(f"          ansible_host: {host.ip}")
        lines.append(
            f'          iscsi_initiator_name: "iqn.2026-08.site.gits-it:{host.iqn_suffix}"'
        )
    lines.append("")
    return "\n".join(lines)


def render_ansible_cfg(*, local_tmp: Path, roles_path: Path) -> str:
    return (
        "[defaults]\n"
        f"roles_path = {roles_path}\n"
        "interpreter_python = auto_silent\n"
        "host_key_checking = False\n"
        "retry_files_enabled = False\n"
        "stdout_callback = default\n"
        "display_skipped_hosts = True\n"
        "display_ok_hosts = True\n"
        f"local_tmp = {local_tmp}\n"
        "remote_tmp = /tmp/.ansible-kin-mail\n"
        "timeout = 60\n"
        "\n"
        "[privilege_escalation]\n"
        "become = True\n"
        "become_method = sudo\n"
        "become_timeout = 60\n"
        "\n"
        "[ssh_connection]\n"
        "pipelining = False\n"
    )


@dataclass(frozen=True)
class Step:
    step_id: str
    label: str
    playbook: str | None
    """ansible | remote_install | live_check"""
    kind: str
    check_on_join_check: bool = False
    tags: tuple[str, ...] = ()
    skip_tags: tuple[str, ...] = ()
    peer_only_on_join_check: bool = False
    # Cluster-join playbooks. join_mode=check never applies these to a
    # non-member peer — live_join_check dry-runs them against the live pair.
    cluster_join: bool = False


# Proven ha-build-01–15 order. remote_install is the OS+Zimbra step on the new
# node; skip_remote_install=1 leaves it to a prior local Deploy on that host.
STEPS: tuple[Step, ...] = (
    Step("peer_os_prep", "OS prep + Zimbra install on the new node", None, "remote_install"),
    Step(
        "os_hardening",
        "mail-os-hardening.yml (fail2ban + unattended-upgrades)",
        "playbooks/mail-os-hardening.yml",
        "ansible",
        peer_only_on_join_check=True,
    ),
    Step(
        "mon_qnetd",
        "mon-qnetd.yml (qdevice witness)",
        "playbooks/mon-qnetd.yml",
        "ansible",
        check_on_join_check=True,
    ),
    Step(
        "mail_cluster_setup",
        "mail-cluster-setup.yml (pcsd + pcs cluster setup)",
        "playbooks/mail-cluster-setup.yml",
        "ansible",
        cluster_join=True,
    ),
    Step(
        "mail_qdevice",
        "mail-qdevice.yml (qdevice client packages + TLS)",
        "playbooks/mail-qdevice.yml",
        "ansible",
        peer_only_on_join_check=True,
        # check: packages + TLS only. Do not pcs-reload, start qdevice, or
        # assert quorum on a host that is not in the live cluster.
        skip_tags=("configure", "service", "verify"),
    ),
    Step(
        "mon_iscsi",
        "mon-iscsi-target.yml (SBD LUN)",
        "playbooks/mon-iscsi-target.yml",
        "ansible",
        check_on_join_check=True,
    ),
    Step(
        "mail_fencing",
        "mail-fencing.yml (initiator packages, softdog, SBD agent)",
        "playbooks/mail-fencing.yml",
        "ansible",
        peer_only_on_join_check=True,
        # check: packages/softdog/SBD agent only. Never iSCSI-login to the
        # production SBD target, never pcs stonith create, never verify a session.
        skip_tags=("login", "pcs", "verify"),
    ),
    Step(
        "mail_drbd_install",
        "mail-drbd.yml tags=install (DRBD packages)",
        "playbooks/mail-drbd.yml",
        "ansible",
        peer_only_on_join_check=True,
        tags=("install",),
    ),
    Step(
        "mail_drbd_activate",
        "mail-drbd.yml resource activation",
        "playbooks/mail-drbd.yml",
        "ansible",
        check_on_join_check=True,
        skip_tags=("install",),
        cluster_join=True,
    ),
    Step(
        "mail_pacemaker_agents",
        "mail-pacemaker.yml tags=agents (OCF scripts)",
        "playbooks/mail-pacemaker.yml",
        "ansible",
        peer_only_on_join_check=True,
        tags=("agents",),
    ),
    Step(
        "mail_pacemaker_stack",
        "mail-pacemaker.yml stack (constraints, VIP, hostname remap)",
        "playbooks/mail-pacemaker.yml",
        "ansible",
        check_on_join_check=True,
        skip_tags=("agents",),
        cluster_join=True,
    ),
    Step(
        "live_join_check",
        "dry-run mail-drbd + mail-pacemaker against the live Pacemaker pair",
        None,
        "live_check",
    ),
)


def _load_config() -> dict[str, str]:
    from .apply_config import parse_config

    conf = Path(os.environ.get("KIN_MAIL_CONFIG", "/etc/kin-mail/config"))
    if not conf.is_file():
        return {}
    return parse_config(conf.read_text(encoding="utf-8"))


def _load_draft() -> dict[str, Any]:
    from kin_console.draft import load_draft

    return load_draft().model_dump()


def resolve_topology(
    *,
    draft: dict[str, Any],
    config: dict[str, str],
) -> tuple[OrchHost, OrchHost, OrchHost]:
    """Return (local, peer, monitoring)."""
    local_name = (
        str(config.get("MAIL_HOST") or "").strip()
        or os.uname().nodename
    )
    local_ip = str(config.get("SERVER_IP") or "").strip()
    if not valid_name(local_name):
        raise ValueError("MAIL_HOST / local hostname is not a valid node name")
    if local_ip and not valid_ipv4(local_ip):
        raise ValueError("SERVER_IP is not a valid IPv4 address")
    if not local_ip:
        raise ValueError("SERVER_IP is missing from /etc/kin-mail/config")

    peer_ip = str(draft.get("peer_host_ip") or config.get("PEER_HOST_IP") or "").strip()
    peer_name = str(draft.get("peer_host_name") or config.get("PEER_HOST_NAME") or "").strip()
    obs_ip = str(
        draft.get("observability_vm_ip") or config.get("OBSERVABILITY_VM_IP") or ""
    ).strip()

    if not valid_ipv4(peer_ip):
        raise ValueError("Second server IP is missing or not IPv4 (wizard topology)")
    if not valid_ipv4(obs_ip):
        raise ValueError("Observability VM IP is missing or not IPv4 (wizard topology)")
    if not peer_name:
        peer_name = f"mail-{peer_ip.replace('.', '-')}"
    if not valid_name(peer_name):
        raise ValueError("Second server hostname is not a valid node name")

    obs_name = str(os.environ.get("KIN_QNETD_INVENTORY_HOST") or "mon.gits-it.site").strip()
    if not valid_name(obs_name):
        obs_name = f"mon-{obs_ip.replace('.', '-')}"

    local = OrchHost(local_name, local_ip, _iqn_suffix(local_name))
    peer = OrchHost(peer_name, peer_ip, _iqn_suffix(peer_name))
    monitoring = OrchHost(obs_name, obs_ip, _iqn_suffix(obs_name))
    return local, peer, monitoring


def resolve_cluster_vip(
    *,
    draft: dict[str, Any],
    config: dict[str, str],
    local: OrchHost,
    peer: OrchHost,
    monitoring: OrchHost,
) -> str:
    """Floating mail VIP — must not collide with any node NIC."""
    vip = str(draft.get("cluster_vip_ip") or config.get("CLUSTER_VIP_IP") or "").strip()
    if not valid_ipv4(vip):
        raise ValueError("Cluster VIP is missing or not IPv4 (wizard topology)")
    collisions = {
        local.ip: "this server",
        peer.ip: "the second server",
        monitoring.ip: "the Observability VM",
    }
    if vip in collisions:
        raise ValueError(
            f"Cluster VIP {vip} is the same as {collisions[vip]} — "
            "the VIP must be a dedicated unused address"
        )
    return vip


def _write_work_files(inventory_text: str) -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(WORK_DIR, 0o700)
    local_tmp = WORK_DIR / ".ansible" / "tmp"
    local_tmp.mkdir(parents=True, exist_ok=True)
    inv = WORK_DIR / "inventory.yml"
    cfg = WORK_DIR / "ansible.cfg"
    inv.write_text(inventory_text, encoding="utf-8")
    cfg.write_text(
        render_ansible_cfg(local_tmp=local_tmp, roles_path=ANSIBLE_DIR / "roles"),
        encoding="utf-8",
    )
    os.chmod(inv, 0o600)
    os.chmod(cfg, 0o600)
    if "ansible_password:" in inventory_text and "lookup" not in inventory_text:
        raise RuntimeError("refusing to write an inventory that embeds ansible_password")
    return WORK_DIR


async def _stream_redacted(
    argv: list[str],
    *,
    cwd: Path,
    extra_env: dict[str, str],
    secrets: list[str],
    transcript: Path,
) -> AsyncIterator[dict[str, Any]]:
    from .commands import _stream_subprocess

    async for ev in _stream_subprocess(
        argv,
        cwd=cwd,
        extra_env=extra_env,
        transcript=transcript,
        transcript_reset=False,
        secrets=secrets,
    ):
        if ev.get("type") in ("stdout", "stderr") and ev.get("data"):
            ev = dict(ev)
            ev["data"] = redact_text(str(ev["data"]), secrets)
        yield ev


def _event_exit_code(ev: dict[str, Any]) -> int:
    """0 is success — do not treat it as missing via `or 1`."""
    if "exit_code" not in ev or ev.get("exit_code") is None:
        return 1
    return int(ev["exit_code"])


def _ansible_bin() -> str:
    candidates = [
        shutil.which("ansible-playbook") or "",
        "/opt/kin-mail-console/venv/bin/ansible-playbook",
        "/usr/bin/ansible-playbook",
    ]
    for found in candidates:
        if found and Path(found).is_file() and os.access(found, os.X_OK):
            return found
    raise FileNotFoundError(
        "ansible-playbook not found — install ansible-core on this console host"
    )


def _live_drbd_meta_disk() -> str:
    """Read this console host's live kin-zimbra.res meta-disk (rebuild uses a partition)."""
    path = Path("/etc/drbd.d/kin-zimbra.res")
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.findall(r"meta-disk\s+(\S+);", text)
    disks = [d.rstrip(";") for d in found if d and d != "internal"]
    if not disks:
        return ""
    disk = disks[0]
    if not re.match(r"^/dev/[A-Za-z0-9/_.+-]+$", disk):
        return ""
    return disk


def _playbook_path(rel: str) -> Path:
    path = (ANSIBLE_DIR / rel).resolve()
    try:
        path.relative_to(ANSIBLE_DIR.resolve())
    except ValueError as exc:
        raise RuntimeError("playbook path escapes ansible dir") from exc
    if not path.is_file():
        raise FileNotFoundError(f"playbook not found: {path}")
    return path


async def _ssh_run(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
    remote_cmd: str,
    *,
    timeout: int = 12,
) -> tuple[int, str]:
    """Password SSH with a fixed remote command. Password never in argv."""
    sshpass = shutil.which("sshpass")
    if not sshpass:
        return 127, "sshpass not installed"
    argv = [
        sshpass,
        "-e",
        "ssh",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        f"{user}@{host.ip}",
        remote_cmd,
    ]
    env = {**os.environ, "SSHPASS": password}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out_b, err_b = await proc.communicate()
    out = redact_text(out_b.decode("utf-8", errors="replace"), secrets)
    err = redact_text(err_b.decode("utf-8", errors="replace"), secrets)
    text = (out or err).strip()
    return int(proc.returncode or 0), text


async def _ssh_probe(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
) -> tuple[int, str]:
    """Return (exit, hostname) for a password SSH probe. Password never in argv."""
    return await _ssh_run(host, user, password, secrets, "hostname -f || hostname")


async def cmd_run_ha_orchestration(
    args: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    args = args or {}
    join_mode = str(args.get("join_mode") or "apply").strip().lower()
    if join_mode not in ("apply", "check"):
        yield proto.event_stderr("join_mode must be apply or check\n")
        yield proto.event_done(2)
        return
    skip_remote = str(args.get("skip_remote_install") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    try:
        DEPLOY_LAST_LOG.parent.mkdir(parents=True, exist_ok=True)
        DEPLOY_LAST_LOG.write_text("", encoding="utf-8")
        os.chmod(DEPLOY_LAST_LOG, 0o640)
    except OSError:
        pass

    def emit_line(text: str, err: bool = False) -> dict[str, Any]:
        line = text if text.endswith("\n") else text + "\n"
        try:
            with DEPLOY_LAST_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
        return proto.event_stderr(line) if err else proto.event_stdout(line)

    yield emit_line("=== HA orchestration Slice 2 ===")
    yield emit_line(f"join_mode={join_mode} skip_remote_install={skip_remote}")

    secrets_map = load_secrets()
    root_pass = secrets_map.get("host_root_pass") or ""
    kin_pass = secrets_map.get("kin_user_pass") or ""
    if not root_pass or not kin_pass:
        yield emit_line(
            "Refusing: provisioning vault is empty. Store host credentials in the wizard first.",
            err=True,
        )
        yield proto.event_done(2)
        return
    hacluster_pass = secrets_map.get("hacluster_pass") or ""
    if not hacluster_pass:
        import secrets as pysecrets

        from .provisioning_secrets import store_secrets

        hacluster_pass = pysecrets.token_urlsafe(24)
        try:
            store_secrets({"hacluster_pass": hacluster_pass})
        except ValueError as exc:
            yield emit_line(f"Refusing: could not persist hacluster password: {exc}", err=True)
            yield proto.event_done(2)
            return
        yield emit_line("generated hacluster pcs password (stored in vault, not logged)")
    secrets = [root_pass, kin_pass, hacluster_pass]

    try:
        draft = _load_draft()
        config = _load_config()
        local, peer, monitoring = resolve_topology(draft=draft, config=config)
        vip_ip = resolve_cluster_vip(
            draft=draft, config=config, local=local, peer=peer, monitoring=monitoring
        )
    except Exception as exc:  # noqa: BLE001
        yield emit_line(f"Refusing: {exc}", err=True)
        yield proto.event_done(2)
        return

    topology = str(draft.get("topology") or config.get("TOPOLOGY") or "").strip()
    if topology and topology not in ("2vm", "2"):
        yield emit_line(
            f"Refusing: topology is {topology!r}; HA orchestration requires 2 servers.",
            err=True,
        )
        yield proto.event_done(2)
        return

    yield emit_line(f"local={local.name} ({local.ip})")
    yield emit_line(f"peer={peer.name} ({peer.ip})")
    yield emit_line(f"observability={monitoring.name} ({monitoring.ip})")
    yield emit_line(f"cluster_vip={vip_ip}")

    from .maintenance import gather_status, parse_corosync_ring_addrs

    st = await gather_status()
    live_nodes = list(st.get("nodes") or [])
    if st.get("standby"):
        yield emit_line(
            f"Refusing: a node is in maintenance (standby={st['standby']}). "
            "Exit maintenance first.",
            err=True,
        )
        yield proto.event_done(1)
        return

    corosync_txt = ""
    conf = Path("/etc/corosync/corosync.conf")
    if conf.is_file():
        try:
            corosync_txt = conf.read_text(encoding="utf-8")
        except OSError:
            corosync_txt = ""
    ring = parse_corosync_ring_addrs(corosync_txt) if corosync_txt else {}
    peer_is_member = peer.name in live_nodes or peer.ip in set(ring.values())

    if join_mode == "apply" and live_nodes and not peer_is_member:
        yield emit_line(
            "Refusing join_mode=apply: the wizard peer is not a member of the live "
            f"Pacemaker cluster ({live_nodes}). Applying mail-cluster-setup / "
            "mail-drbd / mail-pacemaker would mutate the production CIB. Use "
            "join_mode=check for a dry-run of the join playbooks against the live "
            "pair, or set the wizard peer to the intended HA partner.",
            err=True,
        )
        yield proto.event_done(2)
        return

    if not ANSIBLE_DIR.is_dir():
        yield emit_line(
            f"Refusing: ansible tree missing at {ANSIBLE_DIR}. "
            "Re-run console/bootstrap.sh so playbooks are installed.",
            err=True,
        )
        yield proto.event_done(2)
        return

    try:
        ansible_playbook = _ansible_bin()
    except FileNotFoundError as exc:
        yield emit_line(str(exc), err=True)
        yield proto.event_done(2)
        return

    # Prefer the standing lab OS admin (cursor), then kin, then root.
    # Trying a missing user first trips fail2ban on a freshly hardened peer.
    ssh_user = "root"
    ssh_pass = root_pass
    for user, pwd in (("cursor", kin_pass), ("kin", kin_pass), ("root", root_pass)):
        code, ident = await _ssh_probe(peer, user, pwd, secrets)
        yield emit_line(f"ssh_probe user={user} exit={code} ident={ident.splitlines()[0] if ident else ''}")
        if code == 0:
            ssh_user = user
            ssh_pass = pwd
            break
    else:
        yield emit_line(
            "Refusing: could not SSH to the peer with stored provisioning credentials.",
            err=True,
        )
        yield proto.event_done(1)
        return

    mon_code, mon_ident = await _ssh_probe(monitoring, ssh_user, ssh_pass, secrets)
    yield emit_line(
        f"ssh_probe monitoring user={ssh_user} exit={mon_code} "
        f"ident={(mon_ident.splitlines()[0] if mon_ident else '')}"
    )
    if mon_code != 0:
        yield emit_line(
            "Refusing: could not SSH to the observability VM with the same credentials "
            "(needed for qnetd/iSCSI playbooks).",
            err=True,
        )
        yield proto.event_done(1)
        return

    from .ha_disk import (
        REMOTE_PROBE,
        collect_local_facts,
        combine_results,
        evaluate_facts,
        parse_remote_probe,
    )

    yield emit_line("Checking DRBD backing disks (read-only lsblk)…")
    local_res = evaluate_facts(
        collect_local_facts(),
        label=f"this server ({local.name})",
        require_zimbra_on_data=True,
    )
    peer_code, peer_blob = await _ssh_run(
        peer, ssh_user, ssh_pass, secrets, REMOTE_PROBE, timeout=15
    )
    if peer_code != 0:
        peer_res = {
            "ok": False,
            "label": f"second server ({peer.name})",
            "errors": [
                f"second server ({peer.name}): could not inspect disks "
                f"(ssh exit {peer_code})"
            ],
            "seen": [],
        }
    else:
        peer_res = evaluate_facts(
            parse_remote_probe(peer_blob),
            label=f"second server ({peer.name})",
            require_zimbra_on_data=True,
        )
    disk = combine_results(local_res, peer_res)
    if not disk["ok"]:
        for err in disk["errors"]:
            yield emit_line(f"Refusing: {err}", err=True)
        yield emit_line(str(disk.get("instructions") or ""), err=True)
        yield proto.event_done(2)
        return
    yield emit_line(
        f"DRBD disk preflight ok data={local_res.get('data_disk')} "
        f"meta={local_res.get('meta_disk')}"
    )

    ansible_env = {
        "HOME": str(WORK_DIR),
        "KIN_ANSIBLE_USER": ssh_user,
        "KIN_ANSIBLE_PASSWORD": ssh_pass,
        "KIN_ANSIBLE_BECOME_PASSWORD": ssh_pass,
        "KIN_HACLUSTER_PASSWORD": hacluster_pass,
        "ANSIBLE_CONFIG": str(WORK_DIR / "ansible.cfg"),
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOCAL_TEMP": str(WORK_DIR / ".ansible" / "tmp"),
    }

    peer_inv = render_inventory([peer], monitoring, vip_ip=vip_ip)
    full_inv = render_inventory([local, peer], monitoring, vip_ip=vip_ip)
    live_hosts = []
    for name in live_nodes:
        ip = ring.get(name, "")
        if not ip:
            # fall back to local/peer known IPs
            if name == local.name:
                ip = local.ip
            elif name == peer.name:
                ip = peer.ip
        if ip and valid_ipv4(ip) and valid_name(name):
            live_hosts.append(OrchHost(name, ip, _iqn_suffix(name)))
    live_inv = render_inventory(live_hosts, monitoring, vip_ip=vip_ip) if live_hosts else ""

    # Sanity: generated YAML must never contain the vault plaintext.
    for blob, label in ((peer_inv, "peer"), (full_inv, "full"), (live_inv, "live")):
        if any(s and s in blob for s in secrets):
            yield emit_line(f"internal error: {label} inventory would contain a secret", err=True)
            yield proto.event_done(1)
            return

    _write_work_files(peer_inv if join_mode == "check" else full_inv)

    failed_step = ""
    exit_code = 0
    for index, step in enumerate(STEPS, start=1):
        total = len(STEPS)
        if step.kind == "live_check" and join_mode != "check":
            yield emit_line(
                f"[{index}/{total}] SKIP {step.step_id} proof=skip "
                "(join_mode=apply — live dry-run is check-mode only)"
            )
            continue
        if step.kind == "remote_install" and skip_remote:
            yield emit_line(
                f"[{index}/{total}] SKIP {step.step_id} proof=skip "
                "(skip_remote_install=1) — OS prep + Zimbra on the new node "
                "is not run in this invocation."
            )
            continue
        if join_mode == "check" and step.cluster_join:
            yield emit_line(
                f"[{index}/{total}] SKIP {step.step_id} proof=skip "
                "cluster-join playbook is not applied to a non-member peer; "
                "live_join_check dry-runs it against the live Pacemaker pair only."
            )
            continue

        if join_mode == "check" and (
            step.check_on_join_check or step.kind == "live_check"
        ):
            proof = "dry-run"
            proof_why = (
                "ansible-playbook --check against live inventory; "
                "must not mutate Corosync/Pacemaker/DRBD/SBD membership"
            )
        elif join_mode == "check" and step.peer_only_on_join_check:
            proof = "apply"
            proof_why = (
                "per-node packages/config on the wizard peer only "
                "(not a cluster join)"
            )
        else:
            proof = "apply"
            proof_why = "full sequence (idempotent resume is a re-run from the top)"

        yield emit_line(
            f"[{index}/{total}] START {step.step_id}: {step.label} "
            f"proof={proof} — {proof_why}"
        )

        if step.kind == "remote_install":
            # Stream a remote full-install if the deploy tree already exists on the peer.
            remote_cmd = (
                "if [ -x /opt/kin-mail-deploy/install/kin-mail.sh ]; then "
                "sudo -n env KIN_CONSOLE_CONFIRMED=1 "
                "/opt/kin-mail-deploy/install/kin-mail.sh --full-install; "
                "else echo KIN_REMOTE_INSTALL_MISSING; exit 3; fi"
            )
            argv = [
                shutil.which("sshpass") or "sshpass",
                "-e",
                "ssh",
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "StrictHostKeyChecking=accept-new",
                f"{ssh_user}@{peer.ip}",
                remote_cmd,
            ]
            async for ev in _stream_redacted(
                argv,
                cwd=WORK_DIR,
                extra_env={"SSHPASS": ssh_pass, **ansible_env},
                secrets=secrets,
                transcript=DEPLOY_LAST_LOG,
            ):
                if ev.get("type") == "done":
                    exit_code = _event_exit_code(ev)
                else:
                    yield ev
            if exit_code != 0:
                failed_step = step.step_id
                yield emit_line(
                    f"[{index}/{total}] FAIL {step.step_id} exit={exit_code} — stopping. "
                    "No auto-retry, no auto-rollback.",
                    err=True,
                )
                break
            yield emit_line(f"[{index}/{total}] FINISH {step.step_id}")
            continue

        if step.kind == "live_check":
            if not live_inv or not live_hosts:
                yield emit_line(
                    f"[{index}/{total}] SKIP {step.step_id} — no live Pacemaker nodelist "
                    "to dry-run against."
                )
                continue
            (WORK_DIR / "inventory-live.yml").write_text(live_inv, encoding="utf-8")
            os.chmod(WORK_DIR / "inventory-live.yml", 0o600)
            yield emit_line(
                f"live dry-run inventory mail_nodes={[h.name for h in live_hosts]} "
                f"(peer {peer.name} is NOT in this inventory — will not join it)"
            )
            drbd_skip = ["--skip-tags", "install,verify"]
            meta = _live_drbd_meta_disk()
            if meta:
                drbd_skip.extend(["-e", f"drbd_resource_meta_disk={meta}"])
                yield emit_line(
                    f"live DRBD meta-disk from this host resource file: {meta} "
                    "(role default loop device is the old lab; verify tags skipped)"
                )
            for rel, extra in (
                ("playbooks/mail-cluster-setup.yml", ["--skip-tags", "auth,pcs,properties"]),
                ("playbooks/mail-drbd.yml", drbd_skip),
                ("playbooks/mail-pacemaker.yml", ["--skip-tags", "agents"]),
            ):
                play = _playbook_path(rel)
                argv = [
                    ansible_playbook,
                    "-i",
                    str(WORK_DIR / "inventory-live.yml"),
                    "--check",
                    *extra,
                    str(play),
                ]
                yield emit_line(f"RUN {' '.join(argv)}")
                async for ev in _stream_redacted(
                    argv,
                    cwd=ANSIBLE_DIR,
                    extra_env=ansible_env,
                    secrets=secrets,
                    transcript=DEPLOY_LAST_LOG,
                ):
                    if ev.get("type") == "done":
                        exit_code = _event_exit_code(ev)
                    else:
                        yield ev
                if exit_code != 0:
                    failed_step = f"{step.step_id}:{play.name}"
                    yield emit_line(
                        f"[{index}/{total}] FAIL {failed_step} exit={exit_code} — stopping. "
                        "No auto-retry, no auto-rollback.",
                        err=True,
                    )
                    break
            if failed_step:
                break
            yield emit_line(f"[{index}/{total}] FINISH {step.step_id}")
            continue

        # ansible step
        assert step.playbook
        play = _playbook_path(step.playbook)
        use_check = join_mode == "check" and step.check_on_join_check
        use_peer_only = join_mode == "check" and step.peer_only_on_join_check
        inv_path = WORK_DIR / ("inventory-peer.yml" if use_peer_only else "inventory.yml")
        if use_peer_only:
            inv_path.write_text(peer_inv, encoding="utf-8")
            os.chmod(inv_path, 0o600)
        else:
            body = full_inv if join_mode == "apply" else peer_inv
            inv_path.write_text(body, encoding="utf-8")
            os.chmod(inv_path, 0o600)

        argv = [ansible_playbook, "-i", str(inv_path)]
        if use_check:
            argv.append("--check")
        tags = list(step.tags)
        skip = list(step.skip_tags) if join_mode == "check" else []
        if tags:
            argv.extend(["--tags", ",".join(tags)])
        if skip:
            argv.extend(["--skip-tags", ",".join(skip)])
        argv.append(str(play))
        yield emit_line(f"RUN {' '.join(argv)}")
        async for ev in _stream_redacted(
            argv,
            cwd=ANSIBLE_DIR,
            extra_env=ansible_env,
            secrets=secrets,
            transcript=DEPLOY_LAST_LOG,
        ):
            if ev.get("type") == "done":
                exit_code = _event_exit_code(ev)
            else:
                yield ev
        if exit_code != 0:
            failed_step = step.step_id
            yield emit_line(
                f"[{index}/{total}] FAIL {step.step_id} playbook={play.name} "
                f"exit={exit_code} — stopping. No auto-retry, no auto-rollback.",
                err=True,
            )
            break
        yield emit_line(f"[{index}/{total}] FINISH {step.step_id}")

    if failed_step:
        yield emit_line(
            f"ORCH_FAILED step={failed_step} join_mode={join_mode}",
            err=True,
        )
        yield proto.event_done(exit_code or 1)
        return
    yield emit_line(f"ORCH_DONE join_mode={join_mode}")
    yield proto.event_done(0)
