"""Console-driven 2-node HA Ansible sequence (Slice 2).

Reads host passwords from the privhelper vault only. Generated inventory never
contains secrets (passwords travel in env vars for ansible_password). Ansible
stdout/stderr is redacted before it is yielded to the console or written to
the deploy transcript.

join_mode=apply - greenfield / idempotent resume of the full sequence.
join_mode=check - per-node package/hardening/TLS install against the wizard
                   peer only; cluster-join playbooks (--check) against the
                   live Pacemaker nodelist. Never adds a non-member peer to
                   the live CIB/DRBD resource.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tarfile
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
    cib_text: str | None = None,
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
        corosync_conf,
        status_text=status_text,
        live_nodes=live_nodes,
        cib_text=cib_text,
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


def wrap_privileged_remote(inner: str) -> str:
    """Run inner as root on the SSH target without requiring NOPASSWD sudo.

    Ansible already uses become_password. Raw SSH used ``sudo -n`` and failed on
    a greenfield ``kin`` account that can sudo only with a password. Prefer
    passwordless sudo, else ``sudo -S`` (password on stdin, never in argv).
    """
    quoted = inner.strip().replace("'", "'\"'\"'")
    # Close stdin on the root and sudo -n branches. HA still pipes the SSH
    # password for sudo -S; without </dev/null, NOPASSWD sudo would leak that
    # line into kin-mail.sh / zmsetup as a bogus prompt answer.
    return (
        "if [ \"$(id -u)\" -eq 0 ]; then "
        f"bash -c '{quoted}' </dev/null; "
        "elif sudo -n true </dev/null >/dev/null 2>&1; then "
        f"sudo -n bash -c '{quoted}' </dev/null; "
        "else "
        f"sudo -S -p '' bash -c '{quoted}'; "
        "fi"
    )


def ssh_password_candidates(kin_pass: str, root_pass: str) -> tuple[tuple[str, str], ...]:
    """SSH (username, password) pairs in try order.

    ``kin`` is the only default automation account. ``root`` is last-resort.
    Do not add extra usernames: a missing account fails first and trips fail2ban.
    """
    return (("kin", kin_pass), ("root", root_pass))


def choose_shared_ssh_user(
    ok: dict[tuple[str, str], bool],
    *,
    hosts: tuple[str, ...],
    users: tuple[str, ...] = ("kin", "root"),
) -> str:
    """First username that succeeded on every host. Empty if none is shared."""
    for user in users:
        if all(bool(ok.get((host, user))) for host in hosts):
            return user
    return ""


def _iqn_suffix(hostname: str) -> str:
    short = hostname.split(".")[0].lower()
    short = re.sub(r"[^a-z0-9-]", "", short) or "mail"
    return short[:32]


def hostnames_compatible(expected: str, live: str) -> bool:
    """True when inventory name and hostname -f / uname share an identity."""
    exp = (expected or "").strip().lower().rstrip(".")
    got = (live or "").strip().splitlines()[0].strip().split()[0].lower().rstrip(".")
    if not exp or not got:
        return False
    if got in {"localhost", "localhost.localdomain"}:
        return False
    if exp == got:
        return True
    exp_short = exp.split(".", 1)[0]
    got_short = got.split(".", 1)[0]
    if exp_short != got_short:
        return False
    # Same short name: allow mail vs mail.example.test, not two different FQDNs.
    return "." not in exp or "." not in got


def observability_inventory_name(*, ip: str, hostname: str = "") -> str:
    """Ansible inventory name for the qnetd/iSCSI VM. Never a lab FQDN."""
    name = (hostname or "").strip()
    if name and valid_name(name):
        return name
    dotted = (ip or "").strip().replace(".", "-")
    return f"obs-{dotted}" if dotted else "observability"


def monitoring_host_from_ssh_ident(
    current: OrchHost,
    ident: str,
    *,
    env_locked: bool,
) -> OrchHost:
    """Prefer the live hostname so delegate_to matches the qnetd VM."""
    if env_locked:
        return current
    line = (ident or "").strip().splitlines()[0] if ident else ""
    name = line.strip().split()[0] if line else ""
    if not name or not valid_name(name) or name == current.name:
        return current
    lowered = name.lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        return current
    return OrchHost(name, current.ip, _iqn_suffix(name))


def render_inventory(
    mail_hosts: list[OrchHost],
    monitoring: OrchHost,
    *,
    vip_ip: str = "",
    vip_nic: str = "",
    data_disk: str = "",
    meta_disk: str = "",
) -> str:
    """YAML inventory with env-lookup passwords - no secret values in the file."""
    disk = (data_disk or "").strip() or "/dev/sdb1"
    meta = (meta_disk or "").strip() or "/dev/sdb2"
    if not re.fullmatch(r"/dev/[A-Za-z0-9/._+-]+", disk):
        disk = "/dev/sdb1"
    if not re.fullmatch(r"/dev/[A-Za-z0-9/._+-]+", meta):
        meta = "/dev/sdb2"
    lines = [
        "all:",
        "  vars:",
        "    ansible_connection: ssh",
        "    ansible_user: '{{ lookup(\"env\", \"KIN_ANSIBLE_USER\") }}'",
        "    ansible_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_PASSWORD\") }}'",
        "    ansible_become: true",
        "    ansible_become_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_BECOME_PASSWORD\") }}'",
        "    ansible_ssh_common_args: '-o StrictHostKeyChecking=accept-new "
        "-o ServerAliveInterval=30 -o ServerAliveCountMax=120'",
        "    corosync_qdevice_qnetd_ip: " + monitoring.ip,
        "    corosync_qdevice_qnetd_inventory_host: " + monitoring.name,
        "    iscsi_initiator_portal: \"" + monitoring.ip + ":3260\"",
        "    cluster_node_base_hacluster_password: '{{ lookup(\"env\", \"KIN_HACLUSTER_PASSWORD\") }}'",
        # SBD LUN CHAP - same generated secret on target and every initiator.
        # Userid isn't sensitive (the password is what authenticates), so it
        # doesn't need the env-lookup indirection.
        "    iscsi_target_chap_userid: kin-sbd-chap",
        "    iscsi_target_chap_password: '{{ lookup(\"env\", \"KIN_CHAP_PASSWORD\") }}'",
        "    iscsi_initiator_chap_userid: kin-sbd-chap",
        "    iscsi_initiator_chap_password: '{{ lookup(\"env\", \"KIN_CHAP_PASSWORD\") }}'",
        "    cluster_setup_name: kin-mail",
        "    drbd_resource_disk: " + disk,
        "    drbd_resource_meta_disk: " + meta,
        # Proven rebuild uses /dev/sdb2. Do not keep the old-lab loop unit.
        '    pacemaker_agents_keep_meta_loop_unit: ""',
        # Console ansible/ is not next to install/. Roles copy helper scripts
        # from here (KIN_MAIL_DEPLOY_DIR), not via role_path/../../../install.
        '    kin_mail_deploy_dir: "' + kin_mail_deploy_dir() + '"',
    ]
    if mail_hosts:
        primary = mail_hosts[0]
        short = primary.name.split(".")[0].lower() or primary.name
        # Role defaults ship example.test placeholders. Remap hostname and
        # optional prefer pin to this pair's real FQDNs.
        lines.append(f"    pacemaker_mail_stack_zimbra_service_hostname: {primary.name}")
        lines.append(f"    pacemaker_mail_stack_zimbra_service_shortname: {short}")
        lines.append(f"    pacemaker_mail_stack_prefer_node: {primary.name}")
    if vip_ip:
        lines.append(f"    pacemaker_mail_stack_vip_ip: {vip_ip}")
    nic = (vip_nic or "").strip()
    if nic and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,15}", nic):
        # IPaddr2 nic= must match the Promoted node's LAN iface. Role default
        # used to be lab ens33; a new site with ens18/eth0 would create a VIP
        # that never starts. Leave empty unless an operator pins a nic; IPaddr2
        # then picks the iface for the VIP subnet on each node.
        lines.append(f"    pacemaker_mail_stack_vip_nic: {nic}")
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
            f'          iscsi_initiator_name: "iqn.2026-08.example.test:{host.iqn_suffix}"'
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
        "timeout = 180\n"
        "\n"
        "[privilege_escalation]\n"
        "become = True\n"
        "become_method = sudo\n"
        "become_timeout = 120\n"
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
    # non-member peer - live_join_check dry-runs them against the live pair.
    cluster_join: bool = False


# Proven HA orchestration order. remote_install is the OS+Zimbra step on the new
# node; skip_remote_install=1 leaves it to a prior local Deploy on that host.
STEPS: tuple[Step, ...] = (
    Step("peer_os_prep", "OS prep + Zimbra install on the new node", None, "remote_install"),
    # Nginx snippets live on the OS disk; the Zimbra template on DRBD includes
    # them. Full Z-Push (PHP/PPA/zmproxy) stays off on B. tags=snippets only.
    Step(
        "mail_zpush_snippets",
        "mail-zpush.yml tags=snippets (nginx includes on every mail node)",
        "playbooks/mail-zpush.yml",
        "ansible",
        tags=("snippets",),
        peer_only_on_join_check=True,
    ),
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
    # Built-in metrics for the console Monitoring tab. Last of the applying
    # steps on purpose: it is additive, it touches no cluster state, and a
    # package-install hiccup here must never be able to strand a half-built
    # HA pair.
    Step(
        "mail_monitoring",
        "mail-monitoring.yml (built-in Prometheus metrics for the console)",
        "playbooks/mail-monitoring.yml",
        "ansible",
        check_on_join_check=True,
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

    obs_name = str(os.environ.get("KIN_QNETD_INVENTORY_HOST") or "").strip()
    if not obs_name or not valid_name(obs_name):
        hint = str(
            draft.get("observability_vm_name") or config.get("OBSERVABILITY_VM_NAME") or ""
        ).strip()
        obs_name = observability_inventory_name(ip=obs_ip, hostname=hint)

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
    """Floating mail VIP - must not collide with any node NIC."""
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
            f"Cluster VIP {vip} is the same as {collisions[vip]} - "
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
    stdin_text: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    from .commands import _stream_subprocess

    async for ev in _stream_subprocess(
        argv,
        cwd=cwd,
        extra_env=extra_env,
        transcript=transcript,
        transcript_reset=False,
        secrets=secrets,
        stdin_text=stdin_text,
    ):
        if ev.get("type") in ("stdout", "stderr") and ev.get("data"):
            ev = dict(ev)
            ev["data"] = redact_text(str(ev["data"]), secrets)
        yield ev


def _event_exit_code(ev: dict[str, Any]) -> int:
    """0 is success - do not treat it as missing via `or 1`."""
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
        "ansible-playbook not found - install ansible-core on this console host"
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
        (
            "playbooks/mail-pacemaker.yml",
            ["--skip-tags", "agents,verify,ldap,memcached"],
        )
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
    stdin_text: str | None = None,
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
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=120",
        "-o",
        f"ConnectTimeout={timeout}",
        f"{user}@{host.ip}",
        remote_cmd,
    ]
    env = {**os.environ, "SSHPASS": password}
    stdin = asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=stdin,
        env=env,
    )
    payload = None
    if stdin_text is not None:
        blob = stdin_text if stdin_text.endswith("\n") else stdin_text + "\n"
        payload = blob.encode("utf-8")
    out_b, err_b = await proc.communicate(input=payload)
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


# Deploy tree must exist. Privilege uses wrap_privileged_remote (NOPASSWD or
# password sudo), matching Ansible become - do not require sudo -n here.
PEER_OS_PREP_READINESS_CMD = (
    "missing=\"\"; "
    '[ -x /opt/kin-mail-deploy/install/kin-mail.sh ] || missing="${missing}deploy-tree "; '
    'if [ -n "$missing" ]; then echo "KIN_PEER_NOT_READY:${missing}"; exit 4; fi; '
    "echo KIN_PEER_READY"
)

_PEER_READY_LABELS = {
    "deploy-tree": (
        "missing /opt/kin-mail-deploy/install/kin-mail.sh "
        "(Build HA pair copies it from this host when missing)"
    ),
    "sudo-n": "cannot sudo -n (NOPASSWD sudo for this SSH user)",
}

_SCP_TMP_RE = re.compile(r"^/tmp/kin-mail-peer-[A-Za-z0-9._-]+$")
_INSTALL_DEST_RE = re.compile(
    r"^/(etc/kin-mail/(config|ha-setup-complete|setup-complete|topology|server-id)|etc/letsencrypt/[A-Za-z0-9._-]+|var/lib/kin-mail-console/(users\.json|license\.token|session\.secret))$"
)


def should_refresh_peer_deploy_tree(
    *,
    join_mode: str,
    ready_ok: bool,
    missing: list[str],
) -> bool:
    """Whether to copy this host's install/ onto the peer before full-install.

    join_mode=apply always refreshes. A retry of Build HA pair otherwise keeps
    B's stale /opt/kin-mail-deploy from the first attempt, and 02/03 on B would
    not see handoff/probe fixes that only exist on A. --check only copies when
    the tree is missing (copying is a mutation).
    """
    if (join_mode or "").strip().lower() == "apply":
        return True
    if ready_ok:
        return False
    blob = " ".join(missing)
    return "deploy-tree" in blob or "kin-mail.sh" in blob


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


def peer_disk_selector_src() -> Path:
    """select_drbd_disk.py on the controller (console ansible tree)."""
    ansible_dir = Path(os.environ.get("KIN_ANSIBLE_DIR", str(ANSIBLE_DIR)))
    return ansible_dir / "roles/drbd_disk_prep/files/select_drbd_disk.py"


def build_peer_install_archive(
    src_install: Path,
    dest_tgz: Path,
    *,
    selector: Path | None = None,
) -> None:
    """Tar install/ (and the DRBD disk selector) for /opt/kin-mail-deploy on the peer.

    02-prepare-os.sh on the peer calls prepare-zimbra-data-disk.sh, which needs
    select_drbd_disk.py. The install/ tarball alone does not include ansible/.
    Without the selector, B falls back to the OS volume and later DRBD handoff
    fights a mounted /opt/zimbra on sda.
    """
    src = src_install.resolve()
    if not (src / "kin-mail.sh").is_file():
        raise FileNotFoundError(f"kin-mail.sh missing under {src}")
    dest_tgz.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest_tgz, "w:gz") as tar:
        tar.add(src, arcname="install", filter=_peer_install_tarinfo)
        if selector is not None:
            sel = selector.resolve()
            if sel.is_file():
                tar.add(
                    sel,
                    arcname="ansible/roles/drbd_disk_prep/files/select_drbd_disk.py",
                    filter=_peer_install_tarinfo,
                )


def _peer_install_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = info.name.replace("\\", "/")
    if name.startswith("/") or ".." in Path(name).parts:
        return None
    return info


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
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=120",
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


async def _push_peer_deploy_tree(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
) -> tuple[int, str]:
    """Copy this host's install/ tree to /opt/kin-mail-deploy on the peer."""
    src = Path(kin_mail_deploy_dir()) / "install"
    local_tgz = WORK_DIR / "peer-install.tgz"
    selector = peer_disk_selector_src()
    if not selector.is_file():
        return 2, (
            f"select_drbd_disk.py missing at {selector}. "
            "Refusing to copy a peer tree that would install Zimbra on the OS disk."
        )
    try:
        build_peer_install_archive(src, local_tgz, selector=selector)
    except (OSError, FileNotFoundError) as exc:
        return 2, str(exc)
    remote_tmp = "/tmp/kin-mail-peer-install.tgz"
    scp_code, scp_text = await _scp_put(
        host, user, password, secrets, local_tgz, remote_tmp, timeout=120
    )
    if scp_code != 0:
        return scp_code, scp_text
    extract = wrap_privileged_remote(
        "mkdir -p /opt/kin-mail-deploy && "
        "tar -C /opt/kin-mail-deploy -xzf /tmp/kin-mail-peer-install.tgz && "
        "find /opt/kin-mail-deploy/install -type f -name '*.sh' -exec chmod a+rx {} + && "
        "( test ! -d /opt/kin-mail-deploy/ansible || "
        "find /opt/kin-mail-deploy/ansible -type f -name 'select_drbd_disk.py' "
        "-exec chmod a+r {} + ) && "
        "rm -f /tmp/kin-mail-peer-install.tgz && "
        "test -x /opt/kin-mail-deploy/install/kin-mail.sh"
    )
    return await _ssh_run(
        host, user, password, secrets, extract, timeout=60, stdin_text=password
    )


