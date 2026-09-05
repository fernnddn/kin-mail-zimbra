# KIN Mail - HA runbook (Pacemaker / DRBD / SBD)

This is a template: hostnames/IPs/the SBD LUN id below are placeholders like
`<HOST_A_IP>`, not real values - this file is public. For the actual values of
a specific deployment, see that deployment's local (gitignored) values file
(e.g. `docs/HA-RUNBOOK.values.md`).

Operational reference for the **active-passive** KIN Mail pair. Commands and
behaviors below are taken from live lab work (Phase 2, HA rebuild 2026-08-12/13,
ha-build-14/15, maintenance mode, Observability resize, backup/restore, KIN Sight,
fail2ban OCF hooks) - not from unverified theory.

Cluster name: `kin-mail`. Greenfield formation (empty pair, not this live lab) is
`ansible-playbook -i inventory/lab.yml playbooks/mail-cluster-setup.yml` - idempotent;
skips if the cluster is already running. Never `pcs cluster destroy` against this pair.

| Role | Host | IP | Notes |
|---|---|---|---|
| Mail A | `mail.<MAIL_DOMAIN>` | `<HOST_A_IP>` | Console `:9443`. Currently **Unpromoted**. |
| Mail B | `mail2.<MAIL_DOMAIN>` | `<HOST_B_IP>` | Console `:9443` after Build HA. Currently **Promoted** (serves mail + VIP). |
| Observability | `mon.<MAIL_DOMAIN>` | `<OBSERVABILITY_IP>` | qnetd `:5403`, iSCSI SBD LUN `:3260`, Zabbix `:8080` |
| Backup | `backup.<MAIL_DOMAIN>` | `<BACKUP_IP>` | Backup repo + Grafana `:3000`. Scratch restore target. |
| Cluster VIP | Pacemaker `kin-vip` | **`<CLUSTER_VIP_IP>/24`** on `<NET_IFACE>` | FortiGate NAT targets this, **not** Host A `<HOST_A_IP>`. |

There is **no** Pacemaker "preferred production node." ha-build-15 removed the
prefer-A=50 location pin (it caused automatic failback after the 14 fence test).
`resource-stickiness=1000` / `promoted-resource-stickiness=1000` keep whoever is
Promoted until that node is down or an operator moves it. Resting state as of
2026-08-13: **B Promoted, A Unpromoted**, no location constraints.

> **Audience:** anyone operating a KIN Mail HA pair.  
> **Not covered here:** first Zimbra install (`install/kin-mail.sh`) - see `README.md`.  
> **Single-node → HA:** if `/opt/zimbra` is still on the OS volume, Build HA pair
> refuses until you migrate onto the data partition - **§13**.  
> **Console:** `https://<HOST_A_IP>:9443` and `https://<HOST_B_IP>:9443` after
> Build HA (or Add Second Server). Console is **not** on the VIP. Mail stays
> VIP-only. If Host A is down, open the console on Host B's management IP.

### Expectation: measured downtime, not zero-downtime

This design is **active-passive**, not active-active. Only one node runs Zimbra and
holds the VIP. Every **failover** and every **controlled move** causes a real service
gap.

**Measured (hard-kill of the then-Promoted node, ha-build-14 Stage 2):** kill → VIP
HTTP 200 again ≈ **3 min 19 s** (~80 s SBD msgwait fence, then DRBD promote/mount,
then full `zmcontrol start`, then VIP). Graceful `pcs node standby` of the Promoted
node is on the same order **minus** the fence wait (~2 min).

**There is no automatic failback downtime** when the old node rejoins. Stickiness
keeps the survivor. That is deliberate (ha-build-15). The old prefer-A pin would
have moved the stack a second time as soon as A was UpToDate.

**Why the gap exists (data safety):**

1. DRBD must **promote** on the survivor before `/opt/zimbra` is mounted (never dual-Primary).
2. `ocf:kin:zimbra` runs a **full** `zmcontrol start` (and starts local slapd first when
   `ldap_url` is `127.0.0.1`) - not a hot JVM hand-off.
3. VIP starts **after** Zimbra in the group, so clients see the old VIP disappear
   before the new side is ready.

**What this HA buys you:** automatic (or controlled) recovery with **bounded,
repeatable downtime**, fencing against split-brain, and a single consistent store -
**not** continuous availability. Do not plan SLAs as if failover were transparent.

---

## 1. Architecture (short)

```
                    ┌──────────────────────────┐
                    │  mon (Observability)     │
                    │  qnetd :5403  (vote)     │
                    │  iSCSI → SBD LUN :3260   │
                    │  Zabbix :8080            │
                    └────────────┬─────────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         │                       │                       │
┌────────▼────────┐    Corosync/Pacemaker     ┌────────▼────────┐
│ Host A          │◄─────────────────────────►│ Host B          │
│ mail            │                           │ mail2           │
│ console :9443   │      DRBD kin-zimbra      │ console :9443   │
│                 │◄═════════════════════════►│ (Promoted today)│
│ stickiness 1000 │   /dev/drbd0 ↔ /opt/zimbra (when Master)    │
└─────────────────┘     data /dev/sdb1  meta /dev/sdb2          └─────────────────┘
                                 │
                    when Promoted: group kin-mail-svc
                                 ▼
              kin-fs → kin-zimbra → kin-vip (Cluster VIP)
```

Backup VM `<BACKUP_IP>` is **not** in the cluster. It pulls backups over SSH from whichever
mail node currently has `/opt/zimbra` mounted, and hosts Grafana.

**Before the first HA pair** (single-node Zimbra still on the OS volume): migrate
`/opt/zimbra` onto the data partition (`/dev/sdb1`) - **§13**. Build HA pair
refuses to wrap an empty DRBD disk while mail still lives on root.

| Layer | What | Notes (proven) |
|---|---|---|
| Quorum | Corosync + **qdevice/qnetd** on `<OBSERVABILITY_IP>` | Expected votes **3**, quorum **2**. `no-quorum-policy=stop`. |
| Fencing | **SBD** + softdog + `fence_sbd` | No ESXi/hypervisor fence. Live device `<SBD_LUN_WWN>`. |
| Replication | DRBD **`kin-zimbra`** → `/dev/drbd0` | Protocol C; **`meta-disk /dev/sdb2`** (no `kin-drbd-meta-loop`). |
| Pacemaker DRBD | Promotable clone **`kin-drbd-clone`** | `promoted-max=1`, `notify=true`, stickiness **1000**, **no** node-location prefer. |
| App stack | Group **`kin-mail-svc`**: `kin-fs` → `kin-zimbra` → `kin-vip` | Colocation + order with **Promoted** DRBD. |
| Zimbra RA | Custom **`ocf:kin:zimbra` v1.2** | `zmcontrol` wrapper. Start **600s**, stop **300s**, monitor 30s/60s. Starts local LDAP before `zmcontrol` when needed. Refreshes fail2ban jails on start/stop (section 10). |

