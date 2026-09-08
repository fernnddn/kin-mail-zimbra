"""A privileged job must finish even when nobody is listening.

Several commands do real work after their streaming loop: cmd_run_full_install
marks the install finished, remove_observability clears OBSERVABILITY_VM_IP,
run_ha_orchestration writes its completion marker. All of that is safe only
because _drive_handler keeps consuming the handler once the client is gone,
instead of abandoning the generator.

If that ever changes, every one of those tails silently stops happening and the
appliance is left half-configured with no error anywhere. So it is asserted
here rather than relied upon.
"""

from __future__ import annotations

import unittest
from typing import Any, AsyncIterator

from kin_privhelper import daemon


class _DeadWriter:
    """A writer whose send always fails the way a dropped client does."""

    def __init__(self) -> None:
        self.sent = 0

    def write(self, _data: bytes) -> None:
        raise ConnectionResetError("client went away")

    async def drain(self) -> None:  # pragma: no cover - never reached
        raise ConnectionResetError("client went away")


class _LiveWriter:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        return None


class DriveHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_tail_runs_after_the_client_disconnects(self) -> None:
        import logging

        ran: dict[str, bool] = {"tail": False}

        async def handler(_args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
            for i in range(5):
                yield {"type": "stdout", "data": f"step {i}\n"}
            # Everything that matters on this appliance happens here: markers
            # written, config cleared, metrics installed.
            ran["tail"] = True
            yield {"type": "done", "exit_code": 0}

        exit_code, client_gone = await daemon._drive_handler(
            handler,
            {},
            _DeadWriter(),
            log=logging.getLogger("test"),
            audit_cmd="test_cmd",
            username="tester",
        )

        self.assertTrue(client_gone, "the writer should have been seen as gone")
        self.assertTrue(
            ran["tail"],
            "the handler was abandoned when the client left; every after-loop "
            "tail in this codebase depends on it not being",
        )
        self.assertEqual(exit_code, 0)

    async def test_a_live_client_still_gets_every_event(self) -> None:
        import logging

        async def handler(_args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "stdout", "data": "a\n"}
            yield {"type": "stdout", "data": "b\n"}
            yield {"type": "done", "exit_code": 0}

        writer = _LiveWriter()
        exit_code, client_gone = await daemon._drive_handler(
            handler,
            {},
            writer,
            log=logging.getLogger("test"),
            audit_cmd="test_cmd",
            username="tester",
        )
        self.assertFalse(client_gone)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(writer.sent), 3)

    async def test_a_handler_that_never_says_done_still_reports(self) -> None:
        import logging

        async def handler(_args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "stdout", "data": "no done event\n"}

        writer = _LiveWriter()
        exit_code, _gone = await daemon._drive_handler(
            handler,
            {},
            writer,
            log=logging.getLogger("test"),
            audit_cmd="test_cmd",
            username="tester",
        )
        # A missing done must not leave the console waiting forever.
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(writer.sent), 2)


if __name__ == "__main__":
    unittest.main()