async def _install_staged_peer_file(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
    *,
    remote_tmp: str,
    dest: str,
    mode: str = "600",
    owner: str = "root",
    group: str = "root",
) -> tuple[int, str]:
    """sudo-install a staged /tmp file to dest, then remove the temp copy."""
    if mode not in ("600", "644"):
        return 2, "refusing unsafe install mode"
    if owner not in ("root", "kin-console") or group not in ("root", "kin-console"):
        return 2, "refusing unsafe install owner"
    if not _SCP_TMP_RE.match(remote_tmp) or not _INSTALL_DEST_RE.match(dest):
        return 2, "refusing unsafe install path"
    dest_dir = str(Path(dest).parent)
    chmod_dir = ""
    if dest_dir.rstrip("/") == "/etc/kin-mail":
        chmod_dir = f"chmod 755 {dest_dir} && "
    # Mail B gets a full console via ensure_peer_console_runtime (peer activate).
    # create kin-console here too so identity pushes stay safe if activate was
    # skipped on a health-OK resume path that somehow lacks the user.
    ensure_owner = ""
    if owner == "kin-console" or group == "kin-console":
        ensure_owner = (
            "getent passwd kin-console >/dev/null "
            "|| useradd --system --home /var/lib/kin-mail-console "
            "--shell /usr/sbin/nologin kin-console; "
            "install -d -o kin-console -g kin-console -m 750 "
            "/var/lib/kin-mail-console; "
        )
    # remote_tmp was scp'd by the unprivileged SSH user before this runs as
    # root - refuse if it's a symlink (planted by another local user between
    # the scp and this step) instead of letting `install` read through it
    # into a root-owned destination.
    symlink_guard = (
        f"[ ! -L {remote_tmp} ] "
        f"|| {{ echo 'refusing: {remote_tmp} is a symlink' >&2; exit 1; }} && "
    )
    inner = (
        f"{symlink_guard}"
        f"{ensure_owner}"
        f"mkdir -p {dest_dir} && "
        f"{chmod_dir}"
        f"install -m {mode} -o {owner} -g {group} {remote_tmp} {dest} && "
        f"rm -f {remote_tmp}"
    )
    cmd = wrap_privileged_remote(inner)
    return await _ssh_run(
        host, user, password, secrets, cmd, timeout=20, stdin_text=password
    )


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
    owner: str = "root",
    group: str = "root",
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
            owner=owner,
            group=group,
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
    "if [ -f /etc/kin-mail/config ]; then "
    + wrap_privileged_remote("cat /etc/kin-mail/config")
    + "; fi; "
    "echo KIN_PEER_CFG_END"
)


