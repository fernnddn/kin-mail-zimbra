# Deploying KIN Mail 0.1.10, with the mail gateway

The order matters. Follow it top to bottom.

This replaces the "bootstrap and you're done" flow of 0.1.9. From 0.1.10 a
deployment is **two machines**: the KIN Mail appliance, and a Proxmox Mail
Gateway VM in front of it. The appliance is deployed first and works on its own
from the start; the gateway is put in front afterwards, without downtime.

If you only read one thing: **do not move the MX record until Verify is
green.** Everything else here is recoverable. That one is not — mail addressed
to a gateway that is not ready is rejected, and the sender is told the address
does not work.

---

## What you need before you start

| | |
| --- | --- |
| Appliance VM | Ubuntu 22.04, 4 vCPU / 8 GB / 100 GB, fixed IP |
| Gateway VM | 2 vCPU / 4 GB / 32 GB, fixed IP, on the same network |
| DNS control | you must be able to edit MX, A, TXT at the registrar |
| PTR control | ask the ISP to point the gateway's IP at its hostname |
| Port 25 outbound | from the **gateway** to the internet. Most providers block this by default — ask, and get it in writing before deployment day. |

The two machines must be able to reach each other on TCP 25 and 26. If there
is a firewall between them, open those now; it is the single most common cause
of "everything is configured and no mail moves".

Decide the two names now and keep them:

```
mail.example.com    ->  appliance IP     (webmail, admin console, submission)
pmg.example.com     ->  gateway IP       (MX, filtering)
```

---

## Phase 1 — the appliance (unchanged from 0.1.9)

This is the part you already know. Nothing about it changes.

**1.1** Create the VM, install Ubuntu 22.04, set the fixed IP and hostname.

**1.2** Bootstrap the console:

```sh
sudo ./console/bootstrap.sh
```

**1.3** Open the console on `https://<appliance-ip>:9443`, accept the licence,
and work through the wizard: domain, credentials, TLS, licensing, review.

Keep **Topology: single server**. That is the product.

**1.4** Press **Deploy** and let it run. It takes roughly 45–70 minutes.

**1.5** At the end the healthcheck runs. Expect this, and do not be alarmed:

```
[WARN]   No mail gateway is linked yet - this appliance is still its own MX
```

That is **BLOCKED**, not FAIL. The deploy has not failed. A gateway cannot
exist before the appliance does, so on a first install this is the correct
state — it is the reminder that Phase 2 is still to come.

**1.6** Point DNS at the **appliance** for now, so it works immediately:

```
A     mail.example.com.    ->  <appliance ip>
MX    example.com.     10      mail.example.com.
TXT   example.com.             "v=spf1 ip4:<appliance ip> ~all"
```

Send yourself a test message in and out. Get this working before adding the
gateway. Debugging two new machines at once is twice the work and four times
the confusion.

---

## Phase 2 — the gateway VM

**2.1** Download the Proxmox Mail Gateway ISO from
<https://www.proxmox.com/en/downloads> and install it on the second VM.

During installation it asks for:

- **hostname** — use the FQDN, `pmg.example.com`. PMG requires a resolvable
  FQDN and will not install cleanly without one.
- **IP / gateway / DNS** — fixed address, same network as the appliance.
- **root password** and an **administrator email**.

**2.2** First boot. Log in at `https://<gateway-ip>:8006` (user `root`, realm
`Linux PAM`). The certificate is self-signed; that is expected and KIN Mail
handles it deliberately — see 3.2.

**2.3** Update it:

```sh
# on the gateway, as root
apt update && apt dist-upgrade -y
```

Without a subscription you will see a "no valid subscription" notice. That is
normal and does not affect function.

**2.4** Set the timezone to match the appliance
(`Configuration ▸ Administration ▸ Time zone`, e.g. `Asia/Jakarta`). Mismatched
clocks make two sets of logs impossible to correlate when something goes wrong.

**2.5** Decide how KIN Mail will authenticate to the gateway.

**Username and password is the default, and it is fine.** The `root@pam` login
you set during installation is the one credential every PMG install definitely
has, and it is stored encrypted on the appliance. Nothing further to do here.

If you prefer an API token, create one on the gateway:

```sh
pmgsh create /access/users/root@pam/token/kinmail
```

