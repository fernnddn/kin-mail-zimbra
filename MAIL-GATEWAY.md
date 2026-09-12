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

### The gateway has two identities, and confusing them breaks mail

`GATEWAY_HOST` is the address **this appliance** uses to reach the gateway.
In a normal deployment the two machines share an internal network and that is a
LAN address. `GATEWAY_PUBLIC_HOST` and `GATEWAY_PUBLIC_IP` are what the
**internet** sees, and they are the only values that belong in DNS.

The first version of this used one value for both. A live deployment was
therefore told to publish an SPF record naming its `10.x` LAN address, which
authorises nobody —
every outbound message would have failed SPF. The console now asks for the
public identity separately, refuses a private address for it, refuses a bare IP
as the hostname (an MX record can only name a host), and prints no DNS records
at all until it has both.

1. **A record** for the gateway: `relay.example.com` → the gateway's **public**
   address.
2. **MX record** for the domain: `example.com` MX 10 `relay.example.com`.
   The old MX pointing at the mail host is removed. An MX can only name a
   host — this is why the public hostname is required and not optional.
3. **SPF** must now authorise the gateway's **public** address, because
   outbound mail leaves from there:
   `v=spf1 ip4:<gateway public ip> ~all`
4. **rDNS/PTR** for that address should resolve to `relay.example.com`, or a
   good share of the internet will refuse the mail. Only the ISP can set it.

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
| `/config/mail` | `verifyreceivers` | `450` | ask the mail server whether the recipient exists, and **defer** if not. Not `550`: a permanent refusal throws real mail away for good if Zimbra happens to be restarting, where a deferral costs a delay and loses nothing. |
| `/config/mail` | `greylist` | `0` | **off by default.** It defers the first message from any unseen sender triplet, and PMG's delay is hardcoded at a few minutes — which is exactly why inbound mail felt slow while outbound felt instant in live QA. Every other layer still runs. `GATEWAY_GREYLIST=1` turns it on for a site that wants it. |
| `/config/mail` | `spf` | `1` | |

| `/config/mail` | `tls` | `1` | opportunistic TLS outbound |
| `/config/mail` | `banner` | `ESMTP KIN Mail Gateway` | do not advertise the product version |
| `/config/mail` | `hide_received` | `1` | do not leak the internal IP of the mail node in headers |
| `/config/mail` | `before_queue_filtering` | `0` | after-queue: PMG accepts then filters. Before-queue rejects during the SMTP session, which is stricter but drops mail under load. Deliberately off. |
| `/config/domains` | relay domain | `example.com` | PMG only accepts mail for domains listed here |
| `/config/transport` | domain/host/port | `example.com` → mail node `:25` | |
| `/config/mynetworks` | cidr | `<mail-node-ip>/32` | lets the mail node relay out through :26 |
| `/config/admin` | `dkim_sign` | `0` | **must stay off** — Zimbra already signed |

`relaynomx=1` and `dkim_sign=0` are the two that silently break mail if they
are wrong, so both are read back at the end of `apply` and asserted again in
`verify`. Accepting a write and honouring it are different things, and an
`apply` that claims success on a relay the gateway never kept is worse than
one that fails.

The settings are written in **two requests**, not one. PMG rejects an entire
request when any parameter in it is invalid, so routing (`relay`, `relayport`,
`relaynomx`, `int_port`, `ext_port`) goes first and alone: one unrecognised
policy field cannot take mail delivery down with it. If the policy write is
rejected, `apply` says so and still succeeds, because mail flows without it.

### Deliberately not set by KIN Mail

- **`rejectunknown`.** It refuses senders whose client address has no reverse
  DNS. Plenty of small but entirely legitimate senders have none, PMG's own
  default is off, and an installer has no business imposing that policy on a
  customer's mail. The operator can turn it on in the PMG UI.
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
| Gateway VM down | all inbound mail queues at the sender; outbound queues on Zimbra | the Monitoring tab's **Mail gateway** card probes ports 25 and 26 every minute and says "Not answering"; Zimbra's queue is on the Reports tab. **A single gateway is a single point of failure — accepted for 0.1.10 and stated plainly.** |
| The gateway's settings are accepted but not honoured | `apply` says success; mail is lost at the gateway | `apply` reads `relay`, `relaynomx` and `dkim_sign` back and refuses with `settings-did-not-stick` rather than recording the link as applied |
| Something between the two machines blocks the traffic | every setting is right and no mail moves | `apply` and `verify` open a TCP connection to the gateway on 25 and 26 and say so |
| Operator reverts | | `mail-gateway.sh revert` restores the direct-MX configuration and prints the DNS change needed |

