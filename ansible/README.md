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

**DRBD scope:** Ubuntu `drbd-utils` + in-tree module (no DKMS); `kin-zimbra` with
**external** meta on `/dev/loop21`; meta-loop unit.  
**Out of scope here:** Pacemaker promotable DRBD / Zimbra group (later slice).

## Layout

```
ansible/
  playbooks/
    mail-drbd.yml
    …
  roles/
    drbd_install/
    drbd_resource/
    …
```

## Dry-run only against the live lab

```bash
cd ansible
ansible-playbook playbooks/mail-drbd.yml --check
```

`kin-zimbra` holds live production data. Mutating DRBD commands stay dry under
`--check` (never `check_mode: false` on create-md/up/primary). See
`docs/progress/3.5-ansible-drbd-port.md`.
