"""CLI entry: python -m kin_console"""

from __future__ import annotations

import uvicorn

from .settings import settings


def main() -> None:
    uvicorn.run(
        "kin_console.app:app",
        host=settings.console_bind,
        port=settings.console_port,
        ssl_certfile=str(settings.tls_cert),
        ssl_keyfile=str(settings.tls_key),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
