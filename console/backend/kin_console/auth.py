"""Session cookie auth — bcrypt password hash on disk, no plaintext storage."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

import bcrypt
from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .settings import settings


def _serializer() -> URLSafeTimedSerializer:
    secret = settings.session_secret_file.read_text(encoding="utf-8").strip()
    return URLSafeTimedSerializer(secret, salt="kin-mail-console-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def password_hash_exists() -> bool:
    return settings.password_hash_file.is_file()


def load_password_hash() -> str:
    return settings.password_hash_file.read_text(encoding="utf-8").strip()


def write_password_hash(password_hash: str) -> None:
    path = settings.password_hash_file
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = path.stat() if path.is_file() else None
    path.write_text(password_hash + "\n", encoding="utf-8")
    if prev is not None:
        try:
            os.chown(path, prev.st_uid, prev.st_gid)
        except PermissionError:
            pass
    path.chmod(0o600)


def ensure_session_secret() -> None:
    path = settings.session_secret_file
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    path.chmod(0o600)


def set_session_cookie(response: Response, username: str) -> None:
    token = _serializer().dumps({"u": username})
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.session_max_age_sec,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.cookie_name, path="/")


def current_username(request: Request) -> str | None:
    raw = request.cookies.get(settings.cookie_name)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=settings.session_max_age_sec)
    except (BadSignature, SignatureExpired):
        return None
    user = data.get("u")
    return user if isinstance(user, str) else None


def require_user(request: Request) -> str:
    user = current_username(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_console_user(request: Request):
    """Return ConsoleUser for the session (fresh role from disk)."""
    from . import users as users_mod

    username = require_user(request)
    record = users_mod.get_user(username)
    if record is None or record.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return record


@dataclass(frozen=True)
class WizardActor:
    """Caller for wizard endpoints — real user, or anonymous setup pre-deploy."""

    username: str
    role: str
    anonymous_setup: bool = False


def wizard_actor(request: Request) -> WizardActor:
    """Allow anonymous wizard access only until mail is genuinely deployed.

    After deploy (setup-complete marker, or legacy mailboxd running), always
    requires a normal console session. A leftover /opt/zimbra tree from a
    failed installer is not enough.
    """
    from kin_privhelper.deploy_state import SETUP_USERNAME, is_mail_deployed
    from kin_privhelper.rbac import ROLE_SUPER_ADMIN

    from . import users as users_mod

    if is_mail_deployed():
        user = require_console_user(request)
        return WizardActor(username=user.username, role=user.role, anonymous_setup=False)

    # Pre-deploy: prefer a real session if present, else synthetic setup identity.
    uname = current_username(request)
    if uname:
        record = users_mod.get_user(uname)
        if record is not None and not record.disabled:
            return WizardActor(
                username=record.username, role=record.role, anonymous_setup=False
            )

    return WizardActor(
        username=SETUP_USERNAME,
        role=ROLE_SUPER_ADMIN,
        anonymous_setup=True,
    )


def require_roles(*allowed: str):
    allowed_set = frozenset(allowed)

    def _dep(request: Request):
        user = require_console_user(request)
        if user.role not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {', '.join(sorted(allowed_set))} (your role: {user.role})",
            )
        return user

    return _dep


def generate_bootstrap_password() -> str:
    return secrets.token_urlsafe(18)
