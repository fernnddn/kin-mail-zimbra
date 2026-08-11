# KIN Mail Admin Console

Isolated from Zimbra/DRBD/Pacemaker. Bootstrap installs:

1. `kin-mail-console` — unprivileged FastAPI + React UI (HTTPS, default `:9443`)
2. `kin-mail-privhelperd` — root helper on **local Unix socket only**
   (`/run/kin-mail/privhelper.sock`, mode 660, group `kin-console`)

```bash
sudo ./console/bootstrap.sh
```

## Local users + RBAC

| Role | ID | Notes |
|---|---|---|
| KIN Super Admin | `kin_super_admin` | Full ops + user CRUD + audit log |
| KIN Support-Ops | `kin_support_ops` | Privileged deploy/firewall ops; no audit/user admin |
| Customer Admin | `customer_admin` | Wizard draft + read-only status; **cannot** apply draft / full install / cancel dead-man |

Store: `/var/lib/kin-mail-console/users.json` (mode 600). Each account is either:

| `auth_type` | Credentials |
|---|---|
| `local` | bcrypt hash in `users.json` |
| `ad` | no local password; LDAP bind via the same AD settings as `06-hybrid-auth.sh` (`/etc/kin-mail/config` → synced to `/etc/kin-mail-console/ad.env`) |

AD login is fail-closed (unreachable / disabled AD never grants access). Roles stay manually assigned — no AD group mapping in this slice.

Sensitive commands are denied in **both** the FastAPI stream endpoint and privhelperd (role resolved from `users.json` by username — client cannot claim a role).

## Privileged helper (Option B)

| Item | Value |
|---|---|
| Socket | `/run/kin-mail/privhelper.sock` (no TCP/UDP) |
| Audit log | `/var/log/kin-mail/privhelper.log` (root-owned, not writable by `kin-console`) |
| Whitelist | `get_status`, hardening status/full, `apply_wizard_draft`, `run_full_install`, `cancel_firewall_deadman`, `get_audit_log` |
| Concurrency | Second request while busy → `busy` (no silent queue); **`cancel_firewall_deadman`** and **`get_audit_log`** bypass busy |

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
| 9.5 Full install + dead-man cancel | Done |
| 9.8 Local multi-user + RBAC | Done |
| 9.9 AD/LDAP console login | This tree |
