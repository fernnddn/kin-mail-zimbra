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

const CHECKED = "25 Aug 2026, ~11:30 WIB";

export default function KinMailReAuditAfterFixes() {
  return (
    <Stack gap={24}>
      <Stack gap={8}>
        <H1>KIN Mail re-audit after Claude fixes</H1>
        <Text tone="secondary">
          {CHECKED}. Checked commit `0dc818d` against the previous canvas, then
          hunted leftover and new operator-breaking bugs on current main. This
          is static review of the live tree, not a live 2vm run.
        </Text>
        <Row gap={8} wrap>
          <Pill tone="success" active>
            5 HA items from last canvas are real fixes
          </Pill>
          <Pill tone="warning" active>
            Not clear to practice HA / Remove Host yet
          </Pill>
          <Pill tone="warning" active>
            1vm daily mail still the safer path
          </Pill>
        </Row>
      </Stack>

      <Callout tone="danger" title="Do not treat this as all-clear">
        Claude closed the previous Critical (lock steal) and most of that
        High HA list. A newer HA bug can still make Build HA pair look
        finished after a few seconds while Ansible keeps running. Remove
        Host can still report success when topology demote or uninstall
        failed. Practice 1vm if you want; do not practice Add second
        server / Remove Host until the new Critical is fixed.
      </Callout>

      <Grid columns={4} gap={16}>
        <Stat value="5/7" label="Prior HA findings actually closed" tone="success" />
        <Stat value="1" label="New Critical (HA stream dies early)" tone="danger" />
        <Stat value="1" label="Prior High only half-fixed" tone="warning" />
        <Stat value="8" label="Still open, operator-visible" tone="warning" />
      </Grid>

      <H2>Previous canvas: what Claude actually closed</H2>
      <Table
        headers={["Prior finding", "Status on HEAD", "Evidence"]}
        rowTone={[
          "success",
          "success",
          "success",
          "success",
          "success",
          "warning",
          "neutral",
          "neutral",
        ]}
        rows={[
          [
            "HA lock steal after 8s",
            "Fixed",
            "daemon.py:245-250 now defaults to pipeline_in_progress(); test_helper_busy_slot.py covers live HA marker",
          ],
          [
            "peer_console_state users last",
            "Fixed",
            "orchestration.py:1137-1151 pushes users.json before any completion marker",
          ],
          [
            "apply-file races users.json",
            "Fixed for that race",
            "console_users_sync.py flock USERS_MUTATION_LOCK shared by run_mutate and apply-file",
          ],
          [
            "ADMIN_PASS missing from HA redaction",
            "Fixed",
            "orchestration.py:1491-1496 appends ADMIN_PASS to the secrets list",
          ],
          [
            "Remove-host never demotes TOPOLOGY",
            "Half-fixed",
            "Demote helper exists (remove_host.py:233-322) but failure still returns ok:true / exit 0",
          ],
          [
            "License invalid-token fail-open",
            "Not touched",
            "commands.py:787-791 and app.py:826-852 still set provisioning_blocked False",
          ],
          [
            "E@syEmail bootstrap",
            "Left on purpose",
            "0dc818d commit message: called a deliberate, already-tested design",
          ],
          [
            "Unknown topology fail-open",
            "Left on purpose",
            "Same commit message; deploy_state.py:524-531 unchanged",
          ],
        ]}
      />

      <H2>New Critical: Build HA pair can lie that it finished</H2>
      <Text>
        Commit `5665e1e` added `--ensure-ssh-password` so a 1vm host that
        already reverted password SSH can still be reached by Ansible. The
        loop yields every subprocess event, including `done`.
      </Text>
      <Table
        headers={["What happens", "Why it is furious"]}
        rowTone={["danger", "warning", "warning"]}
        rows={[
          [
            "orchestration.py:1426-1433: async for ev in _stream_subprocess(... --ensure-ssh-password): yield ev",
            "Ansible steps later swallow subprocess done. This block does not. First done hits the UI.",
          ],
          [
            "DeploySession.tsx:337-341: on type===done, onFinished + es.close()",
            "Live log stops. Operator thinks HA ended after a few seconds. Job may still run in privhelper.",
          ],
          [
            "commands.py:870-876: non-zero ensure done stamps ORCH_FAILED step=preflight then continues HA",
            "If ensure-ssh fails, the transcript says preflight failed and Ansible still starts.",
          ],
        ]}
      />
      <Text tone="secondary">
        This is worse than the old lock-steal for day-to-day HA practice:
        the UI is wrong even on a successful ensure-ssh (exit 0). Fix:
        capture exit code, do not yield that done, abort HA if ensure-ssh
        fails.
      </Text>

      <H2>Still open bugs that break or lie to the operator</H2>
      <Table
        headers={["Sev", "Finding", "Hits", "Evidence"]}
        rowTone={[
          "warning",
          "warning",
          "warning",
          "warning",
          "warning",
          "info",
          "info",
          "info",
        ]}
        rows={[
          [
            "High",
            "Remove Host reports success even if topology demote failed. Survivor can stay TOPOLOGY=2vm; next password/user change 503s to a dead peer. Same original blast radius, now with a false sense that it was fixed.",
            "After Remove Host",
            "remove_host.py:580-642: demote notes are only logged; result ok is always True",
          ],
          [
            "High",
            "Graceful Remove Host: pcs standby then ansible fail leaves the node in standby. No unstandby.",
            "Failed remove-host",
            "remove_host.py:481-576",
          ],
          [
            "High",
            "Departing-node uninstall uses sudo -n. Password sudo fails; overall still ok:true; retired host left fully installed.",
            "Remove Host",
            "remove_host.py:591-629 vs wrap_privileged_remote",
          ],
          [
            "High",
            "Install can print All selected pipeline stages exited 0 and write setup-complete after 09-hardening / 11-admin-path-lockdown failed (warn-and-continue). UI greps that success line.",
            "1vm Deploy",
            "kin-mail.sh:377-404, 443-448",
          ],
          [
            "High",
            "Mailbox create: zmprov ca can succeed, auth probe fail, script returns 1. Account exists, seat burned, UI says create failed.",
            "Create mailbox",
            "08-create-mailbox.sh:304-320",
          ],
          [
            "High",
            "HA peer copy allowlist has no server-id and no license.token. Each node can mint its own Server ID. License apply is local. After VIP failover, Settings/license/seats can disagree with the other node.",
            "2vm + Settings license",
            "orchestration.py:787-788 _INSTALL_DEST_RE; deploy_state.py server-id / license.token",
          ],
          [
            "High",
            "Missing or invalid license token still means provisioning_blocked=False. CONTRACTED_SEATS in config remains the create gate. Corrupt the token after a valid apply and grace/expired no longer blocks.",
            "Licensed then tampered host",
            "commands.py:787-791; app.py:826-852; quota-gate.sh",
          ],
          [
            "Medium",
            "session.secret still per-node. VIP failover logs everyone out. Password change still needs no current password. AD down still 503-oracles AD usernames. Empty wizard Admin IPs still skip ufw.",
            "1vm and 2vm",
            "auth.py, Security.tsx, users.py, kin-mail.sh:256-265",
          ],
        ]}
      />

      <H3>Smaller, still real</H3>
      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>Replica ahead of Promoted</CardHeader>
          <CardBody>
            <Text>
              Users sync still pushes the replica first, then writes local
              with no rollback. If local save fails after a good push,
              password works on one node and not the other until the next
              Promoted write.
            </Text>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>License write order</CardHeader>
          <CardBody>
            <Text>
              write_license_token runs before CONTRACTED_SEATS is updated.
              A failed seats write can leave a token on disk and old seats
              in config.
            </Text>
          </CardBody>
        </Card>
      </Grid>

      <Divider />

      <H2>What is actually OK to practice now</H2>
      <Table
        headers={["Flow", "Practice?"]}
        rowTone={["success", "warning", "danger", "danger"]}
        rows={[
          [
            "Quiet 1vm: login, mailboxes (watch for orphan-on-auth-probe-fail), Settings that you already trust",
            "Yes, with eyes open on deploy success text vs 09/11",
          ],
          [
            "Fresh 1vm Deploy",
            "Yes, but do not trust All selected pipeline stages exited 0 if 09 or 11 printed a warn-and-continue",
          ],
          [
            "Add second server / Build HA pair",
            "No until the early done event is fixed. Log and UI will lie.",
          ],
          [
            "Remove Host",
            "No until demote failure fails the job, standby rolls back, and uninstall is not sudo -n",
          ],
        ]}
      />

      <H2>Fix order for Claude next</H2>
      <Text>
        1. HA ensure-ssh: do not yield subprocess done; abort HA on
        non-zero. 2. Remove Host: fail the job if demote failed; unstandby
        on ansible fail; password-sudo uninstall. 3. Sync server-id +
        license.token on HA finish (same class of bug as users.json used to
        be). 4. Invalid license must not unblock seats. 5. Do not write
        setup-complete / success line if 09/11 failed. 6. Rollback mailbox
        if auth probe fails after zmprov ca.
      </Text>
      <Text tone="secondary">
        Source: HEAD including 0dc818d and 5665e1e. Prior canvas items that
        Claude called deliberate (E@syEmail, topology fail-open) are still
        present; they are not the reason to stop 1vm practice.
      </Text>
    </Stack>
  );
}
