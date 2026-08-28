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
