"""HTTP/TLS mux: plain HTTP 308s to HTTPS; TLS first byte is classified."""

from __future__ import annotations

import socket
import threading
import unittest

from kin_console.https_mux import (
    classify_first_byte,
    handle_client,
    parse_http_target,
    redirect_response,
)


class ClassifyTests(unittest.TestCase):
    def test_tls_clienthello_byte(self) -> None:
        self.assertEqual(classify_first_byte(b"\x16"), "tls")
        self.assertEqual(classify_first_byte(b"GET / HTTP/1.1\r\n"), "http")
        self.assertEqual(classify_first_byte(b""), "empty")

    def test_parse_and_redirect(self) -> None:
        head = b"GET /login HTTP/1.1\r\nHost: mail.example.test:9443\r\n\r\n"
        host, path = parse_http_target(head, listen_port=9443)
        self.assertEqual(host, "mail.example.test:9443")
        self.assertEqual(path, "/login")
        resp = redirect_response(host, path)
        self.assertIn(b"308 Permanent Redirect", resp)
        self.assertIn(b"Location: https://mail.example.test:9443/login\r\n", resp)

    def test_handle_client_http_redirect(self) -> None:
        server, client = socket.socketpair()
        thread = threading.Thread(
            target=handle_client,
            args=(server,),
            kwargs={
                "listen_port": 9443,
                "backend_host": "127.0.0.1",
                "backend_port": 1,
            },
            daemon=True,
        )
        thread.start()
        client.sendall(b"GET /cluster HTTP/1.1\r\nHost: 203.0.113.10:9443\r\n\r\n")
        client.settimeout(2)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            client.close()
        thread.join(timeout=2)
        self.assertIn(b"308", data)
        self.assertIn(b"https://203.0.113.10:9443/cluster", data)


if __name__ == "__main__":
    unittest.main()
