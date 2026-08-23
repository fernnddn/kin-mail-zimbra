"""CLI entry: python -m kin_console"""

from __future__ import annotations

import uvicorn

from .https_mux import run_mux_thread
from .settings import settings


def main() -> None:
    public_bind = settings.console_bind
    public_port = int(settings.console_port)
    internal_port = int(settings.console_internal_port)
    run_mux_thread(
        bind=public_bind,
        port=public_port,
        backend_host="127.0.0.1",
        backend_port=internal_port,
    )
    # TLS lives on loopback. The mux on console_port forwards ClientHello and
    # answers plain HTTP with 308 to https://<host>:console_port/...
    uvicorn.run(
        "kin_console.app:app",
        host="127.0.0.1",
        port=internal_port,
        ssl_certfile=str(settings.tls_cert),
        ssl_keyfile=str(settings.tls_key),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
