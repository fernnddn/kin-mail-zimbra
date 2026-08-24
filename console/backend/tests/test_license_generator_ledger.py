"""Ledger helper for the off-tree license generator. No product private key."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_GEN = _REPO / "licensing-generator"
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))

from generate_license import LEDGER_FIELDS, append_ledger  # noqa: E402


class LicenseLedgerTests(unittest.TestCase):
    def test_append_writes_header_then_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "license-ledger.csv"
            append_ledger(
                {
                    "recorded_at": "2026-08-24T00:00:00+00:00",
                    "company_name": "PT Example",
                    "short_name": "example",
                    "purchase_date": "2026-08-01",
                    "type": "perpetual",
                    "server_id": "11111111-2222-4333-8444-555555555555",
                    "seats": "32",
                    "issued_at": "2026-08-24T00:00:00+00:00",
                    "expires_at": "",
                    "token": "payload.sig",
                },
                path=path,
            )
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(list(rows[0].keys()), list(LEDGER_FIELDS))
            self.assertEqual(rows[0]["company_name"], "PT Example")
            self.assertEqual(rows[0]["token"], "payload.sig")
            self.assertEqual(len(rows), 1)
            append_ledger({"company_name": "Second Co", "token": "t2"}, path=path)
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["company_name"], "Second Co")
