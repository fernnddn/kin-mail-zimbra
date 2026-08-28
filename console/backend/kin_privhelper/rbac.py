"""RBAC matrix for privhelper whitelist commands (shared with console API)."""

from __future__ import annotations

ROLE_SUPER_ADMIN = "kin_super_admin"
ROLE_SUPPORT_OPS = "kin_support_ops"
ROLE_CUSTOMER_ADMIN = "customer_admin"

ALL_ROLES = frozenset(
    {
        ROLE_SUPER_ADMIN,
        ROLE_SUPPORT_OPS,
        ROLE_CUSTOMER_ADMIN,
    }
)

ROLE_LABELS: dict[str, str] = {
    ROLE_SUPER_ADMIN: "KIN Super Admin",
    ROLE_SUPPORT_OPS: "KIN Support-Ops",
    ROLE_CUSTOMER_ADMIN: "Customer Admin",
}

OPS_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_SUPPORT_OPS})

SENSITIVE_OPS_COMMANDS = frozenset(
    {
        "apply_wizard_draft",
        "run_full_install",
        "run_hardening",
        "cancel_firewall_deadman",
        "store_provisioning_secrets",
        "run_ha_orchestration",
        "ha_disk_preflight",
        "remove_host",
        "add_host",
        "remove_observability",
        "add_observability",
        "store_observability_secrets",
    }
)

MAINTENANCE_MUTATE_OPS = frozenset(
    {"enter", "exit", "cleanup", "failback", "clearban"}
)

SUPER_ONLY_COMMANDS = frozenset(
    {
        "get_audit_log",
        "apply_appliance_settings",
    }
)


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def command_allowed(
    role: str,
    cmd: str,
    *,
    args: dict | None = None,
    username: str | None = None,
) -> bool:
    if role not in ALL_ROLES:
        return False
    if cmd in SUPER_ONLY_COMMANDS:
        return role == ROLE_SUPER_ADMIN
    if cmd == "mutate_console_users":
        op = str((args or {}).get("op") or "").strip().lower()
        if op in ("create", "delete"):
            return role == ROLE_SUPER_ADMIN
        if op == "set_password":
            target = str((args or {}).get("username") or "").strip()
            actor = (username or "").strip()
            if target and actor and target == actor:
                return True
            return role == ROLE_SUPER_ADMIN
        return False
    if cmd in SENSITIVE_OPS_COMMANDS:
        return role in OPS_ROLES
    if cmd == "maintenance":
        op = str((args or {}).get("op") or "status").strip().lower()
        if op in MAINTENANCE_MUTATE_OPS:
            return role in OPS_ROLES
    return True


def deny_message(role: str, cmd: str) -> str:
    label = role_label(role) if role in ALL_ROLES else "unknown role"
    if cmd in SUPER_ONLY_COMMANDS:
        return (
            f"Denied: {cmd} requires {ROLE_LABELS[ROLE_SUPER_ADMIN]} "
            f"(your role: {label})"
        )
    if cmd in SENSITIVE_OPS_COMMANDS:
        return (
            f"Denied: {cmd} requires KIN Super Admin or KIN Support-Ops "
            f"(your role: {label})"
        )
    if cmd == "mutate_console_users":
        return f"Denied: console user change not allowed for {label}"
    return f"Denied: {cmd} not allowed for {label}"
