"""Short-lived Observability VM credentials (privhelperd only).

Separate from the mail-node provisioning vault. Same Fernet key. Cleared
after a successful Add Observability apply. Never returned to the console.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken

from .provisioning_secrets import KEY_PATH, _chmod_root_only, _fernet

VAULT_PATH = Path(
    os.environ.get(
        "KIN_PRIVHELPER_OBS_VAULT",
        "/var/lib/kin-mail-privhelper/observability-secrets.json",
    )
)

ALLOWED_FIELDS = ("ip", "hostname", "root_pass", "kin_user_pass")


def load_observability_secrets(
    *,
    key_path: Path = KEY_PATH,
    vault_path: Path = VAULT_PATH,
) -> dict[str, str]:
    if not vault_path.is_file():
        return {}
    raw = json.loads(vault_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("observability vault is not a JSON object")
    token = str(raw.get("ciphertext") or "")
    if not token:
        return {}
    try:
        plain = _fernet(key_path).decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("cannot decrypt observability secrets (key mismatch?)") from exc
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for field in ALLOWED_FIELDS:
        val = data.get(field)
        if isinstance(val, str) and val:
            out[field] = val
    return out


def store_observability_secrets(
    updates: dict[str, str],
    *,
    key_path: Path = KEY_PATH,
    vault_path: Path = VAULT_PATH,
) -> dict[str, Any]:
    ip = str(updates.get("ip") or "").strip()
    hostname = str(updates.get("hostname") or "").strip()
    root_pass = str(updates.get("root_pass") or "")
    kin_pass = str(updates.get("kin_user_pass") or "")
    if not ip:
        raise ValueError("observability ip is required")
    if len(root_pass) < 8 or len(kin_pass) < 8:
        raise ValueError("root and kin passwords must be at least 8 characters")
    if len(root_pass) > 256 or len(kin_pass) > 256:
        raise ValueError("password too long")
    payload = {
        "ip": ip,
        "hostname": hostname,
        "root_pass": root_pass,
        "kin_user_pass": kin_pass,
    }
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    token = _fernet(key_path).encrypt(blob).decode("ascii")
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"v": 1, "alg": "fernet", "ciphertext": token}, indent=2) + "\n"
    tmp = vault_path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    _chmod_root_only(tmp)
    tmp.replace(vault_path)
    _chmod_root_only(vault_path)
    return {"set": True, "ip": ip, "hostname": hostname}


def clear_observability_secrets(*, vault_path: Path = VAULT_PATH) -> None:
    if vault_path.is_file():
        vault_path.unlink()
