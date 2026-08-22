"""Public IPv4 probe for wizard DNS paste."""

from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError

from kin_console.public_ip import (
    is_public_ipv4,
    probe_public_ipv4,
    reset_public_ip_cache,
)


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = BytesIO(body)

    def read(self, _n: int) -> bytes:
        return self._body.read()

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_a: object) -> None:
        return None


class PublicIpTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_public_ip_cache()

    def test_private_and_cgnat_rejected(self) -> None:
        self.assertFalse(is_public_ipv4("10.0.0.1"))
        self.assertFalse(is_public_ipv4("192.168.1.8"))
        self.assertFalse(is_public_ipv4("172.16.0.1"))
        self.assertFalse(is_public_ipv4("127.0.0.1"))
        self.assertFalse(is_public_ipv4("100.64.1.1"))
        self.assertFalse(is_public_ipv4("not-an-ip"))
        self.assertTrue(is_public_ipv4("119.82.245.37"))

    def test_probe_uses_first_public_source(self) -> None:
        with patch(
            "kin_console.public_ip.urllib.request.urlopen",
            side_effect=[_Resp(b"119.82.245.37\n")],
        ) as opener:
            ip = probe_public_ipv4(now=10.0)
        self.assertEqual(ip, "119.82.245.37")
        opener.assert_called_once()
        again = probe_public_ipv4(now=11.0)
        self.assertEqual(again, "119.82.245.37")
        opener.assert_called_once()

    def test_probe_skips_private_echo_and_uses_next_source(self) -> None:
        with patch(
            "kin_console.public_ip.urllib.request.urlopen",
            side_effect=[_Resp(b"10.0.0.1"), _Resp(b"8.8.8.8")],
        ):
            ip = probe_public_ipv4(now=20.0)
        self.assertEqual(ip, "8.8.8.8")

    def test_probe_falls_through_to_second_source(self) -> None:
        with patch(
            "kin_console.public_ip.urllib.request.urlopen",
            side_effect=[URLError("down"), _Resp(b"1.1.1.1")],
        ):
            ip = probe_public_ipv4(now=30.0)
        self.assertEqual(ip, "1.1.1.1")


if __name__ == "__main__":
    unittest.main()
