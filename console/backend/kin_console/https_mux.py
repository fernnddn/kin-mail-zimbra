"""Public :9443 accepts HTTPS and redirects plain HTTP to https://.

Uvicorn still terminates TLS on 127.0.0.1 (CONSOLE_INTERNAL_PORT). This mux
listens on the public console port: TLS ClientHello is forwarded unchanged;
plain HTTP gets 308 to the https URL on the same host and port.
"""

from __future__ import annotations

import selectors
import socket
import threading
import time
TLS_HANDSHAKE = 0x16
MAX_HTTP_HEAD = 8192


def classify_first_byte(first: bytes) -> str:
    if not first:
        return "empty"
    if first[0] == TLS_HANDSHAKE:
        return "tls"
    return "http"


def parse_http_target(head: bytes, *, listen_port: int) -> tuple[str, str]:
    """Return (host_header, path) for a 308 Location. Path defaults to /."""
    text = head.decode("iso-8859-1", errors="replace")
    path = "/"
    host = ""
    lines = text.split("\r\n")
    if lines:
        parts = lines[0].split(" ")
        if len(parts) >= 2 and parts[1].startswith("/"):
            path = parts[1]
            if not path.startswith("/"):
                path = "/"
    for line in lines[1:]:
        if line.lower().startswith("host:"):
            host = line.split(":", 1)[1].strip()
            break
    if not host:
        host = f"localhost:{listen_port}"
    elif ":" not in host:
        host = f"{host}:{listen_port}"
    return host, path


def redirect_response(host: str, path: str) -> bytes:
    location = f"https://{host}{path}"
    body = b""
    return (
        b"HTTP/1.1 308 Permanent Redirect\r\n"
        + f"Location: {location}\r\n".encode("ascii", errors="replace")
        + b"Connection: close\r\n"
        + b"Content-Length: 0\r\n"
        + b"\r\n"
        + body
    )


def _recv_http_head(sock: socket.socket) -> bytes:
    sock.settimeout(8)
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < MAX_HTTP_HEAD:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
    return buf


def _splice(left: socket.socket, right: socket.socket) -> None:
    sel = selectors.DefaultSelector()
    left.setblocking(False)
    right.setblocking(False)
    sel.register(left, selectors.EVENT_READ, right)
    sel.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            for key, _mask in sel.select(timeout=120):
                src: socket.socket = key.fileobj  # type: ignore[assignment]
                dst: socket.socket = key.data
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        sel.close()


def handle_client(
    client: socket.socket,
    *,
    listen_port: int,
    backend_host: str,
    backend_port: int,
) -> None:
    try:
        first = client.recv(1, socket.MSG_PEEK)
        kind = classify_first_byte(first)
        if kind != "tls":
            head = _recv_http_head(client)
            host, path = parse_http_target(head, listen_port=listen_port)
            client.sendall(redirect_response(host, path))
            return
        backend = None
        last_exc: OSError | None = None
        for _ in range(20):
            try:
                backend = socket.create_connection((backend_host, backend_port), timeout=0.5)
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.1)
        if backend is None:
            raise last_exc or OSError("backend not ready")
        try:
            _splice(client, backend)
        finally:
            backend.close()
    except OSError:
        return
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()


class HttpsMux:
    def __init__(
        self,
        *,
        bind: str,
        port: int,
        backend_host: str = "127.0.0.1",
        backend_port: int = 19443,
    ) -> None:
        self.bind = bind
        self.port = port
        self.backend_host = backend_host
        self.backend_port = backend_port
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def serve_forever(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, self.port))
        sock.listen(128)
        sock.settimeout(1.0)
        self._sock = sock
        try:
            while not self._stop.is_set():
                try:
                    client, _addr = sock.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    continue
                threading.Thread(
                    target=handle_client,
                    args=(client,),
                    kwargs={
                        "listen_port": self.port,
                        "backend_host": self.backend_host,
                        "backend_port": self.backend_port,
                    },
                    daemon=True,
                ).start()
        finally:
            sock.close()

    def shutdown(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


def run_mux_thread(
    *,
    bind: str,
    port: int,
    backend_host: str,
    backend_port: int,
) -> tuple[HttpsMux, threading.Thread]:
    mux = HttpsMux(
        bind=bind,
        port=port,
        backend_host=backend_host,
        backend_port=backend_port,
    )
    thread = threading.Thread(target=mux.serve_forever, name="kin-https-mux", daemon=True)
    thread.start()
    return mux, thread


def wait_port(host: str, port: int, timeout_s: float = 8.0) -> None:
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"backend {host}:{port} did not start")


__all__ = [
    "HttpsMux",
    "classify_first_byte",
    "handle_client",
    "parse_http_target",
    "redirect_response",
    "run_mux_thread",
    "wait_port",
]