**Boot ownership:**

- `drbd.service` (systemd) must stay **disabled** - Pacemaker owns DRBD.
- `kin-softdog.service` must stay **enabled** - Ubuntu blacklists softdog; SBD needs it.
- There is **no** `kin-drbd-meta-loop.service` on this rebuild (meta is a real partition).

Ansible: `pacemaker_mail_stack_prefer_enabled: false` (default). Do not re-enable the
prefer pin without an explicit operator decision - it will restore automatic failback.

---

## 2. Daily health checks

Run on **either** mail node (root / sudo). Healthy = one Promoted, one Unpromoted,
VIP on the Promoted node, fail-count 0. Today that is **B**; tomorrow it is whoever
stickiness last left Promoted.

### Cluster membership and resources

```bash
pcs status
pcs constraint location          # expect: empty (no prefer / no leftover ban)
```

**Normal (2026-08-13 resting state):**

- `Online: [ mail.<MAIL_DOMAIN> mail2.<MAIL_DOMAIN> ]`
- `kin-drbd-clone`: `Promoted: [ mail2.<MAIL_DOMAIN> ]`, `Unpromoted: [ mail.<MAIL_DOMAIN> ]`
- `kin-mail-svc`: `kin-fs`, `kin-zimbra`, `kin-vip` all **Started** on `mail2.<MAIL_DOMAIN>`
- `sbd` (stonith) Started on one of the nodes
- Clone meta includes `resource-stickiness=1000` and `promoted-resource-stickiness=1000`
- No location constraints, no `Failed Resource Actions`, no `UNCLEAN`, no pending fence

**Not normal - investigate before changing anything:**

- Only one node Online, or `pending` / `UNCLEAN`
- Two Promoted entries, or DRBD `Primary/Primary` (see assert below)
- `kin-zimbra` Stopped/Failed while DRBD is Promoted on that node
- VIP present on the wrong node or on both
- A `cli-ban-kin-drbd-clone-on-…` location constraint left over from a controlled move
- `stonith-enabled=false` outside an Observability maintenance window (section 7)

### DRBD

```bash
drbdadm status kin-zimbra
drbdadm role kin-zimbra
/usr/local/sbin/kin-assert-no-dual-primary.sh
# expect: NO_DUAL_PRIMARY_OK
```

**Normal on the Promoted node:** `Primary/Secondary`, both disks `UpToDate`, `Established`.  
**Normal on the Unpromoted node:** `Secondary/Primary` (local/peer), peer Primary, `UpToDate`.

### Mount, VIP, Zimbra (Promoted node only)

```bash
findmnt /opt/zimbra                 # /dev/drbd0 on Promoted only; empty on Unpromoted
ip -4 addr show <NET_IFACE> | grep <CLUSTER_VIP_IP>
curl -skI -o /dev/null -w '%{http_code}\n' https://<CLUSTER_VIP_IP>/
su - zimbra -c 'zmcontrol status'   # only where /opt/zimbra is mounted
crm_failcount --query --resource kin-zimbra   # expect value=0
```

### Stickiness (should remain; this replaced prefer-A)

```bash
pcs resource config kin-drbd-clone | grep stickiness
# expect: resource-stickiness=1000 and promoted-resource-stickiness=1000
pcs constraint location
# expect: no output / no prefers / no leftover bans
```

### fail2ban (Promoted vs Unpromoted)

```bash
fail2ban-client status
systemctl is-active fail2ban      # must stay active on BOTH nodes
```

Promoted: `sshd, zimbra-auth, zpush-auth`. Unpromoted: `sshd` only. If Unpromoted
fail2ban is **failed**, see section 10 - do not "fix" it by pointing jails at
unmounted `/opt/zimbra` logs.

---

## 3. Controlled failover (maintenance move)

Use this to **intentionally** move the Master (OS work on the current Promoted
node, or a planned swap). Prefer the console Cluster page (section 6) when you
are taking a node out of service. Use `pcs resource ban` when you only want to
move the Master and leave both nodes Online.

Expect **~2-3 minutes** of real downtime while Zimbra fully restarts on the target.

Do **not** use `pcs resource move … --promoted`. Ban the **currently Promoted**
node; stickiness holds the new Master after you clear the ban.

### Move Master off the current Promoted node

Example: stack is on B, move to A:

```bash
pcs resource ban kin-drbd-clone mail2.<MAIL_DOMAIN> --promoted

# Wait until:
#   Promoted: [ mail.<MAIL_DOMAIN> ]
#   kin-mail-svc Started on mail (Zimbra start can take several minutes)
#   https://<CLUSTER_VIP_IP>/ → 200
pcs status
/usr/local/sbin/kin-assert-no-dual-primary.sh
# Old Master: findmnt /opt/zimbra empty; no VIP
# New Master: findmnt + VIP <CLUSTER_VIP_IP>
```

Then **clear the ban**. Stickiness 1000 keeps the new Master; you do **not**
leave the ban in place to prevent failback (there is no prefer pin to fight):

```bash
pcs resource clear kin-drbd-clone
pcs constraint location    # must be empty
```

The inverse (A → B) is the same command with `mail.<MAIL_DOMAIN>`.

### After any controlled move

```bash
pcs resource cleanup kin-zimbra   # if fail-count / failed actions linger
pcs status
/usr/local/sbin/kin-assert-no-dual-primary.sh
fail2ban-client status            # both nodes; see section 10
```

If a move looks stuck, fail-count rises, or VIP stays `000` well past ~10 minutes
(OCF start timeout is 600s): **stop**. Do not issue a second ban/move. Capture
`pcs status`, `crm_failcount`, `drbdadm status`, `journalctl -u pacemaker`.

---

## 4. Automatic behavior on node failure vs still manual

### What Pacemaker + SBD do automatically (proven ha-build-14)

When the Master becomes unreachable (hard crash / `sysrq-b` / partition from peer
while qnetd is still reachable):

