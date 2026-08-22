"""Session lifetime during setup vs post-deploy, and bootstrap password."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kin_console import auth
from kin_console.settings import settings


class SessionLifetimeTests(unittest.TestCase):
    def test_setup_cookie_is_seven_days(self) -> None:
        with patch.object(auth, "_mail_is_deployed", return_value=False):
            self.assertEqual(auth.cookie_max_age_sec(), settings.setup_session_max_age_sec)
            self.assertEqual(auth.cookie_max_age_sec(), 60 * 60 * 24 * 7)

    def test_deployed_cookie_is_twelve_hours(self) -> None:
        with patch.object(auth, "_mail_is_deployed", return_value=True):
            self.assertEqual(auth.cookie_max_age_sec(), settings.session_max_age_sec)
            self.assertEqual(auth.cookie_max_age_sec(), 60 * 60 * 12)

    def test_loads_accepts_the_longest_issued_cookie(self) -> None:
        """A long setup must still validate after the deploy gate flips."""
        self.assertEqual(auth.loads_max_age_sec(), settings.setup_session_max_age_sec)
        self.assertGreater(auth.loads_max_age_sec(), settings.session_max_age_sec)

    def test_undeployed_permission_error_uses_setup_lifetime(self) -> None:
        with patch(
            "kin_privhelper.deploy_state.is_mail_deployed",
            side_effect=PermissionError("denied"),
        ):
            self.assertFalse(auth._mail_is_deployed())
            self.assertEqual(auth.cookie_max_age_sec(), settings.setup_session_max_age_sec)

    def test_set_cookie_uses_setup_max_age_when_not_deployed(self) -> None:
        response = MagicMock()
        with (
            patch.object(auth, "_mail_is_deployed", return_value=False),
            patch.object(auth, "_serializer") as ser,
        ):
            ser.return_value.dumps.return_value = "token"
            auth.set_session_cookie(response, "admin")
        kwargs = response.set_cookie.call_args.kwargs
        self.assertEqual(kwargs["max_age"], settings.setup_session_max_age_sec)
        self.assertEqual(kwargs["key"], settings.cookie_name)


class BootstrapPasswordTests(unittest.TestCase):
    def test_generate_bootstrap_password_is_fixed(self) -> None:
        self.assertEqual(auth.generate_bootstrap_password(), "E@syEmail")
        self.assertEqual(auth.DEFAULT_BOOTSTRAP_PASSWORD, "E@syEmail")

    def test_bootstrap_sh_uses_fixed_password_not_random(self) -> None:
        root = Path(__file__).resolve().parents[3]
        text = (root / "console" / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("BOOT_PASS='E@syEmail'", text)
        self.assertNotIn("token_urlsafe(18)", text)


if __name__ == "__main__":
    unittest.main()
