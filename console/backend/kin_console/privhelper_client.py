"""Async client for kin-mail-privhelperd (used by unprivileged console)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from kin_privhelper import protocol as proto
from kin_privhelper.protocol import encode_line, decode_line, make_request


async def run_command(
    cmd: str,
    username: str,
    *,
    socket_path: Path,
    connect_timeout: float = 5.0,
) -> AsyncIterator[dict[str, Any]]:
    """Connect to the helper socket and yield NDJSON events."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)),
            timeout=connect_timeout,
        )
    except FileNotFoundError as exc:
        yield proto.event_error("unavailable", f"privhelper socket missing: {socket_path}")
        return
    except (ConnectionRefusedError, PermissionError, OSError) as exc:
        yield proto.event_error("unavailable", f"cannot connect to privhelper: {exc}")
        return

    try:
        writer.write(encode_line(make_request(cmd, username)))
        await writer.drain()
        while True:
            raw = await reader.readline()
            if not raw:
                break
            yield decode_line(raw)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