1. Survivor detects peer **UNCLEAN / offline**
2. **Fencing** via `fence_sbd` (SBD poison → victim reboot / watchdog). Promote
   of DRBD on the survivor waits until the fence completes.
3. After fence OK: promote **`kin-drbd-clone`** on the survivor
4. Start **`kin-mail-svc`**: Filesystem mount → Zimbra → VIP `<CLUSTER_VIP_IP>`
5. With **`SBD_DELAY_START=yes`**, the fenced node **auto-rejoins as Secondary**
   after reboot without `systemctl start pacemaker`. Stickiness keeps the survivor
   Promoted - **no second move**.

Expect **minutes** of mail downtime on a Promoted-node crash (~3m19s in ha-build-14
Stage 2). Crashing the Unpromoted node does **not** move the VIP (Stage 1: mail
stayed on A the whole time).

### What is still manual / not automatic

| Item | Status |
|---|---|
| **DNS / public NAT to VIP** | FortiGate NAT → VIP `<CLUSTER_VIP_IP>`. `mail.<MAIL_DOMAIN>` DNS may still be Host A `<HOST_A_IP>`. Fixed-node DNAT/DNS will miss the current Master. |
| Controlled maintenance | Console Cluster page (section 6) or `pcs resource ban` / `pcs node standby` (section 3) |
| Failback to a previously Promoted node | **Manual** - ban the current Master (or enter maintenance on it). Recovery of the old node does **not** pull the stack back. |
| `pcs resource cleanup` after failed starts | Operator |
| Observability VM power-off / disk grow | Operator - **must** disarm SBD first (section 7) |
| Changing SBD timeouts | Operator - see section 5 (dangerous if incomplete) |

Do **not** assume "VIP is live for all users" just because `kin-vip` is Started.

---

## 5. Unexpected recovery notes (`SBD_DELAY_START`)

### Why delay exists

If a fenced VM reboots **faster** than the survivor finishes fence acknowledgement,
Pacemaker on the victim can start, then see `We were allegedly just fenced` and
**stay down** (Phase 2 task 2.8). That stay-down is a safety feature; the fix is
**not** to delete it.

Official pattern (SUSE HA / ClusterLabs): delay SBD (and thus Pacemaker) on boot
until ~**msgwait** has elapsed. ha-build-14 Stage 1: B's kernel was pingable ~25s
after kill; `SBD_DELAY_START` held Pacemaker until fence completed (~81s). That is
the mechanism working.

### Lab settings (do not change casually)

| Setting | Lab value | Where |
|---|---|---|
| SBD device | `/dev/disk/by-id/<SBD_LUN_WWN>` | `/etc/default/sbd` on A and B |
| `msgwait` | **70** s | `sbd -d … dump` |
| Watchdog timeout | **35** s | same header / `SBD_WATCHDOG_TIMEOUT` |
| `SBD_DELAY_START` | **yes** (≈ msgwait) | `/etc/default/sbd` |
| `TimeoutStartSec` for `sbd.service` | **180** | `/etc/systemd/system/sbd.service.d/kin-delay-start.conf` |
| `RefuseManualStart` / `RefuseManualStop` | **yes** | Ubuntu `sbd.service` packaging |
| `stonith-timeout` | **150** s | cluster property |
| `pcmk_reboot_timeout` / `pcmk_off_timeout` | **120** s | `fence_sbd` attributes |
| `SBD_TIMEOUT_ACTION` | `flush,reboot` | `/etc/default/sbd` |
| softdog | `soft_margin=35`, `nowayout=0` | `kin-softdog.service` |

**If you raise `msgwait`, you must also ensure:**

1. `SBD_DELAY_START` still covers the new wait (`yes` tracks msgwait, or set seconds ≥ msgwait),
2. `TimeoutStartSec` for `sbd.service` stays **greater** than that delay, and
3. Pacemaker STONITH timeouts remain above the fence window.

Otherwise you recreate the 2.8 stay-down race or `sbd.service` start timeouts.

### If a node is stuck with Pacemaker inactive after fence

1. Confirm survivor is healthy and **sole** Promoted (section 2).
2. On the victim, check journal for `allegedly just fenced` / stay-down.
3. Only after fencing is complete and DRBD on the survivor is Primary: start the
   cluster stack on the victim (`systemctl start pacemaker` or site procedure),
   then verify it joins as **Unpromoted / Secondary** only.
4. Never promote manually on both sides.

---

## 6. Maintenance mode (console `/cluster`)

HCI-style planned work on **one** mail node. It is a safety-gated
`pcs node standby` / `unstandby` - not a new cluster mechanism.

**Where:** admin console `https://<HOST_A_IP>:9443/cluster` (nav after deploy).  
**Who:** `kin_super_admin` / ops can Enter; `customer_admin` can view status only.  
**Browser close:** the node **stays in standby** until someone hits Exit. No timeout.

**Blocks while any node is standby:** Deploy (full install / Build HA pair) and
Create mailbox. Users, audit, login, and wizard settings save stay available.

### When to use it

OS patches, a reboot, or hypervisor work on **one** mail node while the other
keeps serving. Whole-cluster downtime (both nodes) is out of scope here.

Prefer entering the **currently Unpromoted** node when you can - mail does not
move (proven: A standby, B stayed Promoted, VIP 200). Entering the Promoted node
**is** a graceful failover (~2-3 min downtime).

### Pre-flight (Check must pass; Enter is refused if any check fails - no override)

| Check | Meaning |
|---|---|
| `pacemaker_nodes` | Live nodelist parsed |
| `drbd_uptodate` | Both replicas `UpToDate` (not mid-sync) |
| `qdevice_voting` | qdevice reachable and voting |
| `failcount_zero` | `kin-drbd-clone` / `kin-mail-svc` members value=0 on both nodes |
| `peer_online` | The other mail node is in the nodelist |
| `not_already_standby` | Target is not already in maintenance |
| `peer_https` | Serving node `https://<ring0_addr>/` → HTTP 200 |
| `peer_zmcontrol` | `kin-zimbra Started <peer>` in `crm_mon` (SSH `zmcontrol` on the peer is not always possible from the console host) |
| `no_dual_primary` | Live `kin-assert-no-dual-primary.sh` → `NO_DUAL_PRIMARY_OK` |

**"Safe to proceed"** means every check above is OK on **real command output**, not
a UI inference. Run **Check** on that node; Enter stays disabled until it passes.

### Enter / Exit

Enter: `pcs node standby <target>`. If the target was Promoted, watch the stream
until the peer is Promoted, `kin-mail-svc` Started, VIP 200.

