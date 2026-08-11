"""Multi-user local store (bcrypt hashes, roles). Replaces single admin.hash."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import auth
from kin_privhelper.rbac import ALL_ROLES, ROLE_SUPER_ADMIN
from .settings import settings

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")


@dataclass(frozen=True)
class ConsoleUser:
    username: str
    password_hash: str
    role: str
    disabled: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role,
            "disabled": self.disabled,
        }


def users_file() -> Path:
    return settings.users_file


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".users.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _parse_users(data: dict[str, Any]) -> list[ConsoleUser]:
    out: list[ConsoleUser] = []
    for item in data.get("users") or []:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "").strip()
        password_hash = str(item.get("password_hash") or "").strip()
        role = str(item.get("role") or "").strip()
        disabled = bool(item.get("disabled", False))
        if not username or not password_hash or role not in ALL_ROLES:
            continue
        out.append(
            ConsoleUser(
                username=username,
                password_hash=password_hash,
                role=role,
                disabled=disabled,
            )
        )
    return out


def load_users() -> list[ConsoleUser]:
    path = users_file()
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("users.json must be an object")
    return _parse_users(data)


def save_users(users: list[ConsoleUser]) -> None:
    payload = {
        "version": 1,
        "users": [
            {
                "username": u.username,
                "password_hash": u.password_hash,
                "role": u.role,
                "disabled": u.disabled,
            }
            for u in users
        ],
    }
    _atomic_write(users_file(), payload)


def get_user(username: str) -> ConsoleUser | None:
    key = username.strip()
    for u in load_users():
        if u.username == key:
            return u
    return None


def ensure_users_store() -> None:
    """Migrate legacy admin.hash → users.json on first boot after upgrade."""
    path = users_file()
    if path.is_file():
        return
    if auth.password_hash_exists():
        legacy_hash = auth.load_password_hash()
        bootstrap_user = settings.console_user.strip() or "admin"
        save_users(
            [
                ConsoleUser(
                    username=bootstrap_user,
                    password_hash=legacy_hash,
                    role=ROLE_SUPER_ADMIN,
                    disabled=False,
                )
            ]
        )
        return
    raise RuntimeError(
        f"Missing users store at {path} and legacy {settings.password_hash_file}. "
        "Run console/bootstrap.sh first."
    )


def validate_username(username: str) -> str:
    u = username.strip()
    if not _USERNAME_RE.match(u):
        raise ValueError(
            "username must be 1–64 chars: letters, digits, dot, underscore, hyphen"
        )
    return u


def validate_role(role: str) -> str:
    r = role.strip()
    if r not in ALL_ROLES:
        raise ValueError(f"role must be one of: {', '.join(sorted(ALL_ROLES))}")
    return r


def authenticate(username: str, password: str) -> ConsoleUser | None:
    user = get_user(username.strip())
    if user is None or user.disabled:
        return None
    if not auth.verify_password(password, user.password_hash):
        return None
    return user


def list_public_users() -> list[dict[str, Any]]:
    return [u.public() for u in load_users()]


def create_user(username: str, password: str, role: str) -> ConsoleUser:
    username = validate_username(username)
    role = validate_role(role)
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    users = load_users()
    if any(u.username == username for u in users):
        raise ValueError(f"user {username!r} already exists")
    created = ConsoleUser(
        username=username,
        password_hash=auth.hash_password(password),
        role=role,
        disabled=False,
    )
    users.append(created)
    save_users(users)
    return created


def delete_user(username: str, *, actor: str) -> None:
    username = username.strip()
    users = load_users()
    target = next((u for u in users if u.username == username), None)
    if target is None:
        raise KeyError(username)
    if username == actor:
        raise ValueError("cannot delete your own account")
    remaining = [u for u in users if u.username != username]
    supers = [u for u in remaining if u.role == ROLE_SUPER_ADMIN and not u.disabled]
    if target.role == ROLE_SUPER_ADMIN and not supers:
        raise ValueError("cannot delete the last KIN Super Admin")
    save_users(remaining)
