# Ansible — KIN Mail Phase 2 port (incremental)

This tree ports proven Phase 2 lab work into Ansible **one small slice at a time**.

## Slices

| Playbook | Roles | Source | Status |
|---|---|---|---|
| `playbooks/mon-qnetd.yml` | `corosync_qnetd` | 2.1 §2 | dry-run (3.1) |
| `playbooks/mail-qdevice.yml` | `corosync_qdevice` | 2.2 §1 | dry-run (3.2) |
| `playbooks/mon-iscsi-target.yml` | `iscsi_target` | 2.1 §3 | dry-run (3.4) |
| `playbooks/mail-fencing.yml` | `iscsi_initiator`, `softdog`, `sbd_stonith` | 2.2–2.3, 2.9 | dry-run (3.4) |
| `playbooks/mail-drbd.yml` | `drbd_install`, `drbd_resource` | 2.4, 2.5 | dry-run (3.5) |
| `playbooks/mail-pacemaker.yml` | `pacemaker_agents`, `pacemaker_mail_stack` | 2.7 | dry-run (3.6) |

**Pacemaker scope:** `ocf:kin:zimbra`, promotable `kin-drbd-clone`, group
`kin-mail-svc` (fs → zimbra → vip), constraints, LDAP localhost HA fix.  
**Out of scope here:** controlled `pcs ban/clear` moves, fence injection, DNS/NAT → VIP.

## Layout

```
ansible/
  playbooks/
    mail-pacemaker.yml
    …
  roles/
    pacemaker_agents/
    pacemaker_mail_stack/
    …
```

## Dry-run only against the live lab

```bash
cd ansible
ansible-playbook playbooks/mail-pacemaker.yml --check
```

Mutating `pcs` commands stay pure dry-run under `--check` (never
`check_mode: false` on create/constraint). See
`docs/progress/3.6-ansible-pacemaker-port.md`.
