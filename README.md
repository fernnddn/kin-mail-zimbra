# KIN Mail

<p align="center">
  <a href=".github/workflows/checks.yml"><img src="https://github.com/azana-nisaa/kin-mail/actions/workflows/checks.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/release-0.1.12-1B4F72" alt="Release 0.1.12">
  <img src="https://img.shields.io/badge/Ubuntu-22.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white" alt="Ubuntu 22.04 | 24.04">
  <img src="https://img.shields.io/badge/Zimbra-FOSS%2010-0A66C2" alt="Zimbra FOSS 10">
  <img src="https://img.shields.io/badge/gateway-Proxmox%20Mail%20Gateway-E57000?logo=proxmox&logoColor=white" alt="Proxmox Mail Gateway">
  <img src="https://img.shields.io/badge/directory-Active%20Directory-00A4EF?logo=microsoft&logoColor=white" alt="Active Directory">
  <a href="NOTICE"><img src="https://img.shields.io/badge/License-Proprietary-lightgrey" alt="Proprietary"></a>
</p>

<p align="center">
  <b>A complete mail server, deployed the same way every time, with a filtering gateway in front of it.</b>
</p>

<p align="center">
  Zimbra Collaboration 10 (FOSS) on Ubuntu Server, a Proxmox Mail Gateway as the MX,<br>
  and a web console that installs, monitors and maintains both.<br>
  Built by <b>PT Karya Informasi Nusantara</b>.
</p>

---

## What you get

<table>
<tr>
  <td width="33%" valign="top">
    <h3>🛡️ A gateway in front</h3>
    A Proxmox Mail Gateway is the MX. Mail is filtered before Zimbra sees it, and
    the machine holding every mailbox stops being the one on the perimeter.
    Connect it from the console and it configures both sides, then proves it.
  </td>
  <td width="33%" valign="top">
    <h3>⚙️ One deployment, every time</h3>
    Answer the wizard once. Preflight through healthcheck runs unattended, and
    the answers live in one file that every later stage reuses. No two
    appliances drift apart.
  </td>
  <td width="33%" valign="top">
    <h3>📊 It tells you the truth</h3>
    Monitoring shows whether mail is actually moving, not just whether the CPU
    is busy. When a figure cannot be trusted, the console says so instead of
    drawing a confident flat line.
  </td>
</tr>
<tr>
  <td valign="top">
    <h3>🔐 Encrypted where it matters</h3>
    The mail store is LUKS-encrypted, TLS is issued and renewed for you, and
    DKIM, SPF and DMARC are set up and then checked rather than assumed.
  </td>
  <td valign="top">
    <h3>💽 Grows with the customer</h3>
    Add disk in the hypervisor and extend it from the console: partition, LUKS,
    LVM and filesystem, in the right order, with mail still running. It reads
    the disk and says what it would do before it does anything.
  </td>
  <td valign="top">
    <h3>🧩 Fits real networks</h3>
    NAT, split DNS, a single uplink or several. TLS by Cloudflare DNS-01,
    manual DNS-01, or your own certificate. One machine, or the mail store
    held on a second one behind the edge.
  </td>
</tr>
<tr>
  <td valign="top">
    <h3>👥 The directory stays in step</h3>
    Active Directory checks the passwords and also fills the address book. New
    people get a mailbox within minutes of being added in AD, with their real
    name on it. Nobody is ever deleted by a search returning less than usual.
  </td>
  <td valign="top">
    <h3>🔎 It refuses rather than guesses</h3>
    A certificate that cannot be verified stops the directory read before the
    password is sent. A seat count nobody set blocks mailbox creation instead
    of inventing a number. Refusals say what to do next.
  </td>
  <td valign="top">
    <h3>🖥️ Two machines, one console</h3>
    On a split deployment the console shows both: their disks, their services,
    the link between them, and which one a button is about to act on.
  </td>
</tr>
</table>

### Where to start

