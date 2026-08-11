# KIN Mail Admin Console

Isolated from Zimbra/DRBD/Pacemaker. Bootstrap installs:

1. `kin-mail-console` — unprivileged FastAPI + React UI (HTTPS, default `:9443`)
2. `kin-mail-privhelperd` — root helper on **local Unix socket only**
   (`/run/kin-mail/privhelper.sock`, mode 660, group `kin-console`)

```bash
sudo ./console/bootstrap.sh
```

## Privileged helper (Option B)

| Item | Value |
|---|---|
| Socket | `/run/kin-mail/privhelper.sock` (no TCP/UDP) |
| Audit log | `/var/log/kin-mail/privhelper.log` (root-owned, not writable by `kin-console`) |
| Whitelist | `get_status`, hardening status/full, `apply_wizard_draft`, `run_full_install`, `cancel_firewall_deadman` |
| Concurrency | Second request while busy → `busy` (no silent queue); **`cancel_firewall_deadman` bypasses busy** |

SSE: `/api/wizard/deploy/stream?action=…`  
`run_full_install` sets `KIN_CONSOLE_CONFIRMED=1` and runs `kin-mail.sh --full-install`.  
Stage 10 still arms the ufw dead-man; pipeline **waits** for `cancel_firewall_deadman` (never auto-cancel).

## Slices

| Slice | Status |
|---|---|
| 9.1 Skeleton | Done |
| 9.2 Wizard UI + draft | Done |
| 9.3 Privhelper plumbing | Done |
| 9.4 Config apply + hardening | Done |
| 9.5 Full install + dead-man cancel | This tree |
