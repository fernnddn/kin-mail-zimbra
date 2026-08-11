# Ansible — KIN Mail Phase 2 port (incremental)

This tree ports proven Phase 2 lab work into Ansible **one small slice at a time**.

## Current slice (3.1)

| Playbook | Role | Source |
|---|---|---|
| `playbooks/mon-qnetd.yml` | `corosync_qnetd` | `docs/progress/2.1-…` §2 (qnetd only) |

**In scope:** install `corosync-qnetd`, ensure NSS/TLS DB, enable+start, verify `:5403`.  
**Out of scope:** OS prep, iSCSI SBD LUN, `corosync-qdevice` on Host A/B, DRBD, Pacemaker.

## Layout

```
ansible/
  ansible.cfg
  inventory/lab.example.yml   # committed template
  inventory/lab.yml           # local secrets — gitignored
  playbooks/mon-qnetd.yml
  roles/corosync_qnetd/
```

Task names are Indonesian, user-facing progress labels (e.g. `Menginstal paket qnetd`).

## Inventory

```bash
cd ansible
cp inventory/lab.example.yml inventory/lab.yml
# edit ansible_password / ansible_become_password or use SSH keys
```

## Dry-run only against the live lab

The Monitoring / mail hosts already run production qnetd + HA. **Do not** apply this
playbook for real there until a blank test VM exists.

```bash
cd ansible
ansible-playbook playbooks/mon-qnetd.yml --check
```

`--check` validates syntax/module planning and (for verify tasks) read-only probes.
It is **not** an end-to-end proof that a blank host ends in a correct qnetd state.
See `docs/progress/3.1-ansible-qdevice-port.md`.