| I want to&hellip; | Read |
| --- | --- |
| **Deploy this for a customer** | [`DEPLOY-WITH-GATEWAY.md`](DEPLOY-WITH-GATEWAY.md) &mdash; empty VM to verified gateway, in order |
| **Know which version to use** | [`RELEASES.md`](RELEASES.md) &mdash; what each release is ready for |
| **Understand the gateway** | [`MAIL-GATEWAY.md`](MAIL-GATEWAY.md) &mdash; the design, and every tuned setting with its reason |
| **Run it day to day** | the console: mailboxes, monitoring, reports, disks, certificates |

> **Scope, stated plainly.** This release deploys a mail system in one of two
> shapes: **a single appliance** with a gateway in front of it, or a **split**
> one where the edge faces the network and a second machine holds every
> mailbox. Both are offered in the console and both are what 0.1.12 promises.
>
> A two-server *high-availability* layout - shared storage, automatic failover -
> exists in the tree, is deliberately hidden in the console, and is **not**
> part of this release. Split is not HA: it separates the mail store from the
> perimeter, and it does not survive a machine being lost.

---

## The mail gateway

A Proxmox Mail Gateway sits in front of this appliance. It is the MX: inbound
mail is filtered there before Zimbra sees it, outbound mail is relayed through
it on the way out, and the mail node's port 25 accepts SMTP from the gateway
and the LAN only. The machine holding every mailbox stops being the machine an
attacker reaches first.

```
inbound    internet --MX--> gateway :25 --filter--> appliance :25
outbound   appliance --relay--> gateway :26 --> internet
internal   mailbox -> mailbox stays on the appliance and never reaches the gateway
```

KIN Mail does **not** install the gateway. PMG is a Debian appliance and this
is Ubuntu with Zimbra on it, so the gateway is its own VM, installed from the
official Proxmox ISO. What KIN Mail does is configure both sides of the link
over PMG's REST API and Zimbra's `zmprov`, and then prove it works:

- Console &rsaquo; **Mail Gateway** &rsaquo; Connect, with the gateway address
  and a PMG API token. The token secret is encrypted into the privhelper vault
  and never returned to the browser.
- **Plan** reads both sides and changes nothing. **Apply** configures the
  gateway, points outbound mail at it, and lets it hand mail in. Mail keeps
  flowing throughout; nothing restarts.
- The MX, SPF and PTR records still have to be changed at your registrar.
  **Verify** reads them back and says which are still wrong.

Zimbra's own amavis, SpamAssassin, ClamAV and opendkim stay enabled. The
gateway is defence at the perimeter, not a replacement for scanning mail that
never leaves the appliance, and DKIM signing deliberately stays on Zimbra —
the gateway is configured not to sign a second time.

The Monitoring tab carries a **Mail gateway** card. The gateway failing looks
like perfect health from everywhere else — Zimbra up, CPU idle, the queue
quietly climbing — so the card exists to say why.

`05-healthcheck.sh` reports an appliance with **no gateway linked yet** as
BLOCKED rather than FAILED, because a gateway VM cannot exist before the
appliance does and failing there would abort every first install. Once a
gateway *is* recorded, anything wrong with the link is a **failure**.

- [`DEPLOY-WITH-GATEWAY.md`](DEPLOY-WITH-GATEWAY.md) — step by step, in order,
  from an empty VM to a verified gateway. Start here for a deployment.
- [`RELEASES.md`](RELEASES.md) — which release is ready for what, and why.
- [`MAIL-GATEWAY.md`](MAIL-GATEWAY.md) — the design: ports, DNS, every PMG
  setting and why, and the failure modes.

---

## Active Directory

Zimbra's `zimbraAuthMech=ad` delegates the **password check** to Active
Directory. It does not read the directory's account list, so somebody who
exists in AD has no mailbox and cannot receive mail — which reads to whoever is
comparing the two screens as "AD is not syncing".

