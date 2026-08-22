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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from . import protocol as proto
from .deploy_state import (
    DEPLOY_LAST_LOG,
    plan_peer_ha_console_state,
    record_ha_orchestration_success,
    topology_marker_body,
)
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


def kin_mail_deploy_dir() -> str:
    """Controller path of the install/ tree (contains install/lib).

    Same env as commands.DEPLOY_DIR. On a deployed host ansible/ lives under
    /opt/kin-mail-console and is not a sibling of install/.
    """
    raw = (os.environ.get("KIN_MAIL_DEPLOY_DIR") or "/opt/kin-mail-deploy").strip()
    return raw or "/opt/kin-mail-deploy"


def live_cluster_blocks_apply(
    *,
    join_mode: str,
    live_nodes: list[str],
    peer_name: str,
    peer_ip: str,
    corosync_conf: str,
    status_text: str = "",
) -> bool:
    """True when join_mode=apply must stop rather than mutate a live CIB.

    A real Pacemaker cluster that does not already include the wizard peer
    must refuse. The Debian/Ubuntu package-default stub (cluster_name debian,
    loopback-only, no resources) is not a real cluster; detect.yml replaces it.
    """
    if join_mode != "apply" or not live_nodes:
        return False
    from .maintenance import parse_corosync_ring_addrs
    from .corosync_stub import is_harmless_package_stub_cluster

    ring = parse_corosync_ring_addrs(corosync_conf) if corosync_conf else {}
    if peer_name in live_nodes or peer_ip in set(ring.values()):
        return False
    if is_harmless_package_stub_cluster(
        corosync_conf, status_text=status_text, live_nodes=live_nodes
    ):
        return False
    return True


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


def ssh_password_candidates(kin_pass: str, root_pass: str) -> tuple[tuple[str, str], ...]:
    """SSH (username, password) pairs in try order.

    ``kin`` is the only default automation account. ``root`` is last-resort.
    Do not add extra usernames: a missing account fails first and trips fail2ban.
    """
    return (("kin", kin_pass), ("root", root_pass))


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
        # Proven rebuild uses /dev/sdb2. Do not keep the old-lab loop unit.
        '    pacemaker_agents_keep_meta_loop_unit: ""',
        # Console ansible/ is not next to install/. Roles copy helper scripts
        # from here (KIN_MAIL_DEPLOY_DIR), not via role_path/../../../install.
        '    kin_mail_deploy_dir: "' + kin_mail_deploy_dir() + '"',
    ]
    if mail_hosts:
        primary = mail_hosts[0]
        short = primary.name.split(".")[0].lower() or primary.name
        # Role defaults still say mail.gits-it.site. The hostname remap and
        # optional prefer pin must follow this pair, not the lab FQDN.
        lines.append(f"    pacemaker_mail_stack_zimbra_service_hostname: {primary.name}")
        lines.append(f"    pacemaker_mail_stack_zimbra_service_shortname: {short}")
        lines.append(f"    pacemaker_mail_stack_prefer_node: {primary.name}")
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
        # production SBD target, never write /etc/default/sbd (needs the LUN
        # by-id from login), never activate/recycle cluster, never pcs stonith
        # create, never verify a session.
        skip_tags=("login", "config", "activate", "pcs", "verify"),
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
    from .drbd_res import parse_first_meta_disk

    path = Path("/etc/drbd.d/kin-zimbra.res")
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return parse_first_meta_disk(text)


def live_join_check_playbooks(
    live_hosts: list[OrchHost],
    *,
    meta_disk: str = "",
) -> list[tuple[str, list[str]]]:
    """Playbook extras for live_join_check (always ansible-playbook --check).

    cluster_setup detect asserts len(mail_nodes)==2, so skip that playbook
    unless two live members exist. Skip disk_prep: the selector copy uses
    check_mode: false, and a --check run that does not also force cleanup
    would leave /var/tmp/kin-select-drbd-disk.py on the live MX. Skip
    install/verify/agents so the dry-run does not demand packages or a
    finished CIB on a pair that is not HA yet.
    """
    drbd_skip = ["--skip-tags", "install,verify,disk_prep"]
    if meta_disk:
        drbd_skip.extend(["-e", f"drbd_resource_meta_disk={meta_disk}"])
    runs: list[tuple[str, list[str]]] = []
    if len(live_hosts) >= 2:
        runs.append(
            (
                "playbooks/mail-cluster-setup.yml",
                ["--skip-tags", "auth,pcs,properties"],
            )
        )
    runs.append(("playbooks/mail-drbd.yml", drbd_skip))
    runs.append(
        ("playbooks/mail-pacemaker.yml", ["--skip-tags", "agents,verify"])
    )
    return runs


