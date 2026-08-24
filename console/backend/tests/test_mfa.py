"""TOTP MFA: real pyotp round-trip, pending tokens, RBAC recovery."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pyotp

from kin_console import mfa
from kin_console.users import (
    AUTH_AD,
    AUTH_LOCAL,
    ConsoleUser,
    apply_clear_mfa,
    apply_create_user,
    apply_set_local_password,
    apply_set_mfa,
    users_payload,
)
from kin_privhelper.console_users_sync import apply_mutation_to_users
from kin_privhelper.rbac import (
    ROLE_CUSTOMER_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPPORT_OPS,
    command_allowed,
)


class TotpRoundTripTests(unittest.TestCase):
    def test_real_pyotp_now_verifies(self) -> None:
        """Use pyotp.TOTP(...).now() directly, not only our wrapper calling itself."""
        secret = mfa.new_totp_secret()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(mfa.verify_totp(secret, code))
        self.assertFalse(mfa.verify_totp(secret, "000000"))

    def test_fixed_time_window(self) -> None:
        secret = "JBSWY3DPEHPK3PXP"
        at = 1_700_000_000
        code = pyotp.TOTP(secret).at(at)
        self.assertTrue(mfa.verify_totp(secret, code, at_time=at))
        self.assertTrue(mfa.verify_totp(secret, pyotp.TOTP(secret).at(at - 30), at_time=at))
        self.assertFalse(mfa.verify_totp(secret, pyotp.TOTP(secret).at(at - 90), at_time=at))


class PendingLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        secret = Path(self._tmpdir.name) / "session.secret"
        secret.write_text("unit-test-session-secret\n", encoding="utf-8")
        self._patch = mock.patch.object(mfa.settings, "session_secret_file", secret)
        self._patch.start()
        mfa._login_attempts.clear()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_attempt_cap(self) -> None:
        token = mfa.mint_login_pending("alice")
        pending = mfa.read_login_pending(token)
        tid = pending["tid"]
        for _ in range(mfa.MAX_MFA_ATTEMPTS):
            left = mfa.register_login_failure(tid)
        self.assertEqual(left, 0)
        self.assertEqual(mfa.login_attempts_remaining(tid), 0)

    def test_expired_pending_rejected(self) -> None:
        token = mfa.mint_login_pending("alice")
        # Advance the clock so the signed token is past max_age.
        now = time.time()
        with mock.patch("time.time", return_value=now + mfa.PENDING_LOGIN_MAX_AGE_SEC + 5):
            with self.assertRaises(ValueError):
                mfa.read_login_pending(token)


class UsersMfaMutationTests(unittest.TestCase):
    def _admin(self) -> ConsoleUser:
        return ConsoleUser(
            username="admin",
            role=ROLE_SUPER_ADMIN,
            auth_type=AUTH_LOCAL,
            password_hash="$2b$12$not-a-real-hash-value-placeholderxxxx",
        )

    def test_set_and_clear_mfa(self) -> None:
        users = [self._admin()]
        updated, users = apply_set_mfa(users, "admin", mfa_secret="JBSWY3DPEHPK3PXP")
        self.assertTrue(updated.mfa_enabled)
        self.assertEqual(updated.mfa_secret, "JBSWY3DPEHPK3PXP")
        pub = updated.public()
        self.assertTrue(pub["mfa_enabled"])
        self.assertNotIn("mfa_secret", pub)
        cleared, users = apply_clear_mfa(users, "admin")
        self.assertFalse(cleared.mfa_enabled)
        self.assertEqual(cleared.mfa_secret, "")

    def test_password_rotate_preserves_mfa(self) -> None:
        users = [self._admin()]
        _, users = apply_set_mfa(users, "admin", mfa_secret="JBSWY3DPEHPK3PXP")
        updated, users = apply_set_local_password(
            users, "admin", password_hash="$2b$12$rotated-hash-placeholderxxxxxxxxxx"
        )
        self.assertTrue(updated.mfa_enabled)
        self.assertEqual(updated.mfa_secret, "JBSWY3DPEHPK3PXP")

    def test_ad_user_can_hold_mfa(self) -> None:
        users = [self._admin()]
        created, users = apply_create_user(
            users,
            "bob",
            ROLE_CUSTOMER_ADMIN,
            auth_type=AUTH_AD,
            ad_username="bob@example.test",
        )
        updated, users = apply_set_mfa(users, "bob", mfa_secret="JBSWY3DPEHPK3PXP")
        self.assertEqual(updated.auth_type, AUTH_AD)
        self.assertTrue(updated.mfa_enabled)
        payload = users_payload(users)
        bob = next(u for u in payload["users"] if u["username"] == "bob")
        self.assertTrue(bob["mfa_enabled"])
        self.assertEqual(bob["mfa_secret"], "JBSWY3DPEHPK3PXP")


class RbacMfaTests(unittest.TestCase):
    def test_clear_mfa_self_or_super(self) -> None:
        self.assertTrue(
            command_allowed(
                ROLE_CUSTOMER_ADMIN,
                "mutate_console_users",
                args={"op": "clear_mfa", "username": "alice"},
                username="alice",
            )
        )
        self.assertFalse(
            command_allowed(
                ROLE_CUSTOMER_ADMIN,
                "mutate_console_users",
                args={"op": "clear_mfa", "username": "admin"},
                username="alice",
            )
        )
        self.assertTrue(
            command_allowed(
                ROLE_SUPER_ADMIN,
                "mutate_console_users",
                args={"op": "clear_mfa", "username": "alice"},
                username="admin",
            )
        )
        self.assertFalse(
            command_allowed(
                ROLE_SUPPORT_OPS,
                "mutate_console_users",
                args={"op": "clear_mfa", "username": "alice"},
                username="ops",
            )
        )

    def test_set_mfa_self_only(self) -> None:
        self.assertTrue(
            command_allowed(
                ROLE_CUSTOMER_ADMIN,
                "mutate_console_users",
                args={"op": "set_mfa", "username": "alice"},
                username="alice",
            )
        )
        self.assertFalse(
            command_allowed(
                ROLE_SUPER_ADMIN,
                "mutate_console_users",
                args={"op": "set_mfa", "username": "alice"},
                username="admin",
            )
        )


class SyncMfaFieldTests(unittest.TestCase):
    def test_set_mfa_mutation_flows_through_apply_mutation_to_users(self) -> None:
        users = [
            ConsoleUser(
                username="admin",
                role=ROLE_SUPER_ADMIN,
                auth_type=AUTH_LOCAL,
                password_hash="$2b$12$not-a-real-hash-value-placeholderxxxx",
            )
        ]
        updated, users = apply_mutation_to_users(
            users,
            {
                "op": "set_mfa",
                "actor": "admin",
                "username": "admin",
                "mfa_secret": "JBSWY3DPEHPK3PXP",
            },
        )
        assert updated is not None
        self.assertTrue(updated.mfa_enabled)
        self.assertEqual(updated.mfa_secret, "JBSWY3DPEHPK3PXP")
        cleared, users = apply_mutation_to_users(
            users,
            {"op": "clear_mfa", "actor": "admin", "username": "admin"},
        )
        assert cleared is not None
        self.assertFalse(cleared.mfa_enabled)
        self.assertEqual(cleared.mfa_secret, "")


if __name__ == "__main__":
    unittest.main()
