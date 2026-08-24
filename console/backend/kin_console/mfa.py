"""TOTP MFA helpers (RFC 6238 via pyotp). Never log secrets or codes."""

from __future__ import annotations

import time
from typing import Any

import pyotp
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .settings import settings

ISSUER = "KIN Mail Console"
PENDING_LOGIN_MAX_AGE_SEC = 5 * 60
PENDING_ENROLL_MAX_AGE_SEC = 10 * 60
MAX_MFA_ATTEMPTS = 5

# Server-side attempt counters for pending login tokens (token id -> count).
# In-memory is enough: restart clears them and the operator must sign in again.
_login_attempts: dict[str, int] = {}


def _pending_serializer(*, salt: str) -> URLSafeTimedSerializer:
    secret = settings.session_secret_file.read_text(encoding="utf-8").strip()
    return URLSafeTimedSerializer(secret, salt=salt)


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def verify_totp(secret: str, code: str, *, at_time: int | None = None) -> bool:
    """Verify a 6-digit code. Uses pyotp with a one-step window for clock skew."""
    cleaned = (code or "").strip().replace(" ", "")
    if not secret or not cleaned.isdigit() or len(cleaned) != 6:
        return False
    totp = pyotp.TOTP(secret)
    if at_time is None:
        return bool(totp.verify(cleaned, valid_window=1))
    # Fixed-time path for tests: compare against the exact step and +/- 1.
    for offset in (-1, 0, 1):
        if totp.at(at_time + offset * totp.interval) == cleaned:
            return True
    return False


def mint_login_pending(username: str) -> str:
    tid = pyotp.random_base32()[:16]
    _login_attempts[tid] = 0
    return _pending_serializer(salt="kin-mail-console-mfa-login").dumps(
        {"u": username, "tid": tid, "t": int(time.time())}
    )


def read_login_pending(token: str) -> dict[str, Any]:
    try:
        data = _pending_serializer(salt="kin-mail-console-mfa-login").loads(
            token, max_age=PENDING_LOGIN_MAX_AGE_SEC
        )
    except SignatureExpired as exc:
        raise ValueError("MFA challenge expired. Sign in again.") from exc
    except BadSignature as exc:
        raise ValueError("Invalid MFA challenge. Sign in again.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid MFA challenge. Sign in again.")
    username = data.get("u")
    tid = data.get("tid")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Invalid MFA challenge. Sign in again.")
    if not isinstance(tid, str) or not tid:
        raise ValueError("Invalid MFA challenge. Sign in again.")
    return {"username": username.strip(), "tid": tid}


def login_attempts_remaining(tid: str) -> int:
    used = int(_login_attempts.get(tid, 0))
    return max(0, MAX_MFA_ATTEMPTS - used)


def register_login_failure(tid: str) -> int:
    used = int(_login_attempts.get(tid, 0)) + 1
    _login_attempts[tid] = used
    return max(0, MAX_MFA_ATTEMPTS - used)


def clear_login_attempts(tid: str) -> None:
    _login_attempts.pop(tid, None)


def mint_enroll_pending(username: str, secret: str) -> str:
    return _pending_serializer(salt="kin-mail-console-mfa-enroll").dumps(
        {"u": username, "s": secret, "t": int(time.time())}
    )


def read_enroll_pending(token: str, *, expect_username: str) -> str:
    try:
        data = _pending_serializer(salt="kin-mail-console-mfa-enroll").loads(
            token, max_age=PENDING_ENROLL_MAX_AGE_SEC
        )
    except SignatureExpired as exc:
        raise ValueError("Enrollment expired. Start MFA setup again.") from exc
    except BadSignature as exc:
        raise ValueError("Invalid enrollment token. Start MFA setup again.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid enrollment token. Start MFA setup again.")
    username = data.get("u")
    secret = data.get("s")
    if not isinstance(username, str) or username.strip() != expect_username.strip():
        raise ValueError("Enrollment token does not match this account.")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("Invalid enrollment token. Start MFA setup again.")
    return secret.strip()
