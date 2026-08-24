# KIN Mail License Generator

This folder issues signed licenses for KIN Mail installs. It lives inside
the KIN Mail repo checkout for convenience, but `keys/` and
`license-ledger.csv` are gitignored and must never be committed.

## Keep the private key safe

- `keys/ed25519-private.pem` must never leave this machine or reach a
  public repo. Back the `keys/` directory up somewhere private and durable
  outside of git: a password manager's file/secure-note attachment, an
  encrypted external drive, or similar.
- `.gitignore` already blocks `*.pem` and `licensing-generator/keys/`, but
  treat that as a backstop, not the actual protection.
- This whole folder is portable: copy it to another Mac, Linux, or Windows
  machine and it works the same way there, with the same license history
  (`license-ledger.csv` travels with it). If you keep separate copies on
  multiple machines and issue licenses from more than one, reconcile the
  ledgers by hand later - they don't sync with each other automatically.

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

The first run on a machine takes a few extra seconds - it creates a small,
private Python environment inside this folder (`.venv/`, also gitignored) so
nothing gets installed system-wide. Every run after that is instant.

The command prints the signed license string on its own - copy that whole
string and paste it into the customer's console, Settings page, "Paste a
signed license string" box, then Apply.

**List everything you've issued from this kit:**

```bash
./generate.sh list
```

## Where the Email Server ID comes from

Each KIN Mail install generates its own Email Server ID the first time it's
deployed, and shows it on its own console's Settings page (Super Admin only).
It's not something you choose - copy it exactly from that customer's console.
A license only works on the one install whose ID matches.

## Files in this folder

- `generate_license.py` - the actual generator, self-contained.
- `generate.sh` / `generate.bat` - wrappers that set up a local Python
  environment on first run, then just run the script above.
- `keys/ed25519-private.pem` - the signing key. **This is the secret.**
- `keys/ed25519-public.pem` - the matching public key, for reference only.
- `license-ledger.csv` - every license this kit has issued: company,
  seats, type, when, and the license string itself. KIN's own
  record-keeping, never read or trusted by the product itself.
- `.venv/` - created automatically on first run, safe to delete any time.

## Rotating the signing key

Only if the private key is ever actually compromised or genuinely lost with
no backup. Contact the team before doing this - it affects every existing
customer install.
