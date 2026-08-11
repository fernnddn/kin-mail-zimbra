# KIN Mail — HA runbook (Pacemaker / DRBD / SBD)

Operational reference for the **active–passive** KIN Mail lab/cluster proven in Phase 2
(tasks 2.1–2.9, plus operator failover/failback observations in 2.13–2.14). Commands and
behaviors below are taken from that work, not from unverified theory.

Cluster name: `kin-mail`  
Preferred production node: Host A (`mail.gits-it.site` / `10.10.40.13`)  
Standby: Host B (`mail2.gits-it.site` / `10.10.40.12`)  
Witness: Monitoring VM (`mon.gits-it.site` / `10.10.40.14`) — qnetd + iSCSI SBD target  
Cluster VIP: `10.10.40.15/24` on `ens33` (Pacemaker `kin-vip`)

> **Audience:** anyone operating a KIN Mail HA pair.  
> **Not covered here:** Zimbra first install (`kin-mail.sh` / stage scripts) — see `README.md`.

### Expectation: measured downtime, not zero-downtime

This design is **active–passive**, not active–active. Only one node runs Zimbra and holds the
VIP at a time. Every **failover** and every **failback** therefore causes a real service gap —
mail/webmail are unavailable while the stack moves.

**What operators measured in a full power-off / recover cycle (Host A OFF → B Primary → A
rejoin → automatic failback to A):** roughly **~2–3 minutes of downtime each direction**
(failover, then again on failback), on the order of **~5 minutes total** for the full cycle.
Exact length varies with Zimbra start time and how long DRBD promotion / group start take.

**Why the gap exists (by design, for data safety):**

1. DRBD must **promote** on the survivor before `/opt/zimbra` is mounted (never dual-Primary).
2. The Filesystem resource mounts the shared store, then the Zimbra OCF runs a **full**
   `zmcontrol start` on the new Master — not a hot hand-off of an already-running JVM.
3. VIP (`kin-vip`) starts **after** Zimbra in the group order, so clients see the old VIP
   disappear before the new side is ready.

**What this HA actually buys you:** automatic (or controlled) recovery with **bounded,
repeatable downtime**, fencing against split-brain, and a single consistent store — **not**
continuous availability or zero-downtime cutover. Do not plan change windows or SLAs as if
failover were transparent.

---

## 1. Architecture (short)

```
                    ┌─────────────────────┐
                    │  mon.gits-it.site   │
                    │  qnetd (vote)       │
                    │  iSCSI → SBD LUN    │
                    └──────────┬──────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │                     │                     │
┌────────▼────────┐   Corosync/Pacemaker    ┌────────▼────────┐
│ Host A          │◄───────────────────────►│ Host B          │
│ mail.gits-it…   │                         │ mail2.gits-it…  │
│ 10.10.40.13     │      DRBD kin-zimbra    │ 10.10.40.12     │
│                 │◄═══════════════════════►│                 │
│ Prefer score 50 │   /dev/drbd0 ↔ /opt/zimbra (when Master)  │
└────────┬────────┘                         └─────────────────┘
         │
         │  when Promoted: group kin-mail-svc
         ▼
   kin-fs → kin-zimbra → kin-vip (10.10.40.15)
```

| Layer | What | Notes (proven) |
|---|---|---|
| Quorum | Corosync + **qdevice/qnetd** on mon | Tie-break so one side keeps quorum under partition |
| Fencing | **SBD** + softdog + `fence_sbd` | No ESXi/hypervisor fence in this lab |
| Replication | DRBD resource **`kin-zimbra`** → `/dev/drbd0` | Protocol C; external meta on loop (`kin-drbd-meta-loop.service`) |
| Pacemaker DRBD | Promotable clone **`kin-drbd-clone`** (`ocf:linbit:drbd`) | `promoted-max=1`, `notify=true` |
| App stack | Group **`kin-mail-svc`**: `kin-fs` → `kin-zimbra` → `kin-vip` | Colocation + order with **Promoted** DRBD |
| Zimbra RA | Custom **`ocf:kin:zimbra`** | Wraps `zmcontrol` (no official Zimbra OCF) |

**Boot ownership (important):**

- `drbd.service` (systemd) must stay **disabled** — Pacemaker owns DRBD.
- `kin-drbd-meta-loop.service` must stay **enabled** — attaches external meta before DRBD use.
- `kin-softdog.service` must stay **enabled** — Ubuntu blacklists softdog; explicit modprobe is required for SBD.

