"""Add/Remove Observability planners (no live cluster calls).

This is not mail-node Add/Remove Host. Observability holds qnetd + the shared
SBD LUN. Plans refuse to mutate a healthy witness, and refuse Add while any
identity is still configured.
"""

from __future__ import annotations

from dataclasses import dataclass

from .observability_status import classify_observability, observability_actions
from .orchestration import (
    kin_mail_deploy_dir,
    observability_inventory_name,
    valid_ipv4,
    valid_name,
)


def boot_id_unchanged(before: str, after: str) -> bool:
    """True only when both boot_id reads are non-empty and identical.

    Empty either side fails closed: we cannot prove the node stayed up
    through the SBD watchdog window (HA-RUNBOOK section 7).
    """
    left = (before or "").strip()
    right = (after or "").strip()
    if not left or not right:
        return False
    return left == right


def _names_match(left: str, right: str) -> bool:
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return a.split(".", 1)[0] == b.split(".", 1)[0]


def sbd_stop_order(*, promoted: str, hosts: list[str]) -> list[str]:
    """Unpromoted first, then Promoted (HA-RUNBOOK section 7)."""
    names = [h.strip() for h in hosts if h and str(h).strip()]
    promo = (promoted or "").strip().lower()
    if not promo:
        return names

    def is_promoted(name: str) -> bool:
        n = name.strip().lower()
        return n == promo or n.split(".", 1)[0] == promo.split(".", 1)[0]

    first = [h for h in names if not is_promoted(h)]
    last = [h for h in names if is_promoted(h)]
    return first + last


@dataclass(frozen=True)
class RemoveObservabilityPlan:
    configured_ip: str
    status: str
    errors: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class AddObservabilityPlan:
    new_ip: str
    hostname: str
    inventory_name: str
    status: str
    errors: tuple[str, ...]
    notes: tuple[str, ...]


def plan_remove_observability(
    *,
    identity: str,
    reachable: bool,
    ssh_ok: bool,
) -> RemoveObservabilityPlan:
    """Forced cleanup only. Refuse if the VM still answers TCP or SSH."""
    status = classify_observability(identity=identity, reachable=reachable)
    errors: list[str] = []
    notes: list[str] = []
    ident = (identity or "").strip()

    if not ident or status == "absent":
        errors.append("no Observability node is configured; nothing to remove")
    elif reachable or status == "healthy":
        errors.append(
            f"Observability ({ident}) is still answering on the qnetd port. "
            "Power off or delete that VM, wait until the Cluster page shows it "
            "unreachable, then Remove again. There is no graceful online "
            "remove: this is forced cleanup of a witness that is already gone."
        )
    elif ssh_ok:
        # The console gates Remove on the qnetd probe alone, so this case
        # arrives with the button enabled and the Cluster page already saying
        # "unreachable". Saying "it still answers on the qnetd port (or SSH)"
        # then reads as a contradiction of what the operator is looking at.
        errors.append(
            f"qnetd is down on Observability ({ident}) but the VM itself is "
            "still running - it accepted an SSH connection. Remove is forced "
            "cleanup for a witness that is gone, and running it against a live "
            "VM would leave that VM holding a stale SBD LUN and qnetd identity. "
            "Power the VM off (or delete it), then Remove."
        )
    if ident and status == "unreachable" and not ssh_ok and not reachable:
        notes.append(
            "Observability is unreachable; taking the forced cleanup path on "
            "both mail nodes (disarm SBD, clear qdevice and iSCSI pointers)"
        )
    return RemoveObservabilityPlan(
        configured_ip=ident,
        status=status,
        errors=tuple(errors),
        notes=tuple(notes),
    )


