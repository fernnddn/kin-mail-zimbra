"""Sync /var/lib/kin-mail-console/users.json across the HA pair.

Writes go through the Promoted mail node. The replica is updated first; the
Promoted node commits locally only after that push succeeds. Plaintext
passwords and bcrypt hashes are never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import protocol as proto
from .orchestration import OrchHost
from .remove_host import names_match

USERS_JSON_DEST = "/var/lib/kin-mail-console/users.json"
USERS_REMOTE_TMP = "/tmp/kin-mail-peer-users"
MUTATION_REMOTE_TMP = "/tmp/kin-mail-peer-users-mut"
_MUTATION_FILE_RE = re.compile(r"^/tmp/kin-mail-peer-users-mut$")
CONSOLE_USERS_OWNER = os.environ.get("KIN_CONSOLE_USERS_OWNER", "kin-console")

SYNC_FAIL = (
    "Could not sync console users to the other mail node. "
    "The change was not applied. Retry when both mail nodes are reachable."
)


def plan_console_users_commit(
    *,
    topology: str,
    local_host: str,
    promoted: str | None,
    replica_ip: str,
    replica_name: str,
    promoted_ip: str,
) -> dict[str, Any]:
    """Decide how a console-user mutation must land. No I/O.

    commit_local: single-node / no cluster.
    push_peer_then_commit: this node is Promoted; replica first, then local.
    forward_to_promoted: this node is not Promoted; send the op to Promoted.
    refuse: 2-server cluster but Promoted or replica address is unknown.
    """
    topo = (topology or "").strip().lower()
    promo = (promoted or "").strip()
    clustered = topo in ("2vm", "2") or bool(promo)
    if not clustered:
        return {"action": "commit_local", "reason": "no cluster Promoted node"}
    if not promo:
        return {
            "action": "refuse",
            "error": (
                "Cannot determine the Promoted mail node. "
                "Console user change was not applied."
            ),
        }
    if names_match(local_host, promo):
        if not (replica_ip or "").strip():
            return {
                "action": "refuse",
                "error": (
                    "Peer IP is missing. Console user change was not applied."
                ),
            }
        return {
            "action": "push_peer_then_commit",
            "replica_name": replica_name,
            "replica_ip": replica_ip,
        }
    if not (promoted_ip or "").strip():
        return {
            "action": "refuse",
            "error": (
                "Promoted node IP is missing. Console user change was not applied."
            ),
        }
    return {
        "action": "forward_to_promoted",
        "promoted": promo,
        "promoted_ip": promoted_ip,
    }


def commit_users_store(
    *,
    action: str,
    write_local: Callable[[], None],
    push_replica: Callable[[], tuple[bool, str]] | None = None,
) -> tuple[bool, str]:
    """Apply replica-first then local. On push failure, local is left unchanged."""
    if action == "refuse":
        return False, "refused"
    if action == "commit_local":
        write_local()
        return True, "local"
    if action == "push_peer_then_commit":
        if push_replica is None:
            return False, "missing replica push"
        ok, err = push_replica()
        if not ok:
            return False, err or SYNC_FAIL
        write_local()
        return True, "synced"
    if action == "forward_to_promoted":
        return False, "forward is not a local commit"
    return False, "unknown action"


def mutation_from_args(args: dict[str, Any]) -> dict[str, Any]:
    """Build the SSH mutation object. Hashes passwords here; never keeps plaintext."""
    from kin_console.users import (
        AUTH_LOCAL,
        apply_clear_mfa,
        apply_create_user,
        apply_delete_user,
        apply_set_local_password,
        apply_set_mfa,
        load_users,
    )

    op = str(args.get("op") or "").strip().lower()
    actor = str(args.get("actor") or "").strip()
    username = str(args.get("username") or "").strip()
    if op not in ("create", "delete", "set_password", "set_mfa", "clear_mfa"):
        raise ValueError("op must be create, delete, set_password, set_mfa, or clear_mfa")
    if not actor:
        raise ValueError("actor is required")
    if not username:
        raise ValueError("username is required")

    password = str(args.get("password") or "")
    out: dict[str, Any] = {"op": op, "actor": actor, "username": username}
    current = load_users()

    if op == "create":
        auth_type = str(args.get("auth_type") or AUTH_LOCAL).strip() or AUTH_LOCAL
        created, _ = apply_create_user(
            current,
            username,
            str(args.get("role") or ""),
            auth_type=auth_type,
            password=password if auth_type == AUTH_LOCAL else "",
            ad_username=str(args.get("ad_username") or ""),
        )
        out["role"] = created.role
        out["auth_type"] = created.auth_type
        out["ad_username"] = created.ad_username
        if created.auth_type == AUTH_LOCAL:
            out["password_hash"] = created.password_hash
        return out

    if op == "delete":
        apply_delete_user(current, username, actor=actor)
        return out

    if op == "set_mfa":
        secret = str(args.get("mfa_secret") or "").strip()
        updated, _ = apply_set_mfa(current, username, mfa_secret=secret)
        out["mfa_enabled"] = True
        out["mfa_secret"] = updated.mfa_secret
        return out

    if op == "clear_mfa":
        apply_clear_mfa(current, username)
        out["mfa_enabled"] = False
        out["mfa_secret"] = ""
        return out

    updated, _ = apply_set_local_password(current, username, password=password)
    out["password_hash"] = updated.password_hash
    out["auth_type"] = AUTH_LOCAL
    return out


def apply_mutation_to_users(
    users: list[Any],
    mutation: dict[str, Any],
) -> tuple[Any | None, list[Any]]:
    """Apply a hash-only mutation dict to an in-memory user list."""
    from kin_console.users import (
        apply_clear_mfa,
        apply_create_user,
        apply_delete_user,
        apply_set_local_password,
        apply_set_mfa,
    )

    op = str(mutation.get("op") or "").strip().lower()
    username = str(mutation.get("username") or "").strip()
    actor = str(mutation.get("actor") or "").strip()
    if "password" in mutation and mutation.get("password"):
        raise ValueError("mutation file must not contain a plaintext password")

    if op == "create":
        created, new_users = apply_create_user(
            users,
            username,
            str(mutation.get("role") or ""),
            auth_type=str(mutation.get("auth_type") or ""),
            password_hash=str(mutation.get("password_hash") or ""),
            ad_username=str(mutation.get("ad_username") or ""),
        )
        return created, new_users
    if op == "delete":
        return None, apply_delete_user(users, username, actor=actor)
    if op == "set_mfa":
        updated, new_users = apply_set_mfa(
            users,
            username,
            mfa_secret=str(mutation.get("mfa_secret") or ""),
        )
        return updated, new_users
    if op == "clear_mfa":
        updated, new_users = apply_clear_mfa(users, username)
        return updated, new_users
    updated, new_users = apply_set_local_password(
        users,
        username,
        password_hash=str(mutation.get("password_hash") or ""),
    )
    return updated, new_users


def _result_line(payload: dict[str, Any]) -> str:
    return "USERS_RESULT:" + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"


def _host_ip(
    name: str,
    *,
    addrs: dict[str, str],
    config: dict[str, str],
    local_ip: str,
) -> str:
    for host, ip in (addrs or {}).items():
        if names_match(str(host), name) and str(ip).strip():
            return str(ip).strip()
    peer_name = str(config.get("PEER_HOST_NAME") or "").strip()
    if peer_name and names_match(name, peer_name):
        return str(config.get("PEER_HOST_IP") or "").strip()
    local_name = str(config.get("MAIL_HOST") or "").strip()
    if local_name and names_match(name, local_name):
        return str(config.get("SERVER_IP") or local_ip).strip()
    return ""


def _mail_config() -> dict[str, str]:
    from .apply_config import parse_config
    from .deploy_state import KIN_MAIL_CONFIG

    try:
        return parse_config(KIN_MAIL_CONFIG.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _ssh_creds() -> tuple[str, str, list[str]]:
    from .provisioning_secrets import load_secrets

    vault = load_secrets()
    kin = str(vault.get("kin_user_pass") or "")
    root = str(vault.get("host_root_pass") or "")
    if not kin or not root:
        cfg = _mail_config()
        kin = kin or str(cfg.get("KIN_USER_PASS") or "")
        root = root or str(cfg.get("HOST_ROOT_PASS") or "")
    secrets = [p for p in (kin, root) if p]
    return kin, root, secrets


async def _ssh_session(host: OrchHost) -> tuple[str, str, list[str]]:
    from .orchestration import _ssh_run, ssh_password_candidates

    kin, root, secrets = _ssh_creds()
    if not kin and not root:
        raise RuntimeError(SYNC_FAIL)
    for user, pwd in ssh_password_candidates(kin, root):
        if not pwd:
            continue
        code, _text = await _ssh_run(
            host, user, pwd, secrets, "hostname -f || hostname", timeout=10
        )
        if code == 0:
            return user, pwd, secrets
    raise RuntimeError(SYNC_FAIL)


async def push_users_json_to(
    host: OrchHost,
    body: str,
    *,
    user: str = "",
    password: str = "",
    secrets: list[str] | None = None,
) -> tuple[bool, str]:
    """Install users.json on dest as kin-console:kin-console 0600. Never log body."""
    from .orchestration import _push_peer_text_file

    if secrets is None or not user or not password:
        user, password, secrets = await _ssh_session(host)
    code, text = await _push_peer_text_file(
        host,
        user,
        password,
        secrets,
        body=body,
        remote_tmp=USERS_REMOTE_TMP,
        dest=USERS_JSON_DEST,
        mode="600",
        owner=CONSOLE_USERS_OWNER,
        group=CONSOLE_USERS_OWNER,
    )
    if code != 0:
        return False, SYNC_FAIL
    return True, "Wrote console users.json to the other mail node"


async def push_local_users_to_peer(
    host: OrchHost,
    user: str,
    password: str,
    secrets: list[str],
) -> tuple[bool, str]:
    """HA apply: copy this node's users.json onto the peer. Never log contents."""
    from kin_console.users import load_users, users_file, users_json_text

    if not users_file().is_file():
        return False, (
            "This node's console users.json is missing; "
            "the peer would not receive the admin credential."
        )
    try:
        body = users_json_text(load_users())
    except (OSError, ValueError, json.JSONDecodeError):
        return False, (
            "This node's console users.json could not be read; "
            "the peer was not updated."
        )
    return await push_users_json_to(
        host, body, user=user, password=password, secrets=secrets
    )