def _block_between(text: str, begin: str, end: str) -> str:
    start = text.find(begin)
    stop = text.find(end)
    if start < 0 or stop < 0 or stop < start:
        return ""
    return text[start + len(begin) : stop]


def peer_console_activate_src() -> Path:
    """peer-console-activate.sh next to deployed (or checkout) console/deploy/."""
    return Path(__file__).resolve().parents[2] / "deploy" / "peer-console-activate.sh"


def peer_console_opt_root() -> Path:
    return Path(os.environ.get("KIN_MAIL_CONSOLE_OPT", "/opt/kin-mail-console"))


PEER_CONSOLE_HEALTH_CMD = (
    "set -e; "
    "test -x /opt/kin-mail-console/venv/bin/python "
    "-o -x /opt/kin-mail-console/venv/bin/python3; "
    "test -d /opt/kin-mail-console/frontend/dist; "
    "systemctl is-active --quiet kin-mail-privhelperd.service; "
    "systemctl is-active --quiet kin-mail-console.service; "
    "port=9443; "
    "if [ -f /etc/kin-mail-console/console.env ]; then "
    "  . /etc/kin-mail-console/console.env >/dev/null 2>&1 || true; "
    "  port=${CONSOLE_PORT:-9443}; "
    "fi; "
    "code=$(curl -sk -o /dev/null -w '%{http_code}' "
    "https://127.0.0.1:${port}/api/health || true); "
    "test \"$code\" = 200"
)


