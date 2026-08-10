# KIN Mail — Zimbra FOSS deployment scripts

Repeatable deployment of Zimbra Collaboration 10 (FOSS build) on Ubuntu Server
22.04 LTS, with every failure we hit during the first live deployment already
handled in code.

The scripts ask questions **once**. Answers are stored in `/etc/kin-mail/config`
(mode `0600`) and reused by every later stage.

> **Internal tooling.** Built and maintained by PT Karya Informasi Nusantara for
> its own deployments. Prompts and inline guidance are in Indonesian; this
> README is in English by GitHub convention.

---

## Quick start

```bash
chmod +x *.sh

sudo ./01-preflight.sh        # can this site host mail at all?
sudo ./02-prepare-os.sh       # hostname, hosts, resolver, dependencies
sudo ./03-install-zimbra.sh   # download, verify checksum, drive the installer
sudo ./04-tls-dkim.sh         # Let's Encrypt via DNS-01, plus DKIM key
#                               → publish the DKIM record it prints
sudo ./05-healthcheck.sh      # acceptance tests and status summary
```

Reconfigure at any time: `sudo ./00-config.sh --reset`

---

## What each stage does

| Script | Purpose | Idempotent |
|---|---|---|
| `00-config.sh` | Shared library and first-run wizard. Not run directly. | — |
| `01-preflight.sh` | Sizing, OS, conflicting services, outbound access, **SMTP egress**, DNS state, PTR. Read-only. | yes |
| `02-prepare-os.sh` | Hostname, `/etc/hosts`, dnsmasq split-horizon resolver, dependencies. | yes |
| `03-install-zimbra.sh` | Fetches the FOSS build, verifies SHA-256, drives the interactive installer inside tmux. | yes |
| `04-tls-dkim.sh` | Certificate via DNS-01, deploy hook, **hook executed for real**, DKIM generation. | yes |
| `05-healthcheck.sh` | Services, listeners, certificate, DNS, DKIM, SMTP egress, internal mail flow, open-relay check. | yes |

`01` is safe to run on any host — it changes nothing.

---

## Configuration

Asked once, on first run:

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
| Zimbra admin password | — |
| Let's Encrypt contact | `admin@<domain>` |
| External test mailbox | empty |

The Cloudflare API token is requested separately by `04` and is never written to
a log or echoed to the terminal.

---

## Design decisions worth knowing

### SMTP is tested with a banner grab, never a TCP connect

Inline security appliances complete the TCP handshake and then silently discard
the session. `nc -zv` reports the port as open while nothing can be delivered
through it. Only a `220` greeting proves a port is usable, so that is what the
scripts check.

Read a **line**, never a fixed byte count. An earlier revision used
`head -c 100`, which blocks until 100 bytes arrive; a real banner is shorter,
so it hung until the timeout and reported a perfectly working port as blocked.
That inverted the diagnosis of an entire deployment for half a day.

### TLS uses the DNS-01 challenge

The mail host normally sits behind NAT with no inbound path, so the HTTP-01
challenge cannot complete. DNS-01 via the Cloudflare API needs no inbound
connectivity and renews unattended.

### The renewal hook is executed, not merely installed

`certbot renew --dry-run` exercises ACME and DNS but **skips deploy hooks**. A
renewal that succeeds while Zimbra keeps serving the old certificate is a silent
outage ninety days later, so `04` runs the hook once in earnest and verifies the
certificate is still served afterwards.

### The sending address is measured by asking a mail server

A host with more than one uplink can egress HTTP and SMTP from different
addresses. Reading `ifconfig.me` therefore answers the wrong question: in the
first live deployment it reported two addresses that alternated, while SMTP
consistently left from a third. Deliverability is decided entirely by the
address a receiving mail server sees, so that is what the scripts measure —
from Gmail's own `EHLO` response.

### Inbound port 25 cannot be tested from an arbitrary host

Most ISPs block outbound port 25 for their customers. A machine that cannot
reach port 25 anywhere will report a correctly published mail server as
unreachable, and the result looks identical to a firewall misconfiguration.

