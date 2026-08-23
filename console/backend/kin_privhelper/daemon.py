"""kin-mail-privhelperd — root Unix-socket helper (Option B)."""

from __future__ import annotations

import asyncio
import errno
import grp
import json
import logging
import os
import stat
import time
from collections.abc import AsyncIterator, Callable
from logging.handlers import WatchedFileHandler
from pathlib import Path
from typing import Any

from . import commands, protocol as proto
from . import deploy_state, rbac

SOCKET_PATH = Path(os.environ.get("PRIVHELPER_SOCKET", "/run/kin-mail/privhelper.sock"))
LOG_PATH = Path(os.environ.get("PRIVHELPER_LOG", "/var/log/kin-mail/privhelper.log"))
USERS_FILE = Path(os.environ.get("PRIVHELPER_USERS_FILE", "/var/lib/kin-mail-console/users.json"))
SOCKET_GROUP = os.environ.get("PRIVHELPER_SOCKET_GROUP", "kin-console")

_gate = asyncio.Lock()
_running = False
# Incremented on every successful acquire. A hung previous handler must not
# clear _running in its finally if a later Deploy already stole the slot.
_run_gen = 0
_running_since = 0.0
_audit = logging.getLogger("kin_privhelper.audit")

# Wizard pipeline: wait for a just-finished job instead of immediate busy.
# A leftover _running after 05-healthcheck.sh exits (SSE consumer stalled)
# used to make "Deploy again" impossible even though the pipeline had stopped.
PIPELINE_WAIT_CMDS = frozenset(
    {
        proto.CMD_APPLY_WIZARD_DRAFT,
        proto.CMD_RUN_FULL_INSTALL,
        proto.CMD_RUN_HA_ORCHESTRATION,
    }
)
BUSY_WAIT_SEC = 8.0


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        LOG_PATH.touch()
    try:
        os.chown(LOG_PATH.parent, 0, 0)
        os.chmod(LOG_PATH.parent, 0o755)
        os.chown(LOG_PATH, 0, 0)
        os.chmod(LOG_PATH, 0o640)
    except PermissionError:
        pass

    handler = WatchedFileHandler(LOG_PATH)
    handler.setFormatter(
        logging.Formatter("%(asctime)s user=%(user)s cmd=%(cmd)s result=%(result)s%(exit)s")
    )
    _audit.setLevel(logging.INFO)
    _audit.handlers.clear()
    _audit.addHandler(handler)
    _audit.propagate = False

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("kin_privhelper")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(stream)


def _audit_line(username: str, cmd: str, result: str, exit_code: int | None = None) -> None:
    exit_s = f" exit={exit_code}" if exit_code is not None else ""
    _audit.info(
        "",
        extra={"user": username, "cmd": cmd, "result": result, "exit": exit_s},
    )


def _safe_exec_error(exc: BaseException) -> str:
    """Operator-facing error text — type + message, redacted, no traceback dump."""
    import re

    name = type(exc).__name__
    text = str(exc).strip() or repr(exc)
    text = re.sub(
        r"(?i)((?:host_)?root_pass|kin_user_pass|admin_pass|password|passwd|pwd|token|secret)\s*[:=]\s*\S+",
        r"\1=***",
        text,
    )
    # Collapse whitespace for the SSE one-liner.
    text = " ".join(text.split())
    if len(text) > 480:
        text = text[:480] + "…"
    return f"{name}: {text}"


def _prepare_socket_dir() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
    except KeyError as exc:
        raise SystemExit(f"Missing group {SOCKET_GROUP} — create kin-console user first") from exc
    os.chown(SOCKET_PATH.parent, 0, gid)
    os.chmod(SOCKET_PATH.parent, 0o750)


async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(proto.encode_line(obj))
    await writer.drain()


def _is_client_disconnect(exc: BaseException) -> bool:
    """True when the console side of the Unix socket has gone away."""
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionError)):
        return True
    if isinstance(exc, OSError) and exc.errno in (
        errno.EPIPE,
        errno.ECONNRESET,
        errno.ENOTCONN,
        errno.EBADF,
    ):
        return True
    return False


