# KIN Mail Admin Console

Isolated from Zimbra/DRBD/Pacemaker. Bootstrap installs:

1. `kin-mail-console` — unprivileged FastAPI + React UI (HTTPS, default `:9443`)
2. `kin-mail-privhelperd` — root helper on **local Unix socket only**
   (`/run/kin-mail/privhelper.sock`, mode 660, group `kin-console`)

```bash
sudo ./console/bootstrap.sh
```

Bootstrap also copies sibling `install/` into **`/opt/kin-mail-deploy/install/`** as a real tree
(not a symlink into `/home`). `kin-mail-privhelperd` runs with `ProtectHome=true`, so a
`/opt/kin-mail-deploy → /home/...` symlink makes Deploy fail instantly with "file not found".

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
| Whitelist | `get_status`, hardening status/full, `apply_wizard_draft`, `run_full_install`, `cancel_firewall_deadman`, `get_audit_log`, `create_mailbox`, `clear_initial_console_password` |
| Concurrency | Second request while busy → `busy` (no silent queue); **`cancel_firewall_deadman`**, **`get_audit_log`**, **`get_deploy_log`**, **`clear_initial_console_password`**, and mailbox **`--status`** bypass busy |

First-boot local admin password is printed on SSH stdout **and** stored at
`/etc/kin-mail-console/initial-admin-password` (`root:root` `0600`) until the first successful
local login (or a root password rotate via `users.set_local_password`), then deleted.
(Path is under `/etc/kin-mail-console` rather than `/root` so `kin-mail-privhelperd`
can unlink it despite `ProtectHome=true`.) After that, recover access with a real reset
(not by re-reading the file).

Self-service: `POST /api/mailbox` (Customer Admin allowed) wraps `install/08-create-mailbox.sh` so the shared `kin_quota_gate_allow_new_mailbox` runs before any `zmprov ca`. Seat limit (`CONTRACTED_SEATS`) remains ops-only via draft apply.
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
| 9.9 AD/LDAP console login | Done |
| 9.10 Self-service mailbox create | This tree |