---

## 2. Daily health checks

Run on **either** cluster node (as root / sudo). Healthy resting state: stack on **A**.

### Cluster membership and resources

```bash
pcs status
```

**Normal (resting on A):**

- `Online: [ mail.gits-it.site mail2.gits-it.site ]`
- `kin-drbd-clone`: `Promoted: [ mail.gits-it.site ]`, `Unpromoted: [ mail2.gits-it.site ]`
- `kin-mail-svc`: `kin-fs`, `kin-zimbra`, `kin-vip` all **Started** on `mail.gits-it.site`
- `sbd` (stonith) Started on one of the nodes
- No `Failed Resource Actions`, no `UNCLEAN`, no `Pending Fencing Actions`

**Not normal — investigate before changing anything:**

- Only one node Online, or `pending` / `UNCLEAN`
- Two Promoted entries, or DRBD role `Primary/Primary` (see assert below)
- `kin-zimbra` Stopped/Failed while DRBD is Promoted on that node
- VIP present on the wrong node or on both

### DRBD

```bash
drbdadm status kin-zimbra
drbdadm role kin-zimbra
```

**Normal on A (Master):** `Primary/Secondary`, both disks `UpToDate`, replication `Established`.  
**Normal on B (Standby):** `Secondary/Primary` (local/peer), peer Primary, disks `UpToDate`.

Optional dual-primary guard (installed on lab nodes):

```bash
/usr/local/sbin/kin-assert-no-dual-primary.sh
# expect: NO_DUAL_PRIMARY_OK
```

### Mount and VIP (only on the Master)

```bash
findmnt /opt/zimbra          # expect /dev/drbd0 on the Promoted node only
ip -4 addr show ens33 | grep 10.10.40.15
su - zimbra -c 'zmcontrol status'   # only works where /opt/zimbra is mounted
```

### Soft prefer (should remain)

```bash
pcs constraint location
# expect: kin-drbd-clone prefers mail.gits-it.site with score 50
```

---

## 3. Controlled failover / failback (maintenance)

Use this to **intentionally** move the Master (e.g. Host A maintenance). Proven in task 2.7; do **not** rely on plain `pcs resource move … --promoted` while soft prefer is set — it can bounce.

Expect **~2–3 minutes** of real downtime for each controlled move (same order of magnitude as
automatic failover/failback) while Zimbra fully restarts on the target node.

### Move Master A → B

```bash
# On either node:
pcs resource ban kin-drbd-clone mail.gits-it.site --promoted

# Wait until:
#   Promoted: [ mail2.gits-it.site ]
#   kin-mail-svc Started on mail2 (Zimbra start can take several minutes)
pcs status

# Safety:
/usr/local/sbin/kin-assert-no-dual-primary.sh
# On A: findmnt /opt/zimbra should be empty; VIP gone
# On B: findmnt + VIP 10.10.40.15 present
```

Leave the ban in place only while you need B to stay Master. Soft prefer alone is not enough to keep B Master if you clear the ban **or** if A simply comes back healthy (see automatic failback below).

### Return Master B → A (failback) — when you *do* want it

**You do not always need `pcs resource clear` for failback.** That command only matters when a
**ban** (or other location constraint from a controlled move) is still holding A off. If the
only sticky preference is the default soft prefer A score 50 and there is no ban, failback can
happen **without any operator command** as soon as A is Online and DRBD-ready (next subsection).

If a ban on A is still present and you want to allow failback:

```bash
pcs resource clear kin-drbd-clone
```

With prefer A score 50, the stack should return to A. Wait for Promoted + full `kin-mail-svc` on `mail.gits-it.site`, then assert no dual-primary. Budget another **~2–3 minutes** of downtime for that move.

If there is no ban and the stack is already on B without prefer, either restore prefer:

```bash
pcs constraint location kin-drbd-clone prefers mail.gits-it.site=50
```

or ban B’s promotion temporarily, then clear after A owns the stack:

```bash
pcs resource ban kin-drbd-clone mail2.gits-it.site --promoted
# wait for A stack OK
pcs resource clear kin-drbd-clone
pcs constraint location kin-drbd-clone prefers mail.gits-it.site=50
```

