"""Root-only first-boot console password file.

Written by console/bootstrap.sh next to the one-time SSH print. Cleared by
privhelperd after a successful local login (or explicit rotate/reset helpers)
so plaintext does not linger forever under /root.
"""

from __future__ import annotations

import os
from pathlib import Path

# Keep in sync with console/bootstrap.sh (INITIAL_PASS_FILE).
# Not under /root: kin-mail-privhelperd uses ProtectHome=true, which makes /root
# read-only/empty in its mount namespace — unlink after first login would fail.
# /etc/kin-mail-console is root-owned (console has it ReadOnlyPaths); mode 0600
# still blocks kin-console from reading the plaintext.
INITIAL_PASSWORD_FILE = Path("/etc/kin-mail-console/initial-admin-password")


def clear_initial_password_file(path: Path | None = None) -> bool:
    """Remove the root-only first-boot password file if present.

    Returns True when a file was removed, False if already absent.
    Never reads or logs file contents.
    """
    target = path or INITIAL_PASSWORD_FILE
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Best-effort: login must not fail because cleanup could not unlink.
        raise


def write_initial_password_file(password: str, path: Path | None = None) -> Path:
    """Write plaintext password as root-only 0600 (for tests / root tooling)."""
    target = path or INITIAL_PASSWORD_FILE
    if not password:
        raise ValueError("password must be non-empty")
    # Exclusive create then replace so we never leave a world-readable temp.
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, (password + "\n").encode("utf-8"))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    try:
        os.chown(target, 0, 0)
    except PermissionError:
        pass
    return target
