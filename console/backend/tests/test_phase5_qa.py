"""Phase 5 QA findings from the live 2-host deployment (27 Aug 2026).

The deploy itself was clean; these are the console defects the operator found
afterwards.
"""

from __future__ import annotations

import unittest

from kin_privhelper.maintenance import parse_promoted_ban_flag


class PromotedBanFlagTests(unittest.TestCase):
    """Move Master failed outright on the live pair.

        pcs resource ban kin-drbd-clone mail... --promoted
        option --promoted not recognized

    The nodes run Ubuntu 22.04, which ships pcs 0.10; --promoted only exists
    from pcs 0.11. The flag has to come from the local pcs, not a constant.
    """

    PCS_011 = "Usage: pcs resource ban <resource id> [node] [--promoted] [lifetime=...]"
    PCS_010 = "Usage: pcs resource ban <resource id> [node] [--master] [lifetime=...]"

    def test_pcs_011_uses_promoted(self) -> None:
        self.assertEqual(parse_promoted_ban_flag(self.PCS_011), "--promoted")

    def test_pcs_010_uses_master(self) -> None:
        self.assertEqual(parse_promoted_ban_flag(self.PCS_010), "--master")

    def test_prefers_promoted_when_both_are_advertised(self) -> None:
        # pcs 0.10.8+ documents both; --master is deprecated there.
        both = "[--promoted] ... [--master] (deprecated)"
        self.assertEqual(parse_promoted_ban_flag(both), "--promoted")

    def test_unreadable_help_falls_back_to_the_shipped_release(self) -> None:
        # If we cannot tell, guess the release this product actually runs on
        # rather than the one that fails there.
        for text in ("", "   ", "pcs: command not found"):
            self.assertEqual(parse_promoted_ban_flag(text), "--master")


class FailcountReadabilityTests(unittest.IsolatedAsyncioTestCase):
    """A brand-new cluster reported "fail-count nonzero" with nothing wrong.

    preflight gates on failcount_ok, so this blocked Enter Maintenance and
    Remove Host on a healthy pair. The cause was treating a query we could not
    READ as a failure COUNT - and crm_failcount does not answer for the clone
    and the group on every Pacemaker build, so unreadable is normal.
    """

    async def _failcounts(self, responses):
        from unittest.mock import AsyncMock, patch

        from kin_privhelper import maintenance

        async def fake(argv, timeout=30.0):
            res = argv[argv.index("-r") + 1]
            return responses.get(res, (0, "value=0", ""))

        with patch.object(maintenance, "_capture", new=AsyncMock(side_effect=fake)):
            return await maintenance._failcounts(["nodeA"])

    async def test_all_zero_is_ok(self) -> None:
        ok, lines, unreadable = await self._failcounts({})
        self.assertTrue(ok)
        self.assertEqual(unreadable, [])
        self.assertTrue(lines)

    async def test_unreadable_entries_do_not_fail_the_check(self) -> None:
        ok, _lines, unreadable = await self._failcounts(
            {
                "kin-drbd-clone": (1, "", "Resource kin-drbd-clone is a clone"),
                "kin-mail-svc": (0, "unexpected text", ""),
            }
        )
        self.assertTrue(ok, "unreadable must not read as failed")
        self.assertEqual(len(unreadable), 2)

    async def test_a_real_nonzero_count_still_fails(self) -> None:
        ok, _lines, unreadable = await self._failcounts(
            {"kin-zimbra": (0, "scope=status name=fail-count-kin-zimbra value=4", "")}
        )
        self.assertFalse(ok, "a genuine non-zero fail-count must still block")
        self.assertEqual(unreadable, [])

    async def test_absent_records_are_skipped_entirely(self) -> None:
        ok, lines, unreadable = await self._failcounts(
            {r: (1, "", "Resource not found") for r in ("kin-vip", "kin-fs")}
        )
        self.assertTrue(ok)
        self.assertEqual(unreadable, [])
        self.assertFalse(any("kin-vip" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
