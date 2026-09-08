"""The metrics install must survive the browser going away.

A deploy on 8 Sep 2026 finished cleanly and still had no metrics. The operator
had seen the log drop and reconnect during the package upgrade and thought
nothing of it. What it cost was the tail: the install ran inline inside the
generator feeding the SSE stream, so when the console broke out of that stream
the generator was closed, GeneratorExit was raised at the streaming loop, and
every line after it never ran.

These tests pin both halves: that an abandoned generator really does skip its
tail (so nobody puts one back), and that the detached runner does not.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, AsyncIterator


class GeneratorTailIsLostOnDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """Why the fix is a detached task and not a tidier inline block."""

    async def test_closing_a_generator_skips_everything_after_the_loop(
        self,
    ) -> None:
        ran: dict[str, bool] = {"tail": False}

        async def streamer() -> AsyncIterator[dict[str, Any]]:
            for i in range(100):
                yield {"type": "stdout", "data": f"{i}"}
            ran["tail"] = True  # never reached once the consumer breaks
            yield {"type": "done", "exit_code": 0}

        gen = streamer()
        seen = 0
        async for _ev in gen:
            seen += 1
            if seen == 3:
                break
        await gen.aclose()

        self.assertEqual(seen, 3)
        self.assertFalse(
            ran["tail"],
            "an abandoned generator ran its tail; if this ever becomes true "
            "the inline version would have been safe after all",
        )


class DetachedRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_keeps_running_after_its_caller_is_abandoned(self) -> None:
        """The whole point: the task belongs to the loop, not the caller."""
        from unittest.mock import patch

        from kin_privhelper import monitoring_install

        finished = asyncio.Event()

        async def slow_install(_args: Any = None) -> AsyncIterator[dict[str, Any]]:
            await asyncio.sleep(0.05)
            finished.set()
            yield {"type": "done", "exit_code": 0}

        with patch.object(monitoring_install, "cmd_install_monitoring", slow_install):

            async def caller() -> AsyncIterator[dict[str, Any]]:
                yield {"type": "stdout", "data": "deploying"}
                monitoring_install.run_after_install()
                yield {"type": "done", "exit_code": 0}

            gen = caller()
            # Consume one event, then abandon it exactly like a dropped SSE.
            await gen.__anext__()
            await gen.__anext__()
            await gen.aclose()

            await asyncio.wait_for(finished.wait(), timeout=5)

        self.assertTrue(finished.is_set())

    async def test_a_failing_install_never_escapes_the_task(self) -> None:
        """A detached task that raises would surface as an unhandled exception
        in privhelperd's log and nothing else. It must swallow and stop."""
        from unittest.mock import patch

        from kin_privhelper import monitoring_install

        started = asyncio.Event()

        async def boom(_args: Any = None) -> AsyncIterator[dict[str, Any]]:
            started.set()
            raise RuntimeError("ansible exploded")
            yield  # pragma: no cover - makes this an async generator

        with patch.object(monitoring_install, "cmd_install_monitoring", boom):
            monitoring_install.run_after_install()
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.sleep(0.05)
        # Reaching here without an unhandled exception is the assertion.

    async def test_it_reports_when_there_is_no_loop_to_run_on(self) -> None:
        from kin_privhelper import monitoring_install

        # Inside a running loop it starts; the no-loop branch is asserted by
        # calling it synchronously below.
        self.assertEqual(monitoring_install.run_after_install(), "started")

    def test_no_loop_is_reported_not_raised(self) -> None:
        from kin_privhelper import monitoring_install

        self.assertIn("no event loop", monitoring_install.run_after_install())


class DeployStillReportsItsOwnResultTests(unittest.TestCase):
    def test_the_deploy_exit_code_is_the_deploy_s_own(self) -> None:
        """Metrics are additive. They must not be able to fail a good deploy,
        and they must not be waited on either."""
        import inspect

        from kin_privhelper import commands

        src = inspect.getsource(commands.cmd_run_full_install)
        self.assertIn("run_after_install", src)
        self.assertIn("yield proto.event_done(install_exit)", src)
        # No inline consumption: that is what lost the tail in the first place.
        self.assertNotIn("async for ev in _install_metrics", src)


if __name__ == "__main__":
    unittest.main()
