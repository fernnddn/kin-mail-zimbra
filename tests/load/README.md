# Conservative KIN Mail load probe (Timeline 7.3)

Python 3 stdlib only (`imaplib` / `smtplib` / `urllib`). No extra packages.

This is a **handful of concurrent sessions** against a live VIP - IMAPS login,
SMTP submission AUTH (no message send), HTTPS GET `/`, and Zimbra SOAP
`AuthRequest`. It is not a soak or a stress-to-failure test.

## Stability bar (script defaults)

- Failed operations ≤ **1%**
- No HTTPS GET other than 200 (hard abort)
- HTTPS GET p95 ≤ **2000 ms**
- IMAP / SMTP AUTH / SOAP p95 ≤ **3000 ms**
- Cluster watcher (run separately): fail-count stays 0, VIP does not move

## Usage

```bash
export KIN_LOADTEST_PASSWORD='…'   # test mailbox, not admin
python3 tests/load/kin-mail-load-test.py \
  --host <vip-or-promoted> \
  --sni <mail-fqdn> \
  --user test1@example.com \
  --insecure \
  --concurrency 4 \
  --duration 45 \
  --abort-file /tmp/kin-load-abort
```

Password is read from the environment. Do not put it on the command line.
Concurrency is capped at 20 in the script.
