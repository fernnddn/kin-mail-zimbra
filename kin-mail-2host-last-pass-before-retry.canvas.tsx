import {
  Callout,
  Card,
  CardBody,
  CardHeader,
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

const CHECKED = "25 Aug 2026, ~18:55 WIB";

export default function KinMail2HostLastPassBeforeRetry() {
  return (
    <Stack gap={24}>
      <Stack gap={8}>
        <H1>Last pass before retry</H1>
        <Text tone="secondary">
          {CHECKED}. Second Cursor hunt after the fail2ban/handoff fix.
          Target: retry of the failed gits-it pair, and a later fresh 2-server
          apply. Static plus helper tests. Not a live VM run.
        </Text>
        <Row gap={8} wrap>
          <Pill tone="success" active>
            Hunt leftovers closed
          </Pill>
          <Pill tone="warning" active>
            Pull onto Mail A first
          </Pill>
          <Pill tone="info" active>
            Then one Build HA pair retry
          </Pill>
        </Row>
      </Stack>

      <Callout tone="success" title="Clear to retry after this code is on Mail A">
        The first pass closed the live log (fail2ban holders, unmounted-path
        fuser skip, one node continuing create-md alone). This pass closed
        what a retry of that leftover state would still hit in step 1:
        peer_os_prep running stale 02/03 on Mail B against an unmounted
        /opt/zimbra. After git pull on the console host, retry Build HA pair
        once. Do not retry on c2c9994.
      </Callout>

      <Grid columns={4} gap={16}>
        <Stat value="11" label="Holes closed this hunt" tone="danger" />
        <Stat value="3" label="Holes from the handoff pass" tone="success" />
        <Stat value="361" label="Backend tests collected (5 bcrypt import errors, pre-existing)" />
        <Stat value="Static" label="Proof until the VMs run it" tone="warning" />
      </Grid>

      <H2>What this pass would still have broken</H2>
      <Table
        headers={["Hole", "Retry of tonight's pair", "Fresh 2-server apply", "Fix"]}
        rowTone={["danger", "danger", "danger", "danger", "danger", "danger", "danger", "danger", "danger", "danger", "danger"]}
        rows={[
          [
            "B keeps a stale /opt/kin-mail-deploy",
            "peer_os_prep only copied the tree when kin-mail.sh was missing. B already has the first-attempt tree, so 02/03 would not see A's new handoff/probe scripts.",
            "First apply copies because the tree is missing. No issue until a later failed retry.",
            "join_mode=apply always refreshes install/ from A. Copy failure now fails the step even if an old tree exists.",
          ],
          [
            "02/03 treat a busy LUKS mapper as empty",
            "zimbra_data_disk_has_real_install used mount -o ro. If fail2ban still holds the mapper, that mount fails, 03 can run_fresh onto the empty /opt/zimbra mountpoint, or 02 remounts then 03 fail_broken because setup-complete exists.",
            "First apply has /opt/zimbra mounted with a live tree until the handoff. This path is the mid-handoff leftover, not greenfield 02.",
            "debugfs for /bin/zmcontrol without mounting. Busy ext4 + setup-complete still counts as a real install. 02 exits early on zimbra_is_mid_handoff. Probe also drops fail2ban jails before looking.",
          ],
          [
            "create-md if the meta-loop is down",
            "dump-md failing because /dev/loop21 is missing looked like empty metadata. printf yes | create-md would rewrite mail1's existing MD.",
            "Same if the loop unit is not started before activate.",
            "Assert the meta-disk is a live block device before create-md. Missing loop fails closed.",
          ],
          [
            "qdevice needs_device was run_once",
            "First host's grep won for both nodes. Asymmetric corosync.conf would leave B unwired while the assert still passed.",
            "Same on a fresh pair if A is written first and the fact is frozen.",
            "Per-host fact; reload if any host needed a write; confirm assert on every host.",
          ],
          [
            "create-md if dump-md is busy",
            "dump-md failing with busy/open on a live loop still looked empty. printf yes | create-md would rewrite mail1 MD.",
            "Same if udev or another drbdadm has the meta device open.",
            "Refuse create-md when dump-md output matches busy/open/permission errors.",
          ],
          [
            ".res grep under --check",
            "Template skipped in check mode; grep of a missing kin-zimbra.res fails and any_errors_fatal aborts live_join_check.",
            "First-apply dry-run before DRBD has written .res fails the same way.",
            "Gate those greps with not ansible_check_mode. Same for /etc/default/sbd greps.",
          ],
          [
            "LDAP assert on dry-run",
            "If /opt/zimbra is still mounted, --check reads FQDN LDAP URLs, skips the set, then asserts localhost.",
            "join_mode=check before pacemaker apply fails on a healthy single-node tree.",
            "LDAP assert is apply-only. live_join_check skips ldap and memcached tags.",
          ],
          [
            "add-host create-md hangs",
            "drbdadm create-md waits for yes on stdin with no timeout. A leftover MD on the new node hangs the play.",
            "Same on a retry of mail-add-host.yml.",
            "dump-md first, refuse busy/missing meta, printf yes only when metadata is absent.",
          ],
          [
            "pcs cluster setup on first host only",
            "run_once evaluated when: on inventory[0]. If A looks empty and B already runs a cluster, setup still fires.",
            "Same if a botched first form left only B in the cluster.",
            "Form/auth only when no play host is running or configured. Membership assert reads a running host.",
          ],
          [
            "create-md while the resource is already up",
            "Both-Secondary leftover still needs_activate on A. dump-md can fail for in-use reasons outside the busy regex.",
            "Same after an interrupt between up and --force.",
            "create-md only when status was down, or after an intentional Diskless down.",
          ],
          [
            "Corosync reload used host A's changed flag",
            "If only B still had a hostname ring0_addr, B's conf was fixed on disk but reload never ran.",
            "Then hosts.yml maps the service hostname to 127.0.0.1 and membership follows loopback.",
            "Reload on every host whose replace actually changed. ring0 assert is apply-only.",
          ],
        ]}
      />

      <H2>Still closed from the log-phase1 pass</H2>
      <Table
        headers={["Layer", "What was wrong", "Still in this tree"]}
        rowTone={["success", "success", "success"]}
        rows={[
          [
            "Handoff before umount",
            "fail2ban zimbra-auth / zpush-auth kept mailbox.log FDs on the mapper. OCF already dropped those jails; the handoff did not.",
            "drop_zimbra_log_holders before zmcontrol stop. Helper staged into the handoff dir.",
          ],
          [
            "Already-unmounted retry",
            "Shortcut removed fstab and skipped fuser. mail2 is in exactly that state.",
            "Unmounted path drops jails, waits until the mapper has no holders, then removes fstab.",
          ],
          [
            "Pair play + attach window",
            "mail2 failed release; mail1 still ran create-md and drbdadm up. mail1 passed fuser then still could not attach.",
            "any_errors_fatal on mail-drbd and the other 2-node plays. activate.yml waits immediately before drbdadm up.",
          ],
        ]}
      />

      <H2>Retry story for this pair</H2>
      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>Steps 1-9</CardHeader>
          <CardBody>
            <Text>
              Cluster already exists; pcs cluster setup is skipped. SBD dump
              succeeds so create is skipped. Qdevice already wired. peer_os_prep
              now pushes a fresh install tree, 02 sees mid-handoff and leaves
              the mapper unmounted, 03 skips the installer. os_hardening auto
              will not re-enable zimbra jails while mailbox.log is off the
              mountpoint.
            </Text>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Step 10 mail_drbd_activate</CardHeader>
          <CardBody>
            <Text>
              mail1: dump-md skip (create-md already ran), wait for a free
              mapper, drbdadm up, primary --force if not Primary. mail2: drop
              jails, wait fuser, remove dirty fstab, create-md, up. Pacemaker
              still does not own kin-drbd-clone, so activate is supposed to
              run. Mail stays down until the stack play mounts /dev/drbd0.
            </Text>
          </CardBody>
        </Card>
      </Grid>

      <H2>Not this failure, still true later</H2>
      <Table
        headers={["Leftover", "Why it is not a retry blocker"]}
        rowTone={["warning", "warning", "success", "warning"]}
        rows={[
          [
            "session.secret is per-node",
            "VIP stays on Mail A for first apply. Sign in again if you later fail the VIP over.",
          ],
          [
            "Empty Admin IPs skip ufw",
            "Wizard choice. Fill Admin IPs if you want host ufw.",
          ],
          [
            "/api/me/password now requires the current password",
            "Closed in this pass. Security page sends current_password; API rejects a wrong current password.",
          ],
          [
            "AD 503 oracle",
            "Directory lookup fail-open. Not this playbook.",
          ],
        ]}
      />

      <Callout tone="warning" title="Operational, not another code hole">
        Ansible stages the handoff scripts from Mail A's KIN_MAIL_DEPLOY_DIR
        (usually /opt/kin-mail-deploy). git pull on the laptop is not enough.
        The console host that runs Build HA pair must have this tree. Then
        retry once. This pass cannot prove the live mapper is free until
        that run.
      </Callout>
    </Stack>
  );
}
