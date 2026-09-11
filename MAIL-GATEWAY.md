# KIN Mail — Proxmox Mail Gateway in front of Zimbra

Status: design accepted for release 0.1.10. Not yet exercised on real hardware.
Everything below that says "verified" means verified in tests, not on a live
appliance. Nothing here has passed a production smoke test.

---

## 1. Why this exists

Until 0.1.9 KIN Mail was a single all-in-one appliance. Zimbra's own MTA held
port 25 to the internet and did its own filtering with amavis, SpamAssassin,
ClamAV and opendkim. That works, but it means the machine holding every
mailbox is also the machine an attacker reaches first.

From 0.1.10 the product places a **Proxmox Mail Gateway (PMG)** in front. PMG
becomes the MX. Zimbra stops being reachable from the internet on port 25. An
attacker who breaks the SMTP surface lands on a filtering appliance that holds
no mailboxes, no LDAP, and no customer data.

This is a security decision made by the operator's management, and the console
treats it as mandatory: from 0.1.10 an appliance with no gateway configured is
reported as **not compliant**, loudly, on the dashboard and in the healthcheck.

---

## 2. What KIN Mail does and does not build

**KIN Mail does not install PMG.** It cannot. PMG is a Debian-based appliance;
KIN Mail's mail node is Ubuntu 22.04 with Zimbra on it. PMG has to be its own
virtual machine, installed by the operator from the official ISO (or on top of
a plain Debian install using Proxmox's apt repository).

**KIN Mail configures both sides of the link and then proves it works.** That
is the part that is error-prone by hand, and that is the part we automate:

| Side | What KIN Mail sets | How |
| --- | --- | --- |
| PMG | relay host, relay port, relay domains, transport map, trusted networks, spam/virus policy, TLS, banner | PMG REST API over HTTPS :8006 |
| Zimbra | `zimbraMtaRelayHost`, `zimbraMtaMyNetworks`, DKIM stays local | `zmprov` |
| Mail node firewall | port 25 inbound restricted to the gateway | `ufw` in stage 10 |
| Console | Mail Gateway page, status, compliance gate | FastAPI + privhelper |

The operator's only manual steps are: install the PMG VM, give it an IP, and
create an API token. Everything after that is one button.

---

## 3. Mail flow

### Inbound (internet → mailbox)

```
    Internet
       |  MX record for example.com -> pmg.example.com
       v
  PMG  :25   (ext_port)
       |  greylist, SPF, DNSBL, SpamAssassin, ClamAV, rule database
       |  transport map: example.com -> <zimbra-ip>:25
       v
 Zimbra :25  (accepts ONLY from the gateway IP - ufw + mynetworks)
       |
       v
   mailbox
```

### Outbound (mailbox → internet)

```
   mailbox
       |
 Zimbra :25 -> zimbraMtaRelayHost = <pmg-ip>:26
       |  opendkim has already signed here. PMG must not re-sign.
       v
  PMG  :26   (int_port - the port PMG reserves for the internal mail server)
       |  outbound rule set, TLS policy
       v
    Internet
```

### Internal mail (mailbox → mailbox, same domain)

Never touches the gateway. Zimbra delivers it locally because the domain is a
local domain on Zimbra. **This is correct and must stay that way.** Forcing
internal mail through PMG is the classic way to build a mail loop, and the
Proxmox forum says so explicitly.

---

## 4. Ports

| Port | On | From | Purpose |
| --- | --- | --- | --- |
| 25 | PMG | internet | inbound mail (`ext_port`) |
| 26 | PMG | Zimbra only | outbound relay from the mail server (`int_port`) |
| 8006 | PMG | console/admin only | web UI and REST API |
| 25 | Zimbra | **PMG only** | filtered inbound mail |
| 587, 465 | Zimbra | internet | user submission — unchanged, still Zimbra |
| 143/993/110/995 | Zimbra | internet | IMAP/POP — unchanged |

Note that submission stays on Zimbra. PMG is not an endpoint for mail clients;
clients authenticate to the mail server, and the mail server relays through
PMG. Pointing Outlook at PMG does not work and is not supported.

---

## 5. DNS

Three records have to change, and KIN Mail cannot change them — they live at
the registrar. The console prints exactly what to set and then verifies it.

1. **A record** for the gateway: `pmg.example.com` → gateway IP.
2. **MX record** for the domain: `example.com` MX 10 `pmg.example.com`.
   The old MX pointing at the mail host is removed.
3. **SPF** must now authorise the gateway, because outbound mail leaves from
   the gateway's IP, not the mail host's:
   `v=spf1 ip4:<pmg-ip> ~all`
4. **rDNS/PTR** for the gateway IP should resolve to `pmg.example.com`, or a
   good share of the internet will refuse the mail.

DKIM and DMARC records are unchanged. DKIM signing stays on Zimbra.

---

## 6. PMG settings KIN Mail writes

All via the REST API, which writes the config files and reloads the services —
so we never hand-edit `/etc/pmg/*` and never have to call `pmgconfig sync`
ourselves.

| API path | Field | Value | Why |
| --- | --- | --- | --- |
| `/config/mail` | `relay` | mail node IP | where filtered mail goes |
| `/config/mail` | `relayport` | `25` | |
| `/config/mail` | `relaynomx` | `1` | deliver to that host, do not look up its MX — the MX now points back at PMG, so an MX lookup here is a loop |
| `/config/mail` | `int_port` | `26` | port the mail node relays out on |
| `/config/mail` | `ext_port` | `25` | inbound from the internet |
| `/config/mail` | `verifyreceivers` | `550` | reject mail for addresses that do not exist rather than accepting and bouncing (backscatter) |
| `/config/mail` | `rejectunknown` | `1` | refuse senders whose client hostname does not resolve |
| `/config/mail` | `greylist` | `1` | default on; cheap and effective |
| `/config/mail` | `spf` | `1` | |
| `/config/mail` | `dnsbl_sites` | `zen.spamhaus.org` | |
| `/config/mail` | `tls` | `1` | opportunistic TLS outbound |
| `/config/mail` | `banner` | `ESMTP KIN Mail Gateway` | do not advertise the product version |
| `/config/mail` | `hide_received` | `1` | do not leak the internal IP of the mail node in headers |
| `/config/mail` | `before_queue_filtering` | `0` | after-queue: PMG accepts then filters. Before-queue rejects during the SMTP session, which is stricter but drops mail under load. Deliberately off. |
| `/config/domains` | relay domain | `example.com` | PMG only accepts mail for domains listed here |
| `/config/transport` | domain/host/port | `example.com` → mail node `:25` | |
| `/config/mynetworks` | cidr | `<mail-node-ip>/32` | lets the mail node relay out through :26 |
| `/config/admin` | `dkim_sign` | `0` | **must stay off** — Zimbra already signed |

`relaynomx=1` and `dkim_sign=0` are the two that silently break mail if they
are wrong, so both are asserted in `verify`, not just written in `apply`.

### Deliberately not set by KIN Mail

- **Quarantine lifetimes, rule database, custom SpamAssassin scores.** Site
  policy. The operator owns these in the PMG UI.
- **PMG clustering.** Out of scope, exactly like Zimbra HA is out of scope.
- **LDAP recipient verification against Zimbra.** A good future feature — it
  would let PMG reject unknown recipients using Zimbra's own directory — but
  it needs a bind account and its own failure story. `verifyreceivers=550`
  gets most of the benefit via SMTP callout. Tracked for 0.1.11.

---

## 7. Zimbra settings KIN Mail writes

```sh
zmprov ms <mailhost> zimbraMtaRelayHost '<pmg-ip>:26'
zmprov mcf zimbraMtaMyNetworks '127.0.0.0/8 <lan>/24 <pmg-ip>/32'
```

`zimbraMtaMyNetworks` gains the gateway so PMG can hand mail in. It is added,
never replaced wholesale, and `relay_scope_too_wide()` from
`mail-surface-hardening.sh` still guards the result — putting a gateway in
front is not a reason to become an open relay.

Zimbra's own amavis / SpamAssassin / ClamAV stay **enabled**. That is a
deliberate disagreement with the phrase "must not use the native one": the
gateway is mandatory as the MX and the outbound relay, but disabling Zimbra's
filtering as well would remove defence in depth, break Zimbra's own DKIM
signing path, and leave internal mail — which never reaches PMG — unscanned.
Two layers cost some CPU and nothing else.

If management wants the native layer off anyway, that is a policy decision and
**it is not implemented**. Nothing in this release turns Zimbra's filtering off,
and nothing should start doing so without the consequences above being put in
front of whoever asked for it.

---

## 8. Security posture

- **The mail node stops answering port 25 from the internet.** Stage 10 now
  narrows the port-25 rule to the gateway address once a gateway is
  configured. This is the actual point of the exercise.
- **The API credential is a PMG API token, not root's password.** A token can
  be revoked in the PMG UI without changing any password, and its privileges
  can be narrowed. Ticket auth with `root@pam` is supported as a fallback for
  older PMG versions, and the console says which one is in use.
- **The token secret never reaches the browser.** It is held in the existing
  Fernet vault at `/var/lib/kin-mail-privhelper/`, readable only by
  privhelperd, exactly like the host provisioning passwords. API responses
  return `configured: true` and nothing else.
- **PMG's certificate is pinned, not ignored.** A fresh PMG serves a
  self-signed certificate on :8006. `curl -k` would make every later
  connection trust anything. Instead the first connection records the SHA-256
  fingerprint, the operator confirms it, and every connection after that
  verifies against it. If the fingerprint changes the connection fails loudly
  rather than continuing. Supplying a real CA bundle switches it to ordinary
  verification.
- **PMG's own hardening** is the operator's, and the console links a checklist:
  restrict :8006 to admin IPs, disable root SSH password login, keep
  `pmg-no-subscription` updated, set a real certificate.

---

## 9. Failure modes and what we do about them

| Failure | Symptom | Guard |
| --- | --- | --- |
| MX moved to PMG before PMG has the relay domain | inbound mail rejected "relay access denied" | `apply` writes the relay domain **before** anything else and `verify` asserts it |
| `relaynomx=0` with MX already moved | mail loops between PMG and itself until it gives up | forced to `1`, asserted in `verify` |
| Domain not local on Zimbra | Zimbra relays back to PMG — loop | `verify` checks the domain is a local Zimbra domain |
| Gateway IP missing from `mynetworks` on Zimbra | PMG cannot deliver; every message deferred | asserted in `verify` |
| Mail node IP missing from PMG `mynetworks` | outbound mail rejected at :26 | asserted in `verify` |
| Double DKIM signature | some receivers fail DMARC | PMG `dkim_sign` forced to `0`, asserted |
| SPF not updated | outbound marked as spam | `verify` resolves the SPF record and warns with the exact string to set |
| Gateway VM down | all inbound mail queues at the sender; outbound queues on Zimbra | monitoring alert; Zimbra's queue is visible on the Reports tab. **Single gateway is a single point of failure — this is accepted for 0.1.10 and stated plainly.** |
| Operator reverts | | `mail-gateway.sh revert` restores the direct-MX configuration and prints the DNS change needed |

---

## 10. Rollback

`mail-gateway.sh revert` un-does the Zimbra side only — it clears
`zimbraMtaRelayHost` and removes the gateway from `mynetworks`. It does not
touch PMG, because leaving PMG configured is harmless and re-applying is then
instant. The operator still has to move the MX record back. The console prints
that instruction; it cannot do it.

---

## 11. Operating pipeline

```
 0. Operator installs the PMG VM from ISO, sets a static IP.
 1. Operator creates an API token in PMG:
       Configuration > User Management > API Tokens
    or:  pmgsh create /access/users/root@pam/token/kinmail
 2. Console > Mail Gateway > Connect
       host, port 8006, token id, token secret
       -> probe: reachability, auth, version, certificate fingerprint
 3. Console shows the plan. Nothing has changed yet.
 4. Operator presses Apply.
       -> PMG configured, Zimbra configured, firewall narrowed
 5. Console prints the DNS changes. Operator makes them.
 6. Console > Mail Gateway > Verify
       -> end-to-end assertions, including a real test message if asked
 7. Healthcheck and dashboard now report the appliance as compliant.
```

Steps 2 through 4 are one privhelper command each and all of them are
re-runnable: applying twice is a no-op, and a half-finished apply is safe to
repeat.

---

## 12. References

- Proxmox Mail Gateway administration guide — <https://pmg.proxmox.com/pmg-docs/pmg-admin-guide.html>
- `pmg.conf(5)` option reference — <https://pmg.proxmox.com/pmg-docs/pmg.conf.5.html>
- `pmgsh(1)` — <https://pmg.proxmox.com/pmg-docs/pmgsh.1.html>
- Configuration management chapter — <https://pmg.proxmox.com/pmg-docs/chapter-pmgconfig.html>
- PMG with Zimbra, Proxmox forum — <https://forum.proxmox.com/threads/pmg-with-zimbra-server.72849/>
- Konfigurasi Proxmox Mail Gateway, hanara.id — <https://hanara.id/articles/konfigurasi-proxmox-mail-gateway-pmg-21>