---

## 10. Rollback

`mail-gateway.sh revert` un-does the Zimbra side only — it clears
`zimbraMtaRelayHost` and removes the gateway from `mynetworks`. It does not
touch PMG, because leaving PMG configured is harmless and re-applying is then
instant. The operator still has to move the MX record back. The console prints
that instruction; it cannot do it.

---

## 9a. The tuning applied to the gateway

Applying the link does not only route mail. A gateway that routes correctly and
detects nothing is a hop, not a defence, so `apply` also configures the
detection. Every value below differs from stock PMG and every one is a trade.

### Spam detection (`/config/spam`)

| Option | Stock | Ours | Why |
| --- | --- | --- | --- |
| `use_bayes` | `0` | `1` | A classifier that learns this estate's mail. The single biggest reduction in false positives, which is the failure that costs money — a quarantined invoice is worse than a delivered advert. Needs traffic before it helps. |
| `use_awl` | `0` | `1` | Auto-welcomelist: smooths the score of senders with a history, so one unlucky message from a known correspondent is not quarantined. |
| `use_razor` | `1` | `1` | Collaborative signatures. Kept. |
| `rbl_checks` | `1` | `1` | Kept. |
| `extract_text` | `0` | `1` | Reads text out of PDFs and Office attachments so phishing that hides its payload in a document is scored on what it actually says. |
| `maxspamsize` | 256 KiB | 512 KiB | Anything past this is **not scored at all**. A small limit is not caution. |
| `clamav_heuristic_score` | `3` | `5` | The score a heuristic hit carries — encrypted archives, chiefly. Enough to quarantine without being an automatic block. |

### Virus and attachment detection (`/config/clamav`)

| Option | Stock | Ours | Why |
| --- | --- | --- | --- |
| `archiveblockencrypted` | `0` | `1` | A password-protected archive **cannot be scanned**, and "the password is in the next email" is how a large share of ransomware arrives. Flagged, not deleted: it scores as a heuristic hit and lands in quarantine where the operator can look and release. |
| `archivemaxsize` | 25 MB | 50 MB | Past the limit, files are skipped **unscanned** and delivered. |
| `archivemaxrec` | `5` | `8` | Zip inside zip inside zip is the oldest evasion there is. |
| `archivemaxfiles` | `1000` | `5000` | A zip bomb of 1001 small files would otherwise walk past the scanner. |
| `maxscansize` | 100 MB | 150 MB | Same reasoning as `archivemaxsize`. |

The cost of all five is CPU. The cost of the stock values is mail that arrives
unscanned without anybody being told.

### Quarantine (`/config/spamquar`, `/config/virusquar`)

| Option | Stock | Ours | Why |
| --- | --- | --- | --- |
| spam `lifetime` | 7 days | 14 days | Where a false positive waits to be noticed. A week does not cover somebody's holiday. |
| virus `lifetime` | 7 days | 30 days | Evidence, and evidence is cheap to keep. |
| `allowhrefs` | `1` | `0` | A quarantine page that renders a phishing link as a clickable link attacks the person reviewing it. |
| `viewimages` | `1` | `0` | Remote images are tracking pixels. Loading one confirms the address is live and read. |

### The rule database

Scoring a message as a virus does nothing unless a rule says to act on it. A
gateway whose protective rules are switched off filters beautifully and
delivers everything anyway.

The settable object is **`/config/ruledb/rules/{id}/config`**. `/config/ruledb/rules/{id}`
is a directory and answers `501` to a write; aiming at it made every rule
activation fail on PMG 9.1 while reporting, wrongly, that the gateway was too
old.

Rules are matched **by exact name against an explicit policy**, never by
keyword. Keyword matching on "spam" caught *Block Spam (Level 10)*, which does
not quarantine a message — it destroys it.