def _write_local_users(new_users: list[Any], mutation: dict[str, Any]) -> None:
    from kin_console.users import finalize_password_side_effects, save_users

    save_users(new_users)
    op = str(mutation.get("op") or "")
    if op in ("create", "set_password"):
        hashed = str(mutation.get("password_hash") or "")
        username = str(mutation.get("username") or "")
        if hashed and username:
            finalize_password_side_effects(username, hashed)


async def _forward_mutation(
    host: OrchHost,
    mutation: dict[str, Any],
) -> tuple[bool, str]:
    from .orchestration import (
        _scp_put,
        _ssh_run,
        _write_secure_temp,
        wrap_privileged_remote,
    )

    user, password, secrets = await _ssh_session(host)
    raw = json.dumps(mutation, separators=(",", ":"), ensure_ascii=False)
    local = _write_secure_temp(raw)
    try:
        code, _text = await _scp_put(
            host, user, password, secrets, local, MUTATION_REMOTE_TMP
        )
        if code != 0:
            return False, SYNC_FAIL
        inner = (
            "env PYTHONPATH=/opt/kin-mail-console/backend "
            "/opt/kin-mail-console/venv/bin/python -m kin_privhelper.console_users_sync "
            f"apply-file {MUTATION_REMOTE_TMP}"
        )
        cmd = wrap_privileged_remote(inner)
        code, text = await _ssh_run(
            host, user, password, secrets, cmd, timeout=40, stdin_text=password
        )
        if code != 0:
            return False, SYNC_FAIL
        if "USERS_RESULT:" not in (text or ""):
            return False, SYNC_FAIL
        return True, "Forwarded console user change to the Promoted node"
    finally:
        try:
            local.unlink()
        except OSError:
            pass


