"""Certificate reporting: the right file, the right arithmetic, no crashes.

Two ways to get this wrong, both of which report a healthy certificate on an
appliance that is about to serve an expired one:

  reading /etc/letsencrypt instead of what Zimbra deployed, so a failed deploy
  hook looks like a fresh certificate;

  and an off-by-one that turns "expired two days ago" into a positive number.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from kin_privhelper.tls_status import (
    cert_paths,
    days_until,
    parse_openssl_enddate,
    read_certificate,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class CertPathOrderTests(unittest.TestCase):
    def test_the_deployed_certificate_is_read_first(self) -> None:
        paths = [str(p) for p in cert_paths("mail.example.test")]
        deployed = next(i for i, p in enumerate(paths) if "/opt/zimbra/" in p)
        issued = next(i for i, p in enumerate(paths) if "/etc/letsencrypt/" in p)
        # certbot renews into /etc/letsencrypt; a deploy hook copies it into
        # Zimbra. If that hook failed - and on an HA Secondary it cannot run at
        # all - the two disagree, and only one of them is what clients get.
        self.assertLess(deployed, issued)

    def test_no_host_means_no_letsencrypt_path(self) -> None:
        # An empty MAIL_HOST would otherwise build /etc/letsencrypt/live//cert.pem
        paths = [str(p) for p in cert_paths("")]
        self.assertFalse(any(p.endswith("live//cert.pem") for p in paths))

    def test_every_candidate_is_absolute(self) -> None:
        for p in cert_paths("mail.example.test"):
            with self.subTest(path=str(p)):
                self.assertTrue(p.is_absolute())


class EndDateParsingTests(unittest.TestCase):
    def test_openssl_output_shapes(self) -> None:
        cases = {
            "notAfter=Oct  1 12:00:00 2026 GMT": datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
            "notAfter=Oct 11 12:00:00 2026 GMT": datetime(2026, 10, 11, 12, tzinfo=timezone.utc),
            "Jan  5 23:59:59 2027 GMT": datetime(2027, 1, 5, 23, 59, 59, tzinfo=timezone.utc),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_openssl_enddate(text), want)

    def test_junk_returns_none_rather_than_a_guess(self) -> None:
        for text in ("", "   ", "notAfter=", "garbage", "notAfter=Feb 30 00:00:00 2026 GMT", "\x00"):
            with self.subTest(text=text):
                self.assertIsNone(parse_openssl_enddate(text))


class DaysUntilTests(unittest.TestCase):
    def test_future_dates_floor(self) -> None:
        self.assertEqual(days_until(NOW + timedelta(days=30), NOW), 30)
        self.assertEqual(days_until(NOW + timedelta(days=30, hours=12), NOW), 30)
        self.assertEqual(days_until(NOW + timedelta(hours=12), NOW), 0)

    def test_expired_certificates_are_negative_and_exact(self) -> None:
        # An expiry two days ago is -2. A "correction" for the negative branch
        # made it -3, which would have been reported to the operator.
        self.assertEqual(days_until(NOW - timedelta(days=2), NOW), -2)
        self.assertEqual(days_until(NOW - timedelta(hours=12), NOW), -1)
        self.assertEqual(days_until(NOW - timedelta(days=90), NOW), -90)

    def test_unknown_expiry_is_none(self) -> None:
        self.assertIsNone(days_until(None, NOW))


class ReadCertificateTests(unittest.TestCase):
    def test_it_never_raises_when_nothing_is_there(self) -> None:
        info = read_certificate("mail.example.test", "cloudflare")
        self.assertIsNone(info["days_left"])
        self.assertEqual(info["path"], "")
        self.assertEqual(info["method"], "cloudflare")

    def test_it_reads_a_real_certificate_and_labels_the_source(self) -> None:
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest import mock

        from kin_privhelper import tls_status

        with tempfile.TemporaryDirectory() as tmp:
            crt = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            rc = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(crt), "-days", "45",
                 "-subj", "/CN=mail.example.test"],
                capture_output=True, check=False,
            ).returncode
            if rc != 0:
                self.skipTest("openssl cannot generate a certificate here")
            with mock.patch.object(tls_status, "cert_paths", lambda host="": [crt]):
                info = tls_status.read_certificate("mail.example.test", "customer")
        self.assertEqual(info["path"], str(crt))
        # 45 days requested; allow for the boundary.
        self.assertIn(info["days_left"], (44, 45))
        self.assertEqual(info["source"], "issued")

    def test_the_cache_returns_a_copy_callers_cannot_corrupt(self) -> None:
        from kin_privhelper import tls_status

        tls_status.clear_cache()
        first = tls_status.cached_certificate("mail.example.test", "cloudflare")
        first["days_left"] = 9999
        second = tls_status.cached_certificate("mail.example.test", "cloudflare")
        self.assertNotEqual(second.get("days_left"), 9999)


class StatusPayloadTests(unittest.TestCase):
    def test_expiry_rides_on_the_cluster_poll(self) -> None:
        import inspect

        from kin_privhelper import maintenance

        src = inspect.getsource(maintenance.gather_status)
        self.assertIn("cached_certificate", src)
        # A certificate read must never be able to break the status page.
        self.assertIn("never let a certificate read break status", src)

    def test_settings_and_the_poll_use_the_same_reader(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        text = (repo / "kin_privhelper/appliance_settings.py").read_text(encoding="utf-8")
        self.assertIn("from .tls_status import read_certificate", text)
        # The old private copy of the path list is gone.
        self.assertNotIn("def _cert_paths", text)


if __name__ == "__main__":
    unittest.main()
