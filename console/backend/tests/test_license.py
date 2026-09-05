"""Ed25519 license verification. Uses ephemeral keys, never the product private key."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kin_console.license import license_view, sign_payload, verify_license


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

    def test_whitespace_inside_token_still_verifies(self) -> None:
        token = self._token()
        wrapped = token[:40] + "\n  " + token[40:80] + "\t" + token[80:]
        state = verify_license(
            wrapped, server_id=self.server_id, now=self.now, public_key=self.pub
        )
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["seats"], 32)

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

    def test_license_view_missing_token_is_not_blocked(self) -> None:
        state = license_view("", self.server_id)
        self.assertFalse(state["present"])
        self.assertEqual(state["status"], "none")
        self.assertFalse(state["provisioning_blocked"])

    def test_license_view_garbage_token_is_blocked(self) -> None:
        state = license_view("not-a-license", self.server_id)
        self.assertTrue(state["present"])
        self.assertEqual(state["status"], "invalid")
        self.assertTrue(state["provisioning_blocked"])

    def test_license_view_token_without_server_id_is_blocked(self) -> None:
        state = license_view("not-a-license", "")
        self.assertTrue(state["present"])
        self.assertEqual(state["status"], "invalid")
        self.assertTrue(state["provisioning_blocked"])


if __name__ == "__main__":
    unittest.main()


class PastedKeyTests(unittest.TestCase):
    """A license key reaches the operator through email, chat or a document.

    Every one of those mangles it. Curly quotes are the common case: Word,
    Outlook and most chat clients replace straight quotes around a pasted key,
    and curly quotes are not whitespace, so stripping whitespace left them in.
    The operator then got `'ascii' codec can't encode characters in position
    1-2` - a Python error, on a key that was perfectly valid.
    """

    def setUp(self) -> None:
        self.priv = Ed25519PrivateKey.generate()
        self.pub = _raw_public(self.priv)
        self.server_id = "11111111-2222-4333-8444-555555555555"
        self.now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        self.token = sign_payload(
            {
                "server_id": self.server_id,
                "seats": 32,
                "type": "subscription",
                "issued_at": self.now.isoformat(),
                "expires_at": (self.now + timedelta(days=365)).isoformat(),
            },
            self.priv,
        )

    def _verify(self, text: str) -> dict:
        return verify_license(
            text, server_id=self.server_id, now=self.now, public_key=self.pub
        )

    def test_a_key_wrapped_in_quotes_still_applies(self) -> None:
        for wrap in ('"%s"', "'%s'", "“%s”", "‘%s’",
                     "<%s>", "`%s`", "(%s)", "[%s]", "«%s»"):
            with self.subTest(wrap=wrap):
                self.assertEqual(self._verify(wrap % self.token)["status"], "active")

    def test_a_key_wrapped_across_lines_still_applies(self) -> None:
        wrapped = "\n".join(
            self.token[i : i + 40] for i in range(0, len(self.token), 40)
        )
        self.assertEqual(self._verify(wrapped)["status"], "active")
        # Quoted AND wrapped: an email client doing both.
        self.assertEqual(self._verify("“" + wrapped + "”")["status"], "active")

    def test_a_key_with_a_non_breaking_space_still_applies(self) -> None:
        mangled = self.token[:20] + " " + self.token[20:]
        self.assertEqual(self._verify(mangled)["status"], "active")

    def test_stripping_wrappers_cannot_corrupt_a_real_key(self) -> None:
        # None of the wrapper characters are in the base64url alphabet, so a
        # legitimate key can never lose a character to the strip.
        from kin_console.license import _LICENSE_ALPHABET, _WRAPPERS

        self.assertFalse(set(_WRAPPERS) & set(_LICENSE_ALPHABET))
        self.assertTrue(set(self.token) <= set(_LICENSE_ALPHABET))


class UnreadableKeyErrorTests(unittest.TestCase):
    """Whatever is pasted, the operator must get a sentence they can act on."""

    SID = "11111111-2222-4333-8444-555555555555"

    def _err(self, token: str) -> str:
        view = license_view(token, self.SID)
        self.assertEqual(view["status"], "invalid")
        self.assertTrue(view["provisioning_blocked"])
        return str(view.get("error") or "")

    def test_stray_characters_are_named_and_explained(self) -> None:
        err = self._err("påylo≈ad.sig")
        self.assertIn("not part of a key", err)
        self.assertIn("curly quotes", err)
        self.assertNotIn("codec", err)

    def test_bad_base64_does_not_leak_binascii_wording(self) -> None:
        err = self._err("a.b.c.d.e")
        self.assertIn("not valid base64", err)
        self.assertNotIn("data characters", err)

    def test_no_python_internals_reach_the_operator(self) -> None:
        leaky = ("codec", "Traceback", "binascii", "0x", "object at",
                 "position 1-2", "b'", "NoneType")
        for token in ("", "   ", "abcdef", ".", "!!!!.!!!!", "\U0001f511.\U0001f512",
                      "A" * 3000 + "." + "B" * 3000, "aGVsbG8.c2ln",
                      "WzEsMl0.c2ln", "eyJhIjoxfQ.AAAA", "a.b.c.d.e",
                      "“eyJhIjoxfQ.AAAA”"):
            with self.subTest(token=token[:24]):
                view = license_view(token, self.SID)
                err = str(view.get("error") or "")
                for needle in leaky:
                    self.assertNotIn(needle, err)

    def test_nothing_escapes_as_an_exception(self) -> None:
        # license_view catches ValueError only. Anything else would reach the
        # console as a 500 with no explanation - the original complaint was
        # that a license error was undiagnosable, not that it was wrong.
        for token in ("", "\x00\x00.\x00", "\U0001f511", "." * 50,
                      "=" * 10 + "." + "=" * 10, "A.", ".A", "\n\t\r"):
            with self.subTest(token=repr(token)[:24]):
                view = license_view(token, self.SID)
                self.assertIn(view["status"], ("none", "invalid"))
        for sid in ("", "   ", None):
            with self.subTest(sid=sid):
                view = license_view("eyJhIjoxfQ.AAAA", sid)  # type: ignore[arg-type]
                self.assertEqual(view["status"], "invalid")
