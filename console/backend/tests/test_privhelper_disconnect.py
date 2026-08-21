"""Client disconnect must not abort an in-flight privhelper job."""

from __future__ import annotations

import asyncio
import errno
import logging
import unittest
from typing import Any
from unittest.mock import AsyncMock

from kin_privhelper.daemon import _drive_handler, _is_client_disconnect
from kin_privhelper import protocol as proto


class ClientDisconnectTests(unittest.TestCase):
    def test_connection_reset_is_client_disconnect(self) -> None:
        self.assertTrue(_is_client_disconnect(ConnectionResetError("Connection lost")))
        self.assertTrue(_is_client_disconnect(BrokenPipeError()))
        self.assertTrue(_is_client_disconnect(OSError(errno.EPIPE, "Broken pipe")))
        self.assertFalse(_is_client_disconnect(ValueError("nope")))
        self.assertFalse(_is_client_disconnect(OSError(errno.ENOENT, "missing")))

    def test_drive_handler_keeps_consuming_after_disconnect(self) -> None:
        asyncio.run(self._drive_after_disconnect())

    async def _drive_after_disconnect(self) -> None:
        consumed: list[str] = []

        async def handler(_args: dict[str, Any]):
            for i in range(5):
                consumed.append(f"step-{i}")
                yield proto.event_stdout(f"step-{i}\n")
                await asyncio.sleep(0)
            yield proto.event_done(0)

        writer = AsyncMock()
        # Fail on the third drain (after accepted streaming has begun).
        drains = {"n": 0}

        async def flaky_drain() -> None:
            drains["n"] += 1
            if drains["n"] >= 3:
                raise ConnectionResetError("Connection lost")

        writer.drain = flaky_drain
        writer.write = lambda _data: None

        exit_code, client_gone = await _drive_handler(
            handler,
            {},
            writer,
            log=logging.getLogger("test_privhelper_disconnect"),
            audit_cmd="run_ha_orchestration:apply",
            username="setup",
        )

        self.assertTrue(client_gone)
        self.assertEqual(exit_code, 0)
        # All handler steps must run even after the socket died.
        self.assertEqual(consumed, [f"step-{i}" for i in range(5)])

    def test_drive_handler_reraises_non_disconnect_send_errors(self) -> None:
        asyncio.run(self._drive_reraises())

    async def _drive_reraises(self) -> None:
        async def handler(_args: dict[str, Any]):
            yield proto.event_stdout("one\n")
            yield proto.event_done(0)

        writer = AsyncMock()

        async def bad_drain() -> None:
            raise RuntimeError("disk full")

        writer.drain = bad_drain
        writer.write = lambda _data: None

        with self.assertRaises(RuntimeError):
            await _drive_handler(
                handler,
                {},
                writer,
                log=logging.getLogger("test_privhelper_disconnect"),
                audit_cmd="get_status",
                username="admin",
            )

    def test_drive_handler_ok_when_client_stays(self) -> None:
        asyncio.run(self._drive_ok())

    async def _drive_ok(self) -> None:
        async def handler(_args: dict[str, Any]):
            yield proto.event_stdout("ok\n")
            yield proto.event_done(0)

        writer = AsyncMock()
        writer.drain = AsyncMock()
        writer.write = lambda _data: None

        exit_code, client_gone = await _drive_handler(
            handler,
            {},
            writer,
            log=logging.getLogger("test_privhelper_disconnect"),
            audit_cmd="get_status",
            username="admin",
        )
        self.assertFalse(client_gone)
        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(writer.drain.await_count, 2)


if __name__ == "__main__":
    unittest.main()
