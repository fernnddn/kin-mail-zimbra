"""A greenfield appliance must still deploy, gateway or no gateway.

The mail gateway is mandatory from 0.1.10, and the obvious way to enforce that
is to fail the healthcheck when no gateway is linked. That is wrong, and
getting it wrong is expensive: 05-healthcheck.sh is the last stage of the
install pipeline and kin-mail.sh aborts the whole deploy when it returns
non-zero. A gateway is a separate VM which cannot exist before the appliance
does, so failing there would stop every single first install.

The rule this file pins is therefore a shape, not a preference:

  * no gateway recorded at all  -> BLOCKED. Outside this server, like DNS.
  * a gateway recorded but broken -> FAILED. This server is misconfigured.

These are read out of the script source. Every guard here strips comments
first: three earlier guards in this repository passed by matching their own
explanatory prose rather than the code they were meant to be checking.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HEALTHCHECK = REPO / "install/05-healthcheck.sh"
FIREWALL = REPO / "install/10-host-firewall.sh"
PIPELINE = REPO / "install/kin-mail.sh"


def _code(path: Path) -> str:
    """The script with comments and here-doc prose removed.

    A guard that can be satisfied by a comment is not a guard.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Trailing comments too, but not a '#' inside a quoted string.
        if "#" in line and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = re.sub(r"\s+#(?!\{).*$", "", line)
        out.append(line)
    return "\n".join(out)


def _gateway_block(text: str) -> str:
    """The healthcheck's mail gateway section only."""
    start = text.index('say "Mail gateway"')
    end = text.index("# --- summary", start) if "# --- summary" in text[start:] else len(text)
    end = text.index('say "SUMMARY"', start)
    return text[start:end]


class TheHealthcheckMustNotStopAGreenfieldDeploy(unittest.TestCase):
    def setUp(self) -> None:
        self.block = _gateway_block(_code(HEALTHCHECK))

    def test_the_pipeline_really_does_abort_on_a_failed_healthcheck(self) -> None:
        """If this stops being true the rest of this file is arguing with nobody."""
        pipeline = _code(PIPELINE)
        self.assertIn("run_stage 05-healthcheck.sh", pipeline)
        self.assertIn("Pipeline stopped at 05-healthcheck.sh", pipeline)

    def test_a_healthcheck_failure_is_a_non_zero_exit(self) -> None:
        self.assertIn('[ "$FAILED" -eq 0 ] || exit 1', _code(HEALTHCHECK))

    def test_no_gateway_at_all_is_blocked_not_failed(self) -> None:
        # The branch for an appliance that has never been linked.
        m = re.search(r'if \[ -z "\$gw_host" \]; then\n(.*?)\nelif', self.block, re.S)
        self.assertIsNotNone(m, "the no-gateway branch is not where this expects it")
        branch = m.group(1)
        self.assertRegex(
            branch,
            r'^\s*b ',
            "a fresh appliance with no gateway must be BLOCKED; 'f' here aborts every first deploy",
        )
        self.assertNotRegex(branch, r"(?m)^\s*f ")

    def test_a_recorded_but_unapplied_gateway_is_a_failure(self) -> None:
        # Once a gateway exists, a link that was never applied is this
        # server's problem and must be loud.
        m = re.search(r'elif \[ "\$gw_phase" != "applied" \]; then\n(.*?)\nelse', self.block, re.S)
        self.assertIsNotNone(m)
        self.assertRegex(m.group(1), r"(?m)^\s*f ")

    def test_a_disabled_link_is_a_failure(self) -> None:
        m = re.search(r'elif \[ "\$gw_enabled" != "1" \]; then\n(.*?)\nelif', self.block, re.S)
        self.assertIsNotNone(m)
        self.assertRegex(m.group(1), r"(?m)^\s*f ")

    def test_drift_on_a_linked_gateway_is_a_failure(self) -> None:
        """Relay host, trust and reachability each stop mail on their own."""
        for needle in (
            "zimbraMtaRelayHost is empty",
            "zimbraMtaMyNetworks does not include",
            "Port 25 is still open to the internet",
            "outbound mail will queue on this appliance",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.block)

    def test_a_silent_zimbra_does_not_produce_a_second_failure(self) -> None:
        # One root cause should not be reported three times; the checks above
        # already fail when Zimbra is down.
        self.assertIn('if [ -z "$MYN" ]; then', self.block)

    def test_the_operator_is_told_what_to_do_next_not_just_what_is_wrong(self) -> None:
        self.assertIn("Install the Proxmox Mail Gateway VM", self.block)
        self.assertIn("Console > Mail Gateway > Connect", self.block)
        self.assertIn("10-host-firewall.sh apply", self.block)


class TheFirewallMustNotCloseAPortNothingWillOpen(unittest.TestCase):
    def setUp(self) -> None:
        self.code = _code(FIREWALL)

    def test_port_25_only_narrows_once_a_link_has_actually_been_applied(self) -> None:
        # Narrowing on a merely-recorded gateway would stop inbound mail on an
        # appliance that is still its own MX.
        self.assertIn('[ "$gw_phase" = "applied" ]', self.code)
        self.assertIn('[ "$gw_enabled" = "1" ]', self.code)

    def test_without_a_gateway_port_25_stays_open(self) -> None:
        self.assertIn("ufw allow 25/tcp", self.code)
        self.assertIn("no gateway configured", self.code)

    def test_a_gateway_named_by_hostname_is_resolved_before_ufw_sees_it(self) -> None:
        # ufw takes an address, not a name. Handing it a hostname produces a
        # rule that is never created, and port 25 ends up closed to everyone.
        self.assertIn("getent ahostsv4", self.code)
        self.assertIn('ufw allow from "$gw_addr"', self.code)

    def test_an_unresolvable_gateway_leaves_port_25_open(self) -> None:
        """The safe direction is accepting mail, not refusing all of it."""
        self.assertIn('[ -n "$gw_addr" ]', self.code)


if __name__ == "__main__":
    unittest.main()
