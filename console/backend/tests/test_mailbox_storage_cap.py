"""Per-mailbox storage caps: what may be sent to zmprov, and what may not.

A cap is a number that reaches a shell command and then Zimbra's directory. The
validator is the only thing between an operator's text box and that command, so
what it refuses matters more than what it accepts.

It is also the one mailbox operation that is NOT a create: the seat gate counts
mailboxes against a contract and a cap changes no count, which lib/quota-gate.sh
states in its own SCOPE. A cap must never consume or require a seat.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi import HTTPException

from kin_console import app as app_module
from kin_console.users import ConsoleUser
from kin_privhelper.commands import validate_create_mailbox_args
from kin_privhelper.rbac import ROLE_SUPER_ADMIN


class ACapIsCarriedToTheScript(unittest.TestCase):
    def test_bytes_reach_zmprov_unchanged(self) -> None:
        op, argv = validate_create_mailbox_args(
            {"op": "set_quota", "email": "jane@mail.example.test", "quota_bytes": 5 * 1024**3}
        )
        self.assertEqual(op, "set_quota")
        self.assertEqual(argv, ["--set-quota", "jane@mail.example.test", "5368709120"])

    def test_zero_means_no_cap_and_is_not_treated_as_missing(self) -> None:
        # 0 is Zimbra's own spelling for "no cap". A validator that tested the
        # value for truthiness would reject the one way to remove a cap.
        _op, argv = validate_create_mailbox_args(
            {"op": "set_quota", "email": "jane@mail.example.test", "quota_bytes": 0}
        )
        self.assertEqual(argv[2], "0")

    def test_the_address_is_lowercased_like_every_other_op(self) -> None:
        _op, argv = validate_create_mailbox_args(
            {"op": "set_quota", "email": "  Jane@Mail.Example.Test ", "quota_bytes": 1}
        )
        self.assertEqual(argv[1], "jane@mail.example.test")


class ACapThatWouldBeWrongIsRefused(unittest.TestCase):
    def _refused(self, args: dict[str, object]) -> str:
        with self.assertRaises(ValueError) as caught:
            validate_create_mailbox_args({"op": "set_quota", **args})
        return str(caught.exception)

    def test_negative(self) -> None:
        self.assertIn(
            "negative",
            self._refused({"email": "a@b.test", "quota_bytes": -1}),
        )

    def test_a_fraction_is_refused_rather_than_rounded(self) -> None:
        # Rounding here would silently store a different cap than the one the
        # operator typed, and the unit it was typed in is already lost by now.
        self.assertIn(
            "whole number",
            self._refused({"email": "a@b.test", "quota_bytes": 1.5}),
        )

    def test_a_string_of_digits_is_not_a_number(self) -> None:
        self.assertIn(
            "whole number",
            self._refused({"email": "a@b.test", "quota_bytes": "1073741824"}),
        )

    def test_a_boolean_is_not_a_number(self) -> None:
        # bool is an int in Python, so True would otherwise pass every numeric
        # check and reach zmprov as the cap "1".
        self.assertIn(
            "whole number",
            self._refused({"email": "a@b.test", "quota_bytes": True}),
        )

    def test_beyond_exact_representation(self) -> None:
        # The console renders this number in a browser, which cannot hold an
        # integer above 2^53 exactly - the cap shown would differ from the cap
        # stored.
        self.assertIn(
            "too large",
            self._refused({"email": "a@b.test", "quota_bytes": 2**53 + 1}),
        )
        # The boundary itself is allowed.
        _op, argv = validate_create_mailbox_args(
            {"op": "set_quota", "email": "a@b.test", "quota_bytes": 2**53}
        )
        self.assertEqual(argv[2], str(2**53))

    def test_missing_address(self) -> None:
        self.assertIn("email is required", self._refused({"quota_bytes": 1}))
        self.assertIn("email is required", self._refused({"email": "nodomain", "quota_bytes": 1}))

    def test_a_missing_cap_is_not_silently_zero(self) -> None:
        # Sending no number at all must not remove the cap.
        self.assertIn("whole number", self._refused({"email": "a@b.test"}))


class ACapIsNotACreate(unittest.TestCase):
    def test_it_never_reaches_the_create_path(self) -> None:
        op, argv = validate_create_mailbox_args(
            {"op": "set_quota", "email": "a@b.test", "quota_bytes": 1}
        )
        self.assertEqual(op, "set_quota")
        # No password, and the flag the script dispatches on is the cap one.
        self.assertEqual(argv[0], "--set-quota")
        self.assertNotIn("--account-status", argv)

    def test_an_unknown_op_still_names_the_ones_that_exist(self) -> None:
        with self.assertRaises(ValueError) as caught:
            validate_create_mailbox_args({"op": "set_the_quota"})
        self.assertIn("set_quota", str(caught.exception))


def _run(coro):
    return asyncio.run(coro)


def _privhelper_returning(log: str, ok: bool = True):
    async def fake(_cmd, _username, args=None):
        fake.args = args
        return {"ok": ok, "log": log, "error": None if ok else "failed"}

    fake.args = None
    return fake


class TheEndpointAnOperatorActuallyUses(unittest.TestCase):
    """POST /api/mailbox/quota, called directly (no TestClient: httpx is not a dependency)."""

    def _call(self, body, fake, role: str = ROLE_SUPER_ADMIN):
        user = ConsoleUser(username="admin", role=role)
        with mock.patch.object(app_module, "_collect_privhelper", fake):
            return _run(app_module.mailbox_quota(body, user))

    def test_a_cap_is_reported_back_with_what_the_mailbox_holds(self) -> None:
        body = app_module.MailboxQuotaBody(email="Jane@Example.Test", quota_bytes=5 * 1024**3)
        fake = _privhelper_returning(
            'QUOTA_JSON:{"email":"jane@example.test",'
            '"quota_bytes":5368709120,"used_bytes":1073741824}\n'
        )
        out = self._call(body, fake)
        self.assertTrue(out["ok"])
        self.assertEqual(out["email"], "jane@example.test")
        self.assertEqual(out["quota_bytes"], 5 * 1024**3)
        self.assertEqual(out["used_bytes"], 1073741824)
        self.assertFalse(out["over_cap"])
        # The op that reaches the privileged side must be the cap one, never create.
        self.assertEqual(fake.args["op"], "set_quota")

    def test_a_cap_below_current_size_is_flagged(self) -> None:
        # The operator has just stopped this mailbox receiving mail. Saying so
        # here is the difference between a decision and an accident.
        body = app_module.MailboxQuotaBody(email="jane@example.test", quota_bytes=1024)
        fake = _privhelper_returning(
            'QUOTA_JSON:{"email":"jane@example.test","quota_bytes":1024,"used_bytes":2570}\n'
        )
        self.assertTrue(self._call(body, fake)["over_cap"])

    def test_removing_a_cap_is_never_flagged(self) -> None:
        body = app_module.MailboxQuotaBody(email="jane@example.test", quota_bytes=0)
        fake = _privhelper_returning(
            'QUOTA_JSON:{"email":"jane@example.test","quota_bytes":0,"used_bytes":999999}\n'
        )
        out = self._call(body, fake)
        self.assertFalse(out["over_cap"])

    def test_unknown_usage_is_not_guessed_at(self) -> None:
        # A mail store that did not answer must not be reported as an empty
        # mailbox, which would make any cap look safe.
        body = app_module.MailboxQuotaBody(email="jane@example.test", quota_bytes=1024)
        fake = _privhelper_returning(
            'QUOTA_JSON:{"email":"jane@example.test","quota_bytes":1024,"used_bytes":null}\n'
        )
        out = self._call(body, fake)
        self.assertIsNone(out["used_bytes"])
        self.assertFalse(out["over_cap"])

    def test_a_failure_is_a_400_not_a_stack_trace(self) -> None:
        body = app_module.MailboxQuotaBody(email="ghost@example.test", quota_bytes=1024)
        fake = _privhelper_returning("Account not found: ghost@example.test\n", ok=False)
        with self.assertRaises(HTTPException) as caught:
            self._call(body, fake)
        self.assertEqual(caught.exception.status_code, 400)


class TheBodyRefusesWhatTheScriptWould(unittest.TestCase):
    """Caught at the edge of the API, so a bad number never reaches a shell argument."""

    def test_negative_and_oversized_are_rejected_before_any_command_runs(self) -> None:
        from pydantic import ValidationError

        for bad in (-1, 2**53 + 1):
            with self.assertRaises(ValidationError):
                app_module.MailboxQuotaBody(email="a@b.test", quota_bytes=bad)

    def test_zero_and_the_ceiling_are_allowed(self) -> None:
        self.assertEqual(app_module.MailboxQuotaBody(email="a@b.test", quota_bytes=0).quota_bytes, 0)
        self.assertEqual(
            app_module.MailboxQuotaBody(email="a@b.test", quota_bytes=2**53).quota_bytes, 2**53
        )


if __name__ == "__main__":
    unittest.main()
