# When a failover goes wrong

Written after two QA rounds (Aug 2026) in which a healthy-looking 2-node pair
destroyed itself the first time it was asked to move the Master. Every fault
below was live, and none of them was visible from the console before the fix.

Read this before changing anything. The three faults looked identical from the
outside - a node rebooting every three minutes - and had nothing in common.

---

## First: is the node being fenced, or is it crashing?

    last -x reboot shutdown | head

`reboot` entries with no matching `shutdown` are hard resets. A regular period
between them (ours was almost exactly three minutes) means something is
deciding to reset the node, not hardware.

Then read the end of the previous boot, which is where the reason is:

    journalctl -b -1 -e --no-pager

The journal survives reboots on these hosts (`/var/log/journal` exists), so the
evidence is there even though the console shows nothing.

An SBD reset ends the log like this:

    sbd: servant_md: Received command reset from <other node>
    sbd: emerg: do_exit: Rebooting system: reboot

That is the cluster fencing this node on purpose. Work out what it was asked to
do just before, and why that failed. Read upward.

---

## Fault 1: a stop that fails is a fence

    zimbra(kin-zimbra): ERROR: /dev/drbd0 still has open holders after stop
    zimbra(kin-zimbra): ERROR: holder: zimbra 4135 ..c.. sleep
    Result of stop operation for kin-zimbra: error
    Setting fail-count-kin-zimbra#stop_0: (unset) -> INFINITY

**Pacemaker has exactly one response to a stop it cannot complete: fence the
node.** So any resource agent that returns an error from `stop` is choosing a
reset. Ours did that over a single leftover `sleep` owned by `zimbra` whose
working directory was inside `/opt/zimbra` - the `c` in `..c..` is cwd.

Fixed in `ansible/roles/pacemaker_agents/files/zimbra`: the agent now clears
holders it can safely signal, and no longer fails the stop when the mount is
still busy. Zimbra is stopped by that point, which is that agent's job;
unmounting belongs to `kin-fs`, one step later in the same group, whose
`force_unmount` exists for exactly this. Failing early meant `kin-fs` never got
its turn.

**If you write an OCF agent, never fail `stop` for something the next resource
is responsible for.**

Processes that must never be signalled to free a mount: `sbd`, `corosync`,
`pacemaker*`, `sshd`, `systemd*`, `iscsid`, `drbdsetup`. Killing any of those to
avoid a fence causes the outage you were avoiding, or removes your way in.

---

## Fault 2: the peer could never run Zimbra at all

    su - zimbra -c "/opt/zimbra/bin/ldap start"
    Failed to start slapd.
    daemon: bind(8) failed errno=13 (Permission denied)
    slap_open_listener: failed on ldapi:///

`ldapi:///` is a **Unix socket**, not port 389. `bind()` returning EACCES on a
Unix socket means the directory is not writable by the process. Do not go
looking at ports, capabilities, or AppArmor - none of that is involved.

The check that finds it in one line:

    for h in <node-a> <node-b>; do ssh $h 'id -u zimbra; id -g zimbra'; done

`/opt/zimbra` is replicated by DRBD and **ownership on disk is numeric**. If
`zimbra` is uid 997 on one node and 998 on the other, every file on that volume
belongs to a stranger as far as the second node is concerned, and it gets only
the "other" permission bits. Zimbra cannot start there. Ever.

`postfix` and `postdrop` matter too. To see exactly which ids the volume needs:

    find /opt/zimbra -xdev -printf '%U:%G\n' | sort | uniq -c | sort -rn | head

Why they diverge: Ubuntu allocates system uids downward from 999 in creation
order, and the console user is created at a different point in that order on a
peer than on a primary. Nothing warns. A pair can pass its entire build and only
fail the first time it is asked to fail over.

Now prevented by `install/lib/align-service-ids.sh`, which runs on a peer before
Zimbra is installed, and by an assertion in `pacemaker_mail_stack` that refuses
to finish building a pair whose ids disagree.

### The prevention has to RESERVE the number, not wait for it

The first version of that script fixed this in a way that could not work, and it
is worth understanding why, because the mistake is an easy one to repeat.

It renumbered any account that already existed, and for an account that did not
exist yet it printed:

    user zimbra does not exist yet; leaving creation to its installer

The reasoning was that creating `zimbra` early would fight Zimbra's installer.
Two things were wrong with that.

