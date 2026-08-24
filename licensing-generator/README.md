# KIN Mail License Generator

This folder issues signed licenses for KIN Mail installs. It lives inside
the KIN Mail repo checkout for convenience, but `keys/` and
`license-ledger.csv` are gitignored and must never be committed.

## Keep the private key safe — read this part first

- `keys/ed25519-private.pem` is the one secret that makes every KIN Mail
  license real. If it ever reaches a public repo or leaks anywhere, anyone
  could forge a license for any customer, permanently, with no way to revoke
  it short of rotating the key and shipping a new public key to every
  existing install (see "Rotating the signing key" below — a big deal, not a
  quick fix).
- `.gitignore` in this repo already blocks `*.pem` and `licensing-generator/keys/`
  specifically, so a normal `git add`/`git commit` cannot pick it up. Treat
  that as a backstop, not the actual protection — **back this `keys/`
  directory up somewhere private and durable outside of git entirely**: a
  password manager's file/secure-note attachment (1Password, Bitwarden,
  etc.), an encrypted external drive, or similar. If it's lost with no
  backup, you can never issue a new license again for any KIN Mail product
  already shipped with the matching public key
  (`console/backend/kin_console/license_keys.py`).
- This whole folder is portable: copy it to another Mac, Linux, or Windows
  machine and it works the same way there, with the same license history
  (`license-ledger.csv` travels with it). If you keep separate copies on
  multiple machines and issue licenses from more than one, reconcile the
  ledgers by hand later — they don't sync with each other automatically.

## Why this can't be cracked by reverse-engineering the product

KIN Mail only ships the **public** key
(`KIN_LICENSE_PUBLIC_KEY_B64` in `console/backend/kin_console/license_keys.py`)
— it can verify a signature was made by this kit's private key, but it
cannot be used to forge one, and it cannot be used to derive the private key
back out (that's the whole point of Ed25519 / public-key cryptography).
Someone fully disassembling the KIN Mail product, source and binaries both,
still cannot produce a license the product will accept, because they would
still be missing `keys/ed25519-private.pem`, which never ships in the
product. That property depends entirely on `keys/ed25519-private.pem`
staying private — which is exactly why the backup step above matters as
much as the gitignore does.

## Usage

**macOS or Linux:**

```bash
./generate.sh issue \
  --server-id <Email-Server-ID from the customer's console Settings page> \
  --seats 32 \
  --type perpetual \
  --company "Customer Company Name" \
  --short-name shortname \
  --purchase-date 2026-08-24
```

For a trial instead of perpetual: `--type trial --days 90` (pick your own
day count) instead of `--type perpetual`.

**Windows:** same arguments, run `generate.bat` instead of `generate.sh`.

The first run on a machine takes a few extra seconds — it creates a small,
private Python environment inside this folder (`.venv/`, also gitignored) so
nothing gets installed system-wide. Every run after that is instant.

The command prints the signed license string on its own — copy that whole
string and paste it into the customer's console, Settings page, "Paste a
signed license string" box, then Apply.

**List everything you've issued from this kit:**

```bash
./generate.sh list
```

## Where the Email Server ID comes from

Each KIN Mail install generates its own Email Server ID the first time it's
deployed, and shows it on its own console's Settings page (Super Admin only).
It's not something you choose — copy it exactly from that customer's console.
A license only works on the one install whose ID matches.

## Files in this folder

- `generate_license.py` — the actual generator. Self-contained, no
  dependency on the rest of the product's Python package (it carries its own
  copy of the exact signing logic `console/backend/kin_console/license.py`
  verifies against, so this folder alone is enough to run it anywhere).
- `generate.sh` / `generate.bat` — wrappers that set up a local Python
  environment on first run, then just run the script above.
- `keys/ed25519-private.pem` — the signing key. **This is the secret.**
- `keys/ed25519-public.pem` — the matching public key, for reference only
  (the product already has this baked in; this file isn't needed for
  anything day to day).
- `license-ledger.csv` — every license this kit has issued: company,
  seats, type, when, and the license string itself. This is KIN's own
  record-keeping, never read or trusted by the product itself.
- `.venv/` — created automatically on first run, safe to delete any time
  (it will just be recreated on the next run).

## Rotating the signing key (rare — do not do this casually)

Only if the private key is ever actually compromised or genuinely lost with
no backup. This invalidates every license ever issued and requires shipping
a new public key in the product to every existing customer install:

```bash
./generate.sh init-keys --out-dir ./new-keys
```

Then replace `KIN_LICENSE_PUBLIC_KEY_B64` in
`console/backend/kin_console/license_keys.py` in the product repo with the
new public key it prints, ship that, and re-issue every active customer's
license from the new key.
