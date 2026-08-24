"""app._request_client_ipv4: recovers the real peer through the mux's
127.0.0.1-only view via the https_mux port-correlation table."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from kin_console import https_mux
from kin_console.app import _request_client_ipv4


@dataclass
class _FakeClient:
    host: str
    port: int


@dataclass
class _FakeRequest:
    client: _FakeClient | None


class RequestClientIpv4Tests(unittest.TestCase):
    def test_direct_public_ip_passes_through(self) -> None:
        req = _FakeRequest(client=_FakeClient(host="203.0.113.5", port=54321))
        self.assertEqual(_request_client_ipv4(req), "203.0.113.5")

    def test_loopback_with_no_mux_mapping_is_empty(self) -> None:
        req = _FakeRequest(client=_FakeClient(host="127.0.0.1", port=1))
        self.assertEqual(_request_client_ipv4(req), "")

    def test_loopback_resolves_via_mux_port_table(self) -> None:
        port = 55555
        with https_mux._client_ip_lock:
            https_mux._client_ip_by_backend_port[port] = "203.0.113.9"
        try:
            req = _FakeRequest(client=_FakeClient(host="127.0.0.1", port=port))
            self.assertEqual(_request_client_ipv4(req), "203.0.113.9")
        finally:
            with https_mux._client_ip_lock:
                https_mux._client_ip_by_backend_port.pop(port, None)

    def test_no_client_is_empty(self) -> None:
        req = _FakeRequest(client=None)
        self.assertEqual(_request_client_ipv4(req), "")


if __name__ == "__main__":
    unittest.main()