### Recovery of the preferred node triggers automatic failback

**Default resting state** includes:

```bash
pcs constraint location
# kin-drbd-clone prefers mail.gits-it.site with score 50
```

With that constraint still active (and **no** ban pinning Master on B):

1. Host A boots, rejoins Corosync, brings up DRBD as Secondary, becomes eligible.
2. Pacemaker **automatically** schedules promote on A and moves `kin-mail-svc` off B —
   **no `pcs resource clear`, no manual promote**.
3. Operators see a **second** downtime window (~2–3 minutes again), separate from the first
   failover downtime, right after the failed/preferred node comes back.

**Proven (operator-driven test, task 2.14):** after Host A rejoin ~12:07:48, Pacemaker scheduled
automatic promote/move ~12:08:22; transition finished ~12:11:25 — without a new CIB edit or
clear. External webmail showed `http_code=000` during the VIP/Zimbra gap.

**If you do *not* want automatic failback yet** (e.g. keep observing Host B as Master after A
is repaired):

- Act **before** you power Host A back on (or before you let it rejoin with prefer intact).
- Keep / re-apply a **ban** on promoting A, or temporarily remove/lower the prefer-A location
  constraint, **then** bring A online.
- Changing prefer **after** A is already Online is too late if the scheduler has already
  started the move.

Example — hold Master on B while A is allowed to rejoin as Secondary only:

```bash
# Before (or as soon as possible when) bringing A back, while B is still Master:
pcs resource ban kin-drbd-clone mail.gits-it.site --promoted
# Then power on / allow A. A should join Unpromoted; stack stays on B until you clear.
```

When ready to return to the normal resting state on A: `pcs resource clear kin-drbd-clone`
(and ensure prefer A score 50 is present). Expect another measured downtime window.

### After any controlled move

```bash
pcs resource cleanup kin-zimbra   # if fail-count / failed actions linger
pcs status
/usr/local/sbin/kin-assert-no-dual-primary.sh
```

---

## 4. Automatic behavior on node failure vs still manual

### What Pacemaker + SBD do automatically (proven 2.8 / 2.9)

When the Master becomes unreachable (e.g. network partition from peer while qnetd still reachable):

1. Survivor detects peer **UNCLEAN / offline**
2. **Fencing** via `fence_sbd` (SBD poison → victim reboot / watchdog)
3. After fence completes: promote **`kin-drbd-clone`** on the survivor
4. Start **`kin-mail-svc`** in order: Filesystem mount → Zimbra → VIP `10.10.40.15`
5. With **`SBD_DELAY_START=yes`**, the fenced node can **auto-rejoin as Secondary** after reboot (task 2.9) without manual `systemctl start pacemaker`

Expect **minutes** of mail downtime while Zimbra starts on the survivor (~2–3 minutes in the
operator cycle that also measured automatic failback).

### Automatic failback when preferred node returns

If soft prefer A (score 50) is still in place and nothing bans A from promotion, recovery of
Host A as a healthy Online Secondary **also** triggers an **automatic** failback to A (section
3). That is a second, separate downtime window — not only the failover to B. To stay on B for
observation, ban/adjust prefer **before** bringing A back.

### What is still manual / not automatic

| Item | Status |
|---|---|
| **DNS / public NAT to VIP** | External path must target VIP `10.10.40.15`; fixed-node DNAT will miss the current Master after a move. Confirm with network ops. |
| Controlled maintenance move | Operator runs `pcs resource ban` / `clear` (section 3) |
| **Preventing** automatic failback after A recovers | Operator must ban A or change prefer **before** A rejoins (section 3) — failback itself is automatic when prefer is active |
| `pcs resource cleanup` after failed starts | Operator |
| Changing SBD timeouts | Operator — see section 5 (dangerous if incomplete) |

Do **not** assume “VIP is live for all users” just because `kin-vip` is Started.

---

## 5. Unexpected recovery notes (`SBD_DELAY_START`)

### Why delay exists

If a fenced VM reboots **faster** than the survivor finishes fence acknowledgement, Pacemaker on the victim can start, then see `We were allegedly just fenced` and **stay down** (task 2.8). That stay-down is a safety feature; the fix is **not** to delete it.

Official pattern (SUSE HA / ClusterLabs): delay SBD (and thus Pacemaker) on boot until ~**msgwait** has elapsed.