| Factory rule | What we do | Why |
| --- | --- | --- |
| Block Viruses | expect on | infected mail must not reach a mailbox |
| Virus Alert | expect on | tells sender and admin it was blocked |
| Block Dangerous Files | expect on | executable attachments |
| Blocklist / Welcomelist | expect on | the operator's own lists |
| Quarantine/Mark Spam (Level 3) | expect on | marks likely spam without moving it |
| **Quarantine/Mark Spam (Level 5)** | **switch on** | quarantines confident spam, and it can be released |
| Block Spam (Level 10) | **leave** | blocks rather than quarantines. A false positive here is unrecoverable, and anything scoring 10 is already caught by Level 5 |
| Block outgoing Spam | **leave** | protects sending reputation, and blocks your own users' mail when it misfires. The customer's call |
| Quarantine Office Files | **leave** | quarantines every Word and Excel attachment. On a business mail server that is an outage |
| Block Multimedia Files | **leave** | blocks audio and video outright |
| Modify Header, Add Disclaimer | **leave** | cosmetic, or a company decision |

A protective rule the operator has turned **off** is reported loudly and *not*
turned back on — they may have had a reason. A rule not on this list is
reported and left exactly as found. Nothing is ever created or deleted, and a
rule database that cannot be read is a warning, not a failure: mail still
flows.

### Blocklists that reject at the door: deliberately OFF

This is the most expensive lesson in this document.

`dnsbl_sites` in PMG is not "one more signal". It is a **hard SMTP reject** in
postscreen, before scoring happens, and Postfix reads **any** answer in
`127.0.0.0/8` as a listing.

Spamhaus refuses queries that arrive through a public resolver, and it refuses
them *by answering*: `127.255.255.254`, "Error: open resolver". On an appliance
whose gateway resolves through Google or Cloudflare DNS, every sender on earth
therefore looks listed, and every inbound message is rejected:

```
NOQUEUE: reject: RCPT from [...]: 550 5.7.1 Service unavailable;
client [...] blocked using zen.spamhaus.org
```

That happened on a live deployment (QA Phase 16, 12 September 2026), and it
read like the sender's fault. Verified from a workstation: the same
`127.255.255.254` came back for `8.8.8.8`, which is not a spam source.

So KIN Mail sets **no DNSBL**, and `apply` actively **clears** the setting, so
re-applying repairs a gateway configured by an earlier version. SpamAssassin's
`rbl_checks` consults the same data and only adds **score** — a wrong answer
there costs a few points instead of the whole message.

An operator with a local recursive resolver can opt in with `GATEWAY_DNSBL`.
When they do, the site is written with a return-code filter
(`zen.spamhaus.org=127.0.0.[2..11]`), so an error reply can never be read as a
listing.

### Not tuned, on purpose

- **`rejectunknown`** — refuses senders with no reverse DNS. Plenty of small
  but legitimate senders have none. The customer's policy, in the PMG UI.
- **Custom SpamAssassin scores, rules, whitelists** — site policy.
- **`max_filters`, `max_smtpd_in/out`** — PMG sizes these from the machine's
  memory. Overriding them from here would be guessing at hardware we cannot
  see.

---

## 10a. Monitoring

A collector runs on the appliance every 60 seconds and publishes, through the
same node_exporter textfile path as the mail flow metrics:

```
kin_mail_gateway_linked                 a link is configured and applied
kin_mail_gateway_up{port="26"}          the relay port answered
kin_mail_gateway_up{port="25"}          the inbound port answered
kin_mail_gateway_probe_seconds{port=}   how long the connect took
kin_mail_gateway_relay_configured       Zimbra really points at this gateway
```

A TCP connect, not an SMTP conversation: opening a session and dropping it
every minute is how an address gets rate-limited by its own gateway.

`relay_configured` is read from Zimbra with `zmprov`, not from our own state
file, because the interesting failure is the two disagreeing and a metric
sourced from the same file as the claim cannot show that.

The console distinguishes four states and never collapses them: no reading at
all, a reading that has gone stale (the collector stopped), linked-and-up, and
linked-and-down. A stale "up" is not shown as up.

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
