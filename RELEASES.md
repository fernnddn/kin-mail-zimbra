# KIN Mail releases

What each release is, what it is ready for, and what it is not. Written for
whoever has to decide whether to put a version in front of a customer.

A version is called **mature** here when it has been deployed end to end on
real hardware, its faults have been found by someone using it rather than by
its own tests, and those faults have been fixed. A green build is not a smoke
test.

---

## 0.1.11 — multi deployment

**Mature.** Zimbra split across two machines with a Proxmox Mail Gateway in
front of them, deployed end to end on real hardware, its faults found by the
people running it, and those faults fixed.

Three machines with three jobs and no shared state. This is a **multi
deployment**, not a cluster and not a replicated pair: nothing fails over,
nothing is replicated, there is no quorum and no fencing. The machine facing
the internet holds no mail. The whole build runs from that machine over SSH, so
the operator never logs into the other one.

```
internet ──MX──▶ gateway ──filter──▶ edge (MTA + proxy) ──▶ mailbox (directory + store)
```

Proven on real hardware through September 2026: both machines built from one
command, the gateway linked from the console, mail delivered inbound and
outbound through the whole chain, and the console driving both machines
afterwards.

- **Choose it in the wizard.** Address and hostname for the mailbox, and the
  password the edge uses to build it. Nothing else.
- **One Deploy button.** The mailbox is built first, because the edge has no
  directory to join until it exists. Then the edge. Then metrics, TLS, DKIM,
  hardening and the firewall on both.
- **Each node is firewalled for what it is.** The edge faces the internet as a
  single appliance does. The mailbox accepts everything from the edge and
  nothing from anywhere else — and the edge trusts the mailbox by address, so
  the two halves can sit on different subnets, which is the point of a DMZ.
- **Every stage knows which machine it is on**, decided from addresses rather
  than hostnames, and refuses rather than guessing.

### What the console shows and does, for both machines

- **Topology is the mail path**, drawn as the three machines a message passes
  through. Health is the links: each one is checked by opening a connection to
  the port the next machine has to answer on, which proves the service is
  listening *and* that the firewall allows it. A ping would prove neither.
- **Monitoring has a view per machine.** The mailbox holds every message and
  the operator never logs into it, so its CPU, memory and disk are read from
  the edge — node_exporter bound to the mailbox's own address, which its
  firewall already restricts to the edge. Opens live rather than on an hour of
  history.
- **Extend a disk reaches all four disks.** Both machines, both of their
  disks: the system volume and Zimbra's own volume on each. After enlarging one
  in the hypervisor, Check rescans, says what it would do, and does it only
  when asked. A disk that is already taken in full says so rather than asking
  for more space — the two read the same from the guest and call for opposite
  actions.
- **/opt/zimbra is named for whose it is.** On the mailbox it is the mail
  store. On the edge it is Zimbra's MTA and proxy, the outbound queue and the
  logs, with no mailbox on it — and it is a disk of its own in this layout, so
  it is offered and monitored as one.
- **Reports open on the current month.**
- **New mailboxes get 10 GB by default**, set on the default COS during the
  deploy, so a fresh estate is never a set of unlimited mailboxes nobody
  noticed until the store filled up. Change it for everyone in the Zimbra admin
  console, or per account when creating one; set `DEFAULT_MAILBOX_QUOTA_GB` in
  `/etc/kin-mail/config` to deploy with a different figure, or `0` for no cap.
  It is applied once and then left alone — a size an operator chose is not
  reimposed by later maintenance — and it refuses to apply at all if any
  mailbox is already larger, rather than silently stopping that person's mail.
- **Branding reaches the machine that serves the UI.** Its cache flush was
  refused on the edge — `flushCache` is SOAP-only — and swallowed by a
  `|| true`, so the skin attributes were written to the directory and nothing
  was ever told to re-read them. It then tried to restart mailboxd, which does
  not run on an edge, and failed the whole stage: a multi deployment could not
  be branded at all. The flush now goes to the mail store, and the restart
  happens only where mailboxd actually runs.
- **Deleting and renaming a mailbox now actually do it.** `zmprov` on a node
  with no local mailboxd falls back to LDAP-only mode without saying so, and
  there the destructive operations refuse to run unattended: they ask
  "Continue? [Y]es, [N]o", read end-of-file, abort — and exit 0. Nothing here
  runs on a terminal, so on a multi deployment every console delete and rename
  reported success and changed nothing: a departed employee's mailbox went on
  receiving mail and holding a seat. They are now carried out on the mail
  store, and the directory is read back afterwards — a change that did not take
  is reported as the failure it is, in those words.
- **Zimbra's own Server Status page tells the truth about both machines.**
  Each node reports its services to the logger over syslog, and on Ubuntu
  nothing in Zimbra ever switches the logger's receiver on — so a healthy edge
  delivering mail was drawn with every service down. The mailbox now listens
  for it, on its own address and behind the rule that already admits only the
  edge.

### Single server

0.1.10 single-server is unchanged and still supported. Choose it in the wizard
for a one-machine deployment. The active-passive replicated pair is **not
offered**: it is behind a feature flag, it is not what this release deploys,
and nothing a 1vm or multi deployment can reach mentions DRBD, a quorum device
or an observability VM.

### Known limits, stated rather than discovered

- **One gateway and one edge are each a single point of failure.** If either is
  down, mail queues rather than being lost, and nothing moves.
- **The mail disk cannot grow without deleting something.** The installer lays
  it out as a data partition followed by a 256 MiB partition reserved for a
  second server, and new space lands behind it. The console offers to free that
  partition — but only when there is space it would actually release, never to
  gain the 256 MiB itself, and it refuses if anything at all is on it. Done on
  a live mailbox holding mail on 20 September 2026: partition freed, LUKS
  container and filesystem grown to the whole disk, nothing unmounted.
