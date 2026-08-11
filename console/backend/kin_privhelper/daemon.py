"""kin-mail-privhelperd — root Unix-socket helper (Option B)."""

from __future__ import annotations

import asyncio
import grp
import logging
import os
import stat
from logging.handlers import WatchedFileHandler
from pathlib import Path
from typing import Any

from . import commands, protocol as proto

SOCKET_PATH = Path(os.environ.get("PRIVHELPER_SOCKET", "/run/kin-mail/privhelper.sock"))
LOG_PATH = Path(os.environ.get("PRIVHELPER_LOG", "/var/log/kin-mail/privhelper.log"))
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

        handler = commands.get_handler(cmd)
        if handler is None:
            await _send(writer, proto.event_error("denied", f"no handler for {cmd!r}"))
            _audit_line(username, cmd, "denied")
            return

        # Safety override: canceling the ufw dead-man must work while full install
        # is still streaming later stages (11 / 05-healthcheck can outlast the timer).
        bypass_busy = cmd == proto.CMD_CANCEL_FIREWALL_DEADMAN

        if not bypass_busy:
            async with _gate:
                if _running:
                    await _send(
                        writer,
                        proto.event_error("busy", "another privileged execution is in progress"),
                    )
                    _audit_line(username, cmd, "busy")
                    return
                _running = True

        try:
            await _send(writer, proto.event_accepted(cmd))
            exit_code = 1
            saw_done = False
            try:
                async for ev in handler():
                    await _send(writer, ev)
                    if ev.get("type") == "done":
                        exit_code = int(ev.get("exit_code", 1))
                        saw_done = True
                if not saw_done:
                    await _send(writer, proto.event_done(exit_code))
                _audit_line(username, cmd, "ok", exit_code)
            except Exception as exc:  # noqa: BLE001 — keep daemon alive
                await _send(writer, proto.event_error("exec_failed", str(exc)))
                await _send(writer, proto.event_done(1))
                _audit_line(username, cmd, f"exec_failed:{exc}", 1)
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
        _audit_line(username, cmd, f"bad_request:{exc}")
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

    server = await asyncio.start_unix_server(_handle, path=str(SOCKET_PATH))
    await _chmod_socket()
    log.info("listening on %s (mode 660 group %s)", SOCKET_PATH, SOCKET_GROUP)

    async with server:
        await server.serve_forever()


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("kin-mail-privhelperd must run as root")
    asyncio.run(run())