Zimbra's own answer is auto-provisioning, and none of its three modes closes
that gap on the FOSS build: `LAZY` creates the mailbox only at that person's
first sign-in, `EAGER` has no thread in this build, and `MANUAL`'s directory
search is absent from its `zmprov`. So the sweep is ours:

| Stage | What it does |
| --- | --- |
| `14-ad-trust.sh` | Reads the directory's CA — from AD itself, or from a file you supply — and **proves it signs the certificate the domain controller presents** before importing it. A CA that proves nothing is refused. |
| `15-ad-sync.sh` | Lists enabled people, compares against Zimbra, and creates what is missing. Names come across too, so the address book is not a list of addresses. Installs a timer so a new joiner has a mailbox within minutes. |

Three properties worth stating, because each one is a decision:

**It only ever creates.** Never deletes, disables or renames. A person
vanishing from an LDAP search — a filter typo, a moved OU, a directory that
answered slowly — must not be able to destroy their mail. Removing somebody
stays a decision a person makes.

**It counts seats the same way everything else does.** The sync uses the same
gate as the console and the CLI, so a directory import is not a way around a
contracted limit. If the seat count was never set, it says that rather than
reporting a full contract.

**The password is not given away to reach the directory.** The bind password
goes in a file, never in a command line where `ps` would show it, and an
`ldaps://` certificate is verified before the bind is sent. If nothing on the
machine can verify it, the read is refused — and the refusal says that no
password was sent.

Disabled AD accounts are excluded, so a leaver does not consume a seat.

---

## Quick start

```bash
sudo ./install/kin-mail.sh
```