- Backups run on the mailbox node, which is where the mail is. The edge refuses
  and says so. Restore overwrites the mailbox, and afterwards the edge must
  still bind to LDAP (the directory passwords have to match).
- **TLS is issued on the edge only.** The mailbox keeps Zimbra's own private-CA
  certificate on 7071/8443, which no browser trusts, so the browser warning on
  the Zimbra admin console is expected and is not a sign of interception. There
  is no renewal path for a public certificate on the mailbox.
- **Zimbra's admin console is on the mailbox, and only on the mailbox** —
  `https://<mailbox address>:7071`. The edge does not serve it. After a deploy
  it is open to private networks (10/8, 172.16/12, 192.168/16) and to any
  listed Admin IPs, so an operator reaches it without configuring anything
  first, and the site's own firewall decides who gets that far. It is never
  open to the internet: no host out there can hold one of those addresses. To
  allow only the listed addresses, delete the three `Zimbra admin private net`
  rules.
- **A public certificate is not required for mail to work**, and a failed
  issuance no longer stops the install: the rest of the pipeline runs, so the
  host is still hardened and firewalled. The deploy reports "with warnings".
- **HSTS is only switched on for a certificate clients actually accept**,
  checked against the system trust store rather than by comparing the issuer to
  the subject. Zimbra's own certificate satisfies the second test and no client
  on earth accepts it; enabling HSTS there removes the browser's "proceed
  anyway" for a year.
- Removing a deployment means running the uninstaller on both machines. It
  names the one still standing.
- An empty Admin IPs list no longer skips the firewall. Admin access falls back
  to the node's own subnet and the public ports stay the mail ports; re-run
  stage 10 with `KIN_ADMIN_IPS` set to narrow it properly.
- A leftover LDAP server object from a previous edge will confuse routing. The
  build fails if the directory does not list both nodes, and warns if it lists
  more than two.
- **The public side is yours.** MX, SPF, DMARC, the A record and the PTR are
  DNS changes only the operator can make, and the gateway's public hostname and
  address have to be recorded in the console before it can print the exact
  records. It refuses to print a private address as SPF, because publishing one
  authorises nobody.

## 0.1.10 — mature, with the Proxmox Mail Gateway

Single-server appliance with a Proxmox Mail Gateway in front of it. Still
supported, and the right choice when there is only one machine.

The gateway is the MX. Inbound mail is filtered there before Zimbra sees it,
outbound mail is relayed through it, and the appliance's port 25 accepts SMTP
from the gateway and the LAN only. The machine holding every mailbox is no
longer the machine on the perimeter.

Proven on real hardware on 11 September 2026: gateway installed from the
Proxmox ISO, linked from the console, mail delivered inbound and outbound
through it, verified clean.

What it adds over 0.1.9:

- **Console ▸ Proxmox Mail Gateway** — connect, probe, plan, apply, verify,
  revert. The API credential is encrypted on the appliance; the gateway's
  certificate is pinned, not ignored.
- **A tuned gateway.** Applying the link does not just route mail, it
  configures the detection: Bayesian learning and the auto-welcomelist on
  (both off in stock PMG, and the best defence against false positives),
  attachment text extraction for phishing hidden in documents, encrypted
  archives flagged rather than waved through unscanned, deeper archive
  nesting, longer quarantine, and a quarantine view with no live links or
  tracking images.
- **Greylisting off by default.** It defers the first message from any unseen
  sender by minutes, and that is why inbound mail felt slow while outbound
  felt instant. The other layers still run. Switchable.
- **A Mail gateway card on Monitoring.** The gateway failing looks like
  perfect health from the appliance — Zimbra up, CPU idle, the queue quietly
  climbing. This says why.
- `DEPLOY-WITH-GATEWAY.md` — empty VM to verified gateway, in order.

Known limits, stated rather than discovered:

- **One gateway is a single point of failure.** If it is down, mail queues at
  the senders and on the appliance. Nothing is lost; nothing moves. PMG
  supports clustering and this release does not set it up.
- The gateway's own hardening (restricting its web interface, its SSH, its
  certificate) is a documented checklist, not automation.
- PMG is installed by the operator from the official ISO. KIN Mail configures
  it; it does not build it.

---

## 0.1.9 — mature, without a mail gateway

Single-server appliance, all in one. Zimbra holds port 25 to the internet and
does its own filtering with amavis, SpamAssassin, ClamAV and opendkim.

This is a complete, working mail appliance and it was deployed and operated as
one. It is **not** the current shape of the product: from 0.1.10 the gateway is
part of what KIN Mail is. Keep 0.1.9 for an estate that is already running it
and is not ready to add a second VM.

What it settled: the disk-extend feature, the monitoring install running
automatically at deploy, readable CSV exports, and the single-server topology
as the only one offered.

---

## 0.1.8 and earlier — archived

Superseded. Kept so an existing deployment can be identified and understood,
not for new installations.

---

## Which one do I deploy?

| | |
| --- | --- |
| Deployment that must not go wrong | **0.1.10**, proven on hardware |
| Two machines, and the mailbox must be off the internet | **0.1.11**, split, watched |
| One machine only | **0.1.10**, single server with the gateway |
| Existing 0.1.9 estate, gateway planned | upgrade to 0.1.10, then link the gateway — no reinstall |
| Existing 0.1.9 estate, no second VM available | stay on 0.1.9 |
| Anything at 0.1.8 or below | upgrade |

Going from 0.1.9 to 0.1.10 does not require a redeploy. The gateway is added in
front of a running appliance, and mail keeps flowing while the link is applied.
