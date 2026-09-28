"""KIN Mail admin console - settings (env / file, no hardcoded lab IPs)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# systemd passes these through EnvironmentFile= already; reading the file here
# is a convenience for anything started outside the unit.
ENV_FILE_CANDIDATES: tuple[str, ...] = ("/etc/kin-mail-console/console.env",)


def readable_env_files() -> tuple[str, ...]:
    """The dotenv paths that can actually be opened.

    pydantic-settings opens every path in ``env_file`` while the class body is
    being evaluated, so a file that exists but cannot be read raises
    PermissionError during *import* - before logging exists, and with a
    traceback that names python-dotenv rather than the file an operator has to
    fix. The console runs as ``kin-console`` and reads console.env only through
    group ownership (``root:kin-console 0640``). A restore that lands the file
    as ``root:root``, a hand-typed ``chmod 600``, or any other tool importing
    this module as a different user would otherwise take the console down at
    startup - discarding the process environment that systemd has already
    supplied, which is where these values come from in production anyway.

    Skipping an unreadable file is therefore strictly safer than failing: under
    systemd nothing is lost, and outside it the defaults below still apply.
    """
    usable = []
    for path in ENV_FILE_CANDIDATES:
        try:
            with open(path, "rb"):
                pass
        except OSError:
            # Missing, unreadable, a dangling symlink, a directory: all mean
            # "no settings from here", never "refuse to start".
            continue
        usable.append(path)
    return tuple(usable)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Prefer process environment (systemd EnvironmentFile=). The dotenv
        # read is genuinely best-effort - see readable_env_files().
        env_file=readable_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )
    # Override via CONSOLE_PORT in console.env - do not hard-lock in code callers.
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
