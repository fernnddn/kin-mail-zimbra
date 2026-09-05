"""A Move Master that is interrupted must not leave the cluster pinned.

Move Master works by banning the DRBD clone off the node that currently holds
it: `pcs resource ban` writes a -INFINITY location constraint. Every handled
failure in that function removes it again, and the code says why - a ban left
behind means no node is allowed to be Promoted, so DRBD has no Primary, the
resource group cannot start, and mail is down on BOTH nodes with nothing on
screen naming the constraint that did it.

Nothing removed it if something raised. The window between placing the ban and
clearing it runs a settle poll and several gather_status calls, any of which
can raise on a timeout or a subprocess error - and closing the browser tab
closes this async generator, which raises GeneratorExit inside it. That last
one needs no fault at all: click Move Master, close the tab, mail stops.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[1] / "kin_privhelper" / "maintenance.py"
)


def _move_master_region() -> tuple[list[str], str]:
    text = SRC.read_text(encoding="utf-8")
    lines = text.split("\n")
    start = next(
        i for i, l in enumerate(lines) if 'pcs resource ban {DRBD_CLONE}' in l
    )
    end = next(
        i for i, l in enumerate(lines) if i > start and "except BaseException:" in l
    )
    return lines[start:end], text


class TheBanIsAlwaysTornDown(unittest.TestCase):
    def test_the_ban_window_is_wrapped(self) -> None:
        region, text = _move_master_region()
        self.assertIn("ban_cleared = False", "\n".join(region))
        self.assertIn("except BaseException:", text)

    def test_the_teardown_clears_the_same_resource_it_banned(self) -> None:
        text = SRC.read_text(encoding="utf-8")
        after = text[text.index("except BaseException:") :]
        self.assertIn('["pcs", "resource", "clear", DRBD_CLONE]', after)

    def test_the_teardown_reraises(self) -> None:
        # Swallowing here would report a cancelled move as a completed one.
        text = SRC.read_text(encoding="utf-8")
        block = text[text.index("except BaseException:") :][:1200]
        self.assertIn("raise", block)

    def test_the_teardown_does_not_yield(self) -> None:
        # An async generator may not yield while it is being closed; a yield in
        # this handler turns a closed tab into "async generator ignored
        # GeneratorExit" and the clear never runs.
        text = SRC.read_text(encoding="utf-8")
        start = text.index("except BaseException:")
        block = text[start : text.index("raise", start)]
        code = "\n".join(
            line.split("#", 1)[0] for line in block.split("\n")
        )  # the prose in here says "no yields", which is not a yield
        self.assertNotIn("yield", code)

    def test_it_catches_base_exception_not_exception(self) -> None:
        # GeneratorExit and CancelledError both derive from BaseException, not
        # Exception. Catching Exception would miss the closed-tab case, which
        # is the one that needs no fault at all.
        text = SRC.read_text(encoding="utf-8")
        idx = text.index("ban_cleared = False")
        tail = text[idx:]
        first_handler = tail[tail.index("except ") : tail.index("except ") + 40]
        self.assertIn("BaseException", first_handler)

    def test_every_successful_clear_records_that_it_happened(self) -> None:
        # So the teardown does not fire a second, pointless pcs call after a
        # handled path already cleared.
        text = SRC.read_text(encoding="utf-8")
        self.assertEqual(text.count("ban_cleared = c"), 3)

    def test_the_teardown_cannot_itself_raise(self) -> None:
        # If pcs is unreachable the original exception must still propagate.
        text = SRC.read_text(encoding="utf-8")
        block = text[text.index("if not ban_cleared:") :][:600]
        self.assertIn("try:", block)
        self.assertIn("except BaseException:", block)


class TheGuardIsSyntacticallyWhereItClaims(unittest.TestCase):
    """Parse it rather than trust the line ranges above."""

    def test_the_try_encloses_the_settle_and_the_clear(self) -> None:
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body = ast.get_source_segment(SRC.read_text(encoding="utf-8"), node) or ""
            if "wait_failback_settled" in body and "resource" in body:
                handlers = [
                    h
                    for h in node.handlers
                    if isinstance(h.type, ast.Name) and h.type.id == "BaseException"
                ]
                if handlers:
                    found.append(body)
        self.assertTrue(
            found, "no try/except BaseException encloses the settle and the clear"
        )
        body = found[0]
        self.assertIn("pcs", body)
        self.assertIn("clear", body)


if __name__ == "__main__":
    unittest.main()


class TheTeardownActuallyRuns(unittest.IsolatedAsyncioTestCase):
    """Drive the real generator and watch what it does to the cluster.

    Everything above reads the source. This runs it: stub out pcs so the move
    reaches the ban, make the settle raise, and check a `clear` was issued for
    the same resource.
    """

    async def _run_move(self, *, fail_with: BaseException | None, close_early: bool):
        from unittest import mock

        from kin_privhelper import maintenance as mt

        calls: list[list[str]] = []

        async def fake_capture(argv, timeout=None, **kw):  # noqa: ANN001
            calls.append(list(argv))
            return 0, "", ""

        async def fake_settle(target):  # noqa: ANN001
            if fail_with is not None:
                raise fail_with
            return True, []

        status = {
            "promoted": "mail-a.example.test",
            "nodes": ["mail-a.example.test", "mail-b.example.test"],
            "standby": [], "offline": [], "stale_peers": [],
            "qdevice_ok": True, "drbd_uptodate": True,
            "vip_node": "mail-b.example.test", "zimbra_node": "mail-b.example.test",
            "local_host": "mail-a.example.test",
        }

        with mock.patch.object(mt, "_capture", fake_capture), \
             mock.patch.object(mt, "wait_failback_settled", fake_settle), \
             mock.patch.object(mt, "gather_status", mock.AsyncMock(return_value=status)), \
             mock.patch.object(mt, "wait_drbd_uptodate", mock.AsyncMock(return_value=(True, []))), \
             mock.patch.object(mt, "run_preflight", mock.AsyncMock(return_value=(True, [], {}))), \
             mock.patch.object(mt, "try_lock_maintenance", lambda: object()), \
             mock.patch.object(mt, "release_maintenance_lock", lambda fh: None), \
             mock.patch.object(mt, "resolve_target", mock.AsyncMock(return_value="mail-b.example.test")), \
             mock.patch.object(mt, "promoted_ban_flag", mock.AsyncMock(return_value="--promoted")):
            agen = mt._maintenance_events({"op": "failback", "target": "mail-b.example.test"})
            try:
                async for _ev in agen:
                    if close_early and any(
                        c[:3] == ["pcs", "resource", "ban"] for c in calls
                    ):
                        await agen.aclose()
                        break
            except BaseException:
                pass
        return calls

    def _cleared(self, calls: list[list[str]]) -> bool:
        return any(c[:3] == ["pcs", "resource", "clear"] for c in calls)

    def _banned(self, calls: list[list[str]]) -> bool:
        return any(c[:3] == ["pcs", "resource", "ban"] for c in calls)

    async def test_an_exception_mid_move_still_clears_the_ban(self) -> None:
        calls = await self._run_move(fail_with=TimeoutError("settle blew up"), close_early=False)
        self.assertTrue(self._banned(calls), "the move never got as far as banning")
        self.assertTrue(
            self._cleared(calls),
            "the ban was left in place after an exception - no node can be promoted",
        )

    async def test_a_cancelled_move_still_clears_the_ban(self) -> None:
        import asyncio

        calls = await self._run_move(fail_with=asyncio.CancelledError(), close_early=False)
        self.assertTrue(self._banned(calls))
        self.assertTrue(self._cleared(calls), "a cancelled move left the cluster pinned")

    async def test_a_normal_move_clears_the_ban_exactly_once(self) -> None:
        calls = await self._run_move(fail_with=None, close_early=False)
        self.assertTrue(self._banned(calls))
        clears = [c for c in calls if c[:3] == ["pcs", "resource", "clear"]]
        self.assertEqual(len(clears), 1, f"expected one clear, got {clears}")