or `Configuration ▸ User Management ▸ API Tokens ▸ Add`. **Copy the secret
now** — it is shown once. Then choose *API token* in step 3.1.

**2.6** Check the two machines can actually talk. From the **appliance**:

```sh
timeout 5 bash -c 'exec 3<>/dev/tcp/<gateway-ip>/8006' && echo "API reachable"
timeout 5 bash -c 'exec 3<>/dev/tcp/<gateway-ip>/26'   && echo "relay port reachable"
```

Both must succeed before you continue. If they do not, it is a firewall or a
route, and no amount of configuration on either machine will fix it.

---

## Phase 3 — link them

All of this is in the console: **Mail Gateway** in the left rail.

**3.1 Connect.** Console ▸ **Proxmox Mail Gateway**.

Four things, and the first two are not the same as the last two:

| Field | What it is |
| --- | --- |
| Address this console uses | how the **appliance** reaches the gateway — normally its LAN address |
| API port | `8006` |
| Public hostname | `relay.example.com` — what the **internet** sees. This is what the MX record will name. |
| Public address | the routable address the internet reaches it on |

Then authentication *API token* and the two values from 2.5. Press **Connect**.

The public pair is not optional decoration. An MX record can only name a host,
never an address, and an SPF record listing a private address authorises
nobody. The console refuses a private address here, refuses a bare IP as the
hostname, and will print no DNS records at all until it has both — rather than
printing the LAN address and letting you publish it.

The secret is encrypted on the appliance and never shown again — not to the
browser, not in the logs, not in a process listing.

**3.2 Test the connection and confirm the certificate.** Press **Probe**. The
first time, it will refuse:

```
KIN_GW_REFUSED reason=certificate-not-trusted
The gateway's certificate has not been confirmed yet. Fingerprint: AB:CD:...
```

This is correct and deliberate. Compare that fingerprint with the one on the
gateway itself:

```sh
# on the gateway, as root
openssl x509 -in /etc/pmg/pmg-api.pem -noout -fingerprint -sha256
```

If they match, press **This is the right certificate**. From then on the
appliance verifies it every time, and if it ever changes, everything stops
until you confirm the new one.

Probe again. You should see the PMG version and *"the credential works"*.

Nothing further on the page runs until the certificate is confirmed: Plan,
Apply and Verify stay disabled, with a line saying why. That is deliberate —
this is the machine every message will pass through.

**3.3 Plan.** Press **Plan**. Nothing is changed. Read what it says it would
change; it should list the relay, the relay domain, the transport and the
relay host. If it says something you did not expect, stop and ask why.

**3.4 Apply.** Press **Apply**. It takes seconds. Mail keeps flowing the whole
time — nothing restarts, and no queued message is dropped.

Watch for these, in order:

```
KIN_GW_OK relay domain example.com
KIN_GW_OK transport example.com -> <appliance ip>:25
KIN_GW_OK trusted network <appliance ip>/32
KIN_GW_OK relay <appliance ip>:25, no MX lookup, internal port 26
KIN_GW_OK greylisting OFF - no first-contact delay
KIN_GW_OK soft recipient verification, SPF, DNSBL, outbound TLS, no header leak
KIN_GW_OK Bayesian learning, auto-welcomelist, Razor, blocklists, attachment text extraction
KIN_GW_OK encrypted archives flagged, deeper nesting scanned, larger files still scanned
KIN_GW_OK spam kept 14 days, viruses 30 days, no live links or tracking images
KIN_GW_OK rule active: ...
KIN_GW_OK gateway DKIM signing off
KIN_GW_OK zimbraMtaRelayHost <gateway ip>:26
KIN_GW_OK zimbraMtaMyNetworks += <gateway ip>/32
KIN_GW_OK the gateway reports back what we asked it to be
KIN_GW_OK Gateway link applied.
```

Apply does not only route mail. It also tunes the gateway's detection — see
`MAIL-GATEWAY.md` §9a for every value and why. Two you will notice:

- **Greylisting is off.** It defers the first message from any unseen sender
  by a few minutes, and that is the usual answer to "why is inbound mail slow
  when outbound is instant". Spam is judged on content instead. Turn it on
  with the switch on the Apply card if your site wants it.
- **Bayesian learning is on**, and it starts knowing nothing. Accuracy
  improves over the first weeks as it sees your mail.
