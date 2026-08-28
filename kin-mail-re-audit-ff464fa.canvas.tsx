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

const CHECKED = "25 Aug 2026, ~12:20 WIB";

export default function KinMailReAuditFf464fa() {
  return (
    <Stack gap={24}>
      <Stack gap={8}>
        <H1>KIN Mail re-audit of ff464fa</H1>
        <Text tone="secondary">
          {CHECKED}. Claude claimed the previous canvas was fully fixed in
          commit `ff464fa`. This pass checked that commit against live HEAD,
          not the commit message. Static review, not a live 2vm run.
        </Text>
        <Row gap={8} wrap>
          <Pill tone="success" active>
            Critical early-done is a real fix
          </Pill>
          <Pill tone="warning" active>
            Two claimed fixes are incomplete
          </Pill>
          <Pill tone="warning" active>
            Cursor: not clear to practice
          </Pill>
        </Row>
      </Stack>

      <Callout tone="danger" title="Not all-clear">
        Do not practice Add second server, Remove Host, or a fresh Deploy
        as if the last canvas is closed. The HA stream lie is gone. The
        install success grep and the peer identity-sync order are not.
        License invalid-token fail-open was left on purpose.
      </Callout>

      <Grid columns={4} gap={16}>
        <Stat value="6" label="Claimed fixes that actually landed" tone="success" />
        <Stat value="2" label="Claimed fixes still incomplete" tone="warning" />
        <Stat value="1" label="New issue introduced by the sync" tone="danger" />
        <Stat value="No" label="Ready to practice" tone="warning" />
      </Grid>

      <H2>Scorecard of Claude's own list</H2>
      <Table
        headers={["Claim in ff464fa", "On HEAD", "Evidence"]}
        rowTone={[
          "success",
          "success",
          "success",
          "success",
          "warning",
          "success",
          "warning",
          "success",
          "success",
        ]}
        rows={[
          [
            "HA ensure-ssh no longer yields subprocess done; non-zero aborts",
            "Fixed",
            "orchestration.py:1476-1517 swallows that done and returns. Tests: test_failure_aborts_with_exactly_one_done_and_no_ansible, test_success_does_not_yield_its_done_and_continues",
          ],
          [
            "Remove Host fails the job if topology demote fails",
            "Fixed",
            "remove_host.py:622-646 returns ok:false and event_done(1)",
          ],
          [
            "pcs unstandby rollback after a failed remove-host drain",
            "Fixed",
            "remove_host.py:233-248, 559-560, 606-607. Only when this run put the node in standby",
          ],
          [
            "Departing uninstall uses wrap_privileged_remote, not sudo -n",
            "Fixed for SSH",
            "remove_host.py:669-687. Local path still runs bash --decommission (privhelper is root). Job still ok:true if uninstall fails",
          ],
          [
            "Install no longer claims every stage exited 0 after 09/11 warn-and-continue",
            "Half-fixed",
            "kin-mail.sh now prints Full install complete, with warnings. Frontend and backend still grep /Full install complete/i, which matches that banner. Progress bar still says Complete",
          ],
          [
            "Mailbox create: retry auth probe; do not say not created",
            "Fixed as messaging, not rollback",
            "08-create-mailbox.sh:318-339 (3 tries). app.py:_mailbox_create_message. Account still exists; seat still used",
          ],
          [
            "HA syncs server-id and license.token to the peer",
            "Half-fixed",
            "Pushed after ha-setup-complete. Same split-state class as the old users.json-last bug. license.token installed mode 644",
          ],
          [
            "License apply writes CONTRACTED_SEATS before the token",
            "Fixed",
            "appliance_settings.py:238-250. Inverse leftover: seats can update if the later token write fails",
          ],
          [
            "Replica-ahead local-write failure is named, not internal error",
            "Fixed as messaging",
            "console_users_sync.py in commit_users_store, run_mutate, _apply_file. Replica is not rolled back",
          ],
        ]}
      />

      <H2>Still operator-breaking</H2>
      <Table
        headers={["Sev", "Finding", "Why it still hurts"]}
        rowTone={["warning", "warning", "warning", "info", "info"]}
        rows={[
          [
            "High",
            "Install UI still treats a warned install as clean success",
            "deployPipeline.ts:95-96 and deploy_state.py:544-545 match Full install complete as a prefix. Complete is checked before [FAIL], so a 09/11 [FAIL] plus the new with warnings banner still paints the bar Complete / failed:false. setup-complete is still written. Operator will not notice hardening/lockdown never applied unless they read the log.",
          ],
          [
            "High",
            "server-id and license.token are synced after completion markers",
            "orchestration.py:1198-1285. Ansible can already be live. If the new push fails, local HA returns ORCH_FAILED step=peer_console_state and never writes local ha-setup-complete. Peer already has both markers (login-gated). Retry can unstick it; first-run UI still says HA failed while the cluster is up. users.json was moved before markers for this exact reason. The new files were appended after.",
          ],
          [
            "High",
            "Invalid / missing license still does not block new mailboxes",
            "Left on purpose in the commit message. commands.py:787-791: verify_license ValueError -> provisioning_blocked False. quota-gate.sh only reads CONTRACTED_SEATS. _set_seats still allows mutation when status is invalid. Tamper or corrupt the token after a valid apply and seats stay open.",
          ],
          [
            "Medium",
            "license.token is installed on the peer as mode 644 root:root",
            "Local write_license_token uses 0640 root:kin-console. HA push uses mode 644 (only 600 or 644 are allowed). World-readable signed license on the second node.",
          ],
          [
            "Medium",
            "Remove Host can still report overall success while the departing host is fully installed",
            "uninstall_warned is true, ok is still true. Honest-er than before, still easy to miss.",
          ],
        ]}
      />

      <H3>Left from earlier canvases, still live</H3>
      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>Session / password / AD</CardHeader>
          <CardBody>
            <Text>
              session.secret is still not HA-synced (VIP failover logs
              everyone out). /api/me/password still needs no current
              password and does not revoke other sessions. AD down still
              returns 503 for known AD usernames vs 401 for unknown locals.
            </Text>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Firewall / topology / bootstrap</CardHeader>
          <CardBody>
            <Text>
              Empty wizard Admin IPs still skip ufw with a warning
              (kin-mail.sh:259-261). Unknown topology still fail-opens
              is_mail_deployed. E@syEmail bootstrap left as design.
              revert-ssh-password failure is not in soft_failed_stages, so
              a 1vm install can still print All selected pipeline stages
              exited 0 with password SSH still on.
            </Text>
          </CardBody>
        </Card>
      </Grid>

      <Divider />

      <H2>What is actually safe vs not</H2>
      <Table
        headers={["Flow", "Practice?"]}
        rowTone={["warning", "danger", "danger", "warning"]}
        rows={[
          [
            "Quiet 1vm on an already-deployed host: login, mailbox (read the auth-probe message if create returns !ok)",
            "Lowest risk. Not a green light for a new Deploy.",
          ],
          [
            "Fresh Deploy / full install",
            "No until the Complete grep is fixed. Bar will lie if 09 or 11 warn-and-continue.",
          ],
          [
            "Add second server / Build HA pair",
            "No until server-id and license.token are pushed before ha-setup-complete, and license.token is not 644.",
          ],
          [
            "Remove Host",
            "Demote/unstandby/sudo paths look real. Still ok:true if departing uninstall fails. Do not treat as all-clear until the HA apply path above is honest.",
          ],
        ]}
      />

      <H2>Fix order before anyone says practice</H2>
      <Text>
        1. Push server-id and license.token before any peer completion
        marker (same order rule as users.json). Install the token as 600,
        not 644. 2. Stop treating Full install complete, with warnings as
        a clean Complete: tighten the frontend and
        transcript_has_terminal_outcome greps; do not let Complete win
        over [FAIL]. 3. Invalid license must set provisioning_blocked and
        stop _set_seats. 4. Add revert-ssh failure to soft_failed_stages.
        5. Fail Remove Host (or at least refuse ok:true) when departing
        uninstall fails, unless the operator picked forced.
      </Text>
      <Text tone="secondary">
        Source: HEAD `ff464fa` on main. Previous canvas
        kin-mail-re-audit-after-fixes.canvas.tsx is superseded by this file.
      </Text>
    </Stack>
  );
}