Exit: `pcs node unstandby <target>`, then the console **waits** (up to 180s) until:

1. Target is **not** Promoted (Secondary only - stickiness must not steal the Master)
2. DRBD `UpToDate`/`UpToDate`
3. fail-count 0
4. `NO_DUAL_PRIMARY_OK`

Only then is Exit reported done. `unstandby` plus "still mid-resync" is **incomplete**,
not success. A non-zero Exit means the node is Online again but verification failed -
do not assume it is a healthy Secondary.

CLI equivalent (when the console is down):

```bash
# pre-flight (same ideas as the console - do not skip)
drbdadm status kin-zimbra
pcs quorum status
crm_failcount --query --resource kin-zimbra
/usr/local/sbin/kin-assert-no-dual-primary.sh

pcs node standby mail.<MAIL_DOMAIN>     # example: Unpromoted A
# … work …
pcs node unstandby mail.<MAIL_DOMAIN>
# wait: Unpromoted, UpToDate, fail-count 0, NO_DUAL_PRIMARY_OK
```

---

## 7. Observability VM maintenance (SBD / qdevice disarm and re-arm)

Observability (`<OBSERVABILITY_IP>`) is the **iSCSI SBD LUN** and the **qnetd vote**. Powering it
off while SBD watchers are running will miss the device loop; `SBD_TIMEOUT_ACTION=flush,reboot`
plus the 35s softdog can reboot **both** mail nodes. That is a dual self-fence.

This procedure was executed 2026-08-13 (disk grow). Follow it under pressure; do
not invent a shorter path. Naive `systemctl stop sbd` is **refused**
(`RefuseManualStop=true`) and would also stop corosync/pacemaker via `RequiredBy=`.

**Do this before any Observability power-off, resize, or long NIC outage.**

### What the pair loses while Observability is gone

Both of these are deliberate, and both are now shown in the console rather than
left for the operator to infer.

**Fencing, and with it the protection against a split.** The SBD LUN lives on
that VM, so while it is gone nothing can fence a peer. This appliance ships
`no-quorum-policy=ignore` (see `sbd_stonith_no_quorum_policy`), so a lone
surviving mail node **keeps serving** rather than stopping - the availability
trade is deliberate. What it costs is that a partition which cuts the two nodes
off from each other while both stay up can put both into Primary. That is
contained rather than silent: `after-sb-2pri disconnect` makes DRBD refuse to
merge two diverged copies and wait for an operator.

`strip_quorum_device.py` deliberately does not write `two_node: 1`, so the vote
count stays honest about what is present rather than manufacturing a quorum the
cluster does not have.

For a deployment that must prefer consistency over availability, set
`sbd_stonith_no_quorum_policy: stop`. Then the survivor is not quorate and
Pacemaker stops mail on it. The Cluster page follows whichever policy is
actually set rather than assuming one.

**Fencing.** Disarm sets `stonith-enabled=false` as its first action. A disarm
that fails part-way therefore leaves fencing off, and so does a manual override
nobody undid. The console reads `stonith-enabled` and raises **Fencing is
disabled** whenever the witness is healthy but fencing is not on - the one
combination that is unexplained and dangerous. Add Observability verifies the
property came back before reporting success.

### Disarm (mail stays up; fencing is paused on purpose)

Confirm baseline: Promoted + VIP 200, fail-count 0, `NO_DUAL_PRIMARY_OK`, SBD slots
clear, quorum expected 3 / total 3 / quorum 2.

1. `pcs property set stonith-enabled=false` - Pacemaker will not issue `fence_sbd`.
2. `pcs resource unmanage sbd` - monitor will not flap/recover the primitive.
3. Runtime systemd drop-ins under `/run/systemd/system/` (gone on reboot) so sbd
   can be started/stopped by hand (`RefuseManualStart/Stop` overridden) and so
   unit-file `Requires=` is not enough on its own.
4. **`systemctl disable sbd`** on **both** mail nodes (does **not** stop it). This
   is what actually removes the `.requires/` symlinks from corosync + pacemaker.
   Empty `Requires=` drop-ins alone were **not** enough.
5. Stop sbd on the **Unpromoted** node first. Wait **>35s** (watchdog timeout).
   Prove: no reboot (`boot_id` unchanged), pacemaker still up, mail still on the
   Promoted node, VIP 200.
6. Stop sbd on the **Promoted** node. Same wait. VIP/HTTPS unchanged. Watchdog
   `state=inactive` on both (`nowayout=0` + SIGTERM did disarm).
7. `systemctl mask --runtime sbd` so nothing restarts it this boot.
8. Optional proof (then start qnetd again until the actual power-off): stop
   `corosync-qnetd` briefly. Expected votes stay **3**; **total** drops to **2**;
   quorum 2 is still met **iff both mail nodes keep Corosync**. Flags stay
   `Quorate Qdevice` with Qdevice votes 0 / Connect failed. Mail HTTPS 200.

**While Observability is down, do not:**

1. **Reboot or power-off either mail node.** One mail node + no qdevice = 1 vote
   < quorum 2 → `no-quorum-policy=stop` **stops Zimbra/VIP on the survivor**.
2. Start `sbd` or set `stonith-enabled=true`.
3. Partition A↔B (same quorum reason).

Runtime drop-ins and the sbd mask live in `/run` (lost on reboot).
`systemctl disable sbd` **is persistent** - re-arm **must** `systemctl enable sbd`
or RequiredBy / fencing-on-boot stays broken.

### After the VM is back - IP first

A hypervisor resize left Observability on **DHCP**. It came back as `<DHCP_FALLBACK_IP_INCIDENT>`
with qnetd/iSCSI running on the wrong address; mail ARP for `<OBSERVABILITY_IP>` failed; qdevice
votes 0. Before re-arm:

- Guest `/` grown if that was the point of the window (`growpart` + `resize2fs`).
- **Static** `<OBSERVABILITY_IP>/24` on `<NET_IFACE>` (`dhcp4: false`), default via `<GATEWAY_IP>`.
- `/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg` → `network: {config: disabled}`
  so cloud-init does not rewrite DHCP on the next boot.
- `corosync-qnetd` active `:5403`, iSCSI `:3260`, `fileio/kin-sbd` LUN present.
- From both mail nodes: iSCSI session `<OBSERVABILITY_IP>:3260`, SBD by-id present,
  `sbd -d … dump` + `list` (slots still **clear**).
- Quorum: qdevice **Connected**, expected 3, total 3, quorum 2.

