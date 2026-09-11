"""A damaged wizard draft must not take the console down.

Every wizard page reads the draft. When load_draft() raised, FastAPI turned
that into a 500, so one truncated file made the whole wizard answer "internal
server error" on every load - permanently, until somebody deleted it over SSH.
An operator whose deploy had partly failed hit exactly that while trying to
re-run it (live QA Phase 15, 11 September 2026).

The draft is a convenience: it remembers what was typed. Losing it costs some
retyping. Refusing to render costs the operator the machine.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kin_console import draft as D


class ADamagedDraftDegradesInsteadOfRaising(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "draft.json"
        self.addCleanup(self._tmp.cleanup)
        patch = mock.patch.object(D, "draft_path", lambda: self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_missing_draft_is_an_empty_one(self) -> None:
        self.assertIsNotNone(D.load_draft())

    def test_a_truncated_draft_does_not_raise(self) -> None:
        self.path.write_text('{"topology": "1vm", "mail_dom', encoding="utf-8")
        self.assertIsNotNone(D.load_draft())

    def test_an_empty_file_does_not_raise(self) -> None:
        self.path.write_text("", encoding="utf-8")
        self.assertIsNotNone(D.load_draft())

    def test_binary_rubbish_does_not_raise(self) -> None:
        self.path.write_bytes(b"\x00\x01\x02\xff\xfe")
        self.assertIsNotNone(D.load_draft())

    def test_a_json_array_does_not_raise(self) -> None:
        self.path.write_text('["not", "an", "object"]', encoding="utf-8")
        self.assertIsNotNone(D.load_draft())

    def test_a_draft_from_another_version_does_not_raise(self) -> None:
        # A field that is now an int arriving as a dict, say.
        self.path.write_text(
            json.dumps({"topology": {"unexpected": "shape"}, "version": 99}),
            encoding="utf-8",
        )
        self.assertIsNotNone(D.load_draft())

    def test_the_damaged_file_is_kept_rather_than_deleted(self) -> None:
        """It is evidence. Deleting it destroys the only clue to what happened."""
        self.path.write_text("{ truncated", encoding="utf-8")
        D.load_draft()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ truncated")

    def test_a_good_draft_still_round_trips(self) -> None:
        """A rule broad enough to swallow everything would swallow the feature."""
        good = D.default_draft()
        good.mail_domain = "example.test"
        D.save_draft(good)
        self.assertEqual(D.load_draft().mail_domain, "example.test")

    def test_the_route_returns_a_body_rather_than_raising(self) -> None:
        from kin_console import app as app_module

        self.path.write_text("{ truncated", encoding="utf-8")

        class _Actor:
            username = "op"
            role = "kin_super_admin"

        got = app_module.get_wizard_draft(_actor=_Actor())
        self.assertIsInstance(got, dict)


if __name__ == "__main__":
    unittest.main()
