# Handover: KIN Mail 0.1.10 — the mandatory mail gateway

For whoever picks this up on **13 September 2026**. Written by Claude on the
evening of 11 September 2026, after the decision that KIN Mail must put a
Proxmox Mail Gateway in front of Zimbra.

Read `MAIL-GATEWAY.md` first. It is the design: architecture, ports, DNS,
every PMG setting and why, and the failure modes. This document is the state of
the work and what is left.

---

## The one-paragraph version

Until 0.1.9 KIN Mail was one all-in-one box: Zimbra held port 25 to the
internet and filtered its own mail. Management decided that is too exposed, so
from 0.1.10 a Proxmox Mail Gateway VM sits in front. It becomes the MX, filters
inbound mail, relays outbound mail, and the mail node's port 25 narrows to
accept SMTP from the gateway only. KIN Mail does not install PMG — PMG is
Debian, the mail node is Ubuntu — it configures **both sides of the link** over
PMG's REST API and Zimbra's `zmprov`, and then proves the link works.

---

## What is already done and green

| Area | File | State |
| --- | --- | --- |
| Design | `MAIL-GATEWAY.md` | complete |
| Workhorse script | `install/lib/mail-gateway.sh` | complete, 47 tests pass |
| Shell tests | `install/lib/test-mail-gateway.sh` | 47 assertions, all green |
| Credential vault + status | `console/backend/kin_privhelper/mail_gateway.py` | complete |
| Privhelper command | `cmd_mail_gateway` in `commands.py` | complete |
| Protocol / RBAC / busy label | `protocol.py`, `rbac.py`, `daemon.py` | wired |
| REST API | `/api/mail-gateway/{status,connect,trust,forget}` in `app.py` | complete |
| Backend tests | `console/backend/tests/test_mail_gateway_link.py` | 46 tests, all green |
| Console page | `console/frontend/src/pages/MailGateway.tsx` | complete, builds clean |
| Route + nav | `App.tsx`, `ConsoleChrome.tsx` | wired, ops roles only |
| Firewall narrowing | `install/10-host-firewall.sh` | complete |
| Compliance gate | `install/05-healthcheck.sh` | complete |
| Ansible | `roles/mail_gateway_link`, `playbooks/mail-gateway.yml` | complete |
| Version | 0.1.10 in both `__init__.py` and `package.json` | bumped |

Full suite at the time of writing: **1182 backend tests**, **26 shell suites**,
frontend `tsc` clean and `npm run build` clean.

---

## What has NOT happened, and this is the important part

**None of this has ever talked to a real Proxmox Mail Gateway.** There is no
PMG VM yet. Every test stubs `curl`, `openssl`, `zmprov` and `dig`. The tests
prove the logic — ordering, refusals, idempotency, secret handling — they do
not prove that PMG's API accepts the exact field names we send.

That is the single biggest risk in this work, and it is where day one should
go. Treat every API field name below as "read from the documentation and not
yet confirmed against a running gateway".

---

## Day one: build a PMG VM and find out what breaks

1. **Install PMG.** Download the ISO from proxmox.com, make a VM (2 vCPU,
   4 GB RAM, 32 GB disk is plenty for a lab), give it a static IP and a
   resolvable hostname. PMG can also be installed on top of plain Debian 12
   from the `pmg-no-subscription` apt repository, which is easier to automate
   if we ever want to; the ISO is fine for now.

2. **Create an API token** on it:
   `pmgsh create /access/users/root@pam/token/kinmail`
   or Configuration → User Management → API Tokens. Keep the secret.

3. **Confirm the API surface by hand before trusting our code.** On the PMG
   box:
   ```
   pmgsh get /config/mail
   pmgsh get /config/domains
   pmgsh get /config/transport
   pmgsh get /config/mynetworks
   pmgsh help /config/transport -v
   ```
   Compare what comes back against what `mail-gateway.sh apply` sends. The
   fields we send are listed in `MAIL-GATEWAY.md` §6. **`use_mx` on
   `/config/transport` is the one I am least sure about** — the documentation
   names the field, I could not confirm its exact spelling in the API viewer.
   If it is wrong, `api_post /config/transport` returns a 4xx and `apply`
   stops with `KIN_GW_REFUSED reason=api-failed`, which is the correct
   behaviour but is still a bug to fix.

4. **Run the real thing** against a throwaway Zimbra, in this order:
   ```
   mail-gateway.sh probe      # auth + certificate, changes nothing
   mail-gateway.sh plan       # says what it would change
   mail-gateway.sh apply
   mail-gateway.sh verify
   ```
   `probe` will refuse the first time with `certificate-not-trusted` and print
   a fingerprint. That is by design; confirm it in the console, or set
   `KIN_PMG_TRUST_ON_FIRST_USE=1` for a one-off CLI run.

5. **Send mail both ways and read the headers.** Inbound from an external
   account, outbound to Gmail. Check: one DKIM signature not two, SPF pass,
   the mail node's internal address absent from `Received:` headers, and no
   loop in the PMG tracking centre.

---

## Things I decided that you may want to revisit

**Zimbra's own amavis/SpamAssassin/ClamAV stay ON.** The instruction was "must
not use the native one", and I did not take that literally, because turning
Zimbra's filtering off would also remove the scanning of internal mail — which
never reaches the gateway — and would disturb the DKIM signing path. Two layers
cost CPU and nothing else. There is **no switch for this** — I did not build
one, deliberately, because it should not be a one-flag decision. **Raise it
with the operator before implementing anything**; it is a policy question, not
a technical one.

**One gateway is a single point of failure.** If the PMG VM is down, inbound
mail queues at the sender and outbound queues on Zimbra. Nothing is lost, but
mail stops moving. PMG supports clustering; we deliberately did not build it,
exactly like Zimbra HA is parked. Say this out loud to whoever signs off.

