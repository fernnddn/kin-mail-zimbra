# KIN Mail releases

What each release is, what it is ready for, and what it is not. Written for
whoever has to decide whether to put a version in front of a customer.

A version is called **mature** here when it has been deployed end to end on
real hardware, its faults have been found by someone using it rather than by
its own tests, and those faults have been fixed. A green build is not a smoke
test.

---

## 0.1.11 — the split mailbox

**New, and not yet mature by the definition above.** Zimbra across two
machines, with a Proxmox Mail Gateway in front of them.

Every stage is tested and the build is complete end to end in code, but a split
has not yet finished a clean installation on real hardware. Deploy it with
somebody watching, and read the output. **0.1.10 is the proven one**; it is
unchanged here and remains the safe choice if tonight cannot go wrong.

The node facing the internet holds no mail. It runs the MTA and the proxy; the
directory and the mail store live on a second machine that nothing on the
internet can reach. The whole build runs from the first machine over SSH, so
the operator never logs into the second one.

```
internet ──MX──▶ gateway ──filter──▶ edge (MTA + proxy) ──▶ mailbox (directory + store)
```

- **Choose it in the wizard.** Address and hostname for the mailbox, and the
  password the edge uses to build it. Nothing else.
- **One Deploy button.** The mailbox is built first, because the edge has no
  directory to join until it exists. Then the edge. Then TLS, DKIM, hardening
  and the firewall on both.
- **Each node is firewalled for what it is.** The edge faces the internet as a
  single appliance does. The mailbox accepts everything from the edge and
  nothing from anywhere else.
- **Every stage knows which machine it is on**, decided from addresses rather
  than hostnames, and refuses rather than guessing.

Single-server (0.1.10) is unchanged and still supported. Choose it in the
wizard for a one-machine deployment.

Known limits, stated rather than discovered:

- **One gateway and one edge are each a single point of failure.** If either is
  down, mail queues rather than being lost, and nothing moves.
- Backups run on the mailbox node, which is where the mail is. The edge refuses
  and says so.
- Removing a deployment means running the uninstaller on both machines. It
  names the one still standing.

---

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
