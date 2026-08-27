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
        ok, lines, unreadable, nonzero = await self._failcounts({})
        self.assertTrue(ok)
        self.assertEqual(unreadable, [])
        self.assertEqual(nonzero, [])
        self.assertTrue(lines)

    async def test_unreadable_entries_do_not_fail_the_check(self) -> None:
        ok, _lines, unreadable, nonzero = await self._failcounts(
            {
                "kin-drbd-clone": (1, "", "Resource kin-drbd-clone is a clone"),
                "kin-mail-svc": (0, "unexpected text", ""),
            }
        )
        self.assertTrue(ok, "unreadable must not read as failed")
        self.assertEqual(len(unreadable), 2)
        self.assertEqual(nonzero, [])

    async def test_a_real_nonzero_count_still_fails(self) -> None:
        ok, _lines, unreadable, nonzero = await self._failcounts(
            {"kin-zimbra": (0, "scope=status name=fail-count-kin-zimbra value=4", "")}
        )
        self.assertFalse(ok, "a genuine non-zero fail-count must still block")
        self.assertEqual(unreadable, [])
        # The operator must be told WHICH resource, not just that it failed.
        self.assertEqual(len(nonzero), 1)
        self.assertIn("kin-zimbra", nonzero[0])
        self.assertIn("nodeA", nonzero[0])
        self.assertIn("4", nonzero[0])

    async def test_absent_records_are_skipped_entirely(self) -> None:
        ok, lines, unreadable, nonzero = await self._failcounts(
            {r: (1, "", "Resource not found") for r in ("kin-vip", "kin-fs")}
        )
        self.assertTrue(ok)
        self.assertEqual(unreadable, [])
        self.assertEqual(nonzero, [])
        self.assertFalse(any("kin-vip" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()


class PreflightDetailTests(unittest.IsolatedAsyncioTestCase):
    """The live screenshot showed `failcount_ok=False` above a wall of
    `fail-count=0` lines, with the offending entry scrolled out of view. The
    detail must name what is actually wrong."""

    async def _preflight(self, status_extra):
        from unittest.mock import AsyncMock, patch

        from kin_privhelper import maintenance

        base = {
            "nodes": ["a", "b"],
            "standby": [],
            "promoted": "a",
            "drbd_uptodate": True,
            "qdevice_ok": True,
            "failcount_ok": True,
            "failcount_lines": ["  kin-drbd@a fail-count=0"],
            "failcount_unreadable": [],
            "failcount_nonzero": [],
            "addrs": {},
            "raw": {"crm": ""},
        }
        base.update(status_extra)
        with (
            patch.object(maintenance, "gather_status", new=AsyncMock(return_value=base)),
            patch.object(maintenance, "_https_code", new=AsyncMock(return_value=("200", 0))),
            patch.object(maintenance, "_zmcontrol_on", new=AsyncMock(return_value=(True, "ok"))),
            patch.object(maintenance, "_capture", new=AsyncMock(return_value=(0, "NO_DUAL_PRIMARY_OK", ""))),
        ):
            return await maintenance.run_preflight("b")

    async def test_failure_detail_names_the_resource_and_the_way_out(self) -> None:
        _ok, _logs, checks = await self._preflight(
            {
                "failcount_ok": False,
                "failcount_nonzero": ["kin-zimbra on a (fail-count=1)"],
            }
        )
        detail = checks["failcount_zero"]["detail"]
        self.assertFalse(checks["failcount_zero"]["ok"])
        self.assertIn("kin-zimbra", detail)
        self.assertIn("Clear stale fail-counts", detail)
        # And must NOT be a wall of zeroes.
        self.assertNotIn("fail-count=0", detail)

    async def test_passing_detail_is_short(self) -> None:
        _ok, _logs, checks = await self._preflight({})
        self.assertTrue(checks["failcount_zero"]["ok"])
        self.assertIn("fail-count=0", checks["failcount_zero"]["detail"])

    async def test_unreadable_entries_are_explained_not_hidden(self) -> None:
        _ok, logs, checks = await self._preflight(
            {"failcount_unreadable": ["kin-drbd-clone@a: exit 1"]}
        )
        self.assertTrue(checks["failcount_zero"]["ok"])
        joined = "\n".join(logs)
        self.assertIn("could not be read", joined)
        self.assertIn("kin-drbd-clone@a", joined)


class DaemonArgPassthroughTests(unittest.IsolatedAsyncioTestCase):
    """Applying a valid license failed with "That license text is incomplete".

    The daemon's audit-label block popped "token" and "search_bind_password"
    out of args before dispatching, so the handler received an empty token and
    an Active Directory bind password was silently discarded. args are never
    written to the audit log - _audit_line only ever gets a username, a command
    string, a result and an exit code - so the redaction protected nothing and
    only broke the two features (live Phase 5 QA).
    """

    def test_daemon_does_not_strip_secrets_from_args(self) -> None:
        from pathlib import Path

        src = Path(
            __file__
        ).resolve().parents[1] / "kin_privhelper" / "daemon.py"
        text = src.read_text(encoding="utf-8")
        self.assertNotIn('args.pop("token", None)', text)
        self.assertNotIn('args.pop("search_bind_password", None)', text)

    def test_audit_line_never_receives_args(self) -> None:
        import inspect

        from kin_privhelper import daemon

        sig = inspect.signature(daemon._audit_line)
        self.assertEqual(
            list(sig.parameters), ["username", "cmd", "result", "exit_code"]
        )

    async def test_a_valid_token_reaches_the_handler_intact(self) -> None:
        """End-to-end through cmd_apply_appliance_settings: the section
        dispatcher must hand _set_license exactly what it was given."""
        from unittest.mock import AsyncMock, patch

        import kin_privhelper.commands  # noqa: F401
        from kin_privhelper import appliance_settings

        seen: dict[str, object] = {}

        async def fake_set_license(args):
            seen.update(args)
            if False:
                yield {}

        with patch.object(appliance_settings, "_set_license", fake_set_license):
            events = [
                ev
                async for ev in appliance_settings.cmd_apply_appliance_settings(
                    {"section": "license", "token": "payload.signature"}
                )
            ]

        self.assertEqual(seen.get("token"), "payload.signature")
        self.assertEqual(events, [])


class BanFlagFallbackTests(unittest.TestCase):
    """The help probe is a guess; pcs rejecting the flag is proof.

    pcs 0.10 may answer `pcs resource ban --help` with its top-level usage,
    which advertises neither spelling - exactly how the operator's log looked
    when --promoted was rejected. If the probe guesses wrong, the ban must be
    retried with the other spelling rather than failing a move that would have
    worked. Nothing is changed before the retry, so it is safe.
    """

    def test_detects_a_rejected_flag(self) -> None:
        from kin_privhelper.maintenance import flag_not_recognized

        self.assertTrue(flag_not_recognized("option --promoted not recognized"))
        self.assertTrue(flag_not_recognized("Error: unrecognized option '--master'"))
        self.assertTrue(flag_not_recognized("no such option: --promoted"))

    def test_does_not_mistake_a_real_failure_for_a_bad_flag(self) -> None:
        from kin_privhelper.maintenance import flag_not_recognized

        # A genuine operational failure must NOT trigger a retry loop.
        self.assertFalse(flag_not_recognized("Error: resource 'kin-drbd-clone' not found"))
        self.assertFalse(flag_not_recognized("Error: unable to connect to the cluster"))
        self.assertFalse(flag_not_recognized(""))

    def test_the_other_flag_is_the_other_spelling(self) -> None:
        from kin_privhelper.maintenance import other_ban_flag

        self.assertEqual(other_ban_flag("--promoted"), "--master")
        self.assertEqual(other_ban_flag("--master"), "--promoted")

    def test_top_level_usage_falls_back_to_master(self) -> None:
        # What pcs 0.10 actually printed in the operator's log.
        from kin_privhelper.maintenance import parse_promoted_ban_flag

        usage = (
            "Usage: pcs [-f file] [-h] [commands]...\n"
            "Options:\n  -h, --help  Display usage and exit.\n"
            "  --force  Override checks and errors\n"
        )
        self.assertEqual(parse_promoted_ban_flag(usage), "--master")
