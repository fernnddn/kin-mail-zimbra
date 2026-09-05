"""The generator and the appliance must agree about what a license is.

`licensing-generator/generate_license.py` is a portable kit: it runs on KIN's
offline signing machine with no console backend present, so it carries its own
copies of canonical_payload, validate_payload, sign_payload and _parse_ts, plus
its own copy of the embedded public key.

That is a reasonable duplication and a dangerous one. If the two drift, KIN
issues keys that no appliance accepts - and nobody finds out until a customer
pastes one in. If the embedded public keys drift, *every* issued key fails
everywhere at once.

These tests sign with the generator and verify with the appliance, in both
directions, and compare the two validators across a matrix of payloads.
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kin_console import license as appliance
from kin_console.license_keys import KIN_LICENSE_PUBLIC_KEY_B64

REPO = Path(__file__).resolve().parents[3]
GENERATOR = REPO / "licensing-generator" / "generate_license.py"


def _load_generator() -> Any:
    # Without this, a cached .pyc can be reused when an edit leaves the file
    # the same size - which is exactly what a one-character key change does,
    # and it makes this suite pass against stale bytecode.
    importlib.invalidate_caches()
    spec = importlib.util.spec_from_file_location("kin_license_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
SID = "11111111-2222-4333-8444-555555555555"


def _payloads() -> list[dict[str, Any]]:
    iso = NOW.isoformat()
    return [
        {"server_id": SID, "seats": 1, "type": "perpetual",
         "issued_at": iso, "expires_at": None},
        {"server_id": SID, "seats": 25, "type": "trial",
         "issued_at": iso, "expires_at": (NOW + timedelta(days=30)).isoformat()},
        {"server_id": SID, "seats": 5000, "type": "subscription",
         "issued_at": iso, "expires_at": (NOW + timedelta(days=365)).isoformat()},
        # Z-suffixed and offset timestamps must normalize the same both sides.
        {"server_id": SID, "seats": 10, "type": "subscription",
         "issued_at": "2026-09-05T12:00:00Z",
         "expires_at": "2027-09-05T19:00:00+07:00"},
    ]


class EmbeddedKeysMatch(unittest.TestCase):
    def test_the_generator_ships_the_same_public_key_as_the_appliance(self) -> None:
        gen = _load_generator()
        self.assertEqual(
            gen.KIN_LICENSE_PUBLIC_KEY_B64,
            KIN_LICENSE_PUBLIC_KEY_B64,
            "generator and appliance disagree on the product public key - every "
            "license issued would fail on every appliance",
        )

    def test_the_shared_constants_match(self) -> None:
        gen = _load_generator()
        self.assertEqual(gen.LICENSE_PAYLOAD_KEYS, appliance.LICENSE_PAYLOAD_KEYS)
        self.assertEqual(gen.LICENSE_TYPES, appliance.LICENSE_TYPES)
        self.assertEqual(gen.EXPIRING_LICENSE_TYPES, appliance.EXPIRING_LICENSE_TYPES)


class SignedHereVerifiesThere(unittest.TestCase):
    def setUp(self) -> None:
        self.gen = _load_generator()
        self.priv = Ed25519PrivateKey.generate()
        self.pub = self.priv.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )

    def test_generator_signed_licenses_verify_on_the_appliance(self) -> None:
        for payload in _payloads():
            with self.subTest(type=payload["type"], seats=payload["seats"]):
                token = self.gen.sign_payload(dict(payload), self.priv)
                state = appliance.verify_license(
                    token, server_id=SID, now=NOW, public_key=self.pub
                )
                self.assertEqual(state["status"], "active")
                self.assertEqual(state["seats"], payload["seats"])
                self.assertEqual(state["type"], payload["type"])

    def test_appliance_signed_licenses_verify_with_the_generator_canon(self) -> None:
        # The reverse direction catches a canonicalization change made on the
        # appliance side only.
        for payload in _payloads():
            with self.subTest(type=payload["type"]):
                token = appliance.sign_payload(dict(payload), self.priv)
                gen_token = self.gen.sign_payload(dict(payload), self.priv)
                self.assertEqual(
                    token, gen_token,
                    "the same payload signed by each side produced different "
                    "tokens - canonical form has drifted",
                )

    def test_both_canonical_forms_are_byte_identical(self) -> None:
        for payload in _payloads():
            clean_a = appliance.validate_payload(dict(payload))
            clean_b = self.gen.validate_payload(dict(payload))
            self.assertEqual(clean_a, clean_b)
            self.assertEqual(
                appliance.canonical_payload(clean_a),
                self.gen.canonical_payload(clean_b),
            )


class BothValidatorsRejectTheSameThings(unittest.TestCase):
    """A payload one side accepts and the other rejects is an unusable key."""

    def setUp(self) -> None:
        self.gen = _load_generator()

    def test_rejection_matrix_agrees(self) -> None:
        iso = NOW.isoformat()
        bad: list[tuple[str, dict[str, Any]]] = [
            ("perpetual with expiry", {"server_id": SID, "seats": 5, "type": "perpetual",
                                       "issued_at": iso, "expires_at": iso}),
            ("trial with no expiry", {"server_id": SID, "seats": 5, "type": "trial",
                                      "issued_at": iso, "expires_at": None}),
            ("subscription no expiry", {"server_id": SID, "seats": 5, "type": "subscription",
                                        "issued_at": iso, "expires_at": None}),
            ("zero seats", {"server_id": SID, "seats": 0, "type": "perpetual",
                            "issued_at": iso, "expires_at": None}),
            ("negative seats", {"server_id": SID, "seats": -1, "type": "perpetual",
                                "issued_at": iso, "expires_at": None}),
            ("seats not a number", {"server_id": SID, "seats": "many", "type": "perpetual",
                                    "issued_at": iso, "expires_at": None}),
            ("unknown type", {"server_id": SID, "seats": 5, "type": "enterprise",
                              "issued_at": iso, "expires_at": None}),
            ("empty server id", {"server_id": "", "seats": 5, "type": "perpetual",
                                 "issued_at": iso, "expires_at": None}),
            ("no issued_at", {"server_id": SID, "seats": 5, "type": "perpetual",
                              "expires_at": None}),
            ("extra field", {"server_id": SID, "seats": 5, "type": "perpetual",
                             "issued_at": iso, "expires_at": None, "reseller": "x"}),
        ]
        for label, payload in bad:
            with self.subTest(case=label):
                with self.assertRaises(ValueError, msg=f"appliance accepted {label}"):
                    appliance.validate_payload(dict(payload))
                with self.assertRaises(ValueError, msg=f"generator accepted {label}"):
                    self.gen.validate_payload(dict(payload))

    def test_seats_as_a_numeric_string_is_accepted_by_both(self) -> None:
        # The CLI collects seats as text; both sides must coerce identically or
        # a key issued from the prompt would not match one issued from a flag.
        payload = {"server_id": SID, "seats": "25", "type": "perpetual",
                   "issued_at": NOW.isoformat(), "expires_at": None}
        self.assertEqual(appliance.validate_payload(dict(payload))["seats"], 25)
        self.assertEqual(self.gen.validate_payload(dict(payload))["seats"], 25)


if __name__ == "__main__":
    unittest.main()
