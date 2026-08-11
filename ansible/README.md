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
| `playbooks/mail-add-host.yml` | `cluster_node_base`, `cluster_survivor_replace`, `drbd_live_join` (+ reuse) | new | **syntax-only (3.7)** |
| `playbooks/mail-zpush.yml` | `zpush_install` | 4.1 / 4.2 | dry-run / Host A only (B disabled in inventory) |

**Add-host scope:** replace a permanently lost peer with a **new** hostname/IP;
survivor membership + DRBD peer rewrite; new-node live full sync to Primary.  
**Never tested against any host** — see local progress notes for slice 3.7
(not committed; under `docs/progress/` on operator machines).

## Layout

```
ansible/
  playbooks/
    mail-add-host.yml
    …
  inventory/
    add-host.example.yml
  roles/
    cluster_node_base/
    cluster_survivor_replace/
    drbd_live_join/
    …
```

## Syntax-check only (no target VM yet)

```bash
cd ansible
ansible-playbook -i inventory/add-host.example.yml \
  playbooks/mail-add-host.yml --syntax-check
```

Do **not** run this against the live A/B pair as an add-host rehearsal.
Mutating survivor/`pcs`/`drbdadm` tasks stay gated (`not ansible_check_mode`,
never `check_mode: false` on those paths).
