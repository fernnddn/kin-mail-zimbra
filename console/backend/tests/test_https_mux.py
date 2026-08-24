"""HTTP/TLS mux: plain HTTP 308s to HTTPS; TLS first byte is classified."""

from __future__ import annotations

import socket
import threading
import time
import unittest

from kin_console.https_mux import (
    TLS_HANDSHAKE,
    HttpsMux,
    classify_first_byte,
    handle_client,
    parse_http_target,
    real_client_ip_for_backend_port,
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


class RealClientIpTests(unittest.TestCase):
    """The mux relays over a *new* outbound connection to the backend, so
    uvicorn only ever sees 127.0.0.1 unless something recovers the real
    peer. This exercises the actual HttpsMux + a real backend socket, not
    just the lookup table in isolation."""

    def test_backend_can_recover_real_client_ip_via_port(self) -> None:
        backend_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        backend_sock.bind(("127.0.0.1", 0))
        backend_sock.listen(5)
        backend_port = backend_sock.getsockname()[1]

        seen = {}

        def fake_backend():
            conn, addr = backend_sock.accept()
            seen["port"] = addr[1]
            conn.recv(1024)
            conn.sendall(b"ok")
            conn.close()

        threading.Thread(target=fake_backend, daemon=True).start()

        mux = HttpsMux(bind="127.0.0.1", port=0, backend_host="127.0.0.1", backend_port=backend_port)
        threading.Thread(target=mux.serve_forever, daemon=True).start()
        try:
            deadline = time.time() + 2
            while mux._sock is None and time.time() < deadline:
                time.sleep(0.01)
            mux_port = mux._sock.getsockname()[1]

            client_sock = socket.create_connection(("127.0.0.1", mux_port), timeout=5)
            try:
                client_sock.sendall(bytes([TLS_HANDSHAKE]) + b"fake")
                client_sock.recv(16)

                deadline = time.time() + 2
                while "port" not in seen and time.time() < deadline:
                    time.sleep(0.01)
                self.assertIn("port", seen)

                resolved = real_client_ip_for_backend_port(seen["port"])
                self.assertEqual(resolved, "127.0.0.1")
            finally:
                client_sock.close()

            # Table entry must not leak after the connection closes.
            deadline = time.time() + 2
            while real_client_ip_for_backend_port(seen["port"]) is not None and time.time() < deadline:
                time.sleep(0.01)
            self.assertIsNone(real_client_ip_for_backend_port(seen["port"]))
        finally:
            mux.shutdown()
            backend_sock.close()


if __name__ == "__main__":
    unittest.main()
