"""KIN Mail admin console — settings (env / file, no hardcoded lab IPs)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Prefer process environment (systemd EnvironmentFile=). Optional dotenv
        # is best-effort so an unreadable file cannot crash startup.
        env_file=("/etc/kin-mail-console/console.env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Override via CONSOLE_PORT in console.env — do not hard-lock in code callers.
    console_port: int = 9443
    # Local-only HTTPS backend. The public listener on console_port is a mux
    # that 308-redirects plain HTTP and forwards TLS here.
    console_internal_port: int = 19443
    console_bind: str = "0.0.0.0"
    console_user: str = "admin"

    data_dir: Path = Path("/var/lib/kin-mail-console")
    tls_cert: Path = Path("/var/lib/kin-mail-console/tls/cert.pem")
    tls_key: Path = Path("/var/lib/kin-mail-console/tls/key.pem")
    password_hash_file: Path = Path("/var/lib/kin-mail-console/admin.hash")
    users_file: Path = Path("/var/lib/kin-mail-console/users.json")
    session_secret_file: Path = Path("/var/lib/kin-mail-console/session.secret")

    static_dir: Path = Path("/opt/kin-mail-console/frontend/dist")
    cookie_name: str = "kin_console_session"
    session_max_age_sec: int = 60 * 60 * 12  # 12h after deploy
    # Wizard + multi-stage deploy can outlast 12h. Used only while not deployed.
    setup_session_max_age_sec: int = 60 * 60 * 24 * 7

    # Local-only privhelper socket (never a network port).
    privhelper_socket: Path = Path("/run/kin-mail/privhelper.sock")


settings = Settings()