Test inbound instead by watching the server — `tcpdump` on the interface, or
Postfix's own log — while a real mail server connects. The decisive evidence is
a delivery from a known sender, not a port probe.

### PTR is not in your DNS provider

Reverse DNS lives in the `in-addr.arpa` zone belonging to whoever owns the IP
block, normally the ISP. It cannot be created in Cloudflare or any other
provider hosting the forward zone.

Receivers run a forward-confirmed check: PTR of the sending address gives a
hostname, that hostname's A record must resolve back to the same address. The
health check walks that whole chain rather than merely noting that a PTR
exists.

### Long-lived processes cache DNS failures

dnsmasq, Amavis and opendkim all cache negative answers. A DKIM record published
after those services started reads as missing until they are restarted, which
looks exactly like a wrong record. `05` restarts them before asserting anything
about DNS.

---

## Installer traps handled in code

Each of these cost real time during the first deployment:

- **Zimbra's package repository must be accepted.** The `*-components` packages
  (OpenLDAP, Postfix, Jetty, nginx, memcached) exist only there. Declining
  aborts the installation with a misleading "packages missing" error.
- **`zimbra-memcached` appears as a separate prompt** when sourced from the
  repository, shifting the prompt order. The driver matches on prompt text, not
  position.
- **`resolvconf` hijacks `/etc/resolv.conf` mid-installation.** It arrives as a
  dependency of `zimbra-mta-components` and repoints the resolver at the
  systemd-resolved stub, which has been disabled — DNS dies silently.
- **`/etc/resolv.conf` is a managed symlink** and must be replaced with
  `ln -sf`, never deleted.
- **The default mail domain follows the hostname**, producing addresses of the
  form `user@mail.example.com`. It is set explicitly instead.
- **Zimbra's timezone list has no `Asia/Jakarta`.** `Asia/Bangkok` is the same
  UTC+7 with no DST and is the correct equivalent for WIB.
- **`zmsetup.pl` reads resolver configuration once at startup.** Fixing DNS
  afterwards has no effect; the process must be restarted.
- **The telemetry prompt transmits the administrator email address** to Zimbra.
  Answered No.
- **Split DNS delegation** across two providers makes mail fail at random,
  because resolvers pick any nameserver from the set. `01` and `05` detect it.

---

## Source of the Zimbra build

Zimbra no longer publishes Open Source Edition binaries for version 10. These
scripts install the community build from
[`maldua/zimbra-foss`](https://github.com/maldua/zimbra-foss) (BTACTIC,
GPL-2.0), compiled from Zimbra's own published open source sources. The SHA-256
checksum is verified before installation and the install aborts on mismatch.

**Disclosure required before commercial use.** That project states its builds may
lag Zimbra security fixes by up to two months, because those fixes are embargoed
before public release. The builds carry no warranty and are not official Zimbra
binaries. A patch monitoring process should be defined before the platform is
offered to a paying customer.

---

## Deliberately out of scope

No host firewall is enabled — perimeter control belongs on the network device.
If `ufw` is wanted, allow port 22 first so the SSH session is not cut.

These scripts cannot resolve anything that lives outside the server:

- outbound SMTP permission (TCP 25 to any host, or 587 to one nominated relay)
- a stable public address with inbound DNAT to port 25
- a PTR record matching the mail hostname
- a policy route pinning the host to a single uplink

**Port 25 cannot be relocated.** Sending mail servers read the MX record and
connect to port 25; there is no mechanism to direct them elsewhere. Other
services may share the same public address as long as the port numbers differ.

Later phases, not covered here: Active Directory LDAP authentication, backup,
DRBD replication, and clustering.

---

## Requirements

- Ubuntu Server 22.04 LTS, 64-bit, minimal install
- 2 vCPU and 8 GB RAM minimum; 4 vCPU and 16 GB recommended
- 40 GB free minimum; a dedicated volume for `/opt/zimbra` is preferred
- A static LAN address, and root or sudo access

---

Copyright © 2026 PT Karya Informasi Nusantara. All rights reserved.
See [`NOTICE`](NOTICE) for terms.