def _build_plan(st: dict[str, Any] | None) -> dict[str, Any]:
    from .deploy_state import saved_wizard_topology
    from .maintenance import this_hostname

    config = _mail_config()
    topology = saved_wizard_topology()
    local_host = str((st or {}).get("local_host") or this_hostname())
    promoted = str((st or {}).get("promoted") or "").strip() or None
    addrs = dict((st or {}).get("addrs") or {})
    local_ip = str(config.get("SERVER_IP") or "")
    replica_name = str(config.get("PEER_HOST_NAME") or "").strip()
    replica_ip = _host_ip(
        replica_name or str(config.get("PEER_HOST_IP") or ""),
        addrs=addrs,
        config=config,
        local_ip=local_ip,
    )
    if not replica_ip:
        replica_ip = str(config.get("PEER_HOST_IP") or "").strip()
    promoted_ip = ""
    if promoted:
        promoted_ip = _host_ip(
            promoted, addrs=addrs, config=config, local_ip=local_ip
        )
    return plan_console_users_commit(
        topology=topology,
        local_host=local_host,
        promoted=promoted,
        replica_ip=replica_ip,
        replica_name=replica_name,
        promoted_ip=promoted_ip,
    )


async def run_mutate(
    args: dict[str, Any],
    *,
    already_on_promoted: bool = False,
) -> tuple[int, dict[str, Any], str]:
    """Run a user mutation. Returns (exit_code, USERS_RESULT payload, note)."""
    from kin_console.users import load_users
    from .deploy_state import saved_wizard_topology
    from .maintenance import gather_status

    try:
        mutation = mutation_from_args(args)
    except KeyError:
        payload = {"ok": False, "code": "not_found", "error": "User not found"}
        return 1, payload, payload["error"]
    except ValueError as exc:
        payload = {"ok": False, "code": "invalid", "error": str(exc)}
        return 1, payload, str(exc)

    st: dict[str, Any] | None = None
    topology = saved_wizard_topology()
    if topology in ("2vm", "2") or already_on_promoted:
        st = await gather_status()
    plan = _build_plan(st)
    if already_on_promoted and plan["action"] == "forward_to_promoted":
        payload = {
            "ok": False,
            "code": "sync_failed",
            "error": "This node is not Promoted. Console user change was not applied.",
        }
        return 1, payload, payload["error"]
    if plan["action"] == "refuse":
        payload = {"ok": False, "code": "sync_failed", "error": plan.get("error") or SYNC_FAIL}
        return 1, payload, str(payload["error"])

    if plan["action"] == "forward_to_promoted" and not already_on_promoted:
        host = OrchHost(
            str(plan.get("promoted") or "peer"),
            str(plan.get("promoted_ip") or ""),
            "peer",
        )
        try:
            ok, note = await _forward_mutation(host, mutation)
        except RuntimeError as exc:
            payload = {"ok": False, "code": "sync_failed", "error": str(exc)}
            return 1, payload, str(exc)
        if not ok:
            payload = {"ok": False, "code": "sync_failed", "error": note}
            return 1, payload, note
        public = None
        if mutation["op"] != "delete":
            from kin_console.users import get_user

            found = get_user(str(mutation.get("username") or ""))
            public = found.public() if found else None
        payload = {"ok": True, "code": "ok", "user": public}
        return 0, payload, note

    target_user, new_users = apply_mutation_to_users(load_users(), mutation)
    from kin_console.users import users_json_text

    body = users_json_text(new_users)

    def write_local() -> None:
        _write_local_users(new_users, mutation)

    if plan["action"] == "push_peer_then_commit":
        replica = OrchHost(
            str(plan.get("replica_name") or "peer"),
            str(plan.get("replica_ip") or ""),
            "peer",
        )
        ok, err = await push_users_json_to(replica, body)
        if not ok:
            payload = {"ok": False, "code": "sync_failed", "error": err}
            return 1, payload, err
        write_local()
        public = target_user.public() if target_user is not None else None
        payload = {"ok": True, "code": "ok", "user": public}
        return 0, payload, "Pushed console users to the other mail node, then applied locally"

    ok, reason = commit_users_store(action=plan["action"], write_local=write_local)
    if not ok:
        payload = {"ok": False, "code": "sync_failed", "error": reason}
        return 1, payload, reason
    public = target_user.public() if target_user is not None else None
    payload = {"ok": True, "code": "ok", "user": public}
    return 0, payload, "Console user change applied on this node"