async def _drive_handler(
    handler: Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]],
    args: dict[str, Any],
    writer: asyncio.StreamWriter,
    *,
    log: logging.Logger,
    audit_cmd: str,
    username: str,
) -> tuple[int, bool]:
    """Run a privileged handler to completion even if the client disconnects.

    The Unix-socket writer is only a live progress feed. A browser tab blip,
    reverse-proxy idle cut, or SSE cancel must not abort an in-flight HA
    orchestration / install. When send fails with a disconnect, keep
    consuming the handler (so subprocess pumps stay alive) and stop writing.

    Returns (exit_code, client_gone).
    """
    exit_code = 1
    saw_done = False
    client_gone = False
    async for ev in handler(args):
        if not client_gone:
            try:
                await _send(writer, ev)
            except Exception as send_exc:  # noqa: BLE001 - classify below
                if not _is_client_disconnect(send_exc):
                    raise
                client_gone = True
                log.warning(
                    "client connection lost during cmd=%s user=%s (%s); "
                    "continuing privileged job without streaming",
                    audit_cmd,
                    username,
                    type(send_exc).__name__,
                )
        if ev.get("type") == "done":
            exit_code = int(ev.get("exit_code", 1))
            saw_done = True
    if not saw_done and not client_gone:
        await _send(writer, proto.event_done(exit_code))
    return exit_code, client_gone


def _role_for_username(username: str) -> str | None:
    """Resolve role from the console users store (ignore client-claimed role)."""
    try:
        if not USERS_FILE.is_file():
            return None
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        for item in data.get("users") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("username") or "") != username:
                continue
            if bool(item.get("disabled", False)):
                return None
            role = str(item.get("role") or "")
            return role if role in rbac.ALL_ROLES else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