### Lab settings (do not change casually)

| Setting | Lab value | Where |
|---|---|---|
| SBD device `msgwait` | **70** s | `sbd … dump` on the iSCSI LUN |
| SBD watchdog timeout | **35** s | same header / `SBD_WATCHDOG_TIMEOUT` |
| `SBD_DELAY_START` | **yes** (≈ msgwait) | `/etc/default/sbd` on A and B |
| `TimeoutStartSec` for `sbd.service` | **180** | `/etc/systemd/system/sbd.service.d/kin-delay-start.conf` |
| `stonith-timeout` | **150** s | cluster property |
| `pcmk_reboot_timeout` / `pcmk_off_timeout` | **120** s | `fence_sbd` attributes |

**If you raise `msgwait`, you must also ensure:**

1. `SBD_DELAY_START` still covers the new wait (`yes` tracks msgwait, or set an explicit seconds value ≥ msgwait), and  
2. `TimeoutStartSec` for `sbd.service` stays **greater** than that delay, and  
3. Pacemaker STONITH timeouts remain above the fence window (see 2.3: agent timeout was raised after msgwait 70s).

Otherwise you can recreate the 2.8 stay-down race or cause `sbd.service` start timeouts.

### If a node is stuck with Pacemaker inactive after fence

1. Confirm survivor is healthy and **sole** Promoted (section 2).  
2. On the victim, check journal for `allegedly just fenced` / stay-down.  
3. Only after fencing is complete and DRBD on the survivor is Primary: start cluster stack on the victim (`systemctl start pacemaker` or site procedure), then verify it joins as **Unpromoted / Secondary** only.  
4. Never promote manually on both sides.

---

## 6. Known limitations

1. **No hypervisor fencing** — lab has no ESXi/vCenter API access; fencing is **SBD + softdog** by design (2.1–2.3).  
2. **SMTP AUTH via saslauthd** can fail after HA moves when URLs still target `mail.gits-it.site` resolved to a fixed node IP while slapd/mailbox listen paths differ (seen in 2.8/2.9). Functional proofs used local inject + SOAP; treat submission AUTH as a known fragile point until service hostname / sasl endpoints are HA-clean.  
3. **Two separate Zimbra stores historically** — Host B was a standalone install (`mail2`) before production data lived on DRBD from Host A. Standby OS config under `/etc/kin-mail/` is not the shared store; shared data is `/opt/zimbra` on DRBD.  
4. **LDAP bind / interprocess TLS** was adjusted for shared-store HA (localhost LDAP) so Zimbra can start on B; revisit if you split Pacemaker node name from mail service FQDN.  
5. **VIP / DNS / NAT** — cluster VIP works inside the lab segment; external cutover is a separate change.  
6. Soft prefer score **50** is sticky preference, not a hard pin; use **ban** for controlled moves **and** to suppress automatic failback when A returns (section 3). Prefer alone will pull Master back to A once A is healthy.

---

## 7. Escalation

If this runbook is insufficient (split-brain suspicion, dual-primary, DRBD inconsistent after fence, or fencing loop):

| Role | Contact | Notes |
|---|---|---|
| Primary on-call / platform | _TBD — operator fill_ | Prefer call before any `drbdadm --discard-my-data` |
| Network / firewall (DNAT, VIP) | _TBD_ | Required before assuming client traffic follows VIP |
| Vendor / upstream HA questions | ClusterLabs / SUSE HA SBD docs | Cite msgwait + `SBD_DELAY_START` together |

**Stop rule:** any hint of **two Primaries** or unexpected dual mount of `/opt/zimbra` → stop automated recovery, preserve logs (`pcs status`, `drbdadm status`, `journalctl -u pacemaker -u sbd`), escalate.

---

## Quick command cheat sheet

```bash
pcs status
drbdadm status kin-zimbra
/usr/local/sbin/kin-assert-no-dual-primary.sh

# Controlled move Master off A:
pcs resource ban kin-drbd-clone mail.gits-it.site --promoted

# Allow A again / clear move constraint (triggers failback if prefer A=50):
pcs resource clear kin-drbd-clone

# Hold Master on B while bringing A back (prevent auto-failback):
#   ban A BEFORE powering A on; clear later when ready to return to A

# Cleanup failed Zimbra starts:
pcs resource cleanup kin-zimbra
```