def build_peer_console_archive(src_opt: Path, dest_tgz: Path) -> None:
    """Tar Host A's installed /opt/kin-mail-console for the peer."""
    src = src_opt.resolve()
    if not (src / "venv" / "bin" / "python").is_file() and not (
        src / "venv" / "bin" / "python3"
    ).is_file():
        raise FileNotFoundError(f"console venv missing under {src}")
    if not (src / "frontend" / "dist").is_dir():
        raise FileNotFoundError(f"frontend/dist missing under {src}")
    dest_tgz.parent.mkdir(parents=True, exist_ok=True)

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = info.name.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            return None
        parts = Path(name).parts
        skip_dirs = {
            "node_modules",
            "__pycache__",
            ".git",
            ".venv",
        }
        if any(p in skip_dirs for p in parts):
            return None
        base = Path(name).name
        if base.endswith((".pyc", ".pyo", ".retry")):
            return None
        if base.endswith((".pem", ".key")) and "deploy" not in parts:
            return None
        if base in (
            ".env",
            "id_rsa",
            "id_ed25519",
            "users.json",
            "session.secret",
            "lab.yml",
        ):
            return None
        if base.endswith(".local.yml"):
            return None
        return info

    with tarfile.open(dest_tgz, "w:gz") as tar:
        tar.add(src, arcname="kin-mail-console", filter=_filter)


async def _peer_console_listen_port(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
) -> int:
    """Read CONSOLE_PORT from the peer (default 9443)."""
    cmd = wrap_privileged_remote(
        "port=9443; "
        "if [ -f /etc/kin-mail-console/console.env ]; then "
        "  set -a; . /etc/kin-mail-console/console.env >/dev/null 2>&1 || true; set +a; "
        "  port=${CONSOLE_PORT:-9443}; "
        "fi; "
        "printf '%s' \"$port\""
    )
    code, text = await _ssh_run(
        host, user, password, secrets, cmd, timeout=20, stdin_text=password
    )
    if code != 0:
        return 9443
    raw = (text or "").strip().splitlines()
    candidate = (raw[-1] if raw else "").strip()
    try:
        port = int(candidate)
    except ValueError:
        return 9443
    if 1 <= port <= 65535:
        return port
    return 9443


