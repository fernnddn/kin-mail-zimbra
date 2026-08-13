# Ansible — KIN Mail Phase 2 port (incremental)

This tree ports proven Phase 2 lab work into Ansible **one small slice at a time**.

## Slices

| Playbook | Roles | Source | Status |
|---|---|---|---|
| `playbooks/mon-qnetd.yml` | `corosync_qnetd` | 2.1 §2 | dry-run (3.1) |
| `playbooks/mail-cluster-setup.yml` | `cluster_node_base`, `cluster_setup` | greenfield `pcs cluster setup` | idempotent; skip if cluster already running |
| `playbooks/mail-qdevice.yml` | `corosync_qdevice` | 2.2 §1 | dry-run (3.2) |
| `playbooks/mon-iscsi-target.yml` | `iscsi_target` | 2.1 §3 | dry-run (3.4) |
| `playbooks/mail-fencing.yml` | `iscsi_initiator`, `softdog`, `sbd_stonith` | 2.2–2.3, 2.9 | dry-run (3.4) |
| `playbooks/mail-drbd.yml` | `drbd_install`, `drbd_resource` | 2.4, 2.5 | dry-run (3.5) |
| `playbooks/mail-pacemaker.yml` | `pacemaker_agents`, `pacemaker_mail_stack` | 2.7 | dry-run (3.6) |
| `playbooks/mail-add-host.yml` | `cluster_node_base`, `cluster_remove_host` (via `cluster_survivor_replace`), `drbd_live_join` (+ reuse) | new | **syntax-only (3.7)** |
| `playbooks/mail-remove-host.yml` | `cluster_remove_host` | new | **syntax-only (unreachable-peer cleanup)** |
| `playbooks/mail-zpush.yml` | `zpush_install` | 4.1 / 4.2 | dry-run / Host A only (B disabled in inventory) |
| `playbooks/mail-os-hardening.yml` | `os_hardening` | 09 `--os-only` | fail2ban + unattended-upgrades on **both** mail nodes |

**Add-host scope:** replace a permanently lost peer with a **new** hostname/IP;
survivor membership + DRBD peer rewrite; new-node live full sync to Primary.  
**Remove-host scope:** clear retired/unreachable peer from survivor only (force
membership remove, DRBD peer drop, location constraints, precise qnetd NSS
nickname delete). Must not disturb kin-zimbra/VIP/DRBD Primary.  
**Never tested against any host as a live apply** — see progress notes.

**OS hardening scope:** `os_hardening` is fail2ban + unattended-upgrades only
(per-machine). It does not write COS/cleartext/TLS/SMTP LDAP attrs.

**Active-passive proxy:** `pacemaker_mail_stack` remaps the shared Zimbra
service hostname to `127.0.0.1` in `/etc/hosts` (and dnsmasq) so nginx /
memcached / zmlookup talk to the co-located instance on whichever node is
Promoted. Corosync `ring0_addr` is forced to inventory `ansible_host` IPs
first — the hostname cannot stay as the cluster transport address. Do not
set LDAP `zimbraServiceHostname` to `127.0.0.1` (that is also nginx
`server_name`). After a first remap, regenerate proxy on the Promoted node
(`zmproxyctl restart`) so baked-in lookup/memcached IPs update.

**DRBD location:** the clone is **not** pinned to Host A. A `prefers A=50`
constraint auto-failed-back after a real fence (ha-build-14). The role
removes that pin and sets Promoted stickiness so the survivor stays active
until an operator moves it (`pacemaker_mail_stack_prefer_enabled: true`
restores the old pin if ever needed).

## Layout

```
ansible/
  playbooks/
    mail-add-host.yml
    mail-remove-host.yml
    …
  inventory/
    add-host.example.yml
    remove-host.example.yml
  roles/
    cluster_node_base/
    cluster_setup/
    os_hardening/
    cluster_remove_host/
    cluster_survivor_replace/
    drbd_live_join/
    …
```

## Syntax-check only (no target VM yet)

```bash
cd ansible
ansible-playbook -i inventory/add-host.example.yml \
  playbooks/mail-add-host.yml --syntax-check
ansible-playbook -i inventory/remove-host.example.yml \
  playbooks/mail-remove-host.yml --syntax-check
```

Do **not** run remove-host / add-host against the live A/B pair as a rehearsal.
Mutating survivor/`pcs`/`drbdadm` tasks stay gated (`not ansible_check_mode`,
never `check_mode: false` on those paths).