def _authorize(username: str, cmd: str, args: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    """Return (role, deny_audit_tag). role set means allowed; deny_audit_tag set means denied."""
    if username == deploy_state.SETUP_USERNAME:
        if deploy_state.is_mail_deployed():
            return None, "denied_setup_after_deploy"
        if cmd not in deploy_state.SETUP_ALLOWED_COMMANDS:
            return None, "denied_setup_cmd"
        # Pre-deploy setup identity — ops-equivalent for wizard whitelist only.
        return rbac.ROLE_SUPER_ADMIN, None

    role = _role_for_username(username)
    if role is None:
        return None, "denied_no_user"
    if not rbac.command_allowed(role, cmd, args=args, username=username):
        return None, "denied_rbac"
    return role, None


def reset_run_slot_for_tests() -> None:
    global _running, _run_gen, _running_since
    _running = False
    _run_gen = 0
    _running_since = 0.0


def mark_run_slot_busy_for_tests() -> int:
    """Hold the helper lock without a real job (unit tests)."""
    global _running, _run_gen, _running_since
    _run_gen += 1
    _running = True
    _running_since = time.monotonic() - 60.0
    return _run_gen


async def acquire_run_slot(
    cmd: str,
    *,
    wait_sec: float | None = None,
    is_installing: Callable[[], bool] | None = None,
) -> int | None:
    """Take the single-flight slot. None means the caller should send busy.

    Pipeline commands wait briefly for a job that has already printed
    Pipeline stopped but whose Python waiter has not released yet. If no
    kin-mail.sh/zmsetup process remains, steal the stale lock so Deploy
    can run again.
    """
    global _running, _run_gen, _running_since
    installing_fn = is_installing or deploy_state.full_install_in_progress
    timeout = BUSY_WAIT_SEC if wait_sec is None else wait_sec
    can_wait = cmd in PIPELINE_WAIT_CMDS
    deadline = time.monotonic() + (timeout if can_wait else 0.0)

    while True:
        steal = False
        token = 0
        async with _gate:
            if not _running:
                _run_gen += 1
                _running = True
                _running_since = time.monotonic()
                return _run_gen
            if not can_wait:
                return None
            try:
                live_install = bool(installing_fn())
            except Exception:  # noqa: BLE001
                live_install = False
            if live_install:
                return None
            held_for = time.monotonic() - _running_since
            # Fresh holders (apply_draft still writing) must not be stolen.
            if held_for >= max(timeout, 0.05) and time.monotonic() >= deadline:
                steal = True
                _run_gen += 1
                _running = True
                _running_since = time.monotonic()
                token = _run_gen
        if steal:
            logging.getLogger("kin_privhelper").warning(
                "stale helper lock stolen for cmd=%s (no install process)",
                cmd,
            )
            return token
        if timeout <= 0:
            return None
        await asyncio.sleep(0.2)


async def release_run_slot(token: int | None) -> None:
    global _running
    if token is None:
        return
    async with _gate:
        if _run_gen == token:
            _running = False


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    username = "unknown"
    cmd = "unknown"
    log = logging.getLogger("kin_privhelper")
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=30)
        req = proto.decode_line(raw)
        cmd = str(req.get("cmd", ""))
        username = str(req.get("username") or "unknown")[:64]
        ver = req.get("v", 0)
        if ver != proto.PROTOCOL_VERSION:
            await _send(writer, proto.event_error("bad_version", f"unsupported protocol v={ver}"))
            _audit_line(username, cmd, "bad_version")
            return

        if cmd not in proto.ALLOWED_COMMANDS:
            await _send(
                writer,
                proto.event_error("denied", f"command not in whitelist: {cmd!r}"),
            )
            _audit_line(username, cmd, "denied")
            return

        raw_args = req.get("args")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

        role, deny_tag = _authorize(username, cmd, args)
        if role is None:
            if deny_tag == "denied_setup_after_deploy":
                msg = (
                    "setup identity not allowed after mail is deployed — sign in"
                )
            elif deny_tag == "denied_setup_cmd":
                msg = f"setup identity cannot run {cmd!r}"
            elif deny_tag == "denied_rbac":
                # Re-resolve for message (role was known but forbidden).
                known = _role_for_username(username) or "unknown"
                msg = rbac.deny_message(known, cmd)
            else:
                msg = f"unknown or disabled user {username!r} — cannot authorize {cmd}"
            await _send(writer, proto.event_error("denied", msg))
            _audit_line(username, cmd, deny_tag or "denied")
            return

        handler = commands.get_handler(cmd)
        if handler is None:
            await _send(writer, proto.event_error("denied", f"no handler for {cmd!r}"))
            _audit_line(username, cmd, "denied")
            return

        # Audit hint for mailbox create — never include password.
        audit_cmd = cmd
        if cmd == proto.CMD_CREATE_MAILBOX:
            op = str(args.get("op") or "create")
            lp = str(args.get("local_part") or "")[:64]
            audit_cmd = f"{cmd}:{op}" + (f":{lp}" if lp else "")
        elif cmd == proto.CMD_MAINTENANCE:
            mop = str(args.get("op") or "status")[:16]
            tgt = str(args.get("target") or "")[:80]
            audit_cmd = f"{cmd}:{mop}" + (f":{tgt}" if tgt else "")
        elif cmd == proto.CMD_STORE_PROVISIONING_SECRETS:
            audit_cmd = f"{cmd}:redacted"
        elif cmd == proto.CMD_RUN_HA_ORCHESTRATION:
            jmode = str(args.get("join_mode") or "apply")[:16]
            audit_cmd = f"{cmd}:{jmode}"
        elif cmd == proto.CMD_REMOVE_HOST:
            rop = str(args.get("op") or "apply")[:16]
            tgt = str(args.get("target") or "")[:80]
            audit_cmd = f"{cmd}:{rop}" + (f":{tgt}" if tgt else "")
        elif cmd == proto.CMD_REMOVE_OBSERVABILITY:
            rop = str(args.get("op") or "apply")[:16]
            audit_cmd = f"{cmd}:{rop}"
        elif cmd == proto.CMD_ADD_OBSERVABILITY:
            aop = str(args.get("op") or "apply")[:16]
            audit_cmd = f"{cmd}:{aop}"
        elif cmd == proto.CMD_STORE_OBSERVABILITY_SECRETS:
            audit_cmd = f"{cmd}:redacted"
        elif cmd == proto.CMD_APPLY_APPLIANCE_SETTINGS:
            section = str(args.get("section") or "")[:24]
            audit_cmd = f"{cmd}:{section}"
            if section in ("ad", "license"):
                args = dict(args)
                args.pop("search_bind_password", None)
                args.pop("token", None)
        elif cmd == proto.CMD_MUTATE_CONSOLE_USERS:
            mop = str(args.get("op") or "")[:16]
            tgt = str(args.get("username") or "")[:80]
            audit_cmd = f"{cmd}:{mop}" + (f":{tgt}" if tgt else "")
            args = dict(args)
            args["actor"] = username

        # Safety override: canceling the ufw dead-man must work while full install
        # is still streaming later stages (11 / 05-healthcheck can outlast the timer).
        # Audit log is read-only and should remain available during busy work.
        # Mailbox status is a short read-only probe (same script --status).
        bypass_busy = cmd in (
            proto.CMD_CANCEL_FIREWALL_DEADMAN,
            proto.CMD_GET_AUDIT_LOG,
            proto.CMD_GET_DEPLOY_LOG,
            proto.CMD_CLEAR_INITIAL_CONSOLE_PASSWORD,
        ) or (
            cmd == proto.CMD_CREATE_MAILBOX
            and str(args.get("op") or "").strip().lower() in ("status", "list")
        ) or (
            cmd == proto.CMD_APPLY_APPLIANCE_SETTINGS
            and str(args.get("section") or "").strip().lower() in ("status", "tls_status")
        ) or (
            cmd == proto.CMD_MAINTENANCE
            and str(args.get("op") or "").strip().lower() in ("status", "preflight")
        ) or (
            cmd == proto.CMD_REMOVE_HOST
            and str(args.get("op") or "").strip().lower() == "probe"
        ) or (
            cmd == proto.CMD_REMOVE_OBSERVABILITY
            and str(args.get("op") or "").strip().lower() == "probe"
        ) or (
            cmd == proto.CMD_ADD_OBSERVABILITY
            and str(args.get("op") or "").strip().lower() == "probe"
        ) or cmd == proto.CMD_HA_DISK_PREFLIGHT

        slot_token: int | None = None
        if not bypass_busy:
            slot_token = await acquire_run_slot(cmd)
            if slot_token is None:
                await _send(
                    writer,
                    proto.event_error("busy", "another privileged execution is in progress"),
                )
                _audit_line(username, audit_cmd, "busy")
                return

        try:
            await _send(writer, proto.event_accepted(cmd))
            try:
                exit_code, client_gone = await _drive_handler(
                    handler,
                    args,
                    writer,
                    log=log,
                    audit_cmd=audit_cmd,
                    username=username,
                )
                _audit_line(
                    username,
                    audit_cmd,
                    "ok_detached" if client_gone else "ok",
                    exit_code,
                )
            except Exception as exc:  # noqa: BLE001 — keep daemon alive
                # Disconnect during send is handled inside _drive_handler.
                # Reaching here means the privileged job itself failed.
                log.exception("privileged command failed cmd=%s user=%s", audit_cmd, username)
                msg = _safe_exec_error(exc)
                try:
                    await _send(writer, proto.event_error("exec_failed", msg))
                    await _send(writer, proto.event_done(1))
                except Exception as send_exc:  # noqa: BLE001
                    if not _is_client_disconnect(send_exc):
                        log.warning(
                            "failed to report exec_failed to client cmd=%s: %s",
                            audit_cmd,
                            send_exc,
                        )
                _audit_line(username, audit_cmd, "exec_failed", 1)
        finally:
            if not bypass_busy:
                await release_run_slot(slot_token)
    except asyncio.TimeoutError:
        await _send(writer, proto.event_error("timeout", "request timed out"))
        _audit_line(username, cmd, "timeout")
    except Exception as exc:  # noqa: BLE001
        try:
            await _send(writer, proto.event_error("bad_request", str(exc)))
        except Exception:  # noqa: BLE001
            pass
        _audit_line(username, cmd, "bad_request")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        log.info("connection closed peer=%s", peer)