async def _peer_console_reachable_from_here(
    peer_ip: str, *, port: int = 9443, timeout: float = 8.0
) -> tuple[bool, str]:
    """curl peer management IP:port from Host A (firewall / bind check)."""
    ip = (peer_ip or "").strip()
    if not valid_ipv4(ip):
        return False, "peer IP is not a valid IPv4 for remote health"
    curl = shutil.which("curl")
    if not curl:
        return False, "curl not installed on this host for remote peer health"
    url = f"https://{ip}:{port}/api/health"
    proc = await asyncio.create_subprocess_exec(
        curl,
        "-sk",
        "--connect-timeout",
        "5",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, f"remote health timed out for {url}"
    code = (out_b or b"").decode("utf-8", errors="replace").strip()
    if int(proc.returncode or 0) != 0 or code != "200":
        err = (err_b or b"").decode("utf-8", errors="replace").strip()
        detail = err or f"http_code={code or 'none'}"
        return False, f"peer :{port} not reachable from this host ({detail})"
    return True, f"Remote health https://{ip}:{port}/api/health → 200"


PEER_USERS_NONEMPTY_CMD = (
    "set -e; "
    "test -s /var/lib/kin-mail-console/users.json; "
    "py=/opt/kin-mail-console/venv/bin/python; "
    "test -x \"$py\" || py=/opt/kin-mail-console/venv/bin/python3; "
    "\"$py\" -c \"import json; "
    "d=json.load(open('/var/lib/kin-mail-console/users.json')); "
    "u=d.get('users'); "
    "assert isinstance(u, list) and len(u) >= 1\""
)


async def ensure_peer_console_runtime(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
) -> tuple[bool, list[str]]:
    """Install /opt/kin-mail-console on the peer and start :9443 if needed.

    Idempotent: skips transfer when local+remote health already pass. Must run
    before users.json / identity / completion markers.
    """
    notes: list[str] = []
    peer_port = await _peer_console_listen_port(host, user, password, secrets)
    health_code, _health_text = await _ssh_run(
        host,
        user,
        password,
        secrets,
        wrap_privileged_remote(PEER_CONSOLE_HEALTH_CMD),
        timeout=30,
        stdin_text=password,
    )
    if health_code == 0:
        remote_ok, remote_note = await _peer_console_reachable_from_here(
            host.ip, port=peer_port
        )
        if remote_ok:
            notes.append(
                "Peer console already healthy on :9443; skipping tree transfer."
            )
            notes.append(remote_note)
            return True, notes
        notes.append(
            f"Peer localhost health OK but {remote_note}; "
            "re-running activate to repair firewall/bind."
        )

    src_opt = peer_console_opt_root()
    activate_src = peer_console_activate_src()
    if not activate_src.is_file():
        notes.append(
            f"peer-console-activate.sh missing at {activate_src}. "
            "Re-run console/bootstrap.sh on this host, then retry."
        )
        return False, notes
    if not src_opt.is_dir():
        notes.append(
            f"Local console tree missing at {src_opt}. "
            "Bootstrap the console on this host before Build HA."
        )
        return False, notes

    # Peer ops from B need install scripts under /opt/kin-mail-deploy.
    deploy_code, deploy_text = await _push_peer_deploy_tree(
        host, user, password, secrets
    )
    if deploy_code != 0:
        notes.append(
            f"Could not sync /opt/kin-mail-deploy to the peer (exit {deploy_code}): "
            f"{deploy_text or 'no detail'}"
        )
        return False, notes
    notes.append("Synced /opt/kin-mail-deploy install tree to the peer")

    local_tgz = WORK_DIR / "peer-console.tgz"
    try:
        build_peer_console_archive(src_opt, local_tgz)
    except (OSError, FileNotFoundError) as exc:
        notes.append(f"Could not archive local console tree: {exc}")
        return False, notes

    remote_tgz = "/tmp/kin-mail-peer-console.tgz"
    remote_activate = "/tmp/kin-mail-peer-console-activate.sh"
    scp_code, scp_text = await _scp_put(
        host, user, password, secrets, local_tgz, remote_tgz, timeout=600
    )
    if scp_code != 0:
        notes.append(
            f"Could not copy console tree to the peer (exit {scp_code}): {scp_text}"
        )
        return False, notes
    scp_act, scp_act_text = await _scp_put(
        host, user, password, secrets, activate_src, remote_activate, timeout=30
    )
    if scp_act != 0:
        notes.append(
            f"Could not copy peer-console-activate.sh (exit {scp_act}): {scp_act_text}"
        )
        return False, notes

    # Stage real users.json + session.secret before activate starts :9443 so the
    # peer never offers anonymous wizard on a LAN-reachable empty store.
    remote_users = "/tmp/kin-mail-peer-users.json"
    remote_secret = "/tmp/kin-mail-peer-session.secret"
    try:
        from kin_console.users import load_users, users_json_text

        local_users = load_users()
        if not local_users:
            notes.append(
                "Local console users.json is empty; refusing peer activate that "
                "would start without an admin credential."
            )
            return False, notes
        users_body = users_json_text(local_users)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        notes.append(f"Could not read local users.json for peer staging: {exc}")
        return False, notes
    users_tmp = _write_secure_temp(users_body)
    try:
        u_code, u_text = await _scp_put(
            host, user, password, secrets, users_tmp, remote_users, timeout=30
        )
    finally:
        try:
            users_tmp.unlink()
        except OSError:
            pass
    if u_code != 0:
        notes.append(
            f"Could not stage users.json on the peer (exit {u_code}): {u_text}"
        )
        return False, notes

    try:
        from kin_console.auth import ensure_session_secret
        from kin_console.settings import settings as console_settings

        ensure_session_secret()
        secret_body = console_settings.session_secret_file.read_text(
            encoding="utf-8"
        ).strip()
    except OSError as exc:
        notes.append(f"Could not read local session.secret for peer staging: {exc}")
        return False, notes
    if not secret_body:
        notes.append("Local session.secret is empty; refusing peer activate.")
        return False, notes
    secret_tmp = _write_secure_temp(secret_body + "\n")
    try:
        s_code, s_text = await _scp_put(
            host, user, password, secrets, secret_tmp, remote_secret, timeout=30
        )
    finally:
        try:
            secret_tmp.unlink()
        except OSError:
            pass
    if s_code != 0:
        notes.append(
            f"Could not stage session.secret on the peer (exit {s_code}): {s_text}"
        )
        return False, notes

    peer_name = (host.name or "").strip()
    peer_ip = (host.ip or "").strip()

    def _sh_single(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    # Atomic swap: extract aside, stop units, swap, activate. Keep .bak until
    # activate health passes so a mid-failure can restore.
    extract_and_activate = wrap_privileged_remote(
        "set -e; "
        f"[ ! -L {remote_tgz} ] || {{ echo 'refusing: {remote_tgz} is a symlink' >&2; exit 1; }}; "
        f"[ ! -L {remote_activate} ] || {{ echo 'refusing: {remote_activate} is a symlink' >&2; exit 1; }}; "
        f"[ ! -L {remote_users} ] || {{ echo 'refusing: {remote_users} is a symlink' >&2; exit 1; }}; "
        f"[ ! -L {remote_secret} ] || {{ echo 'refusing: {remote_secret} is a symlink' >&2; exit 1; }}; "
        "rm -rf /opt/kin-mail-console.new /tmp/kin-mail-console-extract; "
        "mkdir -p /tmp/kin-mail-console-extract; "
        f"tar -C /tmp/kin-mail-console-extract -xzf {remote_tgz}; "
        "test -d /tmp/kin-mail-console-extract/kin-mail-console; "
        "mv /tmp/kin-mail-console-extract/kin-mail-console /opt/kin-mail-console.new; "
        "rm -rf /tmp/kin-mail-console-extract; "
        f"rm -f {remote_tgz}; "
        "systemctl stop kin-mail-console.service 2>/dev/null || true; "
        "systemctl stop kin-mail-privhelperd.service 2>/dev/null || true; "
        "rm -rf /opt/kin-mail-console.bak; "
        "if [ -d /opt/kin-mail-console ]; then "
        "  mv /opt/kin-mail-console /opt/kin-mail-console.bak; "
        "fi; "
        "mv /opt/kin-mail-console.new /opt/kin-mail-console; "
        f"install -m 755 {remote_activate} /opt/kin-mail-console/deploy/peer-console-activate.sh; "
        f"rm -f {remote_activate}; "
        "export KIN_PEER_CONSOLE_ACTIVATE=1; "
        f"export KIN_PEER_CONSOLE_NAME={_sh_single(peer_name)}; "
        f"export KIN_PEER_CONSOLE_IP={_sh_single(peer_ip)}; "
        "if /opt/kin-mail-console/deploy/peer-console-activate.sh; then "
        "  rm -rf /opt/kin-mail-console.bak; "
        "else "
        "  rc=$?; "
        "  if [ -d /opt/kin-mail-console.bak ]; then "
        "    rm -rf /opt/kin-mail-console; "
        "    mv /opt/kin-mail-console.bak /opt/kin-mail-console; "
        "    systemctl start kin-mail-privhelperd.service 2>/dev/null || true; "
        "    systemctl start kin-mail-console.service 2>/dev/null || true; "
        "  fi; "
        "  exit $rc; "
        "fi"
    )
    # Activate can apt-install + restart services; allow several minutes.
    act_code, act_text = await _ssh_run(
        host,
        user,
        password,
        secrets,
        extract_and_activate,
        timeout=600,
        stdin_text=password,
    )
    if act_code != 0:
        notes.append(
            f"Peer console activate failed (exit {act_code}). "
            "No auto-retry, no auto-rollback beyond restoring the previous tree."
        )
        if act_text.strip():
            notes.append(act_text.strip()[-2000:])
        return False, notes

    peer_port = await _peer_console_listen_port(host, user, password, secrets)
    remote_ok, remote_note = await _peer_console_reachable_from_here(
        peer_ip, port=peer_port
    )
    if not remote_ok:
        notes.append(
            f"Peer console started locally but {remote_note}. "
            "Fail closed: fix UFW/bind on the peer, then retry."
        )
        return False, notes
    notes.append("Installed and activated console on the peer (:9443)")
    notes.append(remote_note)
    return True, notes


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
        host, user, password, secrets, PEER_CONSOLE_PROBE_CMD, timeout=20, stdin_text=password
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
    if plan.get("refuse_incomplete_config"):
        notes.append(
            "Peer /etc/kin-mail/config is missing SERVER_IP/MAIL_HOST/MAIL_DOMAIN; "
            "refusing to write a TOPOLOGY-only skeleton. Stage a full peer config "
            "(Build HA peer prep, or Add Second Server attach config push), then retry."
        )
        return False, notes

    # Full console on Mail B before users/identity/markers. Used to only sync
    # state files; Mail B never ran bootstrap, so :9443 was Host A only.
    runtime_ok, runtime_notes = await ensure_peer_console_runtime(
        host, user, password, secrets
    )
    notes.extend(runtime_notes)
    if not runtime_ok:
        return False, notes

    # Push users.json first, before any completion marker lands on the peer.
    # Used to run last: a failure here after the markers had already been
    # written left the peer durably at TOPOLOGY=2vm + ha-setup-complete
    # (login-gated, cluster "done") while record_ha_orchestration_success()
    # on this node was never reached (caller only calls it when this whole
    # function returns ok) - the local wizard kept the anonymous pre-deploy
    # Super Admin identity even though mail was already live on both nodes.
    # Failing here now, before any marker write, keeps both sides
    # consistently "not yet complete" and safely retryable.
    from .console_users_sync import push_local_users_to_peer

    users_ok, users_note = await push_local_users_to_peer(host, user, password, secrets)
    notes.append(users_note)
    if not users_ok:
        return False, notes

    # Self-heal check: refuse markers if the peer still has an empty placeholder
    # users.json (activate writes [] until this push lands).
    users_verify, users_verify_text = await _ssh_run(
        host,
        user,
        password,
        secrets,
        wrap_privileged_remote(PEER_USERS_NONEMPTY_CMD),
        timeout=20,
        stdin_text=password,
    )
    if users_verify != 0:
        notes.append(
            "Peer users.json is still empty after sync; refusing completion markers. "
            "Retry peer console sync after confirming this host has a real admin user."
        )
        if users_verify_text.strip():
            notes.append(users_verify_text.strip()[-500:])
        return False, notes
    notes.append("Verified peer users.json has at least one console user")

    # Cluster identity next, still before any completion marker. Used to run
    # after ha-setup-complete: a failed server-id / license.token push then
    # left the peer login-gated while this node never wrote its own HA marker
    # (same split-state class as the old users.json-last bug).
    if not await _push_peer_identity_files(host, user, password, secrets, notes):
        return False, notes

    if plan["noop"]:
        notes.append(
            "Peer console already has TOPOLOGY=2vm and completion markers; nothing to write."
        )
    else:
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


async def _push_peer_identity_files(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
    notes: list[str],
) -> bool:
    """Copy this node's server-id, license.token, and session.secret onto
    the peer.

    Call before writing ha-setup-complete / setup-complete. server-id is one
    cluster identity; license.token is verified against it. Both
    license.token and session.secret are 0600 kin-console (never
    world-readable 644).

    session.secret signs the console's login cookie - each node used to mint
    its own independently, so a session started on whichever node the VIP
    currently points at was never valid on the other one (VIP failover
    forced a fresh login even though nothing about the operator's own
    session had actually expired). Push this node's value as the source of
    truth, same as server-id/license.token.
    """
    from .deploy_state import ensure_server_id, read_license_token

    local_server_id = ensure_server_id()
    code, _text = await _push_peer_text_file(
        host,
        user,
        password,
        secrets,
        body=local_server_id + "\n",
        remote_tmp="/tmp/kin-mail-peer-server-id",
        dest="/etc/kin-mail/server-id",
        mode="644",
    )
    if code != 0:
        notes.append(
            f"Failed to write server-id on the peer (exit {code}). "
            "License checks may disagree between nodes until this is retried. "
            "No auto-retry, no auto-rollback."
        )
        return False
    notes.append("Synced server-id to the peer")

    local_license_token = read_license_token()
    if local_license_token:
        code, _text = await _push_peer_text_file(
            host,
            user,
            password,
            secrets,
            body=local_license_token + "\n",
            remote_tmp="/tmp/kin-mail-peer-license-token",
            dest="/var/lib/kin-mail-console/license.token",
            mode="600",
            owner="kin-console",
            group="kin-console",
        )
        if code != 0:
            notes.append(
                f"Failed to write license.token on the peer (exit {code}). "
                "License/seat status may disagree between nodes until this is "
                "retried. No auto-retry, no auto-rollback."
            )
            return False
        notes.append("Synced license.token to the peer")

    try:
        from kin_console.auth import ensure_session_secret
        from kin_console.settings import settings

        ensure_session_secret()
        local_session_secret = settings.session_secret_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        notes.append(
            f"Could not read local session.secret ({exc}); refusing peer identity "
            "sync so both consoles keep a shared cookie signing key."
        )
        return False
    if not local_session_secret:
        notes.append("Local session.secret is empty; refusing peer identity sync.")
        return False
    code, _text = await _push_peer_text_file(
        host,
        user,
        password,
        secrets,
        body=local_session_secret + "\n",
        remote_tmp="/tmp/kin-mail-peer-session-secret",
        dest="/var/lib/kin-mail-console/session.secret",
        mode="600",
        owner="kin-console",
        group="kin-console",
    )
    if code != 0:
        notes.append(
            f"Could not write session.secret on the peer (exit {code}). "
            "Fail closed so A/B login cookies stay consistent; retry peer sync."
        )
        return False
    notes.append(
        "Synced session.secret to the peer (shared cookie signing if both "
        "consoles are used; mail VIP failover does not move the Host A console)"
    )
    return True


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

    # Every host in the rendered inventory - including this one - connects
    # over ansible_password (see render_inventory). A 1vm host that already
    # ran the post-install SSH revert (kin-mail.sh, single-node only) would
    # otherwise refuse the very connection this orchestration needs to reach
    # itself. Idempotent and fast (just an sshd config toggle) if password
    # SSH is already on.
    #
    # Must not re-yield _stream_subprocess's own `done` event: this is one
    # small step of a much longer orchestration, but `done` is the protocol's
    # "the whole command is over" signal - DeploySession.tsx closes its
    # EventSource on the first one it sees. Forwarding it here made the UI
    # think HA orchestration had finished after well under a second, while
    # Ansible kept running server-side with nobody watching (re-audit,
    # 25 Aug 2026). Capture the exit code instead, like every ansible-playbook
    # stage below already does, and hard-abort on failure - continuing into
    # Ansible with password SSH not actually enabled just trades a clear
    # error here for a confusing connection failure several steps later.
    try:
        from .commands import _stream_subprocess, resolve_prepare_os

        prep_script = resolve_prepare_os()
        ensure_ssh_exit = 0
        async for ev in _stream_subprocess(
            [str(prep_script), "--ensure-ssh-password"], cwd=prep_script.parent
        ):
            if ev.get("type") == "done":
                ensure_ssh_exit = _event_exit_code(ev)
            else:
                yield ev
        if ensure_ssh_exit != 0:
            yield emit_line(
                f"ORCH_FAILED step=ensure_ssh_password join_mode={join_mode}",
                err=True,
            )
            yield emit_line(
                f"Refusing: could not ensure local password SSH (exit {ensure_ssh_exit}). "
                "Ansible needs it to reach this host. No auto-retry.",
                err=True,
            )
            yield proto.event_done(ensure_ssh_exit)
            return
    except (FileNotFoundError, RuntimeError) as exc:
        yield emit_line(
            f"ORCH_FAILED step=ensure_ssh_password join_mode={join_mode}",
            err=True,
        )
        yield emit_line(f"Refusing: could not ensure local password SSH: {exc}", err=True)
        yield proto.event_done(1)
        return

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
    chap_pass = secrets_map.get("chap_pass") or ""
    if not chap_pass:
        import secrets as pysecrets

        from .provisioning_secrets import store_secrets

        # iSCSI CHAP secrets are conventionally 12-16 chars; token_urlsafe(12)
        # is comfortably in range after base64 expansion.
        chap_pass = pysecrets.token_urlsafe(12)
        try:
            store_secrets({"chap_pass": chap_pass})
        except ValueError as exc:
            yield emit_line(f"Refusing: could not persist SBD CHAP password: {exc}", err=True)
            yield proto.event_done(2)
            return
        yield emit_line("generated SBD LUN CHAP password (stored in vault, not logged)")
    secrets = [root_pass, kin_pass, hacluster_pass, chap_pass]

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

    # The peer installer's own stdout (echoed through this HA stream) can
    # include the Zimbra admin password it was handed - root/kin/hacluster/
    # chap covered every OS-level secret but not this one.
    admin_pass = str(config.get("ADMIN_PASS") or "").strip()
    if admin_pass:
        secrets.append(admin_pass)

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

    live_names = [os.uname().nodename]
    try:
        import socket

        live_names.append(socket.gethostname())
        live_names.append(socket.getfqdn())
    except OSError:
        pass
    if not any(hostnames_compatible(local.name, name) for name in live_names):
        yield emit_line(
            f"Refusing: this server hostname is {live_names[0]!r} but MAIL_HOST is "
            f"{local.name!r}. Pacemaker/DRBD node names must match. Set MAIL_HOST "
            "to `hostname -f` (or hostnamectl set-hostname to MAIL_HOST) and retry.",
            err=True,
        )
        yield proto.event_done(2)
        return

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
    cib_text = ""
    try:
        from .maintenance import _capture

        _cib_code, cib_out, _cib_err = await _capture(["pcs", "cib"], timeout=30)
        if _cib_code == 0 and cib_out:
            cib_text = cib_out
    except Exception:  # noqa: BLE001
        cib_text = ""

    if live_cluster_blocks_apply(
        join_mode=join_mode,
        live_nodes=live_nodes,
        peer_name=peer.name,
        peer_ip=peer.ip,
        corosync_conf=corosync_txt,
        status_text=status_text,
        cib_text=cib_text or None,
    ):
        yield emit_line(
            "Refusing join_mode=apply: a live Pacemaker cluster already exists "
            f"({live_nodes}) and the wizard peer is not a member. Build HA pair "
            "must not rewrite a production CIB. After Remove host, the survivor "
            "keeps its one-node cluster; re-adding a peer needs the add-host "
            "playbook (mail-add-host), not a fresh Build HA pair. For a brand-new "
            "pair, form the cluster only on empty nodes (no live resources).",
            err=True,
        )
        yield proto.event_done(2)
        return

    if (
        join_mode == "apply"
        and live_nodes
        and not peer_is_member
        and is_harmless_package_stub_cluster(
            corosync_txt,
            status_text=status_text,
            live_nodes=live_nodes,
            cib_text=cib_text or None,
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

    # Need one account that works on BOTH the peer and observability. Using
    # whichever user answered first on the peer (then forcing it on mon)
    # fails when mail B has `kin` but the witness VM only has root.
    probe_ok: dict[tuple[str, str], bool] = {}
    probe_ident: dict[tuple[str, str], str] = {}
    cand = ssh_password_candidates(kin_pass, root_pass)
    for user, pwd in cand:
        for label, host in (("peer", peer), ("monitoring", monitoring)):
            code, ident = await _ssh_probe(host, user, pwd, secrets)
            probe_ok[(label, user)] = code == 0
            probe_ident[(label, user)] = ident
            yield emit_line(
                f"ssh_probe {label} user={user} exit={code} "
                f"ident={ident.splitlines()[0] if ident else ''}"
            )
    ssh_user = choose_shared_ssh_user(
        probe_ok, hosts=("peer", "monitoring"), users=tuple(u for u, _ in cand)
    )
    if not ssh_user:
        yield emit_line(
            "Refusing: no SSH user (kin, then root) works on BOTH the second "
            "mail server and the observability VM. Create Linux user kin with "
            "sudo and the wizard password on every VM, and enable SSH password "
            "login (cloud images default to keys only).",
            err=True,
        )
        yield proto.event_done(1)
        return
    ssh_pass = dict(cand)[ssh_user]
    ident = probe_ident.get(("peer", ssh_user), "")

    if skip_remote and not hostnames_compatible(peer.name, ident):
        got = (ident.splitlines()[0] if ident else "").strip()
        yield emit_line(
            f"Refusing: second server hostname is {got!r} but the wizard name is "
            f"{peer.name!r}. Pacemaker/DRBD use the wizard name. Align them, or "
            "run Build HA pair without skipping remote install so the peer hostname "
            "is set from the wizard.",
            err=True,
        )
        yield proto.event_done(2)
        return

    mon_ident = probe_ident.get(("monitoring", ssh_user), "")
    env_obs = str(os.environ.get("KIN_QNETD_INVENTORY_HOST") or "").strip()
    renamed = monitoring_host_from_ssh_ident(
        monitoring, mon_ident, env_locked=bool(env_obs)
    )
    if renamed.name != monitoring.name:
        yield emit_line(
            f"observability inventory host {monitoring.name} -> {renamed.name} (SSH hostname)"
        )
        monitoring = renamed

    yield emit_line("Checking DRBD backing disks (read-only lsblk)...")
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
    data_disk = str(disk.get("data_disk") or "").strip()
    meta_disk = str(disk.get("meta_disk") or "").strip()
    if disk.get("ok"):
        yield emit_line(
            f"DRBD disk preflight ok data={local.name} / {peer.name}"
            + (f" disk={data_disk} meta={meta_disk}" if data_disk else "")
        )
    else:
        yield emit_line(
            "DRBD data partition missing - unique blank spare disk identified; "
            "will auto-partition, then re-check before DRBD."
        )

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

    peer_inv = render_inventory(
        [peer], monitoring, vip_ip=vip_ip, data_disk=data_disk, meta_disk=meta_disk
    )
    full_inv = render_inventory(
        [local, peer],
        monitoring,
        vip_ip=vip_ip,
        data_disk=data_disk,
        meta_disk=meta_disk,
    )
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
    live_inv = (
        render_inventory(
            live_hosts,
            monitoring,
            vip_ip=vip_ip,
            data_disk=data_disk,
            meta_disk=meta_disk,
        )
        if live_hosts
        else ""
    )

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
            "Auto-partition: unique blank spare disk - "
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
            yield emit_line("Re-checking DRBD backing disks after auto-partition...")
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
                yield emit_line(
                    f"ORCH_FAILED step=disk_preflight join_mode={join_mode}",
                    err=True,
                )
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
                "(join_mode=apply - live dry-run is check-mode only)"
            )
            continue
        if step.kind == "remote_install" and skip_remote:
            yield emit_line(
                f"[{index}/{total}] SKIP {step.step_id} proof=skip "
                "(skip_remote_install=1) - OS prep + Zimbra on the new node "
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
            f"proof={proof} - {proof_why}"
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
            if should_refresh_peer_deploy_tree(
                join_mode=join_mode,
                ready_ok=ready_ok,
                missing=ready_missing,
            ):
                if ready_ok:
                    yield emit_line(
                        "Refreshing the peer install tree from this host "
                        "(retry must not run a stale 02/03 against an unmounted /opt/zimbra)."
                    )
                else:
                    yield emit_line(
                        "Peer is missing /opt/kin-mail-deploy/install; copying it from this host."
                    )
                tree_code, tree_text = await _push_peer_deploy_tree(
                    peer, ssh_user, ssh_pass, secrets
                )
                if tree_code != 0:
                    yield emit_line(
                        f"Could not copy the install tree to the peer (exit {tree_code}): {tree_text}",
                        err=True,
                    )
                    failed_step = step.step_id
                    exit_code = tree_code or 1
                    yield emit_line(
                        f"[{index}/{total}] FAIL {step.step_id} exit={exit_code}; stopping. "
                        "No auto-retry, no auto-rollback.",
                        err=True,
                    )
                    break
                yield emit_line("Install tree is on the peer.")
                ready_code, ready_text = await _ssh_run(
                    peer,
                    ssh_user,
                    ssh_pass,
                    secrets,
                    PEER_OS_PREP_READINESS_CMD,
                    timeout=20,
                )
                ready_ok, ready_missing = parse_peer_prep_readiness(
                    ready_text, ready_code
                )
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
                "(host identity detected there; domain and admin password copied; "
                "TLS/Z-Push/AD skipped so HA join does not re-issue mail2 certs)."
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
                + wrap_privileged_remote(
                    "env KIN_CONSOLE_CONFIRMED=1 KIN_HA_PEER_INSTALL=1 "
                    "/opt/kin-mail-deploy/install/kin-mail.sh --full-install"
                )
                + "; "
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
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=120",
                "-o",
                "ConnectTimeout=30",
                f"{ssh_user}@{peer.ip}",
                remote_cmd,
            ]
            async for ev in _stream_redacted(
                argv,
                cwd=WORK_DIR,
                extra_env={"SSHPASS": ssh_pass, **ansible_env},
                secrets=secrets,
                transcript=DEPLOY_LAST_LOG,
                stdin_text=ssh_pass,
            ):
                if ev.get("type") == "done":
                    exit_code = _event_exit_code(ev)
                else:
                    yield ev
            if exit_code != 0:
                failed_step = step.step_id
                yield emit_line(
                    f"[{index}/{total}] FAIL {step.step_id} exit={exit_code} - stopping. "
                    "No auto-retry, no auto-rollback.",
                    err=True,
                )
                break
            yield emit_line(f"[{index}/{total}] FINISH {step.step_id}")
            continue

        if step.kind == "live_check":
            if not live_inv or not live_hosts:
                yield emit_line(
                    f"[{index}/{total}] SKIP {step.step_id} - no live Pacemaker nodelist "
                    "to dry-run against."
                )
                continue
            (WORK_DIR / "inventory-live.yml").write_text(live_inv, encoding="utf-8")
            os.chmod(WORK_DIR / "inventory-live.yml", 0o600)
            yield emit_line(
                f"live dry-run inventory mail_nodes={[h.name for h in live_hosts]} "
                f"(peer {peer.name} is NOT in this inventory - will not join it)"
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
                        f"[{index}/{total}] FAIL {failed_step} exit={exit_code} - stopping. "
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
                f"exit={exit_code} - stopping. No auto-retry, no auto-rollback.",
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
        if record_ha_orchestration_success(join_mode=join_mode):
            yield emit_line("ha-setup-complete marker written on this node")
    yield emit_line(f"ORCH_DONE join_mode={join_mode}")
    yield proto.event_done(0)