- **No SMTP-level blocklist is set**, and Apply actively clears one if the
  gateway has it. A DNSBL in PMG hard-rejects mail before scoring, and
  Spamhaus answers every query with an error code when it is asked through a
  public resolver — which makes every sender look listed and rejects all
  inbound mail. SpamAssassin still consults the same lists as *score*. See
  "Inbound mail is rejected" below.

If you see `KIN_GW_REFUSED reason=settings-did-not-stick`, **stop, and do not
change DNS.** The gateway accepted the settings but is not reporting them back;
something on the gateway is overriding them, and mail sent there would be lost.

**3.5 Test outbound before touching DNS.** At this point outbound mail already
goes through the gateway and inbound still comes straight to the appliance.
That is a deliberately safe half-state. Send a message out and check on the
receiving side that:

- it arrived,
- SPF still passes (it will not yet — see 4.2 — expect a soft fail and fix it
  there),
- there is exactly **one** DKIM signature.

Check the gateway's `Tracking Center` to see the message pass through it. If
it is not there, outbound is not going through the gateway and something in
3.4 did not take.

---

## Phase 4 — DNS

Only now. Only after 3.4 succeeded.

**4.1** Add the gateway's A record, if you have not:

```
A     pmg.example.com.   ->  <gateway ip>
```

**4.2** Update SPF **first**, and wait for it to propagate. Outbound mail now
leaves from the gateway's address, so SPF has to say so. If you move the MX
first and the SPF later, outbound mail is treated as forged in the gap.

```
TXT   example.com.   "v=spf1 ip4:<gateway PUBLIC ip> ~all"
```

The gateway's **public** address, not the one the console talks to. The Mail
Gateway page prints the exact record once you have given it the public pair;
copy it from there rather than typing it.

If the appliance also sends directly for anything, keep its address listed too:
`"v=spf1 ip4:<gateway public ip> ip4:<appliance public ip> ~all"`.

Check it: `dig +short TXT example.com`

**4.3** Move the MX:

```
MX    example.com.   10   pmg.example.com.
```

Remove the old MX pointing at `mail.example.com`.

Lower the TTL a day beforehand if you can — it means a mistake takes minutes
to undo instead of hours.

**4.4** Ask the ISP for the PTR record on the gateway's IP:

```
<gateway ip>  ->  pmg.example.com
```

A good share of the internet refuses mail from an address with no matching
reverse DNS. This is the item most often forgotten and most often the cause of
"Gmail puts us in spam".

**4.5** DKIM and DMARC do **not** change. Signing stays on the appliance, and
the gateway is configured not to sign a second time.

---

## Phase 5 — close the door

**5.1** Now that inbound mail arrives via the gateway, narrow port 25 on the
appliance so nothing else can deliver to it directly. On the appliance:

```sh
sudo ./install/10-host-firewall.sh apply
```

It reads the applied link and replaces the open port-25 rule with one that
accepts mail from the gateway and the LAN only. Look for:

```
[ OK ]   Port 25 accepts mail from the gateway (<gateway ip>) and the LAN only
```

The dead-man's switch is still armed here: if you lose access, ufw disables
itself in 300 seconds. Once you have confirmed you can still reach the console
and mail still flows, cancel it:

```sh
sudo ./install/10-host-firewall.sh cancel-deadman
```

**5.2 Verify.** Back in the console, **Mail Gateway ▸ Verify**. Expect:

```
KIN_GW_VERIFY problems=0
```

Every problem it lists is a way mail stops. None are cosmetic.

**5.3 Run the healthcheck.**

```sh
sudo ./install/05-healthcheck.sh
```

`FAIL: 0`. The mail gateway section should now read *"Mail gateway
pmg.example.com is linked"* with everything under it passing.

---

## Phase 6 — harden the gateway

KIN Mail does not do this for you; the gateway is its own machine.

**6.1** Restrict the web interface. On the gateway, allow :8006 only from your
admin addresses:

```sh
# on the gateway, as root
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow in on lo
ufw allow 25/tcp                                     comment 'inbound mail'
ufw allow from <appliance-ip> to any port 26 proto tcp comment 'relay from appliance'
ufw allow from <admin-ip>     to any port 8006 proto tcp comment 'admin'
ufw allow from <admin-ip>     to any port 22 proto tcp comment 'ssh admin'
ufw enable
```

Note port 26 is restricted to the appliance. Left open, anyone on the network
can relay mail through your gateway without authenticating.

