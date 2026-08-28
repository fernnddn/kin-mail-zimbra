import {
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  Pill,
  Row,
  Stack,
  Stat,
  Table,
  Text,
} from "cursor/canvas";

const CHECKED = "25 Aug 2026, ~10:45 WIB";
const HEAD_NOTE =
  "Static review of current main (MFA already removed). Findings confirmed by reading the live control flow, not by running a 2vm pair.";

export default function KinMailPlatformBugAudit() {
  return (
    <Stack gap={24}>
      <Stack gap={8}>
        <H1>KIN Mail platform bug audit</H1>
        <Text tone="secondary">
          {CHECKED}. {HEAD_NOTE} Healthy single-node operation is consistent
          with what you reported; the gaps below are mostly things a 1vm day
          would never exercise, plus a few 1vm leftovers.
        </Text>
        <Row gap={8} wrap>
          <Pill tone="success" active>
            1vm daily path mostly solid
          </Pill>
          <Pill tone="warning" active>
            2vm / failover / mid-op failure is where Claude left holes
          </Pill>
          <Pill tone="neutral" active>
            MFA not in HEAD
          </Pill>
        </Row>
      </Stack>

      <Grid columns={4} gap={16}>
        <Stat value="1" label="Critical (2vm only)" tone="danger" />
        <Stat value="6" label="High" tone="warning" />
        <Stat value="9" label="Medium" tone="warning" />
        <Stat value="1vm OK" label="Matches your live read" tone="success" />
      </Grid>

      <Callout tone="warning" title="MFA is gone on current main">
        Console TOTP was added, then removed after a live incident (`mfa.py`
        is absent; login is password then session cookie again). This audit
        does not treat MFA as shipping. Residual: leftover `mfa_secret` keys
        in an old `users.json` until the file is rewritten; Security page is
        password-only.
      </Callout>

      <H2>What a healthy 1vm will not show you</H2>
      <Text tone="secondary">
        Ranked by blast radius. Evidence is file:line on current HEAD.
      </Text>
      <Table
        headers={["Sev", "Area", "Finding", "Hits 1vm?", "Evidence"]}
        rowTone={[
          "danger",
          "warning",
          "warning",
          "warning",
          "warning",
          "warning",
          "warning",
          "info",
          "info",
          "info",
          "neutral",
          "neutral",
        ]}
        rows={[
          [
            "Critical",
            "Privhelper lock",
            "HA job lock can be stolen after 8s because steal checks kin-mail.sh/zmsetup, not the HA running marker. A second Deploy/HA/apply_draft can run while Ansible/peer SSH is still going. First job's finally also unlinks the HA marker.",
            "No (1vm install process protects steal)",
            "daemon.py:245, 265-280; deploy_state.py:170-180; commands.py:863-885; PIPELINE_WAIT_CMDS includes run_ha_orchestration",
          ],
          [
            "High",
            "Remove-host",
            "Successful remove-host never writes TOPOLOGY=1vm or clears PEER_HOST_*. Console user mutations keep using Promoted replica-first toward a dead peer and 503.",
            "No",
            "remove_host.py has no TOPOLOGY rewrite; console_users_sync.py:53-76 still clusters on 2vm",
          ],
          [
            "High",
            "HA finish",
            "Peer can get ha-setup-complete + users copy, then users push fails: A never writes ha-setup-complete, wizard stays anonymous Super Admin on A while cluster is live.",
            "No",
            "orchestration.py:1187-1227 then 2120-2133",
          ],
          [
            "High",
            "Users sync",
            "SSH apply-file on Promoted does not take the privhelper busy slot. Overlapping forwards can race fixed /tmp mutation files and last-write users.json.",
            "No",
            "console_users_sync.py:511+ vs daemon.py:418",
          ],
          [
            "High",
            "License",
            "Missing or invalid license token sets provisioning_blocked=False. Seat count still comes from CONTRACTED_SEATS in config. Deleting or corrupting the token after a valid apply unblocks grace/expired.",
            "Yes, if a signed license was applied then the token file is damaged",
            "commands.py:787-791; app.py:826-852; license.py:153; quota-gate.sh only reads CONTRACTED_SEATS",
          ],
          [
            "High",
            "Bootstrap",
            "First-boot Super Admin password is the well-known E@syEmail until first successful local login clears the initial-password file. Cleanup failure is swallowed.",
            "Yes, until rotated",
            "auth.py:18, 213; app.py login except Exception: pass",
          ],
          [
            "High",
            "Auth gate",
            "Unknown topology + setup-complete, or is_mail_deployed() OSError, is treated as not deployed. Anonymous setup identity stays Super Admin for wizard/HA commands.",
            "Rare on a clean 1vm with TOPOLOGY=1vm",
            "deploy_state.py:524-531; auth.py:170-174",
          ],
          [
            "Medium",
            "HA logs",
            "HA stream redaction list is root/kin/hacluster/chap only. ADMIN_PASS from config is not in that list if peer installer stdout echoes it.",
            "No",
            "orchestration.py:1466",
          ],
          [
            "Medium",
            "Sessions",
            "session.secret is minted per node and is not pushed with users.json. VIP failover logs everyone out (not a forge across nodes).",
            "No",
            "auth.py ensure_session_secret; push_local_users_to_peer copies users.json only",
          ],
          [
            "Medium",
            "Password",
            "POST /api/me/password needs a session, not the current password, and does not revoke other sessions.",
            "Yes (stolen cookie)",
            "app.py /api/me/password; Security.tsx",
          ],
          [
            "Medium",
            "Install",
            "09-hardening and 11-admin-path-lockdown warn-and-continue, then setup-complete can still be written. Empty wizard Admin IPs skip ufw entirely (documented).",
            "Yes, if those stages fail or IPs were left empty",
            "kin-mail.sh:369-404, 256-265",
          ],
          [
            "Medium",
            "AD login",
            "Unknown user: 401 Invalid credentials. Existing AD-mapped user when AD is down: 503 + detail. Username oracle for AD-backed console accounts.",
            "Yes, if AD accounts exist",
            "users.py:225-242",
          ],
        ]}
      />

      <H2>1vm leftovers worth fixing anyway</H2>
      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>License vs seats (product hole)</CardHeader>
          <CardBody>
            <Stack gap={8}>
              <Text>
                Crypto verify itself is solid (canonical payload, wrong
                server_id, tamper). The hole is policy: CLI/quota-gate never
                looks at the signed token, only CONTRACTED_SEATS. Wizard can
                still write that integer. Invalid token is treated like no
                license, not like blocked.
              </Text>
              <Text tone="secondary">
                Risk register R-09 still reads as if licensing was deferred.
                That doc is stale versus the Ed25519 code.
              </Text>
            </Stack>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Auth after MFA removal</CardHeader>
          <CardBody>
            <Stack gap={8}>
              <Text>
                No login attempt cap anymore. Cookie Max-Age is 12h after
                deploy, but loads() still accepts the 7-day setup lifetime so
                a pre-deploy cookie can outlive the 12h story.
              </Text>
              <Text tone="secondary">
                Session cookie flags (HttpOnly, Secure, SameSite=strict) are
                fine. CSRF from a foreign site is not the weak spot; XSS or a
                shared browser is.
              </Text>
            </Stack>
          </CardBody>
        </Card>
      </Grid>

      <H3>Confirmed 2vm failure modes (not exercised on a quiet 1vm)</H3>
      <Table
        headers={["Failure", "What the operator sees", "Code"]}
        rowTone={["danger", "warning", "warning", "info"]}
        rows={[
          [
            "Second HA/Deploy while first still Ansible",
            "Two pipelines; CIB/DRBD/SBD races; log line stale helper lock stolen for cmd=... (no install process)",
            "acquire_run_slot uses full_install_in_progress, not pipeline_in_progress",
          ],
          [
            "Remove Host then change a console user",
            "Could not sync console users to the other mail node",
            "Topology still 2vm, replica push to retired IP",
          ],
          [
            "HA apply dies at peer_console_state",
            "Peer may already be login-gated; A still in wizard with setup Super Admin",
            "Peer markers written before A's ha-setup-complete",
          ],
          [
            "Replica-first push OK, local save crashes",
            "Password works on one node, not the other, until the next Promoted push",
            "commit_users_store has no rollback after successful push",
          ],
        ]}
      />

      <H2>What looks actually solid</H2>
      <Table
        headers={["Area", "Why it is not a finding"]}
        rowTone={["success", "success", "success", "success", "success"]}
        rows={[
          [
            "Replica-first on push failure",
            "If the peer push fails, local users.json is left unchanged. Tests cover that.",
          ],
          [
            "License signatures",
            "Ed25519 verify is against the exact token bytes; tamper and wrong server_id fail closed.",
          ],
          [
            "Local login messages",
            "Missing user and wrong local password both say Invalid credentials.",
          ],
          [
            "Mutation files",
            "apply-file refuses plaintext passwords and symlink mutation paths.",
          ],
          [
            "7071 when ufw does apply",
            "Admin 7071 is not opened world-wide in that path; the gap is skipping ufw, not the allowlist.",
          ],
        ]}
      />

      <Divider />

      <H2>Fix order if you only have a few shots</H2>
      <Text>
        1. Steal lock: `acquire_run_slot` should treat
        `pipeline_in_progress()` (or the HA marker) as live work, same as
        kin-mail.sh. 2. Remove-host must demote to 1vm and clear peer fields.
        3. Invalid/missing license after a previously valid apply should not
        silently unblock seats (or seats should live only inside a verified
        token). 4. Random one-time bootstrap password, not `E@syEmail`. 5.
        Fail closed when topology markers are unreadable on a host that
        already has mail. 6. Route apply-file through the same busy gate or a
        users.json flock.
      </Text>
      <Text tone="secondary">
        Source: static read of console/backend, install/kin-mail.sh, and
        frontend Security/login. Not a live 2vm replay. Private license key
        on this workstation is gitignored; treat that tree as high-value if
        it is ever copied.
      </Text>
    </Stack>
  );
}
