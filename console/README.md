# KIN Mail Admin Console

Isolated from Zimbra/DRBD/Pacemaker. Bootstrap only installs this service.

```bash
sudo ./console/bootstrap.sh
```

Default listen: `https://<host>:9443/` (override `CONSOLE_PORT`).

## Slices

| Slice | Status |
|---|---|
| 9.1 Skeleton — TLS, unprivileged user, login | Done |
| 9.2 Wizard UI — EULA → login → draft steps → deploy stub | This tree |
| Later — execution engine / privileged apply | Not started |

Draft wizard state lives in `/var/lib/kin-mail-console/wizard-draft.json` and never writes `/etc/kin-mail/config`.
