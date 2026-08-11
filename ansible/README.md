# Ansible — KIN Mail Phase 2 port (incremental)

This tree ports proven Phase 2 lab work into Ansible **one small slice at a time**.

## Slices

| Playbook | Role | Source | Status |
|---|---|---|---|
| `playbooks/mon-qnetd.yml` | `corosync_qnetd` | task 2.1 §2 | dry-run checked (3.1) |
| `playbooks/mail-qdevice.yml` | `corosync_qdevice` | task 2.2 §1 | dry-run checked (3.2) |

**mail-qdevice scope:** install `corosync-qdevice`, TLS (certutil request/sign/import),
quorum.device → Monitoring, verify Connected + Expected votes 3.  
**Out of scope:** iSCSI SBD, softdog, Pacemaker STONITH, DRBD.

## Layout

```
ansible/
  ansible.cfg
  inventory/lab.example.yml   # committed template
  inventory/lab.yml           # local secrets — gitignored
  playbooks/mon-qnetd.yml
  playbooks/mail-qdevice.yml
  roles/corosync_qnetd/
  roles/corosync_qdevice/
```

Task names are Indonesian, user-facing progress labels.

## Inventory

```bash
cd ansible
cp inventory/lab.example.yml inventory/lab.yml
# edit ansible_password / ansible_become_password or use SSH keys
```

## Dry-run only against the live lab

Hosts already run production qnetd/qdevice + HA. **Do not** apply for real until a blank
test environment exists.

```bash
cd ansible
ansible-playbook playbooks/mon-qnetd.yml --check
ansible-playbook playbooks/mail-qdevice.yml --check
```

`--check` is not an end-to-end greenfield proof. See `docs/progress/3.1-…` and `3.2-…`.