async def _probe_ha_disks(
    *,
    local: OrchHost,
    peer: OrchHost,
    ssh_user: str,
    ssh_pass: str,
    secrets: list[str],
) -> dict[str, Any]:
    from .ha_disk import (
        REMOTE_PROBE,
        collect_local_facts,
        combine_results,
        evaluate_facts,
        parse_remote_probe,
    )

    local_res = evaluate_facts(
        collect_local_facts(),
        label=f"this server ({local.name})",
        require_zimbra_on_data=True,
    )
    peer_code, peer_blob = await _ssh_run(
        peer, ssh_user, ssh_pass, secrets, REMOTE_PROBE, timeout=15
    )
    if peer_code != 0:
        peer_res: dict[str, Any] = {
            "ok": False,
            "label": f"second server ({peer.name})",
            "errors": [
                f"second server ({peer.name}): could not inspect disks "
                f"(ssh exit {peer_code})"
            ],
            "seen": [],
            "build_allowed": False,
        }
    else:
        peer_res = evaluate_facts(
            parse_remote_probe(peer_blob),
            label=f"second server ({peer.name})",
            require_zimbra_on_data=True,
        )
    return combine_results(local_res, peer_res)


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


# Inlined on the peer so this does not depend on a script already being there.
PEER_OS_PREP_READINESS_CMD = (
    "missing=\"\"; "
    '[ -x /opt/kin-mail-deploy/install/kin-mail.sh ] || missing="${missing}deploy-tree "; '
    'sudo -n true >/dev/null 2>&1 || missing="${missing}sudo-n "; '
    'if [ -n "$missing" ]; then echo "KIN_PEER_NOT_READY:${missing}"; exit 4; fi; '
    "echo KIN_PEER_READY"
)

_PEER_READY_LABELS = {
    "deploy-tree": (
        "missing /opt/kin-mail-deploy/install/kin-mail.sh "
        "(run console/bootstrap.sh on the peer)"
    ),
    "sudo-n": "cannot sudo -n (NOPASSWD sudo for this SSH user)",
}

_SCP_TMP_RE = re.compile(r"^/tmp/kin-mail-peer-[A-Za-z0-9._-]+$")
_INSTALL_DEST_RE = re.compile(
    r"^/(etc/kin-mail/(config|ha-setup-complete|setup-complete|topology)|etc/letsencrypt/[A-Za-z0-9._-]+)$"
)


def parse_peer_prep_readiness(text: str, exit_code: int) -> tuple[bool, list[str]]:
    """Parse the inlined peer readiness probe. Returns (ok, human missing items)."""
    blob = (text or "").strip()
    tokens: list[str] = []
    for line in blob.splitlines():
        line = line.strip()
        if line.startswith("KIN_PEER_NOT_READY:"):
            tokens.extend(t for t in line.split(":", 1)[1].split() if t)
    if exit_code == 0 and "KIN_PEER_READY" in blob and not tokens:
        return True, []
    if not tokens:
        tokens = ["unknown"]
    labels = [_PEER_READY_LABELS.get(t, t) for t in tokens]
    return False, labels