If stage scripts are missing next to the bootstrap, it clones
[`fernnddn/kin-mail-zimbra`](https://github.com/fernnddn/kin-mail-zimbra) into
`/opt/kin-mail-deploy` (override with `KIN_MAIL_REPO_URL` / `KIN_MAIL_DEPLOY_DIR`)
and continues from `install/` inside that clone. A full local clone uses the
files already on disk.

The menu runs the full pipeline (`01 → 02 → 03 → 04 → 06 → [07?] → 09 →
[10 prompted] → 11 → 12 → 05`, stop on first failure) or one stage at a time.
Z-Push (`07`) follows `ZPUSH_ENABLED` from the wizard. Host firewall (`10`)
always asks before applying and keeps the dead-man's switch. Directory trust
and the directory sync (`14`, `15`) are run by `06` when AD is enabled, on the
machine that holds the mail store.

On a **split** deployment `kin-mail.sh` hands the build to
`kin-mail-split.sh`, which prepares and installs both machines and then
hardens and firewalls each of them, before the stages above continue here.

### Manual stage-by-stage

```bash
cd install
chmod +x *.sh

sudo ./01-preflight.sh        # can this site host mail at all?
sudo ./02-prepare-os.sh       # hostname, hosts, resolver, dependencies
sudo ./03-install-zimbra.sh   # download, verify checksum, drive the installer
sudo ./04-tls-dkim.sh         # TLS + DKIM (publish the DKIM record it prints)
sudo ./06-hybrid-auth.sh      # optional AD LDAP + local fallback
sudo ./07-zpush.sh            # ActiveSync (Z-Push + Zimbra backend); optional
sudo ./08-create-mailbox.sh   # create mailbox (shared quota gate); optional
sudo ./09-hardening.sh        # Part A + SMTP rate limits (fail2ban / lockout / TLS / …)
sudo ./10-host-firewall.sh apply   # ufw - needs KIN_ADMIN_IPS; dead-man armed
sudo ./11-admin-path-lockdown.sh   # block /zimbraAdmin on public :443
sudo ./12-branding.sh         # KIN branding on the web and admin clients
sudo ./15-ad-sync.sh --dry-run     # who WOULD get a mailbox; changes nothing
sudo ./15-ad-sync.sh --schedule    # create them, and keep the list in step
sudo ./05-healthcheck.sh      # acceptance tests and status summary
```

Reconfigure anytime: `sudo ./install/00-config.sh --reset` (also in the menu).

---

## Repository layout

| Path | Role |
|---|---|
| `install/` | Every deployment stage, plus the `kin-mail.sh` bootstrap and the `kin-mail-split.sh` two-machine build |
| `install/lib/` | Shared helpers, and a `test-*.sh` suite beside each one — all discovered by CI, never listed by hand |
| `console/` | The admin console: FastAPI backend, React frontend, root privhelper; `console/bootstrap.sh` installs it |
| `ansible/` | The HA port (qnetd / qdevice first) plus OS hardening (`playbooks/mail-os-hardening.yml`) |
| `backup/` | Backup repository pull scripts (`kin-mail-backup.sh` / restore) |
| `DEPLOY-WITH-GATEWAY.md` | The deployment runbook: empty VM to verified gateway |
| `MAIL-GATEWAY.md` | Gateway design, every tuned setting, and the failure modes |
| `RELEASES.md` | What each release is ready for, and what it is not |
| `HA-RUNBOOK.md` | Pacemaker / DRBD / SBD operator runbook (not part of this release) |
| `.github/workflows/` | Syntax, ShellCheck, test suites, hygiene and leaked-data gates |
| `NOTICE` | Proprietary terms |

---

## Install stages

| Script | Purpose | Idempotent |
|---|---|---|
| `install/kin-mail.sh` | Bootstrap menu: ensure scripts present, full install or one stage | yes |
| `install/00-config.sh` | Shared library and first-run wizard (not a normal entry point) | - |
| `install/01-preflight.sh` | Sizing, OS, conflicting services, outbound access, **SMTP egress**, DNS, PTR. Read-only. | yes |
| `install/02-prepare-os.sh` | Hostname, `/etc/hosts`, dnsmasq split-horizon, dependencies | yes |
| `install/03-install-zimbra.sh` | Fetch FOSS build, SHA-256, drive installer in tmux. Skips if healthy; refuses to re-drive a partial install | yes |
| `install/04-tls-dkim.sh` | TLS by `TLS_METHOD` + DKIM | yes |
| `install/06-hybrid-auth.sh` | Optional AD LDAP + local fallback; no-op if AD was skipped | yes |
| `install/07-zpush.sh` | Z-Push ActiveSync + Autodiscover (Zimbra backend); skips wipe/proxy restart when unchanged | yes |
| `install/08-create-mailbox.sh` | Manual mailbox create; calls shared quota gate **before** `zmprov ca` | yes |
| `install/09-hardening.sh` | Part A hardening (fail2ban, COS lockout, cleartext off, unattended-upgrades, TLS, SMTP client rate limits) | yes |
| `install/10-host-firewall.sh` | ufw allowlist + mandatory dead-man's switch (`apply` / `cancel-deadman`) | yes* |
| `install/11-admin-path-lockdown.sh` | Block `/zimbraAdmin` + `/service/admin` on public :443 (admin stays on :7071) | yes |
| `install/12-branding.sh` | KIN branding on the web client and admin console | yes |
| `install/12-node-metrics.sh` | Node exporter on each machine, bound to its own LAN address | yes |
| `install/13-log-relay.sh` | Syslog receiver on the mailbox, so **Server Status** tells the truth about the edge. Split only | yes |
| `install/14-ad-trust.sh` | Import the directory's CA and **prove** it signs what the DC presents, before trusting ldaps:// | yes |
| `install/15-ad-sync.sh` | Create mailboxes for people in AD, on a timer. **Only ever creates** - never deletes, renames or disables | yes |
| `install/kin-mail-split.sh` | Builds both machines of a split deployment, in the only order that works | yes |
| `install/lib/quota-gate.sh` | Shared seat counter / allow-deny for **new creates only** (sourced by 08, 15 and the console) | - |
| `install/lib/ad-ldap.sh` | Every directory read: password in a 0600 file, never argv; ldaps:// verified before the bind | - |
| `install/05-healthcheck.sh` | Services, listeners, cert, DNS, DKIM, SMTP egress, **both auth paths**, mail flow, open-relay | yes |
| `install/check-zimbra-foss-update.sh` | Compare configured FOSS build vs newest GitHub release | yes |

\* Re-`apply` resets ufw rules and re-arms the dead-man; prefer skip when already verified active.

`01` is safe on any host; it changes nothing.

---

## Configuration

Asked once on first run:

| Prompt | Default |
|---|---|
| Mail domain | derived from hostname |
| Mail hostname (FQDN) | `mail.<domain>` |
| Server LAN address | auto-detected |
| Network interface | auto-detected |
| Timezone | from the system |
| Upstream DNS | `1.1.1.1`, `8.8.8.8` |
| Customer-internal zone (AD) | none |
| Zimbra FOSS release | auto-detected from GitHub |
| Zimbra admin password | (required) |
| Active Directory hybrid auth | skipped → local auth only |
| Let's Encrypt contact | `admin@<domain>` |
| TLS method | `1` Cloudflare / `2` manual DNS-01 / `3` customer-provided |
| External test mailbox | empty |
| Contracted mailbox seats | `PLACEHOLDER_UNSET` until operator confirms |
| Admin source IPs/CIDRs (`KIN_ADMIN_IPS`) | empty until set - required for stage 10 |
| Install Z-Push in full install (`ZPUSH_ENABLED`) | `yes` |

Cloudflare API token is requested by `04` only when `TLS_METHOD=cloudflare`, and is
never written to a log or echoed to the terminal.

**Manual DNS-01 (`TLS_METHOD=manual`) does not auto-renew via cron** the way Cloudflare
mode can. `certbot --manual` needs an operator for each issue/renew unless a
provider-specific `--manual-auth-hook` is added later. Plan renewals before expiry.

---

## Design decisions worth knowing

### SMTP is tested with a banner grab, never a TCP connect

Inline security appliances complete the TCP handshake and then silently discard
the session. `nc -zv` reports the port as open while nothing can be delivered.
Only a `220` greeting proves a port is usable.

Read a **line**, never a fixed byte count. An earlier revision used
`head -c 100`, which blocks until 100 bytes arrive; a real banner is shorter,
so it hung until timeout and reported a working port as blocked.

### TLS uses DNS-01 (or a customer certificate)

The mail host often sits behind NAT with no inbound path, so HTTP-01 fails.

- **Cloudflare DNS-01** - API token automates the TXT record and renewal.
- **Manual DNS-01** - certbot prints TXT name/value; the operator creates it in
  the zone panel. Renewal is not automatic in this mode.
- **Customer cert** - skips certbot; documents install with `zmcertmgr`.

### The renewal hook is executed, not merely installed

`certbot renew --dry-run` exercises ACME and DNS but **skips deploy hooks**. A
renewal that succeeds while Zimbra keeps the old certificate is a silent outage
later, so `04` runs the hook once for real and verifies the served certificate.

### The sending address is measured by asking a mail server

A host with more than one uplink can egress HTTP and SMTP from different
addresses. Reading `ifconfig.me` answers the wrong question. Deliverability is
decided by the address a receiving mail server sees (Gmail `EHLO` response).

### Inbound port 25 cannot be tested from an arbitrary host

Most ISPs block outbound port 25. A machine that cannot reach port 25 anywhere
will report a correct mail server as unreachable. Prefer watching the server
(`tcpdump` or Postfix logs) while a real mail server connects.

### PTR is not in your DNS provider

Reverse DNS lives in the `in-addr.arpa` zone of whoever owns the IP block,
normally the ISP. Receivers require forward-confirmed reverse DNS; the health
check walks that full chain.

### Long-lived processes cache DNS failures

dnsmasq, Amavis, and opendkim cache negative answers. A DKIM record published
after those services started looks missing until they are restarted. `05`
restarts them before asserting DNS.

---

## Hard-won behaviour

Every one of these is something that broke a real deployment once and now
cannot break another. They are listed because they are the reason an install
works the second time as well as the first, not because they are interesting.

- Repository and keyring setup that survives a mirror being slow or moved.
- Prompt order and timezone handling that cannot leave a half-answered config.
- `apt` waits for the dpkg lock instead of failing while unattended-upgrades
  holds it.
- Zimbra telemetry is answered rather than left to block the install.
- DNS is checked before it is depended on, and the failure names the record.
- Stages that cannot safely re-run refuse; stages that can are idempotent.
- Every privileged action goes through a fixed allow-list, never a shell.

## Source of the Zimbra build

Zimbra no longer publishes Open Source Edition binaries for version 10. These
scripts install the community build from
[`maldua/zimbra-foss`](https://github.com/maldua/zimbra-foss) (BTACTIC, GPL-2.0),
compiled from Zimbra's published open source sources. SHA-256 is verified before
install; mismatch aborts.

**Disclosure required before commercial use.** Builds may lag Zimbra security
fixes by up to two months (embargo). No warranty; not official Zimbra binaries.
See **Patch monitoring** before offering the platform to a paying customer.

**Checklist - repository visibility.** This deploy repo is intentionally still
**public** (operator decision as of 2026-08-10). Before the first customer
handoff it must be reviewed again: consider making it private and having
`kin-mail.sh` clone via token or deploy key.

---

## Patch monitoring

| Item | Decision |
|---|---|
| **Owner** | Technical lead for KIN Mail (customer deployment owner), with KIN Sight on-call backup when that person is unavailable |
| **Cadence** | Weekly during PoC / first rollouts; monthly in steady state. Align with the Monday ops review |
| **What to check** | Newest matching `UBUNTU22_64` / `UBUNTU24_64` artefact on [maldua/zimbra-foss releases](https://github.com/maldua/zimbra-foss/releases) vs `ZCS_VERSION` in `/etc/kin-mail/config` |
| **How** | `./install/check-zimbra-foss-update.sh` on a host with outbound HTTPS. Exit `1` means a newer tag needs triage. Optional cron: `0 9 * * 1` with notification on non-zero exit |
| **When an update appears** | Read Maldua notes + Zimbra advisories; schedule maintenance; re-run install on staging or Host B before Host A; update `ZCS_VERSION` via `sudo ./install/00-config.sh --reset` or edited config; note residual lag risk |

This does not remove FOSS rebuild lag; it makes the lag visible and owned.

---

### Storage: one disk or two, and growing either

A KIN Mail appliance can be built with a single disk or with a second, blank
disk for mail data. Both are supported and they behave differently, so the
console says which one it is looking at.

With **one disk**, `/opt/zimbra` is a directory on the root filesystem. The two
storage figures on the Monitoring tab are then the same number, which is
correct and reads like a fault, so the card says so explicitly.

With **two disks**, `02-prepare-os.sh` hands a unique blank spare to
`install/lib/prepare-zimbra-data-disk.sh`, which partitions it as a large
`zimbra-data` partition followed by a 256 MiB `drbd-meta` partition at the very
end. The meta partition is unused on a single node and exists so the appliance
can later become a replicated pair without being repartitioned. New disks are
encrypted by default (`KIN_ENCRYPT_ZIMBRA_DATA_DISK=1`). No spare disk, or more
than one, means Zimbra installs on the OS volume and the installer says so.

After enlarging a disk in the hypervisor, the space is added from the console:
Cluster, then Monitoring, then Extend a disk. It checks first, prints what it
would do, and only then offers to do it. `install/lib/grow-disk.sh` does the
work and is deliberately more refusal than action: it will not touch a
partition that has another partition behind it, will not act on replicated or
RAID storage, cannot shrink anything, and has no force flag.

One consequence worth knowing before sizing a machine: on a two-disk appliance
the mail partition is not the last one on its disk, because the meta partition
sits at the end. Space added to that disk therefore lands behind the meta
partition and cannot be given to the mail data without moving it, which is not
done unattended. Add space to the system disk, or plan the data disk large
enough at build time.

---

## Extending storage

Space added to a virtual disk does not reach the filesystem on its own: a
bigger disk is not a bigger partition, and a bigger partition is not a bigger
filesystem. On an encrypted mail volume that is four tools deep, each able to
destroy the data if given the wrong argument.

**Monitoring &rsaquo; Extend a disk** does the whole chain, or refuses and says
why. It asks the kernel to re-read the disk first, reports how much space it
found, and changes nothing until you have read that and pressed the button.

It will not shrink anything, will not move a partition, will not touch
replicated storage, and has no force flag.

### The reserved partition

The mail disk is laid out as a large data partition followed by a small,
unformatted one held back for a future second server. On a single-server
appliance that reserved partition is never used, and because it sits *after*
the data partition it makes the mail disk impossible to extend: new space lands
behind it, out of reach.

The console offers to free it, as a separate and clearly-marked action. Before
deleting anything it checks that the partition is the last one on the disk,
that it carries no filesystem, volume group or replication data, that nothing
is mounted on or stacked above it, that neither `/etc/fstab` nor
`/etc/crypttab` names it, and that the host has no DRBD configured at all. Any
one of those checks failing stops it.

Freeing it means a high-availability pair built later would need a small
separate disk. The console says so before you confirm.

---

## Deliberately out of scope

Everything here is something this product **cannot** do from inside the
server, not something it has chosen not to try.

> Earlier versions of this file said "no host firewall is enabled; perimeter
> control belongs on the network device". That has not been true since
> `10-host-firewall.sh` joined the pipeline: the appliance configures `ufw`
> itself, asks before applying it, and arms a dead-man's switch so a rule that
> locks the operator out reverts on its own. The line is corrected rather than
> quietly deleted, because anyone who read it drew a conclusion about this
> product's security posture that was wrong.

These scripts cannot resolve anything outside the server:

- outbound SMTP permission (TCP 25) from the mail gateway to the internet
- installing the Proxmox Mail Gateway VM itself, and its own hardening
- a stable public address with inbound DNAT to port 25
- a PTR record matching the mail hostname
- a policy route pinning the host to a single uplink

**Port 25 cannot be relocated.** Sending mail servers read the MX and connect
to port 25. Other services may share the public address on different ports.

**High availability is not in this release.** The active-passive Pacemaker /
DRBD / SBD cluster exists in the tree and has its own runbook
([`HA-RUNBOOK.md`](HA-RUNBOOK.md)), but it is hidden in the console and nothing
here promises it. A split deployment separates the mail store from the
perimeter; it does **not** survive losing a machine.

Backup repository automation remains out of scope here; see `backup/`.

---

## Requirements

**Per mail machine**

- Ubuntu Server 22.04 or 24.04 LTS, 64-bit, minimal install
- 2 vCPU / 8 GB RAM minimum; 4 vCPU / 16 GB recommended
- 40 GB free minimum; a dedicated blank volume for `/opt/zimbra` is preferred,
  and is picked up automatically when exactly one unpartitioned spare is present
- A static LAN address, and root or sudo access

**For a split deployment**, two such machines: the edge needs no large mail
volume, and the mailbox gets the storage. They must reach each other on the
LAN; the mailbox needs no public address at all, and is firewalled to accept
only the edge.

**For the gateway**, a Proxmox Mail Gateway VM reachable on TCP 8006 and 25,
and its `root@pam` password. It does not need to be on the same host.

---

Copyright © 2026 PT Karya Informasi Nusantara. All rights reserved.
See [`NOTICE`](NOTICE) for terms.
