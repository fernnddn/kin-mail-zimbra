#!/usr/bin/env python3
"""KIN Mail license generator. Run only on KIN's own machine.

Never copy this directory onto a customer host. Never commit the private key.

Create the signing key once (outside this repo):

  python3 generate_license.py init-keys --out-dir ~/.kin-mail-license

Issue a license:

  python3 generate_license.py issue \\
    --server-id <Email-Server-ID from the console Settings page> \\
    --seats 32 \\
    --type perpetual

  python3 generate_license.py issue \\
    --server-id <id> --seats 10 --type trial --days 90

The private key path defaults to ~/.kin-mail-license/ed25519-private.pem
or KIN_LICENSE_PRIVATE_KEY. Keep that file offline and backed up.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Allow `python3 generate_license.py` from a checkout without installing.
_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "console" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from kin_console.license import sign_payload  # noqa: E402
from kin_console.license_keys import KIN_LICENSE_PUBLIC_KEY_B64  # noqa: E402


def _default_key_dir() -> Path:
    return Path.home() / ".kin-mail-license"


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
    pub_path.write_bytes(
        pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"Wrote {priv_path}")
    print(f"Wrote {pub_path}")
    print("Keep the private key off customer hosts and out of git.")


def _load_private(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("private key is not Ed25519")
    return key


def issue(args: argparse.Namespace) -> None:
    import os

    key_path = Path(
        args.key
        or os.environ.get("KIN_LICENSE_PRIVATE_KEY")
        or (_default_key_dir() / "ed25519-private.pem")
    )
    if not key_path.is_file():
        raise SystemExit(f"private key not found: {key_path}")
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
    print(token)
    print(
        f"# type={args.type} seats={args.seats} server_id={args.server_id} "
        f"product_pub={KIN_LICENSE_PUBLIC_KEY_B64}",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="KIN Mail license generator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init-keys", help="Create a new Ed25519 keypair")
    init.add_argument("--out-dir", type=Path, default=_default_key_dir())
    iss = sub.add_parser("issue", help="Sign a license string")
    iss.add_argument("--server-id", required=True)
    iss.add_argument("--seats", type=int, required=True)
    iss.add_argument("--type", choices=("trial", "perpetual"), required=True)
    iss.add_argument("--days", type=int, default=30, help="Trial duration in days")
    iss.add_argument("--key", default="")
    args = parser.parse_args()
    if args.cmd == "init-keys":
        init_keys(args.out_dir)
        return
    issue(args)


if __name__ == "__main__":
    main()
