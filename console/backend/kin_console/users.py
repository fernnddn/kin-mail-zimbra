"""Multi-user local store (bcrypt hashes and/or AD-backed accounts)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kin_privhelper.rbac import ALL_ROLES, ROLE_SUPER_ADMIN

from . import ad_auth, auth
from .settings import settings

AUTH_LOCAL: Literal["local"] = "local"
AUTH_AD: Literal["ad"] = "ad"
ALL_AUTH_TYPES = frozenset({AUTH_LOCAL, AUTH_AD})

# Console login name / AD UPN (allows @).
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._@+-]{1,128}$")


@dataclass(frozen=True)
class ConsoleUser:
    username: str
    role: str
    auth_type: str = AUTH_LOCAL
    password_hash: str = ""
    ad_username: str = ""
    disabled: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role,
            "auth_type": self.auth_type,
            "ad_username": self.ad_username if self.auth_type == AUTH_AD else "",
            "disabled": self.disabled,
        }

    def ldap_identity(self) -> str:
        if self.auth_type != AUTH_AD:
            return ""
        return (self.ad_username or self.username).strip()


class AuthError(Exception):
    """Login failure with an operator-facing message (never includes passwords)."""

    def __init__(self, message: str, *, http_status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def users_file() -> Path:
    return settings.users_file


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    prev = path.stat() if path.is_file() else None
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".users.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        if prev is not None:
            try:
                os.chown(path, prev.st_uid, prev.st_gid)
            except PermissionError:
                pass
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
        role = str(item.get("role") or "").strip()
        auth_type = str(item.get("auth_type") or AUTH_LOCAL).strip() or AUTH_LOCAL
        password_hash = str(item.get("password_hash") or "").strip()
        ad_username = str(item.get("ad_username") or "").strip()
        disabled = bool(item.get("disabled", False))
        if not username or role not in ALL_ROLES or auth_type not in ALL_AUTH_TYPES:
            continue
        if auth_type == AUTH_LOCAL and not password_hash:
            continue
        if auth_type == AUTH_AD:
            password_hash = ""  # never keep a local hash for AD accounts
        out.append(
            ConsoleUser(
                username=username,
                role=role,
                auth_type=auth_type,
                password_hash=password_hash,
                ad_username=ad_username,
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


def users_payload(users: list[ConsoleUser]) -> dict[str, Any]:
    """JSON object for users.json. Caller must not log this (contains hashes)."""
    return {
        "version": 1,
        "users": [
            {
                "username": u.username,
                "role": u.role,
                "auth_type": u.auth_type,
                "password_hash": u.password_hash if u.auth_type == AUTH_LOCAL else "",
                "ad_username": u.ad_username if u.auth_type == AUTH_AD else "",
                "disabled": u.disabled,
            }
            for u in users
        ],
    }


def save_users(users: list[ConsoleUser]) -> None:
    _atomic_write(users_file(), users_payload(users))


def users_json_text(users: list[ConsoleUser]) -> str:
    return json.dumps(users_payload(users), indent=2, ensure_ascii=False) + "\n"


def get_user(username: str) -> ConsoleUser | None:
    key = username.strip()
    for u in load_users():
        if u.username == key:
            return u
    return None


def ensure_users_store() -> None:
    """Migrate legacy admin.hash → users.json; rewrite missing auth_type fields."""
    path = users_file()
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        users = _parse_users(raw if isinstance(raw, dict) else {})
        if users:
            # Persist auth_type / ad_username keys for older files.
            needs = False
            for item in (raw.get("users") if isinstance(raw, dict) else None) or []:
                if not isinstance(item, dict):
                    continue
                if "auth_type" not in item:
                    needs = True
                    break
            if needs:
                save_users(users)
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
                    auth_type=AUTH_LOCAL,
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
            "username must be 1-128 chars: letters, digits, . _ @ + -"
        )
    return u


def validate_role(role: str) -> str:
    r = role.strip()
    if r not in ALL_ROLES:
        raise ValueError(f"role must be one of: {', '.join(sorted(ALL_ROLES))}")
    return r


def validate_auth_type(auth_type: str) -> str:
    a = (auth_type or AUTH_LOCAL).strip()
    if a not in ALL_AUTH_TYPES:
        raise ValueError("auth_type must be 'local' or 'ad'")
    return a


def authenticate(username: str, password: str) -> ConsoleUser:
    """Return the user on success; raise AuthError on failure (fail closed for AD)."""
    user = get_user(username.strip())
    if user is None or user.disabled:
        raise AuthError("Invalid credentials")

    if user.auth_type == AUTH_LOCAL:
        if not auth.verify_password(password, user.password_hash):
            raise AuthError("Invalid credentials")
        return user

    if user.auth_type == AUTH_AD:
        result = ad_auth.verify_ad_password(user.ldap_identity(), password)
        if result.ok:
            return user
        if result.reason in ("not_enabled", "misconfigured", "unreachable"):
            raise AuthError(result.detail or "AD authentication unavailable", http_status=503)
        raise AuthError("Invalid credentials")

    raise AuthError("Invalid credentials")


def list_public_users() -> list[dict[str, Any]]:
    return [u.public() for u in load_users()]


def apply_create_user(
    users: list[ConsoleUser],
    username: str,
    role: str,
    *,
    auth_type: str = AUTH_LOCAL,
    password: str = "",
    password_hash: str = "",
    ad_username: str = "",
) -> tuple[ConsoleUser, list[ConsoleUser]]:
    """Return (created, new_list). Does not write disk."""
    username = validate_username(username)
    role = validate_role(role)
    auth_type = validate_auth_type(auth_type)
    if any(u.username == username for u in users):
        raise ValueError(f"user {username!r} already exists")

    if auth_type == AUTH_LOCAL:
        hashed = (password_hash or "").strip()
        if hashed:
            if password:
                raise ValueError("pass password or password_hash, not both")
        else:
            if len(password) < 8:
                raise ValueError("password must be at least 8 characters")
            hashed = auth.hash_password(password)
        created = ConsoleUser(
            username=username,
            role=role,
            auth_type=AUTH_LOCAL,
            password_hash=hashed,
            ad_username="",
            disabled=False,
        )
    else:
        if password or password_hash:
            raise ValueError("AD users must not have a local password")
        ad_id = validate_username(ad_username.strip() or username)
        created = ConsoleUser(
            username=username,
            role=role,
            auth_type=AUTH_AD,
            password_hash="",
            ad_username=ad_id,
            disabled=False,
        )

    return created, [*users, created]


def apply_delete_user(
    users: list[ConsoleUser],
    username: str,
    *,
    actor: str,
) -> list[ConsoleUser]:
    """Return the list with username removed. Does not write disk."""
    username = username.strip()
    target = next((u for u in users if u.username == username), None)
    if target is None:
        raise KeyError(username)
    if username == actor:
        raise ValueError("cannot delete your own account")
    remaining = [u for u in users if u.username != username]
    supers = [u for u in remaining if u.role == ROLE_SUPER_ADMIN and not u.disabled]
    if target.role == ROLE_SUPER_ADMIN and not supers:
        raise ValueError("cannot delete the last KIN Super Admin")
    return remaining


def apply_set_local_password(
    users: list[ConsoleUser],
    username: str,
    *,
    password: str = "",
    password_hash: str = "",
) -> tuple[ConsoleUser, list[ConsoleUser]]:
    """Return (updated, new_list). Does not write disk."""
    username = validate_username(username)
    hashed = (password_hash or "").strip()
    if hashed:
        if password:
            raise ValueError("pass password or password_hash, not both")
    else:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        hashed = auth.hash_password(password)
    idx = next((i for i, u in enumerate(users) if u.username == username), None)
    if idx is None:
        raise KeyError(username)
    target = users[idx]
    if target.auth_type != AUTH_LOCAL:
        raise ValueError("cannot set a local password on an AD-backed account")
    updated = ConsoleUser(
        username=target.username,
        role=target.role,
        auth_type=AUTH_LOCAL,
        password_hash=hashed,
        ad_username="",
        disabled=target.disabled,
    )
    out = list(users)
    out[idx] = updated
    return updated, out


def finalize_password_side_effects(username: str, password_hash: str) -> None:
    """Keep legacy admin.hash aligned; drop first-boot plaintext when possible."""
    if username == (settings.console_user.strip() or "admin"):
        auth.write_password_hash(password_hash)
    try:
        from kin_privhelper.initial_password import clear_initial_password_file

        clear_initial_password_file()
    except OSError:
        pass


def create_user(
    username: str,
    role: str,
    *,
    auth_type: str = AUTH_LOCAL,
    password: str = "",
    ad_username: str = "",
) -> ConsoleUser:
    created, users = apply_create_user(
        load_users(),
        username,
        role,
        auth_type=auth_type,
        password=password,
        ad_username=ad_username,
    )
    save_users(users)
    return created


def delete_user(username: str, *, actor: str) -> None:
    remaining = apply_delete_user(load_users(), username, actor=actor)
    save_users(remaining)


def set_local_password(username: str, password: str) -> ConsoleUser:
    """Rotate a local user's password; drop any root-only first-boot plaintext."""
    updated, users = apply_set_local_password(load_users(), username, password=password)
    save_users(users)
    finalize_password_side_effects(updated.username, updated.password_hash)
    return updated
