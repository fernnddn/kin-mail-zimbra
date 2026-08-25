"""Encrypted-at-rest host provisioning passwords (privhelperd only).

Key:  /etc/kin-mail/privhelper-secrets.key   (root:root 0600, Fernet key)
Vault: /var/lib/kin-mail-privhelper/provisioning-secrets.json  (root:root 0600)
Marker (no secrets): /var/lib/kin-mail-console/provisioning-secrets.present

The console process cannot read the key or vault. API responses never include
plaintext. Rotation: generate a new key, re-encrypt, replace both files
(see rotate_key). Revocation: delete vault + key; operator re-enters passwords
in the wizard.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

KEY_PATH = Path(os.environ.get("KIN_PRIVHELPER_SECRETS_KEY", "/etc/kin-mail/privhelper-secrets.key"))
VAULT_PATH = Path(
    os.environ.get(
        "KIN_PRIVHELPER_SECRETS_VAULT",
        "/var/lib/kin-mail-privhelper/provisioning-secrets.json",
    )
)
MARKER_PATH = Path(
    os.environ.get(
        "KIN_PRIVHELPER_SECRETS_MARKER",
        "/var/lib/kin-mail-console/provisioning-secrets.present",
    )
)

ALLOWED_FIELDS = ("host_root_pass", "kin_user_pass", "hacluster_pass", "chap_pass")


def marker_present() -> bool:
    if not MARKER_PATH.is_file():
        return False
    try:
        data = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return MARKER_PATH.stat().st_size > 0
    return bool(data.get("set"))


def _chmod_root_only(path: Path) -> None:
    os.chmod(path, 0o600)
    try:
        os.chown(path, 0, 0)
    except PermissionError:
        pass


def _load_or_create_key(path: Path = KEY_PATH) -> bytes:
    if path.is_file():
        key = path.read_bytes().strip()
        if not key:
            raise RuntimeError(f"empty secrets key at {path}")
        return key
    from .deploy_state import ensure_kin_mail_dir

    ensure_kin_mail_dir(path.parent)
    key = Fernet.generate_key()
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key + b"\n")
    _chmod_root_only(tmp)
    tmp.replace(path)
    _chmod_root_only(path)
    return key


def _fernet(path: Path = KEY_PATH) -> Fernet:
    return Fernet(_load_or_create_key(path))


def load_secrets(
    *,
    key_path: Path = KEY_PATH,
    vault_path: Path = VAULT_PATH,
) -> dict[str, str]:
    if not vault_path.is_file():
        return {}
    raw = json.loads(vault_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("vault is not a JSON object")
    token = str(raw.get("ciphertext") or "")
    if not token:
        return {}
    try:
        plain = _fernet(key_path).decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("cannot decrypt provisioning secrets (key mismatch?)") from exc
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for field in ALLOWED_FIELDS:
        val = data.get(field)
        if isinstance(val, str) and val:
            out[field] = val
    return out


def store_secrets(
    updates: dict[str, str],
    *,
    key_path: Path = KEY_PATH,
    vault_path: Path = VAULT_PATH,
    marker_path: Path = MARKER_PATH,
) -> dict[str, Any]:
    """Merge updates into the vault. Empty strings are ignored (preserve-on-empty)."""
    current = {}
    try:
        current = load_secrets(key_path=key_path, vault_path=vault_path)
    except RuntimeError:
        current = {}
    merged = dict(current)
    for field in ALLOWED_FIELDS:
        val = updates.get(field)
        if isinstance(val, str) and val:
            if len(val) < 8:
                raise ValueError(f"{field} must be at least 8 characters")
            if len(val) > 256:
                raise ValueError(f"{field} too long")
            merged[field] = val
    if "host_root_pass" not in merged or "kin_user_pass" not in merged:
        raise ValueError("both host_root_pass and kin_user_pass are required")

    payload = json.dumps(merged, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    token = _fernet(key_path).encrypt(payload).decode("ascii")
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"v": 1, "alg": "fernet", "ciphertext": token},
        indent=2,
    ) + "\n"
    tmp = vault_path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    _chmod_root_only(tmp)
    tmp.replace(vault_path)
    _chmod_root_only(vault_path)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    marker = json.dumps({"set": True, "updated_at": stamp, "fields": list(ALLOWED_FIELDS)}) + "\n"
    mtmp = marker_path.with_suffix(".tmp")
    mtmp.write_text(marker, encoding="utf-8")
    os.chmod(mtmp, 0o640)
    try:
        import grp

        gid = grp.getgrnam("kin-console").gr_gid
        os.chown(mtmp, 0, gid)
    except (KeyError, PermissionError, OSError):
        pass
    mtmp.replace(marker_path)
    try:
        os.chmod(marker_path, 0o640)
    except OSError:
        pass
    return {"set": True, "updated_at": stamp}


def rotate_key(
    *,
    key_path: Path = KEY_PATH,
    vault_path: Path = VAULT_PATH,
    marker_path: Path = MARKER_PATH,
) -> None:
    """Re-encrypt the vault with a freshly generated key. Old key is replaced."""
    secrets = load_secrets(key_path=key_path, vault_path=vault_path)
    if vault_path.is_file():
        vault_path.unlink()
    if key_path.is_file():
        key_path.unlink()
    if secrets:
        store_secrets(
            secrets,
            key_path=key_path,
            vault_path=vault_path,
            marker_path=marker_path,
        )
