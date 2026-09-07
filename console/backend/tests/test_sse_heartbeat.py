"""Guards on the SSE frame pump.

Every streamed operation goes through this: deploy, Build HA pair, maintenance,
remove host, the observability lifecycle. A bug here does not break one screen,
it breaks the operator's view of every long job at once.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, AsyncIterator

from kin_console.app import sse_with_heartbeat


async def _never_disconnected() -> bool:
    return False


def _connected_after(n: int):
    """is_disconnected that flips to True after n calls."""
    state = {"calls": 0}

    async def check() -> bool:
        state["calls"] += 1
        return state["calls"] > n

    return check


async def _drain(gen: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in gen]


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_pass_through_in_order(self) -> None:
        async def src() -> AsyncIterator[dict[str, Any]]:
            for i in range(3):
                yield {"type": "stdout", "data": f"line {i}\n"}

        out = await _drain(
            sse_with_heartbeat(src(), is_disconnected=_never_disconnected)
        )
        self.assertEqual(len(out), 3)
        for i, chunk in enumerate(out):
            self.assertTrue(chunk.startswith("data: "))
            self.assertTrue(chunk.endswith("\n\n"))
            self.assertIn(f"line {i}", chunk)

    async def test_a_quiet_source_still_produces_keepalives(self) -> None:
        """The whole point: an idle connection across a firewall gets reaped,
        and the operator blames a job that was running fine."""

        async def slow() -> AsyncIterator[dict[str, Any]]:
            await asyncio.sleep(0.25)
            yield {"type": "done", "exit_code": 0}

        out = await _drain(
            sse_with_heartbeat(
                slow(), is_disconnected=_never_disconnected, heartbeat_sec=0.05
            )
        )
        beats = [c for c in out if c.startswith(":")]
        data = [c for c in out if c.startswith("data: ")]
        self.assertGreaterEqual(len(beats), 2, out)
        self.assertEqual(len(data), 1)
        # An SSE comment carries no data, so EventSource ignores it entirely.
        for beat in beats:
            self.assertTrue(beat.startswith(": "))
            self.assertTrue(beat.endswith("\n\n"))

    async def test_no_keepalive_when_the_source_is_busy(self) -> None:
        async def chatty() -> AsyncIterator[dict[str, Any]]:
            for i in range(5):
                yield {"type": "stdout", "data": f"{i}"}

        out = await _drain(
            sse_with_heartbeat(
                chatty(), is_disconnected=_never_disconnected, heartbeat_sec=5.0
            )
        )
        self.assertEqual([c for c in out if c.startswith(":")], [])

    async def test_ends_when_the_browser_goes_away(self) -> None:
        async def endless() -> AsyncIterator[dict[str, Any]]:
            while True:
                yield {"type": "stdout", "data": "x"}
                await asyncio.sleep(0)

        out = await asyncio.wait_for(
            _drain(
                sse_with_heartbeat(endless(), is_disconnected=_connected_after(2))
            ),
            timeout=5,
        )
        self.assertLessEqual(len(out), 3)

    async def test_a_source_that_raises_surfaces_the_error(self) -> None:
        class Boom(RuntimeError):
            pass

        async def bad() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "stdout", "data": "before\n"}
            raise Boom("privhelper went away")

        gen = sse_with_heartbeat(bad(), is_disconnected=_never_disconnected)
        with self.assertRaises(Boom):
            await _drain(gen)

    async def test_the_pump_does_not_outlive_the_stream(self) -> None:
        """A leaked pump task would keep reading privhelperd after the client
        is gone, for every abandoned stream, forever."""
        started = asyncio.Event()

        async def endless() -> AsyncIterator[dict[str, Any]]:
            started.set()
            while True:
                await asyncio.sleep(0.01)
                yield {"type": "stdout", "data": "x"}

        before = len(asyncio.all_tasks())
        gen = sse_with_heartbeat(endless(), is_disconnected=_never_disconnected)
        await gen.__anext__()
        await started.wait()
        await gen.aclose()
        await asyncio.sleep(0.05)
        self.assertLessEqual(len(asyncio.all_tasks()), before + 1)


if __name__ == "__main__":
    unittest.main()
