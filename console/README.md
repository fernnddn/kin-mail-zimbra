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
| Whitelist (slice 9.4) | `get_status`, `run_script:09-hardening.sh --status`, `apply_wizard_draft`, `run_hardening` |
| Concurrency | Second request while busy → `busy` (no silent queue) |

SSE: `/api/wizard/deploy/stream?action=apply_draft|run_hardening|hardening_status|get_status`
(`action` is a fixed alias map — never a free-form command string).

`apply_wizard_draft` merges the console draft into `/etc/kin-mail/config` after a
timestamped `config.bak.<epoch>` backup; it does **not** restart services.

## Slices

| Slice | Status |
|---|---|
| 9.1 Skeleton | Done |
| 9.2 Wizard UI + draft | Done |
| 9.3 Privhelper plumbing | This tree |
| Later — firewall apply / full install | Not started |