async def _chmod_socket() -> None:
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
    except KeyError as exc:
        raise SystemExit(f"Missing group {SOCKET_GROUP}") from exc
    os.chown(SOCKET_PATH, 0, gid)
    os.chmod(SOCKET_PATH, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)


async def run() -> None:
    _setup_logging()
    _prepare_socket_dir()
    log = logging.getLogger("kin_privhelper")
    log.info("starting kin-mail-privhelperd socket=%s log=%s", SOCKET_PATH, LOG_PATH)
    try:
        if deploy_state.reclaim_stale_full_install_marker():
            log.warning("stamped unexpected-stop on leftover full-install marker")
    except OSError:
        log.exception("failed to reclaim leftover full-install marker")
    try:
        if deploy_state.reclaim_stale_ha_orchestration_marker():
            log.warning("stamped ORCH_FAILED on leftover HA orchestration marker")
    except OSError:
        log.exception("failed to reclaim leftover HA orchestration marker")
    try:
        deploy_state.ensure_kin_mail_dir()
    except OSError:
        log.exception("failed to ensure /etc/kin-mail is traversable")
    try:
        if deploy_state.backfill_topology_marker_from_config():
            log.info("wrote topology marker from /etc/kin-mail/config")
    except OSError:
        log.exception("failed to backfill topology marker")
    try:
        sid = deploy_state.ensure_server_id()
        log.info("email server id %s", sid)
    except OSError:
        log.exception("failed to ensure server-id")

    server = await asyncio.start_unix_server(_handle, path=str(SOCKET_PATH))
    await _chmod_socket()
    log.info("listening on %s (mode 660 group %s)", SOCKET_PATH, SOCKET_GROUP)

    async with server:
        await server.serve_forever()


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("kin-mail-privhelperd must run as root")
    asyncio.run(run())