### Re-arm

Mail nodes must **not** have rebooted (runtime mask still in `/run`). If they did,
recreate the allow-manual drop-ins before `systemctl start sbd`.

1. `systemctl unmask --runtime sbd`. If `enable` still fails, `rm /run/systemd/system/sbd.service`
   (plain `unmask` left the `/run` mask in place once).
2. `systemctl enable sbd` - restores `.requires/` on corosync + pacemaker.
   Confirm `systemctl show pacemaker -p Requires` includes `sbd.service`.
3. Remove pacemaker/corosync disarm drop-ins; **keep** the allow-manual drop-in
   until after start (`RefuseManualStart` would otherwise block `systemctl start`).
4. `systemctl start sbd` on **Unpromoted first**. Expect **~70s** (`SBD_DELAY_START`
   ≈ msgwait; `TimeoutStartSec=180`). Watchdog `state=active`. VIP 200.
5. Start sbd on **Promoted**. Same ~70s. VIP must not move.
6. Remove allow-manual drop-ins; packaging `RefuseManualStart=yes` again.
7. `pcs resource cleanup sbd`; `pcs resource manage sbd`; **`pcs property set stonith-enabled=true`**.
8. `pcs stonith sbd status` - from B, A may show N/A (pcsd 401, pre-existing).
   Confirm on **A locally**: both `YES | YES | YES`, plus `systemctl is-active sbd` and `sbd list`.
9. Full health: quorum 3/2, slots clear, fail-count 0, HTTPS VIP 200, DRBD
   Established/UpToDate, `NO_DUAL_PRIMARY_OK`, VIP still on the same Promoted node.

---

## 8. Backup / restore

Backup **repository** is the Backup VM (`<BACKUP_IP>`), not a mail node. Cron
survives a dual-mail-node event. The script SSHs to every `MAIL_NODES` entry,
finds which host has `/opt/zimbra` mounted (the Promoted replica), and collects
there. Node identity is not hardcoded.

FOSS has **no** `zmbackup` / `zmrestore` (Network Edition). Collector uses
`zmslapcat`, `mysqldump --single-transaction`, read-only rsync of store/index/redolog
onto `/var/tmp` (root fs, never DRBD), config tarballs, and per-account
`zmmailbox getRestURL '//?fmt=tgz'`.

## Mail reports (Cluster -> Reports)

Answers "what happened last month" without leaving the console or reading
Postfix logs. Read-only: range queries against the same loopback Prometheus the
Monitoring tab uses.

| What | Where |
|---|---|
| Report | Cluster page -> **Reports** tab |
| Periods | This month, last month, rolling 7 / 30 / 90 days |
| Figures | Accepted, delivered to mailboxes, sent to other servers, rejected, deferral events, bounced, queue peak, longest wait |
| Export | **Export CSV** (one row per day), **Print / PDF** (a clean page with its own title, period and generation time) |
| Raw samples | Monitoring tab -> **Export CSV** (every charted metric, one row per sample) |
| API | `GET /api/reports/mail?period=…`, `…/mail.csv`, `GET /api/monitoring/series.csv?metrics=…&range=…` |

**The figures come from the mail flow collector**
(`monitoring/mailflow/kin-mail-flow-metrics.py`), installed by the
`monitoring_stack` role. An appliance deployed before that role carried it
shows an empty report until the monitoring step is re-run:

```bash
ansible-playbook -i inventory/lab.yml playbooks/mail-monitoring.yml
```

Figures start from the day the collector first runs. There is no back-fill:
Postfix's log is rotated and the counters are cumulative from first start.

**Reading the numbers honestly.** "Accepted" counts messages entering the
queue. The two delivered columns count per RECIPIENT, so a message to three
people is one acceptance and three deliveries - which is why the summary quotes
delivered as a share of *final outcomes* (delivered plus bounced) rather than
against accepted. "Deferral events" counts retries, not messages: Postfix logs
`status=deferred` every time it postpones, so a message that retried nine times
before arriving appears nine times and is still a successful delivery. Queue
figures are the worst point in each day, not an average - a queue that peaked
at 400 is the fact worth keeping.

**Days are this server's days.** Period totals are exact in any timezone. The
day-by-day breakdown uses a fixed 24-hour step, so in a timezone that observes
DST the buckets after a transition sit an hour off local midnight. Asia/Jakarta
has no DST, so the breakdown is exact on the shipped configuration.

### Setting up the Backup VM

Everything below is installed by one idempotent script, run from a checkout on
the backup VM. Until 0.1.3 this table described a layout that nothing created:
the scripts existed in the repo and the nightly schedule did not exist at all,
so a VM was "set up" only if somebody had wired the cron entry by hand.

```bash
sudo backup/install-backup-vm.sh
```

It installs the three scripts, creates `/etc/kin-mail-backup/`, generates the
SSH identity if there is none, writes the cron entry and a logrotate rule, and
installs the freshness probe when a Zabbix agent is present. Re-running it is
safe: an existing config and an existing SSH key are never overwritten, because
rotating that key would leave every mail node trusting the old one and break
the next unattended run silently.

Two things still need a human afterwards, and the script says so rather than
reporting success:

1. Set `MAIL_NODES` in `/etc/kin-mail-backup/config` to the real mail nodes.
2. Trust the printed public key on each node as the `kin` user.

Then confirm the pull path end to end:

```bash
sudo /usr/local/sbin/kin-mail-backup.sh --probe
```

To ask whether this VM is actually taking backups - scripts present, schedule
installed, cron running, newest verified set inside the 26h window:

```bash
sudo backup/install-backup-vm.sh --check
```

`--uninstall` removes the schedule and leaves every backup in place.

### Where backups live / retention

On the Backup VM:

| What | Path / value |
|---|---|
| Config | `/etc/kin-mail-backup/config` (mode 0600; not in git) |
| Daily sets | `/var/lib/kin-mail-backup/daily/<UTC timestamp>` |
| Weekly | `/var/lib/kin-mail-backup/weekly/<ISO week>` (`cp -al` of the first daily of that week) |
| Keep | `DAILY_KEEP=7`, `WEEKLY_KEEP=4` - **verified sets only** |
| Partial sets | `UNVERIFIED_KEEP=2`. A run interrupted between the pull and the checksum leaves a set with no `.backup-ok` marker. These are kept for inspection, never counted towards the quota, and never reported as fresh. |
| Failed sets | `/var/lib/kin-mail-backup/failed/<timestamp>` (checksum mismatch; newest 5 kept) |
| Install | `backup/install-backup-vm.sh` (idempotent; `--check` to audit, `--uninstall` to unschedule) |
| Staging | `/var/tmp/kin-mail-backup-staging` on the **Promoted mail node**, cleared on every exit path |

