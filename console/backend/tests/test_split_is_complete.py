"""A split is two machines, and every stage has to know which one it is on.

The split topology separates the MTA and proxy (edge, facing the internet) from
the directory and mail store (mailbox, facing nothing). Three stages were taught
that. The rest still assumed one host that does everything, and each of those
assumptions fails differently:

  the firewall   opened 110/143/993/995 on the mailbox, which is the precise
                 exposure a split exists to remove
  the gateway    aimed at SERVER_IP, so on a split it pointed the MX at a
                 machine with no MTA: mail accepted from the internet and then
                 undeliverable
  hardening      wrote Postfix settings on a node with no Postfix
  the healthcheck reported a dozen failures on a node that was working
  backup         refused with Pacemaker vocabulary on a deployment with no
                 cluster, so the real instruction never reached the operator
  the uninstaller wiped one half and said nothing about the other

These are source-level guards. Every one names the failure it prevents, because
a guard whose purpose nobody remembers is a guard somebody deletes.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def code(rel: str) -> str:
    """The file with comment lines removed.

    A guard satisfied by the comment explaining it is not a guard; this
    repository has been caught by that more than once.
    """
    return "\n".join(
        ln
        for ln in (REPO / rel).read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )


class TheFirewallKnowsWhichNodeItIsOn(unittest.TestCase):
    def setUp(self) -> None:
        self.c = code("install/10-host-firewall.sh")

    def _mailbox_branch(self) -> str:
        """The mailbox-only rules, ending at the branch's own early return.

        Sliced on `return 0` rather than on a comment: code() strips comments,
        so any marker inside one is gone by the time this runs.
        """
        start = self.c.index('if [ "$node_role" = "mailbox" ]')
        end = self.c.index("\n    return 0", start)
        return self.c[start:end]

    def test_it_asks_for_the_role(self) -> None:
        self.assertIn("kin_node_role", self.c)

    def test_it_refuses_rather_than_guessing(self) -> None:
        # One wrong guess publishes every mailbox port; the other stops all mail.
        block = self.c[self.c.index("node_role=$(kin_node_role)") :][:600]
        self.assertIn("exit 2", block)

    def test_the_mailbox_gets_no_public_mail_ports(self) -> None:
        mbox = self._mailbox_branch()
        for port in ("110", "143", "993", "995", "465", "587"):
            with self.subTest(port=port):
                self.assertNotIn(f'ufw allow "{port}"', mbox)
                self.assertNotIn(f"ufw allow {port}/tcp", mbox)

    def test_the_mailbox_trusts_the_edge(self) -> None:
        self.assertIn('ufw allow from "$EDGE_IP"', self.c)

    def test_it_refuses_a_mailbox_with_no_edge_address(self) -> None:
        """Otherwise the node is firewalled off from the only machine it talks to."""
        self.assertIn('if [ -z "${EDGE_IP:-}" ]; then', self.c)

    def test_the_mailbox_branch_still_arms_the_dead_man(self) -> None:
        # It returns early, so it has to do this itself or a mistake locks the
        # operator out permanently.
        mbox = self._mailbox_branch()
        self.assertIn("start_deadman", mbox)
        self.assertIn("ufw --force enable", mbox)

    def test_a_single_appliance_is_untouched(self) -> None:
        self.assertIn('local node_role="all"', self.c)
        self.assertIn("ufw allow 25/tcp", self.c)


class TheGatewayAimsAtTheMachineWithAnMta(unittest.TestCase):
    def setUp(self) -> None:
        self.c = code("install/lib/mail-gateway.sh")

    def test_the_mta_address_is_derived_once(self) -> None:
        self.assertIn('MTA_IP="$EDGE_IP"', self.c)
        self.assertIn('MTA_IP="$SERVER_IP"', self.c)

    def test_nothing_after_the_config_check_still_uses_server_ip(self) -> None:
        """One missed use points the MX at a machine with no Postfix."""
        after = self.c[self.c.index("resolve_tls() {") :]
        leftovers = [ln.strip() for ln in after.splitlines() if "SERVER_IP" in ln]
        self.assertEqual(leftovers, [], f"still aimed at SERVER_IP: {leftovers}")

    def test_it_refuses_to_be_run_from_the_mailbox_node(self) -> None:
        self.assertIn("wrong-node", self.c)
        self.assertIn("host_has_ipv4", self.c)

    def test_a_split_without_an_edge_address_is_named(self) -> None:
        self.assertIn("no-edge-ip", self.c)
        self.assertIn("bad-edge-ip", self.c)


class HardeningSkipsWhatIsNotOnThisNode(unittest.TestCase):
    def setUp(self) -> None:
        self.c = code("install/09-hardening.sh")

    def test_it_knows_the_role(self) -> None:
        self.assertIn("kin_node_role", self.c)

    def test_mta_hardening_is_skipped_on_the_mailbox(self) -> None:
        block = self.c[self.c.index('if [ "$KIN_NODE_ROLE" = "mailbox" ]') :][:400]
        self.assertNotIn("configure_smtp_rates", block.split("else")[0])
        self.assertIn("configure_smtp_rates", block.split("else")[1])
        self.assertIn("configure_mail_surface", block.split("else")[1])

    def test_directory_settings_still_run_everywhere(self) -> None:
        # Password lockout, cleartext login and TLS are LDAP-global and belong
        # on whichever node is running.
        for fn in ("configure_lockout", "configure_cleartext", "configure_tls"):
            with self.subTest(fn=fn):
                self.assertRegex(self.c, rf"(?m)^{fn}$")


class TheHealthcheckDoesNotCryWolfOnASplit(unittest.TestCase):
    def setUp(self) -> None:
        self.c = code("install/05-healthcheck.sh")

    def test_it_reports_which_node_it_is_on(self) -> None:
        self.assertIn("KIN_NODE_ROLE", self.c)
        self.assertIn("this is the ${KIN_NODE_ROLE} node", self.c)

    def test_mta_checks_are_blocked_not_failed_on_the_mailbox(self) -> None:
        """BLOCKED means outside this machine. FAILED would abort the pipeline."""
        start = self.c.index('say "Outbound SMTP"')
        block = self.c[start : start + 400]
        self.assertIn("  b ", block)
        self.assertNotIn("\n  f ", block)


class LifecycleKnowsThereAreTwoMachines(unittest.TestCase):
    def test_backup_names_the_machine_that_holds_the_mail(self) -> None:
        c = code("backup/kin-mail-backup-remote.sh")
        self.assertIn("MAILBOX_HOST", c)
        self.assertIn("edge node of a split", c)

    def test_backup_still_refuses_rather_than_taking_an_empty_one(self) -> None:
        c = code("backup/kin-mail-backup-remote.sh")
        block = c[c.index("if ! is_promoted_here; then") :][:900]
        self.assertEqual(block.count("exit 3"), 2, "both paths must refuse")

    def test_the_uninstaller_says_what_is_still_standing(self) -> None:
        c = code("install/kin-mail-uninstall.sh")
        self.assertIn("only wipes THIS machine", c)
        self.assertIn("MAILBOX_HOST", c)
        self.assertIn("EDGE_HOST", c)

    def test_the_uninstaller_does_not_refuse(self) -> None:
        """Rebuilding one node is legitimate; being uninformed is not."""
        c = code("install/kin-mail-uninstall.sh")
        block = c[c.index("only wipes THIS machine") :][:1400]
        self.assertNotIn("exit 1", block)
        self.assertNotIn("exit 2", block)


class ASplitDeploymentFinishesTheWholePipeline(unittest.TestCase):
    """The biggest gap of all, and the easiest to reintroduce.

    kin-mail.sh delegated the entire full-install to kin-mail-split.sh and
    returned its exit code. The splitter builds Zimbra on both machines and
    stops, so on a split NOTHING after stage 03 ever ran: no TLS certificate,
    no DKIM, no hardening, no firewall, no admin-path lockdown, no branding.
    It looked finished, because the splitter printed SPLIT BUILD DONE - and it
    was an unhardened, uncertificated, unfirewalled mail server facing the
    internet.

    The edge continues the ordinary pipeline; the mailbox is dealt with inside
    the splitter, over the SSH channel it already has.
    """

    def setUp(self) -> None:
        self.main = code("install/kin-mail.sh")
        self.split = code("install/kin-mail-split.sh")

    def test_the_pipeline_does_not_return_after_the_split_build(self) -> None:
        block = self.main[self.main.index("local splitter=") :][:1400]
        self.assertNotIn("return $?", block, "returning here skips every later stage")
        self.assertIn("split_built=1", block)

    def test_a_failed_split_build_still_stops_the_pipeline(self) -> None:
        """Continuing past a broken build would harden half a machine."""
        block = self.main[self.main.index("local splitter=") :][:1400]
        self.assertIn('"$splitter" || {', block)
        self.assertIn('return "$rc"', block)

    def test_the_first_three_stages_are_not_repeated_on_a_split(self) -> None:
        # The splitter already ran them, in the only order that works.
        self.assertIn('[ "${split_built:-0}" = 1 ] ||', self.main)

    def test_split_built_is_declared_so_set_u_cannot_trip(self) -> None:
        self.assertRegex(self.main, r"local .*split_built=0")

    def test_the_mailbox_is_hardened_and_firewalled_by_the_splitter(self) -> None:
        self.assertIn("mailbox-hardened", self.split)
        self.assertIn("mailbox-firewalled", self.split)
        self.assertIn("09-hardening.sh", self.split)
        self.assertIn("10-host-firewall.sh", self.split)

    def test_the_firewall_runs_last_on_the_mailbox(self) -> None:
        """It is the one stage that can cut the SSH session doing the work."""
        self.assertLess(
            self.split.index("mailbox-hardened"),
            self.split.index("mailbox-firewalled"),
        )

    def test_the_remote_firewall_is_given_its_apply_argument(self) -> None:
        # Without it the stage prints usage and does nothing, and the marker
        # would record that as done.
        self.assertIn('10-host-firewall.sh "mailbox 10-host-firewall" "" apply', self.split)
        self.assertIn('${stage} ${5:-}', self.split)

    def test_the_mailbox_dead_man_is_cancelled(self) -> None:
        """Nobody is watching that machine to do it, and ufw would switch off."""
        self.assertIn("cancel-deadman", self.split)

    def test_a_failed_mailbox_firewall_is_loud(self) -> None:
        block = self.split[self.split.index("mailbox-firewalled") :][:900]
        self.assertIn("unfirewalled", block)
        self.assertIn("_fail=1", block)

    def test_the_closing_message_does_not_claim_more_than_was_done(self) -> None:
        # It used to end by saying the next step was the mail gateway, which
        # read as "everything else is finished".
        tail = self.split[self.split.index("SPLIT BUILD DONE") :]
        self.assertIn("follow now", tail)


class TheStoreHandsOutboundMailToTheEdge(unittest.TestCase):
    """Hole 2.1: SMTPHOST on the mailbox pointed at itself.

    mailboxd hands outbound mail to SMTPHOST. The mailbox has no Postfix, so
    a self-address here is how webmail queues forever with nothing in the log
    that looks like an error. The generated defaults must name the edge, and
    the validator must refuse a store file that does not.
    """

    def setUp(self) -> None:
        self.c = code("install/lib/zcs-defaults.sh")

    def test_the_store_profile_names_the_edge(self) -> None:
        self.assertIn('SMTPHOST="${EDGE_HOST}"', self.c)
        self.assertNotIn('SMTPHOST="${MAILBOX_HOST}"', self.c)

    def test_a_store_that_points_at_itself_is_rejected(self) -> None:
        self.assertIn("store SMTPHOST is this machine", self.c)
        self.assertIn("store has no SMTPHOST", self.c)

    def test_snmp_traps_still_go_to_the_mailbox(self) -> None:
        # Logger lives there. Traps are off (SNMPNOTIFY=no), so a wrong
        # value cannot move mail, but the destination is still the logger.
        self.assertIn('SNMPTRAPHOST="${MAILBOX_HOST}"', self.c)


class TheGatewayWritesRelayHostOnTheMtaServerObject(unittest.TestCase):
    """Hole 2.2: zmprov ms MAIL_HOST, which is the public name, not the edge.

    MAIL_HOST is what the operator typed. In LDAP the edge's server object
    is EDGE_HOST. Writing the relay onto the public name either no-ops or
    writes a server that does not run Postfix. MTA_HOST was derived for
    this and has to be the name used at the three RelayHost calls.
    """

    def setUp(self) -> None:
        self.c = code("install/lib/mail-gateway.sh")

    def test_relay_host_reads_and_writes_use_the_mta_server_object(self) -> None:
        after = self.c[self.c.index("zimbra_relay_host()") :]
        self.assertIn('zm gs "${MTA_HOST}" zimbraMtaRelayHost', after)
        self.assertIn('zm ms "${MTA_HOST}" zimbraMtaRelayHost', after)
        self.assertNotIn('zm gs "${MAIL_HOST}" zimbraMtaRelayHost', after)
        self.assertNotIn('zm ms "${MAIL_HOST}" zimbraMtaRelayHost', after)

    def test_gateway_is_self_still_compares_the_public_name(self) -> None:
        # That check is "did the operator type this appliance as the
        # gateway", which is the public name, not the server object.
        block = self.c[self.c.index("gateway-is-self") :][:800]
        self.assertIn('"${GW_HOST}" = "${MAIL_HOST}"', block)


class ZPushTalksToMailboxdNotToLoopback(unittest.TestCase):
    """Hole 2.3: Z-Push on the edge pointed at 127.0.0.1:8443.

    8443 is mailboxd, which is on the mailbox. Loopback on the edge has
    nothing listening there. ActiveSync dies while webmail and IMAP still
    work, so nobody notices during the install.
    """

    def setUp(self) -> None:
        self.c = code("install/07-zpush.sh")

    def test_a_split_points_at_the_mailbox_not_loopback(self) -> None:
        # The split default has to be assigned first, otherwise the
        # 1vm loopback default wins on both topologies.
        split_url = self.c.index("https://${MAILBOX_HOST}:8443")
        loopback = self.c.index("https://127.0.0.1:8443")
        self.assertLess(split_url, loopback)
        preamble = self.c[:split_url]
        self.assertIn("kin_topology_is_split", preamble[-400:])

    def test_it_refuses_to_install_on_the_mailbox_node(self) -> None:
        self.assertIn("Z-Push is installed on the edge", self.c)

    def test_throttle_safe_ips_include_the_edge_address(self) -> None:
        # mailboxd sees the edge, not loopback, so 127.0.0.1 on the
        # mailbox server object does not cover the Z-Push path.
        self.assertIn("+zimbraHttpThrottleSafeIPs", self.c)
        self.assertIn("mailboxd will not throttle the edge", self.c)


class InternalMailFlowIsNotFailedOnTheWrongNode(unittest.TestCase):
    """Hole 2.4: T1 submitted on the mailbox, then read the local store.

    Submission is on the edge. Delivery lands on the mailbox. A local-log
    check on the wrong node reports FAILED, and FAILED aborts the install.
    Off-node checks are BLOCKED.
    """

    def setUp(self) -> None:
        self.c = code("install/05-healthcheck.sh")

    def test_t1_is_blocked_not_failed_on_the_mailbox(self) -> None:
        start = self.c.index('say "T1 - internal mail flow"')
        t1 = self.c[start : start + 1800]
        mailbox_branch = t1.split('if [ "$KIN_NODE_ROLE" = "mailbox" ]', 1)[1].split("else", 1)[0]
        self.assertIn("  b ", mailbox_branch)
        self.assertNotIn("\n  f ", mailbox_branch)
        self.assertIn("submission is on the edge", mailbox_branch)

    def test_the_edge_proves_lmtp_instead_of_reading_the_store(self) -> None:
        self.assertIn("LMTP path to the mailbox", self.c)
        self.assertIn("${MAILBOX_IP}/7025", self.c)

    def test_dkim_is_blocked_on_the_mailbox_not_failed(self) -> None:
        start = self.c.index('say "DKIM"')
        block = self.c[start : start + 700]
        mailbox_branch = block.split('if [ "$KIN_NODE_ROLE" = "mailbox" ]', 1)[1].split("else", 1)[0]
        self.assertIn("  b ", mailbox_branch)
        self.assertNotIn("\n  f ", mailbox_branch)

    def test_an_unknown_role_is_refused_not_treated_as_a_single_appliance(self) -> None:
        self.assertIn('KIN_NODE_ROLE="unknown"', self.c)
        unknown = self.c[self.c.index('if [ "$KIN_NODE_ROLE" = "unknown" ]') :][:500]
        self.assertIn("exit 1", unknown)


class TheFirewallAppliesWithoutAdminIps(unittest.TestCase):
    """Hole 2.5, and the rest of it.

    The wizard treats Admin IPs as optional and says they can be narrowed
    later from the console. Refusing to firewall without them printed
    SPLIT BUILD INCOMPLETE over a store that was still on the internet.

    That was fixed for the mailbox only, on the stated grounds that an edge
    without admin IPs "would publish SSH and the console". It would not:
    without an admin list those stay open to this host's own subnet via
    CLUSTER_NET and to nothing else, while the public ports are the mail
    ports. What publishes SSH and the console is refusing - because then ufw
    never runs and every port on the machine is open, on the node that faces
    the internet by design.

    So the exemption is not the mailbox. It is every node.
    """

    def setUp(self) -> None:
        self.c = code("install/10-host-firewall.sh")
        self.m = code("install/kin-mail.sh")

    def test_the_role_is_known_before_the_admin_ip_decision(self) -> None:
        role = self.c.index("node_role=$(kin_node_role)")
        decision = self.c.index("No admin IPs configured")
        self.assertLess(role, decision)

    def test_the_mailbox_is_still_firewalled_without_admin_ips(self) -> None:
        self.assertIn("This mailbox will still be firewalled", self.c)

    def test_every_other_node_is_too(self) -> None:
        self.assertIn("This host will still be firewalled", self.c)

    def test_no_node_exits_over_a_missing_admin_list(self) -> None:
        start = self.c.index('if [ "$node_role" != "mailbox" ] && [ -z "$ADMIN_IPS" ]')
        block = self.c[start : self.c.index('say "Installing ufw', start)]
        self.assertNotIn("exit 2", block)
        self.assertNotIn("exit 1", block)

    def test_admin_surface_without_a_list_is_the_lan_not_the_internet(self) -> None:
        # The claim the warning makes has to be the rules the stage writes.
        for port in ("22", "7071", '"$CONSOLE_PORT"'):
            self.assertIn(
                f'ufw allow from "$CLUSTER_NET" to any port {port}',
                self.c,
                f"port {port} is not restricted to the local subnet",
            )

    def test_the_console_path_no_longer_refuses_before_the_stage_runs(self) -> None:
        # kin-mail.sh gated this too, so the stage never got the chance.
        self.assertIn("KIN_ADMIN_IPS is empty - applying the firewall", self.m)
        block = self.m[self.m.index("ensure_admin_ips_for_firewall()") :][:1400]
        console = block[block.index('if [ "$CONSOLE_CONFIRMED" = "1" ]') :][:800]
        self.assertIn("return 0", console)

    def test_it_still_refuses_what_it_cannot_guess(self) -> None:
        # Dropping the admin-IP refusal must not drop the refusals that stop
        # the stage guessing a peer address it was never told.
        self.assertIn("MAILBOX_IP is not set", self.c)
        self.assertIn("EDGE_IP is not set", self.c)
        self.assertIn("exit 2", self.c)


class ACertificateIsNotWorthAnUnfirewalledServer(unittest.TestCase):
    """04 issuance depends on public DNS, propagation, a CA's rate limits and,
    for TLS_METHOD=manual, a human publishing a TXT record within 25 minutes.

    All of it is outside the machine, and none of it stops mail: Zimbra keeps
    serving its self-signed certificate. Stopping the pipeline there cost the
    DKIM key, the hardening, the host firewall, the admin-path lockdown and the
    healthcheck - on a split, on the edge.
    """

    def setUp(self) -> None:
        self.c = code("install/04-tls-dkim.sh")
        self.m = code("install/kin-mail.sh")

    def test_a_failed_tls_phase_does_not_end_the_stage(self) -> None:
        self.assertIn("TLS_PHASE_OK", self.c)

    def test_dkim_still_runs_after_a_failed_certificate(self) -> None:
        recorded = self.c.index('TLS_PHASE_OK" -eq 0')
        dkim = self.c.index('say "DKIM"')
        self.assertLess(recorded, dkim)

    def test_the_stage_still_exits_non_zero(self) -> None:
        dkim = self.c.index('say "DKIM"')
        self.assertIn('TLS_PHASE_OK" -eq 0', self.c[dkim:])

    def test_the_pipeline_continues_and_records_it(self) -> None:
        self.assertIn('soft_failed_stages+=("04-tls-dkim.sh")', self.m)

    def test_it_is_not_reported_as_a_clean_install(self) -> None:
        self.assertIn("Full install complete, with warnings", self.m)


class LifecycleKnowsTheMailboxHoldsTheMail(unittest.TestCase):
    def test_restore_refuses_the_edge_of_a_split(self) -> None:
        c = code("backup/kin-mail-restore.sh")
        self.assertIn("restore onto the mailbox, which holds the mail", c)
        self.assertIn("same directory passwords", c)

    def test_tls_is_skipped_on_the_mailbox(self) -> None:
        c = code("install/04-tls-dkim.sh")
        self.assertIn("This is the mailbox node; skipping", c)
        self.assertIn("kin_node_role", c)

    def test_a_directory_that_does_not_list_the_edge_fails_the_build(self) -> None:
        c = code("install/kin-mail-split.sh")
        self.assertIn("The directory does not list this edge", c)
        self.assertIn("The directory does not list the mailbox", c)
        self.assertIn("A leftover server object", c)

    def test_the_backup_split_refusal_does_not_call_undefined_info(self) -> None:
        # set -u. `info` is not defined in this script; using it after the
        # fail line would crash and hide the instruction to run on the mailbox.
        c = code("backup/kin-mail-backup-remote.sh")
        self.assertNotRegex(c, r"(?m)^\s+info ")


class EveryStageThatBranchesOnRoleAsksTheSameWay(unittest.TestCase):
    """One way of asking, so a stage cannot answer it differently."""

    STAGES = (
        "install/10-host-firewall.sh",
        "install/09-hardening.sh",
        "install/05-healthcheck.sh",
        "install/02-prepare-os.sh",
        "install/03-install-zimbra.sh",
        "install/07-zpush.sh",
        "install/04-tls-dkim.sh",
    )

    def test_none_of_them_decide_the_role_from_a_hostname(self) -> None:
        # Hostnames are set by stage 02 and are wrong for the whole window
        # before it runs. An address is a fact about the machine.
        for rel in self.STAGES:
            with self.subTest(stage=rel):
                c = code(rel)
                self.assertNotRegex(
                    c,
                    r"hostname.*==.*(EDGE_HOST|MAILBOX_HOST)",
                    "role must come from addresses, never hostnames",
                )

    def test_each_one_sources_the_config_that_defines_the_helpers(self) -> None:
        for rel in self.STAGES:
            with self.subTest(stage=rel):
                self.assertIn(". ./00-config.sh", code(rel))


if __name__ == "__main__":
    unittest.main()
