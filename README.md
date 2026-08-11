# KIN Mail

<p align="center">
  <a href=".github/workflows/checks.yml"><img src="https://github.com/azana-nisaa/kin-mail/actions/workflows/checks.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Ubuntu-22.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white" alt="Ubuntu 22.04 | 24.04">
  <img src="https://img.shields.io/badge/Zimbra-FOSS%2010-0A66C2" alt="Zimbra FOSS 10">
  <a href="NOTICE"><img src="https://img.shields.io/badge/License-Proprietary-lightgrey" alt="Proprietary"></a>
  <img src="https://img.shields.io/badge/Maintainer-PT%20Karya%20Informasi%20Nusantara-1B4F72" alt="KIN">
</p>

Repeatable Zimbra Collaboration 10 (FOSS) deployment for Ubuntu Server, plus an
active-passive HA lab stack (Pacemaker / DRBD / SBD). Built by
**PT Karya Informasi Nusantara** for its own customer work. Installer prompts,
Ansible task labels, and operator guidance are in English.

<table>
<tr><td><b>One-shot install</b></td><td>Menu-driven <code>install/kin-mail.sh</code> runs preflight through healthcheck, or any single stage.</td></tr>
<tr><td><b>Answers once</b></td><td>Config lives in <code>/etc/kin-mail/config</code> (mode <code>0600</code>) and is reused by every later stage.</td></tr>
<tr><td><b>Failures already encoded</b></td><td>Installer traps from the first live deployment (repos, resolvconf, prompt order, timezone, telemetry) are handled in code.</td></tr>
<tr><td><b>TLS that fits NAT</b></td><td>Cloudflare DNS-01, manual DNS-01, or customer-supplied cert via <code>zmcertmgr</code>.</td></tr>
<tr><td><b>Optional AD hybrid auth</b></td><td>LDAP bind to Active Directory with local password fallback when AD is skipped or unreachable.</td></tr>
<tr><td><b>HA runbook</b></td><td>Operational Pacemaker/DRBD/SBD procedures in <a href="HA-RUNBOOK.md"><code>HA-RUNBOOK.md</code></a>; Ansible ports start under <code>ansible/</code>.</td></tr>
</table>

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
sudo ./10-host-firewall.sh apply   # ufw — needs KIN_ADMIN_IPS; dead-man armed
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
| `ansible/` | Phase 2 HA port (qnetd / qdevice first; dry-run only on live lab) |
| `HA-RUNBOOK.md` | Pacemaker / DRBD / SBD operator runbook |
| `.github/workflows/` | Shell syntax, ShellCheck, hygiene gates |
| `NOTICE` | Proprietary terms |

---

## Install stages

| Script | Purpose | Idempotent |
|---|---|---|
| `install/kin-mail.sh` | Bootstrap menu: ensure scripts present, full install or one stage | yes |
| `install/00-config.sh` | Shared library and first-run wizard (not a normal entry point) | — |
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
| `install/lib/quota-gate.sh` | Shared seat counter / allow-deny for **new creates only** (sourced by 08 + future console) | — |
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
| Admin source IPs/CIDRs (`KIN_ADMIN_IPS`) | empty until set — required for stage 10 |
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

- **Cloudflare DNS-01** — API token automates the TXT record and renewal.
- **Manual DNS-01** — certbot prints TXT name/value; the operator creates it in
  the zone panel. Renewal is not automatic in this mode.
- **Customer cert** — skips certbot; documents install with `zmcertmgr`.

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

## Installer traps handled in code

- **Zimbra's package repository must be accepted.** Declining aborts with a
  misleading "packages missing" error.
- **`zimbra-memcached` appears as a separate prompt** when sourced from the
  repository, shifting prompt order. The driver matches on prompt text, not position.
- **`resolvconf` hijacks `/etc/resolv.conf` mid-installation** via
  `zimbra-mta-components` and can kill DNS silently.
- **`/etc/resolv.conf` is a managed symlink** and must be replaced with `ln -sf`.
- **The default mail domain follows the hostname** (`user@mail.example.com`);
  it is set explicitly instead.
- **Zimbra's timezone list has no `Asia/Jakarta`.** `Asia/Bangkok` is the UTC+7
  equivalent without DST.
- **`zmsetup.pl` reads resolver configuration once at startup.** Fixing DNS
  afterwards requires restarting the process.
- **The telemetry prompt transmits the administrator email.** Answered No.
- **Split DNS delegation** across two providers makes mail fail at random;
  `01` and `05` detect it.

---

## Source of the Zimbra build

Zimbra no longer publishes Open Source Edition binaries for version 10. These
scripts install the community build from
[`maldua/zimbra-foss`](https://github.com/maldua/zimbra-foss) (BTACTIC, GPL-2.0),
compiled from Zimbra's published open source sources. SHA-256 is verified before
install; mismatch aborts.

**Disclosure required before commercial use.** Builds may lag Zimbra security
fixes by up to two months (embargo). No warranty; not official Zimbra binaries.
See **Patch monitoring** before offering the platform to a paying customer.

**Checklist — repository visibility.** This deploy repo is intentionally still
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

## Deliberately out of scope

No host firewall is enabled; perimeter control belongs on the network device.
If `ufw` is wanted, allow port 22 first so SSH is not cut.

These scripts cannot resolve anything outside the server:

- outbound SMTP permission (TCP 25, or 587 to a nominated relay)
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
- 40 GB free minimum; a dedicated volume for `/opt/zimbra` is preferred
- A static LAN address, and root or sudo access

---

Copyright © 2026 PT Karya Informasi Nusantara. All rights reserved.
See [`NOTICE`](NOTICE) for terms.