**A set can be checksum-valid and still incomplete.** `SHA256SUMS` proves the
set arrived intact; it says nothing about whether every mailbox was exported.
The collector records the result of that in `MANIFEST.txt`:

| Field | Meaning |
|---|---|
| `mailboxes_expected` / `mailboxes_exported` | per-account tgz exports attempted vs written |
| `mailboxes_failed` | the accounts with no tgz in this set |
| `complete` | `false` when any export failed |

An incomplete set is still restorable - the store, MySQL and LDAP layers are
the restore path and they are all present - but the per-account hot exports for
those accounts are missing. The nightly run says so after verification, and
`install-backup-vm.sh --check` fails while the newest set is incomplete.

**Staging is on the mail node's OS disk, by design**, so nothing is written to
the DRBD volume. The shipped sizing is a 100GB OS disk beside a 500GB data
disk, so a large enough store would fill `/` on a live mail node and take mail
down. The collector sizes `store` + `index` + `redolog` first and refuses
(exit 4) when the staging filesystem cannot hold roughly 150% of it. Skipping a
night's backup is the better failure. If staging lives on its own disk and the
estimate is wrong for that node, raise `KIN_BACKUP_SPACE_FACTOR` or set
`KIN_BACKUP_SKIP_SPACE_CHECK=1`.
| Cron | `/etc/cron.d/kin-mail-backup` → `15 2 * * * root /usr/local/sbin/kin-mail-backup.sh` |
| Log | `/var/log/kin-mail-backup.log` |
| Scripts | `/usr/local/sbin/kin-mail-backup.sh` (orchestrator), `kin-mail-backup-remote.sh` (collector on mail nodes), `kin-mail-restore.sh` |

Manual run (Backup VM):

```bash
sudo /usr/local/sbin/kin-mail-backup.sh
```

### Restore drills - scratch only, never A/B

`kin-mail-restore.sh` **refuses** to run if Pacemaker is active or `/opt/zimbra` is
on DRBD. That is the guard against restoring onto live Host A/B.

Scratch target in this lab: Backup VM itself (isolated Zimbra FOSS, same version).
It answers as `mail.<MAIL_DOMAIN>` while running - **it must not stay up on the VLAN**.

Always pass **`--stop-after`** so the script `zmcontrol stop`s when the drill
finishes. If you omit it, stop by hand immediately after proof:

```bash
su - zimbra -c 'zmcontrol stop'
ss -lntp | grep -E ':443 |:25 ' || echo NO_MAIL_LISTENERS
```

**Full restore:**

```bash
# On Backup VM only. --set is a daily directory from the repo.
sudo /usr/local/sbin/kin-mail-restore.sh \
  --set /var/lib/kin-mail-backup/daily/<timestamp> \
  --mode full \
  --i-understand-this-overwrites-zimbra \
  --stop-after
```

Optional: `KIN_MAIL_RESTORE_MARKER=…` to search for an injected Inbox marker
(the 3.4 proof used `KIN-BACKUP-MARKER-20260813T0315Z` on `test1@`, never restored
onto live DRBD).

**Partial:**

```bash
# LDAP-only (e.g. after a forced zmprov -l da on scratch)
sudo /usr/local/sbin/kin-mail-restore.sh --set DIR --mode ldap \
  --i-understand-this-overwrites-zimbra --stop-after

# MySQL mailbox metadata only
sudo /usr/local/sbin/kin-mail-restore.sh --set DIR --mode mysql \
  --i-understand-this-overwrites-zimbra --stop-after

# store/index rsync (--mode store) also exists; 3.5 proved mysql row restore.
```

`--verify-only` checks SHA256SUMS / MANIFEST without writing Zimbra.

Live cluster must stay Promoted on the current Master with VIP 200 throughout a
drill. Never point this script at A or B.

---

## 9. Monitoring (KIN Sight - Zabbix / Grafana)

