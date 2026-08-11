"""Session cookie auth — bcrypt password hash on disk, no plaintext storage."""

from __future__ import annotations

import secrets

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
    path.write_text(password_hash + "\n", encoding="utf-8")
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


def generate_bootstrap_password() -> str:
    return secrets.token_urlsafe(18)
