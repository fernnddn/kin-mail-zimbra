# Ansible — KIN Mail Phase 2 port (incremental)

This tree ports proven Phase 2 lab work into Ansible **one small slice at a time**.

## Slices

| Playbook | Roles | Source | Status |
|---|---|---|---|
| `playbooks/mon-qnetd.yml` | `corosync_qnetd` | 2.1 §2 | dry-run (3.1) |
| `playbooks/mail-qdevice.yml` | `corosync_qdevice` | 2.2 §1 | dry-run (3.2) |
| `playbooks/mon-iscsi-target.yml` | `iscsi_target` | 2.1 §3 | dry-run (3.4) |
| `playbooks/mail-fencing.yml` | `iscsi_initiator`, `softdog`, `sbd_stonith` | 2.2 §2–4, 2.3, 2.9 | dry-run (3.4) |

**Fencing scope:** LIO target on Monitoring; initiator login; `kin-softdog.service`;
`/etc/default/sbd` with **`SBD_DELAY_START=yes`** + `TimeoutStartSec=180`; `fence_sbd` STONITH.
**Out of scope here:** DRBD, Zimbra Pacemaker resources, real fence/partition tests.

## Layout

```
ansible/
  playbooks/
    mon-qnetd.yml
    mail-qdevice.yml
    mon-iscsi-target.yml
    mail-fencing.yml
  roles/
    corosync_qnetd/
    corosync_qdevice/
    iscsi_target/
    iscsi_initiator/
    softdog/
    sbd_stonith/
```

## Dry-run only against the live lab

```bash
cd ansible
ansible-playbook playbooks/mon-iscsi-target.yml --check
ansible-playbook playbooks/mail-fencing.yml --check
```

`--check` is not greenfield proof and cannot prove softdog reboot or SBD_DELAY_START
race mitigation. See `docs/progress/3.4-ansible-sbd-fencing-port.md`.