async def _scp_put(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
    local_path: Path,
    remote_tmp: str,
    *,
    timeout: int = 30,
) -> tuple[int, str]:
    """Password scp of a local file to a fixed /tmp name on the peer."""
    sshpass = shutil.which("sshpass")
    if not sshpass:
        return 127, "sshpass not installed"
    if not _SCP_TMP_RE.match(remote_tmp):
        return 2, "refusing unsafe scp destination"
    argv = [
        sshpass,
        "-e",
        "scp",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        str(local_path),
        f"{user}@{host.ip}:{remote_tmp}",
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


async def _install_staged_peer_file(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
    *,
    remote_tmp: str,
    dest: str,
    mode: str = "600",
) -> tuple[int, str]:
    """sudo-install a staged /tmp file to dest, then remove the temp copy."""
    if mode not in ("600", "644"):
        return 2, "refusing unsafe install mode"
    if not _SCP_TMP_RE.match(remote_tmp) or not _INSTALL_DEST_RE.match(dest):
        return 2, "refusing unsafe install path"
    dest_dir = str(Path(dest).parent)
    cmd = (
        f"sudo -n mkdir -p {dest_dir} && "
        f"sudo -n install -m {mode} -o root -g root {remote_tmp} {dest} && "
        f"rm -f {remote_tmp}"
    )
    return await _ssh_run(host, user, password, secrets, cmd, timeout=20)


def _write_secure_temp(body: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="kin-mail-peer-",
        suffix=".tmp",
        delete=False,
    )
    try:
        os.chmod(handle.name, 0o600)
        handle.write(body)
        handle.flush()
    finally:
        handle.close()
    return Path(handle.name)


async def _push_peer_text_file(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
    *,
    body: str,
    remote_tmp: str,
    dest: str,
    mode: str,
) -> tuple[int, str]:
    """scp + sudo-install a small text file onto the peer."""
    local = _write_secure_temp(body)
    try:
        code, text = await _scp_put(host, user, password, secrets, local, remote_tmp)
        if code != 0:
            return code, text or f"scp of {dest} failed"
        return await _install_staged_peer_file(
            host,
            user,
            password,
            secrets,
            remote_tmp=remote_tmp,
            dest=dest,
            mode=mode,
        )
    finally:
        try:
            local.unlink()
        except OSError:
            pass


PEER_CONSOLE_PROBE_CMD = (
    "echo KIN_PEER_STATE_BEGIN; "
    "if [ -f /etc/kin-mail/ha-setup-complete ]; then echo HA=1; else echo HA=0; fi; "
    "if [ -f /etc/kin-mail/setup-complete ]; then echo SETUP=1; else echo SETUP=0; fi; "
    "if [ -f /etc/kin-mail/config ]; then echo CFG=1; else echo CFG=0; fi; "
    "if [ -f /etc/kin-mail/topology ]; then echo TOPO=1; else echo TOPO=0; fi; "
    "echo KIN_PEER_STATE_END; "
    "echo KIN_PEER_HA_BEGIN; "
    "if [ -f /etc/kin-mail/ha-setup-complete ]; then cat /etc/kin-mail/ha-setup-complete; fi; "
    "echo KIN_PEER_HA_END; "
    "echo KIN_PEER_TOPO_BEGIN; "
    "if [ -f /etc/kin-mail/topology ]; then cat /etc/kin-mail/topology; fi; "
    "echo KIN_PEER_TOPO_END; "
    "echo KIN_PEER_CFG_BEGIN; "
    "if [ -f /etc/kin-mail/config ]; then sudo -n cat /etc/kin-mail/config; fi; "
    "echo KIN_PEER_CFG_END"
)


def _block_between(text: str, begin: str, end: str) -> str:
    start = text.find(begin)
    stop = text.find(end)
    if start < 0 or stop < 0 or stop < start:
        return ""
    return text[start + len(begin) : stop]


def parse_peer_console_probe(text: str) -> dict[str, Any]:
    """Parse PEER_CONSOLE_PROBE_CMD stdout. Config body is never logged by callers."""
    blob = text or ""
    state = _block_between(blob, "KIN_PEER_STATE_BEGIN", "KIN_PEER_STATE_END")
    flags: dict[str, str] = {}
    for line in state.splitlines():
        line = line.strip()
        if "=" in line:
            key, val = line.split("=", 1)
            flags[key.strip()] = val.strip()
    return {
        "ha_present": flags.get("HA") == "1",
        "setup_present": flags.get("SETUP") == "1",
        "cfg_present": flags.get("CFG") == "1",
        "topology_present": flags.get("TOPO") == "1",
        "ha_text": _block_between(blob, "KIN_PEER_HA_BEGIN", "KIN_PEER_HA_END"),
        "topology_text": _block_between(blob, "KIN_PEER_TOPO_BEGIN", "KIN_PEER_TOPO_END"),
        "config_text": _block_between(blob, "KIN_PEER_CFG_BEGIN", "KIN_PEER_CFG_END"),
        "ok": "KIN_PEER_STATE_BEGIN" in blob and "KIN_PEER_STATE_END" in blob,
    }


async def sync_peer_ha_console_state(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
) -> tuple[bool, list[str]]:
    """Write TOPOLOGY=2vm + HA (and missing setup) markers on the peer.

    Uses the same sshpass SSH/scp path as peer_os_prep. Does not log config
    contents. Returns (ok, operator-facing lines).
    """
    notes: list[str] = []
    code, blob = await _ssh_run(
        host, user, password, secrets, PEER_CONSOLE_PROBE_CMD, timeout=20
    )
    if code != 0:
        notes.append(
            f"Could not read console state on the peer (ssh exit {code}). "
            "That node will still show the from-scratch wizard until this is retried."
        )
        return False, notes
    probe = parse_peer_console_probe(blob)
    if not probe["ok"]:
        notes.append(
            "Peer console probe returned no KIN_PEER_STATE markers. "
            "That node will still show the from-scratch wizard until this is retried."
        )
        return False, notes
    plan = plan_peer_ha_console_state(
        config_text=str(probe.get("config_text") or ""),
        ha_marker_present=bool(probe.get("ha_present")),
        ha_marker_text=str(probe.get("ha_text") or ""),
        setup_marker_present=bool(probe.get("setup_present")),
        topology_marker_text=str(probe.get("topology_text") or ""),
    )
    if plan["noop"]:
        notes.append(
            "Peer console already has TOPOLOGY=2vm and completion markers; nothing to write."
        )
        return True, notes

    if plan["write_config"]:
        code, text = await _push_peer_text_file(
            host,
            user,
            password,
            secrets,
            body=str(plan["config_body"]),
            remote_tmp="/tmp/kin-mail-peer-config",
            dest="/etc/kin-mail/config",
            mode="600",
        )
        if code != 0:
            notes.append(
                f"Failed to write TOPOLOGY=2vm on the peer config (exit {code}). "
                "That node will still show the from-scratch wizard until this is retried. "
                "No auto-retry, no auto-rollback."
            )
            return False, notes
        notes.append("Wrote TOPOLOGY=2vm into the peer /etc/kin-mail/config")

    if plan["write_topology_marker"]:
        code, text = await _push_peer_text_file(
            host,
            user,
            password,
            secrets,
            body=str(plan["topology_marker_body"]),
            remote_tmp="/tmp/kin-mail-peer-topology",
            dest="/etc/kin-mail/topology",
            mode="644",
        )
        if code != 0:
            notes.append(
                f"Failed to write /etc/kin-mail/topology on the peer (exit {code}). "
                "That node will still show the from-scratch wizard until this is retried. "
                "No auto-retry, no auto-rollback."
            )
            return False, notes
        notes.append("Wrote topology marker on the peer")

    if plan["write_ha_marker"]:
        code, text = await _push_peer_text_file(
            host,
            user,
            password,
            secrets,
            body=str(plan["ha_marker_body"]),
            remote_tmp="/tmp/kin-mail-peer-ha-setup",
            dest="/etc/kin-mail/ha-setup-complete",
            mode="644",
        )
        if code != 0:
            notes.append(
                f"Failed to write /etc/kin-mail/ha-setup-complete on the peer (exit {code}). "
                "That node will still show the from-scratch wizard until this is retried. "
                "No auto-retry, no auto-rollback."
            )
            return False, notes
        notes.append("Wrote ha-setup-complete on the peer")

    if plan["write_setup_marker"]:
        code, text = await _push_peer_text_file(
            host,
            user,
            password,
            secrets,
            body=str(plan["setup_marker_body"]),
            remote_tmp="/tmp/kin-mail-peer-setup-complete",
            dest="/etc/kin-mail/setup-complete",
            mode="644",
        )
        if code != 0:
            notes.append(
                f"Failed to write /etc/kin-mail/setup-complete on the peer (exit {code}). "
                "That node will still show the from-scratch wizard until this is retried. "
                "No auto-retry, no auto-rollback."
            )
            return False, notes
        notes.append("Wrote setup-complete on the peer (was missing)")

    return True, notes


async def _push_peer_install_files(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
    payload: dict[str, str],
) -> tuple[int, str]:
    """scp + sudo install of staged config (and Cloudflare creds when present)."""
    local_cfg = _write_secure_temp(payload["body"])
    try:
        code, text = await _scp_put(
            host, user, password, secrets, local_cfg, "/tmp/kin-mail-peer-config"
        )
        if code != 0:
            return code, text or "scp of peer config failed"
        code, text = await _install_staged_peer_file(
            host,
            user,
            password,
            secrets,
            remote_tmp="/tmp/kin-mail-peer-config",
            dest="/etc/kin-mail/config",
        )
        if code != 0:
            return code, text or "install of /etc/kin-mail/config failed"
    finally:
        try:
            local_cfg.unlink()
        except OSError:
            pass
    topo = topology_marker_body(payload.get("topology") or "2vm")
    if not topo:
        return 2, "refusing empty topology marker"
    code, text = await _push_peer_text_file(
        host,
        user,
        password,
        secrets,
        body=topo,
        remote_tmp="/tmp/kin-mail-peer-topology",
        dest="/etc/kin-mail/topology",
        mode="644",
    )
    if code != 0:
        return code, text or "install of /etc/kin-mail/topology failed"
    cf_body = payload.get("cf_body")
    if not cf_body:
        return 0, ""
    cf_dest = payload.get("cf_dest") or "/etc/letsencrypt/cloudflare.ini"
    local_cf = _write_secure_temp(cf_body)
    try:
        code, text = await _scp_put(
            host, user, password, secrets, local_cf, "/tmp/kin-mail-peer-cf.ini"
        )
        if code != 0:
            return code, text or "scp of Cloudflare creds failed"
        code, text = await _install_staged_peer_file(
            host,
            user,
            password,
            secrets,
            remote_tmp="/tmp/kin-mail-peer-cf.ini",
            dest=cf_dest,
        )
        if code != 0:
            return code, text or f"install of {cf_dest} failed"
    finally:
        try:
            local_cf.unlink()
        except OSError:
            pass
    return 0, ""


def build_peer_install_payload(
    *,
    primary: dict[str, str],
    peer: OrchHost,
    ip_addr_text: str,
    cloudflare_text: str | None,
) -> tuple[int, list[str], dict[str, str]]:
    """Build the peer /etc/kin-mail/config body (and optional CF creds).

    ip_addr_text is `ip -4 -o addr show scope global` from the peer.
    cloudflare_text is the primary CF creds file, or None when unused.
    On success payload keys: body, MAIL_HOST, SERVER_IP, NET_IFACE, TLS_METHOD,
    cf_dest, cf_body (cf_body only when TLS_METHOD=cloudflare).
    Log lines never include passwords or API tokens.
    """
    from .apply_config import format_config, iface_for_ipv4, peer_install_config

    lines: list[str] = []
    iface = iface_for_ipv4(ip_addr_text, peer.ip)
    if not iface:
        lines.append(
            f"Could not detect NET_IFACE on the peer for SERVER_IP={peer.ip} "
            "(ip -4 -o addr had no matching inet line)."
        )
        return 2, lines, {}
    try:
        values = peer_install_config(
            primary,
            peer_host=peer.name,
            peer_ip=peer.ip,
            peer_iface=iface,
        )
    except ValueError as exc:
        lines.append(f"Refusing to stage peer config: {exc}")
        return 2, lines, {}
    tls = str(values.get("TLS_METHOD") or "cloudflare").strip()
    cf_dest = str(values.get("CF_CREDS") or "/etc/letsencrypt/cloudflare.ini").strip()
    payload: dict[str, str] = {
        "body": format_config(values),
        "MAIL_HOST": values["MAIL_HOST"],
        "SERVER_IP": values["SERVER_IP"],
        "NET_IFACE": values["NET_IFACE"],
        "MAIL_DOMAIN": str(values.get("MAIL_DOMAIN") or ""),
        "TLS_METHOD": tls,
        "cf_dest": cf_dest,
        "topology": str(values.get("TOPOLOGY") or "2vm"),
    }
    if tls == "cloudflare":
        if not _INSTALL_DEST_RE.match(cf_dest):
            lines.append(f"Refusing CF_CREDS path {cf_dest}")
            return 2, lines, {}
        if cloudflare_text is None or not str(cloudflare_text).strip():
            lines.append(
                "TLS_METHOD=cloudflare but this host has no usable "
                f"{cf_dest}; 04-tls-dkim.sh would prompt for a token on the peer."
            )
            return 2, lines, {}
        if "PASTE" in cloudflare_text:
            lines.append(
                f"{cf_dest} still contains PASTE; 04-tls-dkim.sh would prompt on the peer."
            )
            return 2, lines, {}
        payload["cf_body"] = cloudflare_text
    lines.append(
        f"peer config MAIL_HOST={payload['MAIL_HOST']} "
        f"SERVER_IP={payload['SERVER_IP']} NET_IFACE={payload['NET_IFACE']} "
        f"MAIL_DOMAIN={payload['MAIL_DOMAIN']} TLS_METHOD={tls}"
    )
    return 0, lines, payload


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
    from .corosync_stub import is_harmless_package_stub_cluster

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
    status_text = str((st.get("raw") or {}).get("crm") or "")

    if live_cluster_blocks_apply(
        join_mode=join_mode,
        live_nodes=live_nodes,
        peer_name=peer.name,
        peer_ip=peer.ip,
        corosync_conf=corosync_txt,
        status_text=status_text,
    ):
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

    if (
        join_mode == "apply"
        and live_nodes
        and not peer_is_member
        and is_harmless_package_stub_cluster(
            corosync_txt, status_text=status_text, live_nodes=live_nodes
        )
    ):
        yield emit_line(
            "Live Pacemaker nodelist is the Debian/Ubuntu package-default stub "
            f"({live_nodes}). Not a real cluster; mail-cluster-setup will stop it "
            "and write kin-mail."
        )

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

    # kin first, root last. A missing username first trips fail2ban on a
    # freshly hardened peer.
    ssh_user = "root"
    ssh_pass = root_pass
    for user, pwd in ssh_password_candidates(kin_pass, root_pass):
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

    yield emit_line("Checking DRBD backing disks (read-only lsblk)…")
    disk = await _probe_ha_disks(
        local=local,
        peer=peer,
        ssh_user=ssh_user,
        ssh_pass=ssh_pass,
        secrets=secrets,
    )
    for item in disk.get("will_auto_partition") or []:
        yield emit_line(str(item.get("message") or item))
    if not disk.get("build_allowed"):
        for err in disk.get("errors") or []:
            yield emit_line(f"Refusing: {err}", err=True)
        yield emit_line(str(disk.get("instructions") or ""), err=True)
        yield proto.event_done(2)
        return
    need_disk_prep = bool(disk.get("will_auto_partition")) and not bool(disk.get("ok"))
    if disk.get("ok"):
        yield emit_line(
            f"DRBD disk preflight ok data={local.name} / {peer.name}"
        )
    else:
        yield emit_line(
            "DRBD data partition missing — unique blank spare disk identified; "
            "will auto-partition, then re-check before DRBD."
        )

    ansible_env = {
        "HOME": str(WORK_DIR),
        "KIN_ANSIBLE_USER": ssh_user,
        "KIN_ANSIBLE_PASSWORD": ssh_pass,
        "KIN_ANSIBLE_BECOME_PASSWORD": ssh_pass,
        "KIN_HACLUSTER_PASSWORD": hacluster_pass,
        "KIN_MAIL_DEPLOY_DIR": kin_mail_deploy_dir(),
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

    if need_disk_prep:
        play = _playbook_path("playbooks/mail-drbd.yml")
        inv_path = WORK_DIR / "inventory.yml"
        argv = [ansible_playbook, "-i", str(inv_path), "--tags", "disk_prep"]
        if join_mode == "check":
            argv.append("--check")
        argv.append(str(play))
        yield emit_line(
            "Auto-partition: unique blank spare disk — "
            + ("dry-run only (--check), no writes" if join_mode == "check" else "writing GPT")
        )
        yield emit_line(f"RUN {' '.join(argv)}")
        prep_exit = 0
        async for ev in _stream_redacted(
            argv,
            cwd=ANSIBLE_DIR,
            extra_env=ansible_env,
            secrets=secrets,
            transcript=DEPLOY_LAST_LOG,
        ):
            if ev.get("type") == "done":
                prep_exit = _event_exit_code(ev)
            else:
                yield ev
        if prep_exit != 0:
            yield emit_line(
                f"Refusing: DRBD disk prep exited {prep_exit}. No auto-retry.",
                err=True,
            )
            yield proto.event_done(prep_exit or 1)
            return
        if join_mode == "apply":
            yield emit_line("Re-checking DRBD backing disks after auto-partition…")
            disk = await _probe_ha_disks(
                local=local,
                peer=peer,
                ssh_user=ssh_user,
                ssh_pass=ssh_pass,
                secrets=secrets,
            )
            if not disk.get("ok"):
                for err in disk.get("errors") or []:
                    yield emit_line(f"Refusing: {err}", err=True)
                yield emit_line(str(disk.get("instructions") or ""), err=True)
                yield proto.event_done(2)
                return
            yield emit_line("DRBD disk preflight ok after auto-partition")
        else:
            yield emit_line(
                "check mode did not write partitions; continuing dry-run with the planned disk."
            )

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
            ready_code, ready_text = await _ssh_run(
                peer,
                ssh_user,
                ssh_pass,
                secrets,
                PEER_OS_PREP_READINESS_CMD,
                timeout=20,
            )
            ready_ok, ready_missing = parse_peer_prep_readiness(ready_text, ready_code)
            if not ready_ok:
                yield emit_line(
                    "Peer is not ready for remote full-install. Fix all of these, then retry:",
                    err=True,
                )
                for item in ready_missing:
                    yield emit_line(f"  - {item}", err=True)
                failed_step = step.step_id
                exit_code = ready_code or 4
                yield emit_line(
                    f"[{index}/{total}] FAIL {step.step_id} exit={exit_code}; stopping. "
                    "No auto-retry, no auto-rollback.",
                    err=True,
                )
                break

            ip_code, ip_text = await _ssh_run(
                peer,
                ssh_user,
                ssh_pass,
                secrets,
                "ip -4 -o addr show scope global",
                timeout=15,
            )
            if ip_code != 0:
                yield emit_line(
                    f"Could not read peer addresses (ssh exit {ip_code}): {ip_text}",
                    err=True,
                )
                failed_step = step.step_id
                exit_code = ip_code or 1
                yield emit_line(
                    f"[{index}/{total}] FAIL {step.step_id} exit={exit_code}; stopping. "
                    "No auto-retry, no auto-rollback.",
                    err=True,
                )
                break

            cf_path = str(config.get("CF_CREDS") or "/etc/letsencrypt/cloudflare.ini")
            cf_text: str | None = None
            if str(config.get("TLS_METHOD") or "cloudflare").strip() == "cloudflare":
                try:
                    cf_text = Path(cf_path).read_text(encoding="utf-8")
                except OSError:
                    cf_text = None

            stage_code, stage_lines, payload = build_peer_install_payload(
                primary=config,
                peer=peer,
                ip_addr_text=ip_text,
                cloudflare_text=cf_text,
            )
            for line in stage_lines:
                yield emit_line(line, err=stage_code != 0)
            if stage_code != 0:
                failed_step = step.step_id
                exit_code = stage_code
                yield emit_line(
                    f"[{index}/{total}] FAIL {step.step_id} exit={exit_code}; stopping. "
                    "No auto-retry, no auto-rollback.",
                    err=True,
                )
                break

            yield emit_line(
                "Staging /etc/kin-mail/config on the peer "
                "(host identity detected on that machine; domain/TLS/AD copied)."
            )
            push_code, push_text = await _push_peer_install_files(
                peer,
                ssh_user,
                ssh_pass,
                secrets,
                payload,
            )
            if push_code != 0:
                yield emit_line(
                    f"Failed to install staged config on the peer (exit {push_code}): {push_text}",
                    err=True,
                )
                failed_step = step.step_id
                exit_code = push_code
                yield emit_line(
                    f"[{index}/{total}] FAIL {step.step_id} exit={exit_code}; stopping. "
                    "No auto-retry, no auto-rollback.",
                    err=True,
                )
                break
            yield emit_line(
                "Peer /etc/kin-mail/config is in place; starting remote full-install."
            )

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
            meta = _live_drbd_meta_disk()
            if meta:
                yield emit_line(
                    f"live DRBD meta-disk from this host resource file: {meta} "
                    "(role default loop device is the old lab; verify tags skipped)"
                )
            for rel, extra in live_join_check_playbooks(live_hosts, meta_disk=meta):
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
    if join_mode == "apply":
        if record_ha_orchestration_success(join_mode=join_mode):
            yield emit_line("ha-setup-complete marker written on this node")
        yield emit_line("Syncing console deployed state to the peer")
        peer_ok, peer_notes = await sync_peer_ha_console_state(
            peer, ssh_user, ssh_pass, secrets
        )
        for note in peer_notes:
            yield emit_line(note, err=not peer_ok)
        if not peer_ok:
            yield emit_line(
                f"ORCH_FAILED step=peer_console_state join_mode={join_mode}",
                err=True,
            )
            yield proto.event_done(1)
            return
    yield emit_line(f"ORCH_DONE join_mode={join_mode}")
    yield proto.event_done(0)
