"""Ed25519 license verification. Uses ephemeral keys, never the product private key."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kin_console.license import sign_payload, verify_license


def _raw_public(priv: Ed25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)


class LicenseVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.priv = Ed25519PrivateKey.generate()
        self.pub = _raw_public(self.priv)
        self.server_id = "11111111-2222-4333-8444-555555555555"
        self.now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    def _token(self, **overrides: object) -> str:
        issued = self.now.isoformat()
        payload = {
            "server_id": self.server_id,
            "seats": 32,
            "type": "perpetual",
            "issued_at": issued,
            "expires_at": None,
            **overrides,
        }
        return sign_payload(payload, self.priv)

    def test_valid_perpetual(self) -> None:
        token = self._token()
        state = verify_license(
            token, server_id=self.server_id, now=self.now, public_key=self.pub
        )
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["seats"], 32)
        self.assertFalse(state["provisioning_blocked"])
        self.assertIsNone(state["expires_at"])

    def test_tampered_payload_is_rejected(self) -> None:
        token = self._token()
        payload_b64, sig_b64 = token.split(".", 1)
        pad = "=" * ((4 - len(payload_b64) % 4) % 4)
        raw = bytearray(base64.urlsafe_b64decode(payload_b64 + pad))
        raw[4] ^= 0x01
        flipped = base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
        with self.assertRaises(ValueError) as ctx:
            verify_license(
                f"{flipped}.{sig_b64}",
                server_id=self.server_id,
                now=self.now,
                public_key=self.pub,
            )
        self.assertIn("signature", str(ctx.exception).lower())

    def test_wrong_server_id_is_rejected(self) -> None:
        token = self._token()
        with self.assertRaises(ValueError) as ctx:
            verify_license(
                token,
                server_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                now=self.now,
                public_key=self.pub,
            )
        self.assertIn("email server id", str(ctx.exception).lower())

    def test_perpetual_must_not_set_expiry(self) -> None:
        with self.assertRaises(ValueError):
            sign_payload(
                {
                    "server_id": self.server_id,
                    "seats": 10,
                    "type": "perpetual",
                    "issued_at": self.now.isoformat(),
                    "expires_at": (self.now + timedelta(days=30)).isoformat(),
                },
                self.priv,
            )

    def test_trial_requires_expiry(self) -> None:
        with self.assertRaises(ValueError):
            sign_payload(
                {
                    "server_id": self.server_id,
                    "seats": 10,
                    "type": "trial",
                    "issued_at": self.now.isoformat(),
                    "expires_at": None,
                },
                self.priv,
            )

    def test_trial_grace_then_expired(self) -> None:
        expires = self.now - timedelta(days=5)
        token = self._token(
            type="trial",
            expires_at=expires.isoformat(),
            issued_at=(expires - timedelta(days=90)).isoformat(),
        )
        grace = verify_license(
            token, server_id=self.server_id, now=self.now, public_key=self.pub
        )
        self.assertEqual(grace["status"], "grace")
        self.assertTrue(grace["provisioning_blocked"])
        later = self.now + timedelta(days=40)
        expired = verify_license(
            token, server_id=self.server_id, now=later, public_key=self.pub
        )
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(expired["provisioning_blocked"])

    def test_valid_subscription(self) -> None:
        token = self._token(
            type="subscription",
            expires_at=(self.now + timedelta(days=365)).isoformat(),
        )
        state = verify_license(
            token, server_id=self.server_id, now=self.now, public_key=self.pub
        )
        self.assertEqual(state["status"], "active")
        self.assertFalse(state["provisioning_blocked"])

    def test_subscription_requires_expiry(self) -> None:
        with self.assertRaises(ValueError):
            sign_payload(
                {
                    "server_id": self.server_id,
                    "seats": 10,
                    "type": "subscription",
                    "issued_at": self.now.isoformat(),
                    "expires_at": None,
                },
                self.priv,
            )

    def test_subscription_grace_then_expired(self) -> None:
        expires = self.now - timedelta(days=5)
        token = self._token(
            type="subscription",
            expires_at=expires.isoformat(),
            issued_at=(expires - timedelta(days=365)).isoformat(),
        )
        grace = verify_license(
            token, server_id=self.server_id, now=self.now, public_key=self.pub
        )
        self.assertEqual(grace["status"], "grace")
        self.assertTrue(grace["provisioning_blocked"])
        later = self.now + timedelta(days=40)
        expired = verify_license(
            token, server_id=self.server_id, now=later, public_key=self.pub
        )
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(expired["provisioning_blocked"])

    def test_unknown_type_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            sign_payload(
                {
                    "server_id": self.server_id,
                    "seats": 10,
                    "type": "lifetime",
                    "issued_at": self.now.isoformat(),
                    "expires_at": None,
                },
                self.priv,
            )

    def test_embedded_product_key_rejects_foreign_signature(self) -> None:
        token = self._token()
        with self.assertRaises(ValueError):
            verify_license(token, server_id=self.server_id, now=self.now)


if __name__ == "__main__":
    unittest.main()