Packages come from [kin-sight-monitoring](https://github.com/azana-nisaa/kin-sight-monitoring)
v1.9.3. Custom keys live in this repo (`monitoring/zabbix/`).

| Piece | Where | URL |
|---|---|---|
| Zabbix Server | Observability `<OBSERVABILITY_IP>` | `http://<OBSERVABILITY_IP>:8080/` (not Grafana) |
| Grafana | **Backup** `<BACKUP_IP>` (not Observability) | `https://<BACKUP_IP>:3000/` |
| Dashboard | Grafana uid `kin-mail-ha` | "KIN Mail HA" |
| Datasource | `kinsight-zabbix` | Zabbix API `http://<OBSERVABILITY_IP>:8080/api_jsonrpc.php` |

Hosts: `obser`, `mail.<MAIL_DOMAIN>`, `mail2.<MAIL_DOMAIN>`, `backup.<MAIL_DOMAIN>`
(+ installer default `Zabbix server` @ 127.0.0.1). Mail nodes: template **KIN Mail HA**.
Backup: **KIN Mail Backup**. All: **Linux by Zabbix agent**.

### Custom items - what an alert should prompt

| Key | Where | Good | Alert → do this |
|---|---|---|---|
| `kin.drbd.ok` | A/B | `1` (Established + both disks UpToDate) | **High if 0.** Section 2: `drbdadm status`, dual-primary assert. Do not promote blindly. If Unpromoted is `Connecting`/`Inconsistent`, wait for resync; if both Primary, **stop** (escalation). |
| `kin.qdevice.votes` | A/B | `1` | **High if &lt;1.** Observability qnetd `:5403` / IP still `<OBSERVABILITY_IP>` (DHCP regression to `<DHCP_FALLBACK_IP_INCIDENT>` already happened once). Section 7 if the VM is down. Two mail nodes can remain quorate; **do not** reboot a mail node until the vote is back. |
| `kin.stonith.new_events` | A/B | `0` | **High if &gt;0.** A `pcs stonith history` event completed after **2026-08-13 09:05:00** (the morning fence-test is excluded). Treat as a real fence: section 2 + 4. Confirm one Promoted, slots clear, victim rejoined Secondary. |
| `kin.backup.age_seconds` | Backup | newest daily dir age | **Warning if &gt;93600 (26h).** Cron `15 2 * * *` missed. Check `/var/log/kin-mail-backup.log`, SSH to Promoted, disk on `<BACKUP_IP>`. Do not restore onto A/B. |
| `kin.mailbox.active_count` | Promoted only | gauge (lab last **2**) | No trigger. Unpromoted is `ZBX_NOTSUPPORTED` (expected - no fake zero). Same rules as `install/lib/quota-gate.sh` (excludes admin + system accounts). LDAP-only; does not write `/opt/zimbra`. |
| `kin.mailbox.contracted_seats` | A/B | `CONTRACTED_SEATS` or **0** if unset/`PLACEHOLDER_UNSET` | No trigger. `0` means the quota gate will not create mailboxes - set a real seat count in `/etc/kin-mail/config` on both mail nodes. |

Agent `Timeout` on mail nodes is **30s** so the mailbox listing can finish.

Grafana panels that must show real data (not just "service up"): Mail B CPU idle,
`KIN DRBD replication OK` (`kin.drbd.ok`), Active mailboxes, Contracted seats.

---

## 10. fail2ban across promote / demote

Jail file `/etc/fail2ban/jail.d/kin-mail.conf` is rewritten by
`/usr/local/sbin/kin-fail2ban-jails` (source: `install/lib/kin-fail2ban-jails.sh`).
`ocf:kin:zimbra` v1.2 calls it:

- **stop** (before `zmcontrol stop`, while `/opt/zimbra` is still mounted): `unmounted`
  → **sshd only**, so fail2ban is not watching paths that DRBD is about to unmount.
- **start** (after Zimbra is Running): `mounted` → add `zimbra-auth` / `zpush-auth`
  iff `mailbox.log` / `nginx.access.log` exist.

A fail2ban hiccup is logged (`fail2ban jails mounted|unmounted: …`) and **cannot**
fail the OCF start/stop (best-effort, `timeout 15`). Ansible `os_hardening` and
`09-hardening.sh` use the same script.

**Expected:** Unpromoted `Jail list: sshd` and unit **active**. Promoted
`sshd, zimbra-auth, zpush-auth`. If Unpromoted fail2ban is dead with
`Have not found any log file for zimbra-auth jail`, the OCF hook did not run
(old agent) or failed open - run `/usr/local/sbin/kin-fail2ban-jails unmounted`
on that node; do not leave it watching `/opt/zimbra/log` while unmounted.

---

## 11. Known limitations

1. **No hypervisor fencing** - no ESXi/vCenter API; fencing is **SBD + softdog**.
2. **SMTP AUTH via saslauthd** can fail after HA moves when URLs still target
   `mail.<MAIL_DOMAIN>` resolved to a **fixed node IP**. Functional proofs used
   local inject + SOAP; treat submission AUTH as fragile until service hostname /
   sasl endpoints are HA-clean.
3. **DNS vs VIP** - cluster VIP `<CLUSTER_VIP_IP>` works on the lab segment and is what
   FortiGate NAT should hit. Public DNS may still point `mail.<MAIL_DOMAIN>` at
   Host A `<HOST_A_IP>`. Confirm with network ops after a Master move.
4. **LDAP bind** is localhost (`ldap://127.0.0.1:389`) plus
   `zimbra_require_interprocess_security=0` so Zimbra can start on either node
   from the shared store. Revisit if you split Pacemaker node name from mail FQDN.
5. **Console on both mail nodes** - `:9443` on `<HOST_A_IP>` and `<HOST_B_IP>`
   after Build HA / Add Second Server (not on the VIP). If A is down, use B.
6. **Grafana is not on Observability** - it is on Backup `<BACKUP_IP>`. Probing `<OBSERVABILITY_IP>:3000`
   will look like "Grafana disappeared."
7. **Scratch Zimbra on Backup** remains installed but **must stay stopped** so it
   does not listen as `mail.<MAIL_DOMAIN>` on `:443`/`:25`.
8. **`pcs stonith sbd status` from B** may show A as N/A (pcsd 401). Check from A
   (local) for both `YES|YES|YES`.
9. Stickiness is **not** a hard pin. A Promoted node that is fenced or put in
   standby will move. A healthy returning node will **not** steal the Master
   unless an operator bans the current one or re-enables prefer (do not).

---

## 12. Escalation

If this runbook is insufficient (split-brain suspicion, dual-primary, DRBD
inconsistent after fence, or fencing loop):

| Role | Contact | Notes |
|---|---|---|
| Primary on-call / platform | _TBD - operator fill_ | Prefer call before any `drbdadm --discard-my-data` |
| Network / firewall (DNAT, VIP) | _TBD_ | Required before assuming client traffic follows VIP `<CLUSTER_VIP_IP>` |
| Vendor / upstream HA questions | ClusterLabs / SUSE HA SBD docs | Cite msgwait + `SBD_DELAY_START` together |

**Stop rule:** any hint of **two Primaries** or unexpected dual mount of
`/opt/zimbra` → stop automated recovery, preserve logs (`pcs status`,
`drbdadm status`, `journalctl -u pacemaker -u sbd`), escalate.

---

## 13. Move `/opt/zimbra` onto the DRBD data partition (before Build HA pair)

This is the procedure the console warning refers to when it says Zimbra is still
on the **root volume**, not `/dev/sdb1` (or `KIN_DRBD_DATA_DISK`). Build HA pair
would otherwise replicate an **empty** DRBD disk and leave live mail on the OS
volume.

**Do not run this on a formed cluster** (Pacemaker already mounting `/dev/drbd0`).
**Do not run it on both mail nodes** - only the host that currently has the live
`/opt/zimbra` tree (typical: the single-node Primary after a successful wizard
install). **Do not run it against production without an independent backup** of
`/opt/zimbra` first (the script keeps a rename on the root filesystem; that is
not a substitute for a real backup). Run it **on that mail host**, as root, after
confirming hostname/IP - not from a laptop and not against the wrong VM.

### Order relative to Build HA pair

1. Spare disk attached and GPT-partitioned (`sdb1` data + `sdb2` meta). Disk prep
   (`plan_auto_partition` / `ansible/roles/drbd_disk_prep`) **does not mkfs**
   either slice. If `sdb1` does not exist yet, let Build HA pair GPT it (or
   attach/partition first), then stop - it will re-check and refuse DRBD while
   Zimbra is still on root.
2. This migrate script (mkfs data if needed, rsync, fstab UUID, start Zimbra).
3. Build HA pair again for DRBD + Pacemaker. **Comment/remove the UUID fstab
   line** when `kin-fs` starts mounting `/dev/drbd0`.

### Why a script, and why mkfs is in it

`ansible/roles/drbd_disk_prep` / `plan_auto_partition()` only GPT-partitions the
spare disk:

- partition 1 = Zimbra/DRBD **data** (usually `/dev/sdb1`) - **no mkfs**
- partition 2 ≈ 256 MiB DRBD **meta** (usually `/dev/sdb2`) - **never mkfs**

So after disk prep, `sdb1` is a blank slice. The migrate script will `mkfs.ext4
-L zimbra-data` **only if** `blkid` shows no filesystem; it refuses any unexpected
TYPE and refuses a non-empty ext4. It never formats the meta partition.

### Commands (on that mail host, as root)

```bash
# 1) Independent backup (operator - outside the script)
#    e.g. tar/rsync /opt/zimbra to another disk or the Backup VM.

# 2) Read-only plan (must print OK on size, disk parent ≠ OS disk, mkfs-or-not)
sudo /opt/kin-mail-deploy/install/lib/migrate-zimbra-to-drbd-disk.sh --dry-run
# If the tree lives in the git checkout instead:
# sudo /path/to/kin-mail/install/lib/migrate-zimbra-to-drbd-disk.sh --dry-run

# 3) Real move - stops Zimbra for the duration of rsync + cutover
sudo /opt/kin-mail-deploy/install/lib/migrate-zimbra-to-drbd-disk.sh \
  --i-understand-this-moves-live-mail
```

Override the target if this host is not `sdb1`:

```bash
sudo KIN_DRBD_DATA_DISK=/dev/nvme0n1p1 KIN_DRBD_META_DISK=/dev/nvme0n1p2 \
  /opt/kin-mail-deploy/install/lib/migrate-zimbra-to-drbd-disk.sh --dry-run
```

### What the script does (fail-closed)

1. Refuses: missing `/opt/zimbra`, data disk on the OS parent, Pacemaker KIN
   resources, live `kin-zimbra` DRBD, extra mounts under `/opt/zimbra`, data
   disk already mounted, unexpected filesystem, too-small partition.
2. `mkfs.ext4` on the data slice **only when it has no TYPE**. An existing
   ext4 (including after a failed run that already formatted `sdb1`) is reused,
   never reformatted.
3. `zmcontrol stop`, then `zmconfigdctl stop`, then poll `zmcontrol status`
   (and leftover daemons) until nothing is `Running`. "Stopping X...Done" is
   not enough; mailboxd/onlyoffice can still be up after that line.
4. Mounts the data slice on `/mnt/kin-zimbra-data`, `rsync -aHAX --numeric-ids
   --sparse` of `/opt/zimbra/` onto it, then a dry-run itemize that must show
   **no** pending copies/deletes, plus an entry-count check. Original tree is
   not renamed until that passes. A previous failed rsync (filesystem label
   `zimbra-data`, source still on root) is resumed with `--delete`. Optional
   `KIN_MIGRATE_CHECKSUM=1` adds a `--checksum` verify pass (slow).
5. Appends `UUID=<data> /opt/zimbra ext4 defaults 0 2` to `/etc/fstab` (never
   `/dev/sdX`). Backs up fstab. Renames `/opt/zimbra` →
   `/opt/zimbra.root-<timestamp>` on the **same** root filesystem (rename, not
   a second full copy). `mkdir` + `mount /opt/zimbra` from fstab. Verifies UUID.
6. `zmcontrol start`, waits until **all listed services are Running** (up to
   15 minutes, with progress). Empty status is not treated as success.
7. **Does not delete** `/opt/zimbra.root-*`. Operator removes it later, after
   mail is healthy **and** preferably after Build HA pair has wrapped the disk
   in DRBD.

Any failure after stop attempts rollback (restore fstab, restore the renamed
tree, `zmcontrol start`) and **refuses to call rollback finished unless every
service is Running**. If rollback cannot prove that, it prints `[FAIL]` and
dumps status; treat that as mail down until you fix it.

### After success - Build HA pair

Console preflight should now see Zimbra on the data partition (`sdb1`), not the
root volume. Then:

1. Build HA pair will `drbdadm create-md` with **external** meta on `sdb2`.
   Unmount `/opt/zimbra` first if DRBD refuses a busy backing disk.
2. `create-md` may complain that `sdb1` already has an ext4 signature. That is
   expected (existing mail data). Do **not** `wipefs` the data partition.
3. When Pacemaker `kin-fs` mounts **`/dev/drbd0`** at `/opt/zimbra`,
   **comment or remove** the UUID fstab line the migrate script added, or boot
   and Pacemaker will fight over the mount.

### Rollback without the script

If the script did not finish and `/opt/zimbra.root-*` exists while `/opt/zimbra`
is empty or wrong:

```bash
su - zimbra -c 'zmcontrol stop' || true
umount /opt/zimbra 2>/dev/null || true
# restore fstab from /etc/fstab.kin-pre-zimbra-migrate-*
mv /opt/zimbra.root-<timestamp> /opt/zimbra
su - zimbra -c 'zmcontrol start'
su - zimbra -c 'zmcontrol status'   # all Running
```

---

## Quick command cheat sheet

```bash
pcs status
pcs constraint location                          # must be empty (no prefer, no leftover ban)
pcs resource config kin-drbd-clone | grep stickiness
drbdadm status kin-zimbra
/usr/local/sbin/kin-assert-no-dual-primary.sh
curl -skI -o /dev/null -w '%{http_code}\n' https://<CLUSTER_VIP_IP>/
fail2ban-client status                           # Promoted: 3 jails; Unpromoted: sshd; both active

# Controlled move (example: off B onto A), then clear - stickiness keeps the new Master:
pcs resource ban kin-drbd-clone mail2.<MAIL_DOMAIN> --promoted
# wait Promoted + kin-mail-svc + VIP 200
pcs resource clear kin-drbd-clone

# Planned OS work on one node (same as console /cluster Enter/Exit):
pcs node standby mail.<MAIL_DOMAIN>
pcs node unstandby mail.<MAIL_DOMAIN>

# Cleanup failed Zimbra starts:
pcs resource cleanup kin-zimbra

# Backup (Backup VM) / restore drill (Backup VM scratch ONLY, then stop):
/usr/local/sbin/kin-mail-backup.sh
/usr/local/sbin/kin-mail-restore.sh --set DIR --mode full \
  --i-understand-this-overwrites-zimbra --stop-after
```
