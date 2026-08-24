#!/usr/bin/env python3
"""KIN Mail license generator — standalone, portable copy.

This copy has NO dependency on the KIN Mail product repo. It carries its own
copy of the exact signing logic used by the product's verifier
(console/backend/kin_console/license.py) so this whole folder can be copied
between machines (macOS, Linux, Windows) and keep working, without needing a
git checkout of the product alongside it.

keys/ed25519-private.pem in this folder is the one secret that makes every
KIN Mail license real. It must never be committed — .gitignore in this repo
blocks *.pem and licensing-generator/keys/ specifically, but treat that as a
backstop, not the actual protection. Back this keys/ directory up somewhere
private and durable (a password manager's file attachment, an encrypted
drive) outside of git entirely. If it's lost with no backup, no new license
can ever be issued again for the KIN Mail product already shipped with the
matching public key baked in (console/backend/kin_console/license_keys.py).

Usage:

  ./generate.sh issue --server-id <Email-Server-ID> --seats 32 --type perpetual \\
    --company "PT Example" --short-name example --purchase-date 2026-08-24

  ./generate.sh issue --server-id <id> --seats 10 --type trial --days 90

On Windows, use generate.bat with the same arguments instead of generate.sh.

Every issued license is appended to license-ledger.csv next to this script —
that ledger is KIN's own record (company name, seats, when, for whom), never
part of the signed license itself, and never verified or trusted by the
product. Moving this whole folder to a new device keeps that history intact.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent
DEFAULT_KEY_DIR = HERE / "keys"
LEDGER_CSV = HERE / "license-ledger.csv"

LEDGER_FIELDS = (
    "recorded_at",
    "company_name",
    "short_name",
    "purchase_date",
    "type",
    "server_id",
    "seats",
    "issued_at",
    "expires_at",
    "token",
)

# --- Vendored from console/backend/kin_console/license.py. Keep byte-for-byte
# identical to that file's canonical_payload/validate_payload/sign_payload —
# the product's verifier re-derives the exact same bytes and checks the
# signature against them; any drift here would sign licenses the product
# rejects. ---------------------------------------------------------------

LICENSE_PAYLOAD_KEYS = ("expires_at", "issued_at", "seats", "server_id", "type")
LICENSE_TYPES = ("trial", "perpetual")


def canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode(
        "utf-8"
    )


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
        raise ValueError("license type must be trial or perpetual")
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
    if kind == "trial" and expires is None:
        raise ValueError("trial licenses require expires_at")
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


def sign_payload(payload: dict[str, Any], private_key: Ed25519PrivateKey) -> str:
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


# --- End vendored section -------------------------------------------------


def init_keys(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    priv_path = out_dir / "ed25519-private.pem"
    pub_path = out_dir / "ed25519-public.pem"
    if priv_path.exists():
        raise SystemExit(f"refusing to overwrite existing key: {priv_path}")
    priv = Ed25519PrivateKey.generate()
    priv_path.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    priv_path.chmod(0o600)
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_path.write_bytes(
        pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    b64 = base64.urlsafe_b64encode(pub_bytes).decode("ascii").rstrip("=")
    print(f"Wrote {priv_path}")
    print(f"Wrote {pub_path}")
    print(f"KIN_LICENSE_PUBLIC_KEY_B64 = \"{b64}\"")
    print(
        "This must exactly match console/backend/kin_console/license_keys.py "
        "in the product repo. Only run this if you are deliberately rotating "
        "the signing key (a real, rare, product-wide event) — not for normal use."
    )


def _load_private(path: Path) -> Ed25519PrivateKey:
    if not path.is_file():
        raise SystemExit(
            f"private key not found: {path}\n"
            "This kit ships with keys/ already populated. If you moved this "
            "folder, make sure keys/ed25519-private.pem came with it."
        )
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("private key is not Ed25519")
    return key


def _ask(label: str, already: str) -> str:
    text = (already or "").strip()
    if text:
        return text
    if not sys.stdin.isatty():
        return ""
    try:
        return input(f"{label}: ").strip()
    except EOFError:
        return ""


def append_ledger(row: dict[str, str], path: Path | None = None) -> Path:
    dest = path or LEDGER_CSV
    dest.parent.mkdir(parents=True, exist_ok=True)
    new_file = not dest.is_file()
    with dest.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in LEDGER_FIELDS})
    return dest


def issue(args: argparse.Namespace) -> None:
    import os

    key_path = Path(
        args.key
        or os.environ.get("KIN_LICENSE_PRIVATE_KEY")
        or (DEFAULT_KEY_DIR / "ed25519-private.pem")
    )
    priv = _load_private(key_path)
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    expires: str | None = None
    if args.type == "trial":
        if args.days < 1:
            raise SystemExit("trial licenses need --days >= 1")
        expires = (issued + timedelta(days=args.days)).isoformat()
    payload = {
        "server_id": args.server_id.strip(),
        "seats": int(args.seats),
        "type": args.type,
        "issued_at": issued.isoformat(),
        "expires_at": expires,
    }
    token = sign_payload(payload, priv)
    company = _ask("Company name", args.company)
    short_name = _ask("Short company name", args.short_name)
    purchase_date = _ask("Purchase date (YYYY-MM-DD)", args.purchase_date)
    ledger = append_ledger(
        {
            "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "company_name": company,
            "short_name": short_name,
            "purchase_date": purchase_date,
            "type": args.type,
            "server_id": args.server_id.strip(),
            "seats": str(args.seats),
            "issued_at": issued.isoformat(),
            "expires_at": expires or "",
            "token": token,
        }
    )
    print(token)
    print(
        f"# type={args.type} seats={args.seats} server_id={args.server_id} "
        f"company={company or '-'} short={short_name or '-'} "
        f"purchase={purchase_date or '-'} ledger={ledger}",
        file=sys.stderr,
    )


def list_ledger(_args: argparse.Namespace) -> None:
    if not LEDGER_CSV.is_file():
        print("No licenses issued from this kit yet.")
        return
    with LEDGER_CSV.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("No licenses issued from this kit yet.")
        return
    for row in rows:
        print(
            f"{row.get('recorded_at', '')}  "
            f"{row.get('company_name', '') or '(no company recorded)'}  "
            f"type={row.get('type', '')}  seats={row.get('seats', '')}  "
            f"server_id={row.get('server_id', '')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="KIN Mail license generator (portable kit)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser(
        "init-keys",
        help="Rotate the signing key. Rare, deliberate, product-wide — not for normal use.",
    )
    init.add_argument("--out-dir", type=Path, default=DEFAULT_KEY_DIR)

    iss = sub.add_parser("issue", help="Sign a license string")
    iss.add_argument("--server-id", required=True)
    iss.add_argument("--seats", type=int, required=True)
    iss.add_argument("--type", choices=("trial", "perpetual"), required=True)
    iss.add_argument("--days", type=int, default=30, help="Trial duration in days")
    iss.add_argument("--key", default="", help="Override the default keys/ed25519-private.pem")
    iss.add_argument("--company", default="", help="Customer company name (ledger only)")
    iss.add_argument("--short-name", default="", help="Short company name (ledger only)")
    iss.add_argument("--purchase-date", default="", help="Purchase date YYYY-MM-DD (ledger only)")

    sub.add_parser("list", help="List every license this kit has issued")

    args = parser.parse_args()
    if args.cmd == "init-keys":
        init_keys(args.out_dir)
        return
    if args.cmd == "list":
        list_ledger(args)
        return
    issue(args)


if __name__ == "__main__":
    main()
