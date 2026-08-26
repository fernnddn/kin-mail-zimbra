"""wizard_actor: Host A first-boot stays anonymous; peer with users forces login."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


class WizardActorGateTests(unittest.TestCase):
    def _users_file(self, tmp: Path) -> Path:
        path = tmp / "users.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "users": [
                        {
                            "username": "admin",
                            "password_hash": "$2b$12$placeholder",
                            "role": "kin_super_admin",
                            "auth_type": "local",
                            "ad_username": "",
                            "disabled": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_host_a_first_boot_with_users_and_initial_password_is_anonymous(self) -> None:
        from kin_console import auth

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            users_path = self._users_file(tmp)
            initial = tmp / "initial-admin-password"
            initial.write_text("secret\n", encoding="utf-8")
            req = MagicMock()
            req.cookies = {}
            resp = MagicMock()
            with (
                patch("kin_privhelper.deploy_state.is_mail_deployed", return_value=False),
                patch(
                    "kin_privhelper.initial_password.INITIAL_PASSWORD_FILE",
                    initial,
                ),
                patch("kin_console.settings.settings.users_file", users_path),
                patch("kin_console.users.load_users") as load,
            ):
                load.return_value = [
                    type(
                        "U",
                        (),
                        {
                            "username": "admin",
                            "role": "kin_super_admin",
                            "disabled": False,
                        },
                    )()
                ]
                actor = auth.wizard_actor(req, resp)
            self.assertTrue(actor.anonymous_setup)
            self.assertEqual(actor.username, "setup")

    def test_peer_with_users_and_no_initial_password_requires_login(self) -> None:
        from kin_console import auth

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            users_path = self._users_file(tmp)
            missing_initial = tmp / "does-not-exist"
            req = MagicMock()
            req.cookies = {}
            resp = MagicMock()
            with (
                patch("kin_privhelper.deploy_state.is_mail_deployed", return_value=False),
                patch(
                    "kin_privhelper.initial_password.INITIAL_PASSWORD_FILE",
                    missing_initial,
                ),
                patch("kin_console.settings.settings.users_file", users_path),
                patch("kin_console.users.load_users") as load,
            ):
                load.return_value = [
                    type(
                        "U",
                        (),
                        {
                            "username": "admin",
                            "role": "kin_super_admin",
                            "disabled": False,
                        },
                    )()
                ]
                with self.assertRaises(HTTPException) as ctx:
                    auth.wizard_actor(req, resp)
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertIn("Not authenticated", str(ctx.exception.detail))

    def test_wizard_requires_login_helper_matches_gates(self) -> None:
        from kin_console import auth

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            initial = tmp / "initial-admin-password"
            initial.write_text("x\n", encoding="utf-8")
            missing = tmp / "gone"
            with (
                patch("kin_privhelper.deploy_state.is_mail_deployed", return_value=False),
                patch(
                    "kin_privhelper.initial_password.INITIAL_PASSWORD_FILE",
                    initial,
                ),
                patch("kin_console.users.load_users", return_value=[object()]),
            ):
                self.assertFalse(auth.wizard_requires_login())
            with (
                patch("kin_privhelper.deploy_state.is_mail_deployed", return_value=False),
                patch(
                    "kin_privhelper.initial_password.INITIAL_PASSWORD_FILE",
                    missing,
                ),
                patch("kin_console.users.load_users", return_value=[object()]),
            ):
                self.assertTrue(auth.wizard_requires_login())
            with (
                patch("kin_privhelper.deploy_state.is_mail_deployed", return_value=True),
                patch("kin_console.users.load_users", return_value=[]),
            ):
                self.assertTrue(auth.wizard_requires_login())


if __name__ == "__main__":
    unittest.main()