**Certificate pinning instead of `curl -k`.** A fresh PMG is self-signed.
`curl -k` would mean trusting whatever answers that address forever. Instead
the fingerprint is confirmed once by a human and verified every time after; a
change stops everything. If this proves annoying in practice the right fix is a
real certificate on PMG (`GATEWAY_CACERT`), not removing the check.

**`before_queue_filtering = 0`.** After-queue filtering: PMG accepts then
filters. Before-queue rejects during the SMTP session, which is stricter and
avoids backscatter, but drops mail under load. Worth revisiting once we know
the real mail volume.

---

## Known gaps, roughly in priority order

1. **Nothing is confirmed against real PMG.** See above.
2. **No LDAP recipient verification.** PMG can check recipients against
   Zimbra's directory and reject unknown addresses at the door. We use
   `verifyreceivers=550` (SMTP callout) instead, which gets most of the benefit
   without a bind account. Proper LDAP integration is the obvious 0.1.11
   feature.
3. **The install pipeline does not call the gateway stage.** `05-healthcheck.sh`
   *reports* a missing gateway as a failure and `10-host-firewall.sh` narrows
   port 25 once one exists, but a fresh deploy does not stop and ask for a
   gateway. Deciding whether it should is a product question: making it
   blocking means no appliance can be deployed before its gateway VM exists.
4. **No monitoring of the gateway.** The Monitoring tab knows nothing about
   PMG. At minimum it should alert when the gateway stops answering, because
   that is the failure that silently stops all mail.
5. **`revert` does not undo the PMG side.** Deliberate — leaving PMG configured
   is harmless and makes re-applying instant — but somebody will eventually ask
   for a full teardown.
6. **The gateway's own hardening is unautomated.** Restricting :8006 to admin
   IPs, disabling root SSH password login, and installing a real certificate on
   PMG are all manual. A checklist exists in `MAIL-GATEWAY.md` §8; turning
   it into a role would be worth doing.

---

## How to work in this repository

These are house rules the operator has enforced repeatedly. Breaking them
wastes a round trip.

- **This workstation is Ubuntu with GNU sed and GNU coreutils.** It is not
  macOS and it is not a mail appliance. Never run `full-install`, `pcs`,
  `drbd`, or anything that touches `/opt/zimbra` here.
- **Commit messages are in English and say _why_, not _what_.**
- **Finish the job.** When work is done: commit, push to `origin main`, and
  confirm CI went green for that SHA with `gh run list`. Do not stop after the
  commit and wait to be told to push. `origin` has two push URLs
  (`azana-nisaa/kin-mail` and `fernnddn/kin-mail-zimbra`); one `git push`
  reaches both and a credential prompt appearing twice is normal. Never
  force-push.
- **Still ask first** before tagging a version, editing a GitHub Release, or
  touching the live appliance.
- **CI runs the appliance's runtimes, not just modern ones.** Backend tests run
  on Python 3.10 *and* 3.11; Ansible syntax-checks on ansible-core 2.12 *and*
  2.16. Ubuntu 22.04 ships ansible-core 2.12, so `ansible.builtin.systemd_service`
  does not exist there. `asyncio.TimeoutError` is a distinct class before
  Python 3.11. Both of these have already caused production failures.
- **Backend tests run under `unittest discover`, not pytest.** A test file
  written in pytest style is silently not run. Use `unittest.TestCase`.
- **Shell suites are auto-discovered** as `install/lib/test-*.sh`. Add a file
  and CI runs it.
- **CI fails the build on RFC1918 addresses** in `*.sh`, `*.md`, `*.py`,
  `*.yml`. Use the documentation range `192.0.2.x` and `example.test`.
  `test_*.py` is exempt; nothing else is.
- **Every whitelisted privhelper command needs a busy label** in
  `daemon._CMD_LABELS`, or `test_busy_reporting.py` fails.

### Running everything

```sh
# shell suites
for t in install/lib/test-*.sh; do bash "$t"; done

# backend (1182 tests)
PYTHONPATH=console/backend console/backend/.venv/bin/python \
  -m unittest discover -s console/backend/tests

# frontend
cd console/frontend && npx tsc --noEmit && npm run build
```

---

## Map of what to read, in order

1. `MAIL-GATEWAY.md` — the design. Start here.
2. `install/lib/mail-gateway.sh` — all the mail-affecting decisions. The header
   comment lists what it will and will not do.
3. `install/lib/test-mail-gateway.sh` — reads as a specification; every test
   name is a sentence about what must not happen.
4. `console/backend/kin_privhelper/mail_gateway.py` — credential vault,
   configuration file, and the compliance flag.
5. `cmd_mail_gateway` in `console/backend/kin_privhelper/commands.py` — which
   operations take the maintenance lock and why.
6. `console/frontend/src/pages/MailGateway.tsx` — the operator's path through
   it: connect, confirm certificate, plan, apply, DNS, verify.

---

## Contract that must not quietly change

These are load-bearing. If a change makes one of them false, the change is
wrong, not the test.

- `relaynomx = 1` on PMG. The MX for the domain now points at PMG; an MX lookup
  from PMG is a loop.
- `dkim_sign = 0` on PMG. Zimbra already signed; two signatures fail DMARC at
  some receivers.
- The relay domain is written to PMG **before** anything else in `apply`.
- `zimbraMtaMyNetworks` is **appended to**, never replaced.
- The API token secret reaches the script by environment, never by argv, and is
  on the redaction list so it cannot reach the transcript.
- `compliant` requires an applied link, not merely a recorded one.
- `apply` and `revert` take the maintenance lock; `probe`, `plan` and `verify`
  do not.
