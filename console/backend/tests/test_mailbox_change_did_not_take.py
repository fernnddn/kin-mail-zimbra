"""What the console says when a mailbox change reported success and did nothing.

zmprov on a node without a local mailboxd falls back to LDAP-only mode, where
`da` and `ra` ask "Continue? [Y]es, [N]o", read end-of-file, abort - and exit 0.
On a multi deployment that is every delete and rename the console performs, so
the console told operators a mailbox was deleted while it was still receiving
mail and still holding a seat.

The stage now checks the directory afterwards and fails. This pins the other
half: that the reason reaches the operator intact instead of being flattened
into "The mailbox change did not succeed", which is true and tells them nothing
about the mailbox that is still live.
"""

from __future__ import annotations

import unittest

from kin_console.app import _human_mailbox_error


class AChangeThatDidNotTake(unittest.TestCase):
    def test_a_delete_that_removed_nothing_says_the_mailbox_is_still_live(self) -> None:
        msg = _human_mailbox_error(
            "==> Deleting mailbox jane@example.test\n"
            "  [FAIL] The directory still lists jane@example.test after the delete.\n",
            "",
        )
        self.assertIn("did not take effect", msg)
        self.assertIn("still receiving", msg)
        self.assertIn("Do not treat it as done", msg)

    def test_a_rename_that_did_not_move_the_address(self) -> None:
        msg = _human_mailbox_error(
            "  [FAIL] The directory does not list janet@example.test after the rename.\n",
            "",
        )
        self.assertIn("did not take effect", msg)

    def test_a_rename_that_left_the_old_address_behind(self) -> None:
        msg = _human_mailbox_error(
            "  [FAIL] The directory still lists the old address jane@example.test.\n",
            "",
        )
        self.assertIn("did not take effect", msg)

    def test_it_is_not_flattened_into_the_generic_message(self) -> None:
        msg = _human_mailbox_error("The directory still lists jane@example.test.", "")
        self.assertNotEqual(msg, "The mailbox change did not succeed.")


class TheOtherReasonsStillReadAsThemselves(unittest.TestCase):
    """The new branch sits last on purpose and must not swallow the specific ones."""

    def test_already_exists(self) -> None:
        self.assertEqual(
            _human_mailbox_error("account already exists: a@b.test", ""),
            "That mailbox already exists.",
        )

    def test_not_found(self) -> None:
        self.assertEqual(
            _human_mailbox_error("Account not found: ghost@b.test", ""),
            "Mailbox not found.",
        )

    def test_seat_limit(self) -> None:
        self.assertIn(
            "contracted mailboxes are in use",
            _human_mailbox_error("seat limit reached", ""),
        )

    def test_an_unrelated_failure_keeps_the_generic_message(self) -> None:
        self.assertEqual(
            _human_mailbox_error("something else went wrong", ""),
            "The mailbox change did not succeed.",
        )


if __name__ == "__main__":
    unittest.main()
