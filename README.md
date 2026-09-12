# KIN Mail

<p align="center">
  <a href=".github/workflows/checks.yml"><img src="https://github.com/azana-nisaa/kin-mail/actions/workflows/checks.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/release-0.1.10-1B4F72" alt="Release 0.1.10">
  <img src="https://img.shields.io/badge/Ubuntu-22.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white" alt="Ubuntu 22.04 | 24.04">
  <img src="https://img.shields.io/badge/Zimbra-FOSS%2010-0A66C2" alt="Zimbra FOSS 10">
  <img src="https://img.shields.io/badge/gateway-Proxmox%20Mail%20Gateway-E57000?logo=proxmox&logoColor=white" alt="Proxmox Mail Gateway">
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
    NAT, split DNS, a single uplink or several, Active Directory for logins.
    TLS by Cloudflare DNS-01, manual DNS-01, or your own certificate.
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

> **Scope, stated plainly.** This release deploys **one mail server** with **one
> gateway** in front of it. That is the product. A two-server high-availability
> layout exists in the tree, is not offered in the console, and is not part of
> what 0.1.10 promises.

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

## Quick start

```bash
sudo ./install/kin-mail.sh
```

If stage scripts are missing next to the bootstrap, it clones
[`fernnddn/kin-mail-zimbra`](https://github.com/fernnddn/kin-mail-zimbra) into
`/opt/kin-mail-deploy` (override with `KIN_MAIL_REPO_URL` / `KIN_MAIL_DEPLOY_DIR`)
and continues from `install/` inside that clone. A full local clone uses the
files already on disk.

The menu runs the full pipeline
(`01 → 02 → 03 → 04 → 06 → [07?] → 09 → [10 prompted] → 11 → 05`, stop on first
failure) or one stage at a time. Z-Push (`07`) follows `ZPUSH_ENABLED` from the
wizard. Host firewall (`10`) always asks before applying and keeps the
dead-man's switch.

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
sudo ./05-healthcheck.sh      # acceptance tests and status summary
```

Reconfigure anytime: `sudo ./install/00-config.sh --reset` (also in the menu).

---

## Repository layout

| Path | Role |
|---|---|
| `install/` | Single-node Zimbra FOSS stages + `kin-mail.sh` bootstrap |
| `console/` | Admin console skeleton (FastAPI + React); `console/bootstrap.sh` |
| `ansible/` | Phase 2 HA port (qnetd / qdevice first) plus OS hardening (`playbooks/mail-os-hardening.yml`) |
| `backup/` | Backup repository pull scripts (`kin-mail-backup.sh` / restore); Timeline 3.1-3.5 |
| `HA-RUNBOOK.md` | Pacemaker / DRBD / SBD operator runbook |
| `.github/workflows/` | Shell syntax, ShellCheck, hygiene gates |
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
| `install/lib/quota-gate.sh` | Shared seat counter / allow-deny for **new creates only** (sourced by 08 + future console) | - |
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

No host firewall is enabled; perimeter control belongs on the network device.
If `ufw` is wanted, allow port 22 first so SSH is not cut.

These scripts cannot resolve anything outside the server:

- outbound SMTP permission (TCP 25) from the mail gateway to the internet
- installing the Proxmox Mail Gateway VM itself, and its own hardening
- a stable public address with inbound DNAT to port 25
- a PTR record matching the mail hostname
- a policy route pinning the host to a single uplink

**Port 25 cannot be relocated.** Sending mail servers read the MX and connect
to port 25. Other services may share the public address on different ports.

Hybrid AD auth is covered by `06-hybrid-auth.sh`. For the active-passive
Pacemaker / DRBD / SBD cluster, see [`HA-RUNBOOK.md`](HA-RUNBOOK.md). Backup
repository automation remains out of scope here.

---

## Requirements

- Ubuntu Server 22.04 or 24.04 LTS, 64-bit, minimal install
- 2 vCPU / 8 GB RAM minimum; 4 vCPU / 16 GB recommended
- 40 GB free minimum; a dedicated blank volume for `/opt/zimbra` is preferred,
  and is picked up automatically when exactly one unpartitioned spare is present
- A static LAN address, and root or sudo access

---

Copyright © 2026 PT Karya Informasi Nusantara. All rights reserved.
See [`NOTICE`](NOTICE) for terms.
