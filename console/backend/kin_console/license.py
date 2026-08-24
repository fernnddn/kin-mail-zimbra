"""Verify KIN Mail license strings (Ed25519). No private key material here."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .license_keys import KIN_LICENSE_PUBLIC_KEY_B64

LICENSE_PAYLOAD_KEYS = ("expires_at", "issued_at", "seats", "server_id", "type")
GRACE_DAYS = 30
LICENSE_TYPES = ("trial", "subscription", "perpetual")
# Types that carry an expiry and enter the grace-period flow when it passes.
# Perpetual is the only type with no expiry at all.
EXPIRING_LICENSE_TYPES = ("trial", "subscription")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("ascii"))


def public_key_bytes() -> bytes:
    return _b64url_decode(KIN_LICENSE_PUBLIC_KEY_B64)


def canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode(
        "utf-8"
    )


def parse_license_string(token: str) -> tuple[dict[str, Any], bytes, bytes]:
    text = (token or "").strip()
    if "." not in text:
        raise ValueError("license is not a signed payload.signature string")
    payload_b64, sig_b64 = text.rsplit(".", 1)
    if not payload_b64 or not sig_b64:
        raise ValueError("license is missing payload or signature")
    payload_raw = _b64url_decode(payload_b64)
    signature = _b64url_decode(sig_b64)
    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("license payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("license payload must be an object")
    return payload, payload_raw, signature


def _parse_ts(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    extra = set(payload) - set(LICENSE_PAYLOAD_KEYS)
    if extra:
        raise ValueError("license payload has unexpected fields")
    missing = [k for k in ("server_id", "seats", "type", "issued_at") if k not in payload]
    if missing:
        raise ValueError("license payload is incomplete")
    kind = str(payload.get("type") or "")
    if kind not in LICENSE_TYPES:
        raise ValueError("license type must be trial, subscription, or perpetual")
    try:
        seats = int(payload["seats"])
    except (TypeError, ValueError) as exc:
        raise ValueError("license seats must be an integer") from exc
    if seats < 1:
        raise ValueError("license seats must be at least 1")
    issued = _parse_ts(payload.get("issued_at"))
    if issued is None:
        raise ValueError("license issued_at is missing")
    expires = _parse_ts(payload.get("expires_at"))
    if kind == "perpetual" and expires is not None:
        raise ValueError("perpetual licenses must not set expires_at")
    if kind in EXPIRING_LICENSE_TYPES and expires is None:
        raise ValueError(f"{kind} licenses require expires_at")
    server_id = str(payload.get("server_id") or "").strip()
    if not server_id:
        raise ValueError("license server_id is empty")
    return {
        "server_id": server_id,
        "seats": seats,
        "type": kind,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat() if expires else None,
    }


def verify_signature(payload_raw: bytes, signature: bytes, *, public_key: bytes | None = None) -> None:
    key = Ed25519PublicKey.from_public_bytes(public_key or public_key_bytes())
    if len(signature) != 64:
        raise ValueError("license signature is the wrong length")
    try:
        key.verify(signature, payload_raw)
    except InvalidSignature as exc:
        raise ValueError("license signature is not valid") from exc


def verify_license(
    token: str,
    *,
    server_id: str,
    now: datetime | None = None,
    public_key: bytes | None = None,
) -> dict[str, Any]:
    payload, payload_raw, signature = parse_license_string(token)
    # Verify against the exact serialized bytes in the token, not a re-encode.
    verify_signature(payload_raw, signature, public_key=public_key)
    clean = validate_payload(payload)
    expected = canonical_payload(
        {
            "expires_at": payload.get("expires_at"),
            "issued_at": payload.get("issued_at"),
            "seats": payload.get("seats"),
            "server_id": payload.get("server_id"),
            "type": payload.get("type"),
        }
    )
    if expected != payload_raw:
        raise ValueError("license payload is not in canonical form")
    if clean["server_id"] != str(server_id).strip():
        raise ValueError("license is for a different Email Server ID")
    clock = now or datetime.now(timezone.utc)
    expires_at = _parse_ts(clean["expires_at"])
    status = "active"
    grace_until = None
    if clean["type"] in EXPIRING_LICENSE_TYPES and expires_at is not None:
        if clock > expires_at:
            grace_until = expires_at + timedelta(days=GRACE_DAYS)
            if clock <= grace_until:
                status = "grace"
            else:
                status = "expired"
    return {
        **clean,
        "status": status,
        "grace_until": grace_until.isoformat() if grace_until else None,
        "provisioning_blocked": status in ("grace", "expired"),
    }


def sign_payload(payload: dict[str, Any], private_key: Any) -> str:
    """Used by the generator and tests. private_key is Ed25519PrivateKey."""
    clean = validate_payload(payload)
    to_sign = {
        "expires_at": payload.get("expires_at"),
        "issued_at": payload["issued_at"],
        "seats": int(payload["seats"]),
        "server_id": clean["server_id"],
        "type": clean["type"],
    }
    raw = canonical_payload(to_sign)
    sig = private_key.sign(raw)
    return (
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    )