**A fresh peer is exactly the case where the account does not exist.** Zimbra has
not been installed yet - that is the whole point of running before it. So on the
one host that needed aligning, the aligner did nothing at all, and the installers
went on to allocate whatever happened to be free. The deployment of 29 Aug 2026
ran for 35 minutes and stopped at step 12 of 14 on exactly this.

**And the installers do not fight it.** Both create these accounts only `if` they
are missing, so an account already sitting at the right number is simply used.
The proof was in the same log that showed the bug: the three *groups* the script
pre-created survived a full Zimbra install untouched, at exactly the gids it
pinned. Groups matched on both nodes; only the users diverged - the two the
script had skipped.

The lesson generalises past this bug: **when you pin an identity, create it.**
Deferring to whatever runs later is not pinning, it is hoping.

### Checking it yourself, before the console does

Thirty seconds, from the primary, any time after the peer has Zimbra:

    for h in <node-a> <node-b>; do
      echo "== $h"
      ssh $h 'for n in zimbra postfix; do echo "user:$n=$(id -u $n)"; done;
              for g in zimbra postfix postdrop; do echo "group:$g=$(getent group $g | cut -d: -f3)"; done'
    done

The two blocks must be identical. If they are not, the pair will build and then
fail the first time it moves - or, since 29 Aug 2026, refuse to build at all.

### Repairing an existing pair

Only ever renumber the node whose ids do **not** match the volume, with
`/opt/zimbra` unmounted there (move the Master away first), and never touch
`/opt/zimbra` itself - it already carries the correct numbers.

    systemctl stop kin-mail-console
    # free any id you need, then move each account into place
    groupmod -g <free> <squatter>; usermod -u <free> <squatter>
    groupmod -g <wanted> postdrop
    groupmod -g <wanted> postfix
    usermod  -u <wanted> postfix
    usermod  -u <wanted> zimbra
    # rewrite local files that used the old ids, never under /opt/zimbra
    find / -xdev -uid <old> -not -path '/opt/zimbra/*' -exec chown -h <new> {} +
    systemctl start kin-mail-console

Or let the script do all of it, which is what the console now does for a peer
that was installed outside the HA sequence:

    sudo env KIN_PEER_ZIMBRA_UID=<uid>  KIN_PEER_ZIMBRA_GID=<gid> \
             KIN_PEER_POSTFIX_UID=<uid> KIN_PEER_POSTFIX_GID=<gid> \
             KIN_PEER_POSTDROP_GID=<gid> \
             bash /opt/kin-mail-deploy/install/lib/align-service-ids.sh

Run it **on the node that does not match the volume**, with the primary's
numbers. It aligns, then verifies, and exits non-zero if the numbers still
disagree. It stops whatever still holds an id before renumbering it, and it
never touches `/opt/zimbra`.

---

## Fault 2b: an added node has the same problem, and nothing checked it

`Add Host` does not install the new node. It attaches one that was already
deployed on its own, which means its `zimbra` and `postfix` ids were allocated
in whatever order that install happened to run - with no reason to match the
survivor's.

The assertion that catches this lives in `pacemaker_mail_stack`, and
`mail-add-host.yml` never runs that role. So until 29 Aug 2026 an added node
could join, report healthy, and fail the first time it was asked to take over -
the Phase 8 fault exactly, on a path where nothing would have said so.

The console now aligns the ids over SSH before the add-host playbook runs, and
refuses the whole operation if they cannot be made to agree. Same script, same
guarantee as the Build HA pair path.

---

## The one that looks healthy: replication is down, both disks say UpToDate

This is the worst state this product can reach, and until 30 Aug 2026 the
console drew it in green.

After a partition heals badly, or a node is fenced mid-write, DRBD can settle
into **StandAlone with both sides UpToDate**. Every signal the Cluster page had
agreed nothing was wrong: both replicas report UpToDate, there is no resync
percentage because nothing is syncing, and no resource has failed because none
has. Meanwhile nothing written on the serving node is reaching the other one.

Lose the serving node in that state and you lose every message since the split.

    drbdadm status kin-zimbra

Read the **connection**, not the disk:

    kin-zimbra role:Primary
      disk:UpToDate
      mail2 connection:StandAlone role:Secondary     <-- this line
        peer-disk:UpToDate                           <-- this one lies to you

`peer-disk:UpToDate` is the last thing the peer said before the link dropped.
It is a memory, not a measurement.

The console now reports this as **Replication between the nodes is down**, in
red, and Move Master refuses while it lasts, because promoting the other copy
is what turns a recoverable split into a lost mailbox.

