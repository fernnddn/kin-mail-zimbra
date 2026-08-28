import {
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  H2,
  Pill,
  Row,
  Stack,
  Stat,
  Table,
  Text,
} from "cursor/canvas";

const CHECKED = "25 Aug 2026, ~12:35 WIB";

export default function KinMail2HostPracticeGoNogo() {
  return (
    <Stack gap={24}>
      <Stack gap={8}>
        <H1>2-host practice: go / no-go</H1>
        <Text tone="secondary">
          {CHECKED}. HEAD `468fbbb` on local main (1 commit ahead of
          origin). Static re-read of the five blockers from the ff464fa
          canvas, then a 2-host apply-path hunt. Not a live pair run.
        </Text>
        <Row gap={8} wrap>
          <Pill tone="success" active>
            Last canvas blockers are closed
          </Pill>
          <Pill tone="success" active>
            First HA apply is clear to practice
          </Pill>
          <Pill tone="warning" active>
            Live 2vm is still the real proof
          </Pill>
        </Row>
      </Stack>

      <Callout tone="success" title="Cursor: yes for a first 2-host apply">
        The bugs that made Build HA pair, Deploy, and Remove Host lie are
        closed on this commit. A first Add second server / Build HA pair
        is no longer blocked by those. Fill Admin IPs, pick 2-server
        topology from the start, and expect a re-login if you later fail
        the VIP onto the other node.
      </Callout>

      <Grid columns={4} gap={16}>
        <Stat value="5/5" label="ff464fa leftover blockers closed" tone="success" />
        <Stat value="405" label="Backend tests on the fix set" tone="success" />
        <Stat value="468fbbb" label="Local commit" />
        <Stat value="Static" label="No live 2vm in this pass" tone="warning" />
      </Grid>

      <H2>Scorecard vs the last canvas</H2>
      <Table
        headers={["Was", "On 468fbbb", "Evidence"]}
        rowTone={["success", "success", "success", "success", "success"]}
        rows={[
          [
            "server-id / license.token after ha-setup-complete",
            "Fixed",
            "orchestration.py:1153-1158 pushes identity before any completion marker. Failure writes no ha-setup-complete. Token mode 600 kin-console, not 644.",
          ],
          [
            "Full install complete, with warnings grepped as Complete",
            "Fixed",
            "deployPipeline.ts:95-99 and 135-143. Complete cannot win over [FAIL] or the with-warnings banner.",
          ],
          [
            "Invalid license fail-open",
            "Fixed",
            "license_view() blocks present-but-unverified tokens. commands.py create, _set_seats, and /api/mailbox all use it. Missing token still uses CONTRACTED_SEATS.",
          ],
          [
            "revert-ssh failure still claimed every stage exited 0",
            "Fixed",
            "kin-mail.sh:445 adds it to soft_failed_stages",
          ],
          [
            "Remove Host ok:true when departing uninstall failed",
            "Fixed",
            "remove_host.py:698-717 returns ok:false / exit 1. Forced path still skips uninstall.",
          ],
        ]}
      />

      <H2>Still true, not a first-apply blocker</H2>
      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>VIP failover logs you out</CardHeader>
          <CardBody>
            <Text>
              session.secret is still per-node and not HA-copied. First
              apply keeps the VIP on this host, so the console cookie
              stays valid. If you later move the VIP, sign in again on
              the other node. Cluster mail is not affected.
            </Text>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Empty Admin IPs skip ufw</CardHeader>
          <CardBody>
            <Text>
              Wizard still treats Admin IPs as optional. Console deploy
              then skips stage 10 with a warning. Fill them before
              Deploy if you want host firewall on both nodes. FortiGate
              is the only perimeter until you do.
            </Text>
          </CardBody>
        </Card>
      </Grid>

      <Divider />

      <H2>Practice sequence</H2>
      <Table
        headers={["Step", "Do this"]}
        rows={[
          [
            "1. Wizard on host A",
            "Topology 2-server from the start (do not install as 1vm then convert). Set MAIL_HOST to hostname -f. Fill Admin IPs. Store root + kin passwords.",
          ],
          [
            "2. Deploy on A",
            "Watch the log, not only the bar. If you see with warnings, stop and re-run 09/11/firewall before Add second server.",
          ],
          [
            "3. Add second server",
            "Peer is a blank OS with SSH. Do not run console/bootstrap.sh on B. Observability / qdevice VM must be reachable. Build HA pair once, watch until ORCH_DONE.",
          ],
          [
            "4. After ORCH_DONE",
            "Login-gated console on both markers. VIP on A. Mail stays up. Optional later: VIP failover (expect a fresh login). Remove Host only after you have a pair you are willing to tear down.",
          ],
        ]}
      />

      <Text tone="secondary">
        Source: local HEAD `468fbbb`. Untracked copies of old canvases in
        the git repo root are leftover files, not part of this commit.
        Push to origin when you want the remotes to match.
      </Text>
    </Stack>
  );
}