async def cmd_mutate_console_users(
    args: dict[str, Any] | None = None,
) -> Any:
    args = args or {}
    code, payload, note = await run_mutate(args)
    yield proto.event_stdout(note + "\n")
    yield proto.event_stdout(_result_line(payload))
    yield proto.event_done(code)


async def _apply_file(path: str) -> int:
    if not _MUTATION_FILE_RE.match(path):
        print("USERS_RESULT:" + json.dumps({"ok": False, "code": "invalid", "error": "refusing mutation path"}))
        return 2
    mut_path = Path(path)
    try:
        raw = mut_path.read_text(encoding="utf-8")
        mutation = json.loads(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        print("USERS_RESULT:" + json.dumps({"ok": False, "code": "invalid", "error": "invalid mutation file"}))
        return 2
    finally:
        try:
            mut_path.unlink()
        except OSError:
            pass
    if not isinstance(mutation, dict):
        print("USERS_RESULT:" + json.dumps({"ok": False, "code": "invalid", "error": "invalid mutation file"}))
        return 2
    if mutation.get("password"):
        print("USERS_RESULT:" + json.dumps({"ok": False, "code": "invalid", "error": "refusing plaintext password"}))
        return 2
    args = {
        "op": mutation.get("op"),
        "actor": mutation.get("actor"),
        "username": mutation.get("username"),
        "role": mutation.get("role"),
        "auth_type": mutation.get("auth_type"),
        "ad_username": mutation.get("ad_username"),
        "password_hash": mutation.get("password_hash"),
    }
    # Re-hydrate as hash-only create/set by stuffing password_hash through a
    # synthetic args path: mutation_from_args hashes plaintext. For apply-file
    # we already have hashes, so skip mutation_from_args and call run_mutate
    # internals after swapping load.
    from kin_console.users import load_users
    from .maintenance import gather_status

    try:
        st = await gather_status()
        plan = _build_plan(st)
        if plan["action"] != "push_peer_then_commit":
            print(
                "USERS_RESULT:"
                + json.dumps(
                    {
                        "ok": False,
                        "code": "sync_failed",
                        "error": "This node is not Promoted. Console user change was not applied.",
                    }
                )
            )
            return 1
        target_user, new_users = apply_mutation_to_users(load_users(), mutation)
        from kin_console.users import users_json_text

        body = users_json_text(new_users)
        replica = OrchHost(
            str(plan.get("replica_name") or "peer"),
            str(plan.get("replica_ip") or ""),
            "peer",
        )
        ok, err = await push_users_json_to(replica, body)
        if not ok:
            print("USERS_RESULT:" + json.dumps({"ok": False, "code": "sync_failed", "error": err}))
            return 1
        _write_local_users(new_users, mutation)
        public = target_user.public() if target_user is not None else None
        print("USERS_RESULT:" + json.dumps({"ok": True, "code": "ok", "user": public}, separators=(",", ":")))
        return 0
    except KeyError:
        print("USERS_RESULT:" + json.dumps({"ok": False, "code": "not_found", "error": "User not found"}))
        return 1
    except ValueError as exc:
        print("USERS_RESULT:" + json.dumps({"ok": False, "code": "invalid", "error": str(exc)}))
        return 1
    except RuntimeError as exc:
        print("USERS_RESULT:" + json.dumps({"ok": False, "code": "sync_failed", "error": str(exc)}))
        return 1


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "apply-file":
        raise SystemExit(asyncio.run(_apply_file(sys.argv[2])))
    raise SystemExit("usage: python -m kin_privhelper.console_users_sync apply-file PATH")


if __name__ == "__main__":
    main()