**6.2** Disable root password login over SSH on the gateway
(`PermitRootLogin prohibit-password` in `/etc/ssh/sshd_config`, after you have
put an SSH key in place).

**6.3** Put a real certificate on :8006 if you can — Let's Encrypt is built in
under `Configuration ▸ Certificates ▸ ACME`. Then set `GATEWAY_CACERT` on the
appliance and the fingerprint pinning gives way to ordinary verification.

**6.4** Keep it patched. `apt update && apt dist-upgrade` on a schedule, same
as any other machine carrying your mail.

---

## Phase 7 — confirm it really works

Do all four. Each catches something the others do not.

| Test | How | What it proves |
| --- | --- | --- |
| Inbound | send from Gmail to a mailbox | MX, relay domain, transport, port 25 |
| Outbound | send to Gmail, read the headers | relay host, SPF, one DKIM signature |
| Spam | <https://www.mail-tester.com> | SPF, DKIM, DMARC, PTR, blocklists — aim for 9/10 |
| Filtering | send the EICAR test string and a GTUBE spam sample | the gateway is actually filtering, not just forwarding |

In the gateway's **Tracking Center**, every message should appear. If inbound
mail arrives but does not appear there, it is still bypassing the gateway —
check the MX record has actually propagated.

---

## If something goes wrong

**Inbound mail is rejected with "blocked using zen.spamhaus.org".**
A DNSBL is configured on the gateway and its answers cannot be trusted.
Spamhaus refuses queries from public resolvers *by answering* `127.255.255.254`,
and Postfix reads any `127.0.0.0/8` answer as a listing, so every sender looks
blocked. Press **Apply** again: it clears the setting. If you want a blocklist,
give the gateway a local recursive resolver first, then set `GATEWAY_DNSBL` —
the setting is then written with a return-code filter so an error reply cannot
be read as a listing.

**Inbound mail is slow, minutes, while outbound is instant.**
Greylisting. It is off by default in 0.1.10; if it is on, that delay is the
feature working. Turn it off on the Apply card and re-apply. If it is already
off, look at the gateway's Tracking Center to see where the time goes.

**Mail stops arriving after moving the MX.**
Check the gateway's mail queue (`Administration ▸ Queues`). If messages are
queued with "connection refused", the gateway cannot reach the appliance:
check port 25 on the appliance is open to the gateway (Phase 5 may have
narrowed it to the wrong address) and that `mail-gateway.sh verify` passes.

**Mail stops leaving.**
Look at the appliance's queue: `su - zimbra -c 'zmqstat'`. If it is growing,
the appliance cannot reach the gateway on 26. Check 6.1 did not close it.

**"Relay access denied" from the gateway.**
The relay domain is missing on the gateway. Run **Apply** again; it writes the
relay domain first, precisely for this.

**Mail loops, or messages are returned with "too many hops".**
Either `relaynomx` got turned off on the gateway, or the domain is no longer a
local domain on the appliance. **Verify** reports both by name.

**Everything worked and now DMARC fails at some recipients.**
Two DKIM signatures. Check `dkim_sign` is `0` on the gateway
(`Configuration ▸ Administration`). **Verify** reports this too.

**You need to go back.**
Console ▸ Mail Gateway ▸ **Revert**, then move the MX back to
`mail.example.com`, then re-run `10-host-firewall.sh apply` to reopen port 25.
All three, together — doing two of the three stops mail.

---

## Day-to-day, once it is running

- **The Monitoring tab** shows whether the gateway is answering. `Mail Gateway`
  down while everything else looks healthy is exactly the failure this exists
  to catch.
- **Quarantine** lives on the gateway, not the appliance. Released mail is
  delivered to the mailbox as normal.
- **Spam policy, rules, whitelists** are the gateway's UI. KIN Mail sets the
  routing and deliberately does not touch site policy.
- **Adding a domain** means adding it as a relay domain and a transport on the
  gateway as well as on the appliance. The appliance alone is no longer enough.
- **Re-running Apply** at any time is safe. It is idempotent, and it re-asserts
  every setting that matters.

One thing to say out loud to whoever signs this off: **a single gateway is a
single point of failure.** If it is down, mail queues at the senders and on the
appliance; nothing is lost, but nothing moves either. PMG supports clustering
and KIN Mail 0.1.10 does not set it up.