**Do not "fix" it by failing over.** Decide which node has the data you want,
then discard the other side's divergent copy:

    # on the node whose data you are DISCARDING
    drbdadm disconnect kin-zimbra
    drbdadm secondary kin-zimbra
    drbdadm connect --discard-my-data kin-zimbra
    # on the node you are KEEPING
    drbdadm connect kin-zimbra

Watch it come back with `drbdadm status` until the connection is Established
and both sides are UpToDate again. Only then move anything.

If the connection says `Connecting` or `Unconnected` rather than `StandAlone`,
there is no split to resolve: the peer is unreachable. That is a network or a
dead node, and it heals on its own once the peer is back.

---

## "Why is this node in maintenance?" - it probably is not

Pacemaker **standby is a node attribute stored in the CIB, and it survives a
reboot**. A node put into standby and never taken out comes back up still in
standby, which is correct behaviour and reads as inexplicable.

Nothing in the installer, the playbooks, or the OCF agents ever runs
`pcs node standby`. Only two console operations do: maintenance enter, and the
drain step of remove-host. So if a node is in standby, one of those put it
there - or somebody did it by hand.

The Cluster page now says which:

- **"Entered from this console on ..."** - there is a matching record in
  `/var/log/kin-mail/cluster-ops.log`.
- **"No record of this console entering maintenance on that node"** - there is
  not. Treat it as leftover state and exit maintenance to clear it.

A node that is merely powering back on is **Rejoining**, not Maintenance, and
one that is down is **Unreachable**. Those three are different states and the
page draws them differently. A fault is never labelled as maintenance - that is
the same distinction vSphere draws between Maintenance Mode and Not Responding,
and it exists because the operator response is completely different.

To see it directly:

    crm_mon -1 | sed -n '1,12p'
    cibadmin --query --scope nodes | grep -A2 standby

---

## "FAILED - RETRYING" is not a failure

Ansible prints that line on every poll of a task that is waiting for something:

    FAILED - RETRYING: [mail]: Wait until kin-fs, kin-vip, and kin-zimbra are
    Started (90 retries left).

That is a healthy cluster starting up. The console rewrites these as

      waiting   Wait until kin-fs, kin-vip, and kin-zimbra are Started (90 more attempts)

Similarly, the `1200` in a Move Master is the point at which the console gives
up, not how long the move takes. Each poll line now carries `waited=Ns of
1200s`, so a wait that is progressing looks different from one that is stuck.

---

## Fault 3: the node could not rejoin after it went down

    pcs cluster start
    Error: Unable to start pacemaker: Failed to start pacemaker.service:
    Unit systemd-cryptsetup@kin-zimbra-crypt.service not found.

systemd escapes unit instance names: a dash becomes `\x2d`. The generator had
produced `systemd-cryptsetup@kin\x2dzimbra\x2dcrypt.service`; the Pacemaker
drop-in required the unescaped spelling, which resolves to nothing, and systemd
refuses to start a service whose `Requires=` cannot be resolved.

    systemctl list-units 'systemd-cryptsetup@*' --all

shows both side by side, one `loaded` and one `not-found`. Never write these
names by hand:

    systemd-escape --template=systemd-cryptsetup@.service <mapper-name>

This one hid for the entire life of the project, because a node that never comes
back never runs the command that fails.

---

## Proving a pair actually works

Do all five, in order. Anything less will pass on a pair that cannot fail over.
Watch `cut -d. -f1 /proc/uptime` on the other node throughout: if it resets, you
have been fenced and the run is a failure however the console looks.

1. `pcs resource ban kin-drbd-clone <node-a> --master` and wait for the group -
   filesystem, Zimbra and VIP - to be `Started` on node B. Not just Promoted:
   DRBD promoting proves nothing about whether Zimbra can run there.
2. `pcs resource clear kin-drbd-clone`, then move back the same way.
3. `pcs node standby <node-b>` and `pcs node unstandby <node-b>`; DRBD must
   return to `Established` with both replicas `UpToDate`.
4. `pcs cluster stop` on whichever node is serving mail; the other must take
   over completely, VIP included.
5. `pcs cluster start` on it again; it must rejoin as Unpromoted and resync.

A verification that the VIP is genuinely serving, rather than merely assigned:

    for p in 443 25 587 993; do
      timeout 5 bash -c "</dev/tcp/<vip>/$p" && echo "$p open" || echo "$p CLOSED"
    done