def plan_add_observability(
    *,
    current_identity: str,
    current_reachable: bool,
    new_ip: str,
    hostname: str = "",
    has_credentials: bool,
) -> AddObservabilityPlan:
    status = classify_observability(
        identity=current_identity,
        reachable=current_reachable,
    )
    errors: list[str] = []
    notes: list[str] = []
    ip = (new_ip or "").strip()
    host = (hostname or "").strip()
    actions = observability_actions(status)

    if not actions["add"]:
        errors.append(
            "an Observability node is still configured; Remove it first "
            f"(status={status})"
        )
    if not valid_ipv4(ip):
        errors.append("new Observability IP is not a valid IPv4 address")
    if host and not valid_name(host):
        errors.append("Observability hostname is not a valid DNS name")
    if not has_credentials:
        errors.append("root and kin passwords for the new Observability VM are required")

    inv = observability_inventory_name(ip=ip, hostname=host)
    if not errors:
        notes.append(
            f"provision qnetd and iSCSI on {inv} ({ip}), then reconnect both "
            "mail nodes and re-arm fencing"
        )
    return AddObservabilityPlan(
        new_ip=ip,
        hostname=host,
        inventory_name=inv,
        status=status,
        errors=tuple(errors),
        notes=tuple(notes),
    )


def render_observability_inventory(
    *,
    mail_hosts: list[tuple[str, str, str]],
    obs_name: str,
    obs_ip: str,
    retired_ip: str = "",
    local_mail_name: str = "",
    force_tls_reinit: bool = False,
    expect_fresh_sbd: bool = False,
    include_observability_host: bool = True,
    mail_domain: str = "",
) -> str:
    """YAML inventory with env-lookup passwords. Never embed secret values.

    mail_hosts: (name, ip, iqn_suffix)
    Observability SSH uses KIN_OBS_* env; mail nodes use KIN_ANSIBLE_*.

    Forced Remove Observability must set include_observability_host=False:
    the witness is already dead, and listing it with empty KIN_OBS_* only
    risks ansible touching a hole. Add Observability keeps it True.
    """
    lines = [
        "all:",
        "  vars:",
        "    ansible_become: true",
        "    ansible_become_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_BECOME_PASSWORD\") }}'",
        "    ansible_ssh_common_args: '-o StrictHostKeyChecking=accept-new "
        "-o ServerAliveInterval=30 -o ServerAliveCountMax=120'",
        f"    corosync_qdevice_qnetd_ip: {obs_ip}",
        f"    corosync_qdevice_qnetd_inventory_host: {obs_name}",
        f'    iscsi_initiator_portal: "{obs_ip}:3260"',
        f'    observability_retired_ip: "{retired_ip}"',
        f"    corosync_qdevice_force_tls_reinit: {str(bool(force_tls_reinit)).lower()}",
        f"    observability_expect_fresh_sbd: {str(bool(expect_fresh_sbd)).lower()}",
        '    kin_mail_deploy_dir: "' + kin_mail_deploy_dir() + '"',
    ]
    # The replacement VM has to be renamed like the original was. The identity
    # role normally derives the domain from the mail service hostname, which is
    # a mail-stack variable and is not in scope here, so name it outright.
    domain = (mail_domain or "").strip().lstrip(".")
    if domain:
        lines.append(f'    observability_hostname: "observability.{domain}"')
    lines.append("  children:")
    if include_observability_host:
        lines.extend(
            [
                "    monitoring:",
                "      hosts:",
                f"        {obs_name}:",
                f"          ansible_host: {obs_ip}",
                "          ansible_connection: ssh",
                "          ansible_user: '{{ lookup(\"env\", \"KIN_OBS_ANSIBLE_USER\") }}'",
                "          ansible_password: '{{ lookup(\"env\", \"KIN_OBS_ANSIBLE_PASSWORD\") }}'",
                "          ansible_become_password: '{{ lookup(\"env\", \"KIN_OBS_ANSIBLE_BECOME_PASSWORD\") }}'",
            ]
        )
    lines.extend(
        [
            "    mail_nodes:",
            "      hosts:",
        ]
    )
    for name, ip, iqn_suffix in mail_hosts:
        conn = (
            "local"
            if local_mail_name and _names_match(name, local_mail_name)
            else "ssh"
        )
        lines.append(f"        {name}:")
        lines.append(f"          ansible_host: {ip}")
        lines.append(f"          ansible_connection: {conn}")
        if conn == "ssh":
            lines.append(
                "          ansible_user: '{{ lookup(\"env\", \"KIN_ANSIBLE_USER\") }}'"
            )
            lines.append(
                "          ansible_password: '{{ lookup(\"env\", \"KIN_ANSIBLE_PASSWORD\") }}'"
            )
        lines.append(
            f'          iscsi_initiator_name: "iqn.2026-08.example.test:{iqn_suffix}"'
        )
    lines.append("")
    return "\n".join(lines)
