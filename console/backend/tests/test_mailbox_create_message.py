"""mailbox_create's operator-facing message when zmprov ca succeeded but the
post-create auth probe did not (08-create-mailbox.sh). exit_code is non-zero
in that case, so the frontend's !ok branch used to show "Mailbox was not
created." - false, since the account exists and a seat is already used
(re-audit, 25 Aug 2026)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from kin_console.app import _mailbox_create_message


class MailboxCreateMessageTests(unittest.TestCase):
    def test_auth_probe_failed_overrides_with_account_exists_message(self) -> None:
        msg = _mailbox_create_message(
            "some seat message",
            {"email": "new.user@example.test", "auth_probe_failed": True},
        )
        self.assertIn("new.user@example.test", str(msg))
        self.assertIn("was created", str(msg))
        self.assertIn("Do not create it again", str(msg))

    def test_missing_email_still_produces_a_message(self) -> None:
        msg = _mailbox_create_message("seat message", {"auth_probe_failed": True})
        self.assertIn("The account", str(msg))

    def test_normal_failure_keeps_the_seat_message(self) -> None:
        msg = _mailbox_create_message("Quota exceeded", {})
        self.assertEqual(msg, "Quota exceeded")

    def test_success_keeps_the_seat_message(self) -> None:
        msg = _mailbox_create_message(
            "1 of 25 seats used", {"email": "new.user@example.test"}
        )
        self.assertEqual(msg, "1 of 25 seats used")


class CreateMailboxInvalidLicenseTests(unittest.IsolatedAsyncioTestCase):
    async def test_garbage_token_blocks_before_script(self) -> None:
        from kin_privhelper.commands import cmd_create_mailbox

        with (
            patch("kin_privhelper.deploy_state.read_license_token", return_value="garbage"),
            patch("kin_privhelper.deploy_state.read_server_id", return_value="sid"),
            patch("kin_privhelper.deploy_state.ensure_server_id", return_value="sid"),
            patch("kin_privhelper.commands.validate_create_mailbox_args") as validate,
            patch("kin_privhelper.commands._stream_subprocess") as stream,
        ):
            events = [ev async for ev in cmd_create_mailbox({"op": "create"})]

        validate.assert_not_called()
        stream.assert_not_called()
        done = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].get("exit_code"), 1)
        self.assertTrue(
            any("invalid, in grace, or expired" in str(ev.get("data")) for ev in events)
        )


if __name__ == "__main__":
    unittest.main()
