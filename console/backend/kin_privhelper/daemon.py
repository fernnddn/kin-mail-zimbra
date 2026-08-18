"""kin-mail-privhelperd — root Unix-socket helper (Option B)."""

from __future__ import annotations

import asyncio
import grp
import json
import logging
import os
import stat
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
_audit = logging.getLogger("kin_privhelper.audit")


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
    if not rbac.command_allowed(role, cmd, args=args):
        return None, "denied_rbac"
    return role, None


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global _running
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
            and str(args.get("op") or "").strip().lower() == "status"
        ) or (
            cmd == proto.CMD_MAINTENANCE
            and str(args.get("op") or "").strip().lower() in ("status", "preflight")
        ) or cmd == proto.CMD_HA_DISK_PREFLIGHT

        if not bypass_busy:
            async with _gate:
                if _running:
                    await _send(
                        writer,
                        proto.event_error("busy", "another privileged execution is in progress"),
                    )
                    _audit_line(username, audit_cmd, "busy")
                    return
                _running = True

        try:
            await _send(writer, proto.event_accepted(cmd))
            exit_code = 1
            saw_done = False
            try:
                async for ev in handler(args):
                    await _send(writer, ev)
                    if ev.get("type") == "done":
                        exit_code = int(ev.get("exit_code", 1))
                        saw_done = True
                if not saw_done:
                    await _send(writer, proto.event_done(exit_code))
                _audit_line(username, audit_cmd, "ok", exit_code)
            except Exception as exc:  # noqa: BLE001 — keep daemon alive
                log.exception("privileged command failed cmd=%s user=%s", audit_cmd, username)
                msg = _safe_exec_error(exc)
                await _send(writer, proto.event_error("exec_failed", msg))
                await _send(writer, proto.event_done(1))
                _audit_line(username, audit_cmd, "exec_failed", 1)
        finally:
            if not bypass_busy:
                async with _gate:
                    _running = False
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

    server = await asyncio.start_unix_server(_handle, path=str(SOCKET_PATH))
    await _chmod_socket()
    log.info("listening on %s (mode 660 group %s)", SOCKET_PATH, SOCKET_GROUP)

    async with server:
        await server.serve_forever()


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("kin-mail-privhelperd must run as root")
    asyncio.run(run())
