"""Mail gateway link: credentials, configuration file, and the privhelper command.

From 0.1.10 KIN Mail puts a Proxmox Mail Gateway in front of Zimbra. This
module owns the two things mail-gateway.sh deliberately does not: the API
credential, which is encrypted at rest and never leaves privhelperd, and the
small non-secret configuration file that says where the gateway is.

Design notes that are easy to get wrong later:

* The token secret is NEVER returned to the console, logged, or written to the
  transcript. It exists in plaintext only inside the environment of one
  subprocess, for the length of one run.
* The configuration file holds no secret at all, so it can be 0644 and the
  console can read it for status without going through privhelperd.
* "Configured" means a host AND an applied link. A half-finished connect must
  not make the compliance gate go green.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GATEWAY_CONF = Path(os.environ.get("KIN_MAIL_GATEWAY_CONF", "/etc/kin-mail/mail-gateway.conf"))
STATE_DIR = Path(os.environ.get("KIN_PMG_STATE_DIR", "/var/lib/kin-mail-console"))
STATE_FILE = STATE_DIR / "mail-gateway-state.json"
PIN_FILE = STATE_DIR / "mail-gateway-fingerprint"

VAULT_PATH = Path(
    os.environ.get(
        "KIN_PRIVHELPER_GATEWAY_VAULT",
        "/var/lib/kin-mail-privhelper/mail-gateway-secrets.json",
    )
)

ALLOWED_FIELDS = ("token_secret", "password")

# Operations the console may ask for. Anything else is refused by name rather
# than falling through to the script, which would turn a typo into a shell
# argument.
READ_ONLY_OPS = ("probe", "plan", "verify")
MUTATING_OPS = ("apply", "revert")
LOCAL_OPS = ("connect", "trust_certificate", "forget", "status")
ALL_OPS = READ_ONLY_OPS + MUTATING_OPS + LOCAL_OPS

AUTH_MODES = ("token", "ticket")

MAIL_GATEWAY_CANDIDATES = (
    "install/lib/mail-gateway.sh",
    "lib/mail-gateway.sh",
)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def valid_host(value: str) -> bool:
    """A hostname or an IPv4/IPv6 literal, and nothing that could carry a shell
    metacharacter or a newline into a configuration file the script parses."""
    if not value or len(value) > 255:
        return False
    if value[0] in ".-":
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:_-")
    return all(ch in allowed for ch in value)


def valid_token_id(value: str) -> bool:
    """PMG token ids look like ``root@pam!kinmail``.

    The '!' and '@' are part of the format, so the check cannot just be
    alphanumeric; what it must exclude is whitespace, quotes and anything that
    would end the shell word or the config line.
    """
    if not value or len(value) > 128:
        return False
    if "!" not in value or "@" not in value:
        return False
    forbidden = set(" \t\r\n\"'`$\;|&<>()")
    return not (set(value) & forbidden)


def valid_port(value: Any) -> bool:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return False
    return 1 <= port <= 65535


# --------------------------------------------------------------------------
# Credential vault (same Fernet key as the host provisioning secrets)
# --------------------------------------------------------------------------
def store_credentials(updates: dict[str, str]) -> dict[str, Any]:
    """Encrypt the gateway credential at rest. Returns metadata, never values."""
    from .provisioning_secrets import KEY_PATH, _chmod_root_only, _fernet

    payload = {k: str(v) for k, v in updates.items() if k in ALLOWED_FIELDS and v}
    if not payload:
        raise ValueError("no gateway credential supplied")

    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(VAULT_PATH.parent, 0o700)
    except OSError:
        pass

    token = _fernet(KEY_PATH).encrypt(json.dumps(payload).encode("utf-8"))
    meta = {
        "ciphertext": token.decode("ascii"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fields": sorted(payload),
    }
    tmp = VAULT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    _chmod_root_only(tmp)
    os.replace(tmp, VAULT_PATH)
    return {"updated_at": meta["updated_at"], "fields": meta["fields"]}


def load_credentials() -> dict[str, str]:
    from cryptography.fernet import InvalidToken

    from .provisioning_secrets import KEY_PATH, _fernet

    if not VAULT_PATH.is_file():
        return {}
    raw = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("mail gateway vault is not a JSON object")
    token = str(raw.get("ciphertext") or "")
    if not token:
        return {}
    try:
        plain = _fernet(KEY_PATH).decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("cannot decrypt the mail gateway credential (key mismatch?)") from exc
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict):
        return {}
    return {f: str(data[f]) for f in ALLOWED_FIELDS if data.get(f)}


def forget_credentials() -> None:
    try:
        VAULT_PATH.unlink()
    except FileNotFoundError:
        pass


def credential_present() -> bool:
    try:
        return bool(load_credentials())
    except (OSError, RuntimeError, json.JSONDecodeError):
        # An unreadable vault is not a present credential. Saying "yes" here
        # would let the compliance gate pass on a credential nothing can use.
        return False


# --------------------------------------------------------------------------
# Configuration file (no secrets — readable by the console)
# --------------------------------------------------------------------------
def read_config() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = GATEWAY_CONF.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def valid_public_ip(value: str) -> bool:
    """A routable IPv4 address, or nothing.

    Deliberately refuses RFC1918 and loopback. This value is published in an
    SPF record; a private address there authorises nobody, and every outbound
    message fails SPF. A live deployment was told to publish "ip4:10.x"
    because the console reused the management address here.
    """
    if not value:
        return True
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octets):
        return False
    if not all(p.isdigit() for p in parts):
        return False
    a, b = octets[0], octets[1]
    if a in (10, 127, 0):
        return False
    if a == 192 and b == 168:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 169 and b == 254:
        return False
    if a >= 224:
        return False
    return True


def valid_public_hostname(value: str) -> bool:
    """A name an MX record can point at, or nothing.

    An MX record cannot hold an address - only a name - so a bare IP here is
    not a usable answer even when it is a public one.
    """
    if not value:
        return True
    if not valid_host(value):
        return False
    if "." not in value:
        return False
    labels = value.split(".")
    if any(not lab for lab in labels):
        return False
    # All-numeric labels mean somebody typed an address.
    return not all(lab.isdigit() for lab in labels)


def write_config(
    *,
    host: str,
    api_port: int = 8006,
    auth: str = "token",
    token_id: str = "",
    user: str = "root@pam",
    cacert: str = "",
    enabled: bool = True,
    public_host: str = "",
    public_ip: str = "",
    greylist: bool = False,
) -> None:
    """Write the non-secret half of the link configuration.

    0644 on purpose: the console reads it directly for the status panel, and
    there is nothing in it worth protecting. Everything that is worth
    protecting is in the vault.
    """
    from .deploy_state import ensure_kin_mail_dir

    ensure_kin_mail_dir(GATEWAY_CONF.parent)
    body = (
        "# KIN Mail - mail gateway link. Written by the console; no secrets here.\n"
        "# The API credential is encrypted in the privhelper vault.\n"
        f"GATEWAY_ENABLED={'1' if enabled else '0'}\n"
        f'GATEWAY_HOST="{host}"\n'
        f"GATEWAY_API_PORT={int(api_port)}\n"
        f"GATEWAY_AUTH={auth}\n"
        f'GATEWAY_TOKEN_ID="{token_id}"\n'
        f'GATEWAY_USER="{user}"\n'
        f'GATEWAY_CACERT="{cacert}"\n'
        "# What the internet sees. Only these two belong in DNS; GATEWAY_HOST\n"
        "# is how this appliance reaches the gateway on the local network.\n"
        f'GATEWAY_PUBLIC_HOST="{public_host}"\n'
        f'GATEWAY_PUBLIC_IP="{public_ip}"\n'
        "# Greylisting defers the first message from every unseen sender by a\n"
        "# few minutes. Off by default: the other layers still run.\n"
        f"GATEWAY_GREYLIST={'1' if greylist else '0'}\n"
    )
    tmp = GATEWAY_CONF.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, GATEWAY_CONF)


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def pinned_fingerprint(host: str) -> str:
    try:
        text = PIN_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == host:
            return parts[1]
    return ""


def pin_fingerprint(host: str, fingerprint: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    try:
        lines = [
            ln
            for ln in PIN_FILE.read_text(encoding="utf-8").splitlines()
            if ln.split()[:1] != [host]
        ]
    except (OSError, UnicodeDecodeError, IndexError):
        lines = []
    lines.append(f"{host} {fingerprint}")
    tmp = PIN_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, PIN_FILE)


def status() -> dict[str, Any]:
    """What the console shows, and what the compliance gate judges.

    ``compliant`` is deliberately strict: a host on its own is somebody who
    opened the dialog, not a mail gateway that is carrying mail.
    """
    conf = read_config()
    state = read_state()
    host = conf.get("GATEWAY_HOST", "")
    enabled = conf.get("GATEWAY_ENABLED") == "1"
    phase = str(state.get("phase") or "never-applied")
    return {
        "enabled": enabled,
        "gateway_host": host,
        "api_port": conf.get("GATEWAY_API_PORT", "8006"),
        "auth_mode": conf.get("GATEWAY_AUTH", "token"),
        "token_id": conf.get("GATEWAY_TOKEN_ID", ""),
        "public_host": conf.get("GATEWAY_PUBLIC_HOST", ""),
        "public_ip": conf.get("GATEWAY_PUBLIC_IP", ""),
        "greylist": conf.get("GATEWAY_GREYLIST") == "1",
        # Whether DNS advice can be given at all. Without both halves the
        # console must not print records, because the only address it has is
        # the one the internet cannot route to.
        "public_identity_complete": bool(
            conf.get("GATEWAY_PUBLIC_HOST") and conf.get("GATEWAY_PUBLIC_IP")
        ),
        "credential_present": credential_present(),
        "certificate_pinned": bool(pinned_fingerprint(host)) if host else False,
        "phase": phase,
        "applied_at": str(state.get("applied_at") or state.get("at") or ""),
        # Two different questions, and conflating them is how a compliance
        # banner goes green on a link that carries no mail. "configured" means
        # somebody finished the Connect dialog. "compliant" means the link was
        # actually applied to both sides.
        "configured": bool(enabled and host and credential_present()),
        "compliant": bool(enabled and host and phase == "applied"),
    }
