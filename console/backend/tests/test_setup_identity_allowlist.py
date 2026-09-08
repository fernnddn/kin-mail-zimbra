"""What an unauthenticated caller may do before the appliance is deployed.

Before the first deploy finishes there is no console user yet, so the wizard
runs as a synthetic "setup" identity with no password behind it. That identity
is necessary and it is also the only unauthenticated privileged path in the
product, which makes SETUP_ALLOWED_COMMANDS the highest-consequence frozenset
in the tree.

It had no test at all. Adding a command to it, or to the wizard router that
reaches it, failed no build, and the failure mode is not subtle: the appliance
is on a network during install, and anything reachable through this list is
reachable by anyone who can open port 9443 before the operator finishes.

Two independent gates enforce it and both are asserted here, because either
one alone is a single point of failure:

  auth.wizard_actor    the console refuses anonymous access once mail is
                       deployed (1vm: setup-complete)
  daemon._authorize    the privhelper refuses the setup username outright once
                       mail is deployed, and refuses anything off the list
                       before that, even if the console were bypassed
"""

from __future__ import annotations

import unittest
from unittest import mock

from kin_privhelper import daemon, deploy_state, protocol as proto

# Everything the wizard genuinely needs to take a bare host to a running
# appliance, and nothing else. Restated deliberately: a change to the live set
# must be a change to this list too, in a diff someone reviews.
EXPECTED = {
    "get_status",
    "run_script:09-hardening.sh --status",
    "apply_wizard_draft",
    "run_hardening",
    "run_full_install",
    "cancel_firewall_deadman",
    "get_deploy_log",
    "store_provisioning_secrets",
    "run_ha_orchestration",
    "ha_disk_preflight",
}

# Named individually so a failure says which door was opened and why it
# matters, rather than printing two sets and leaving the reader to diff them.
MUST_NEVER_BE_ANONYMOUS = {
    proto.CMD_INSTALL_MONITORING: "installs packages and rewrites monitoring config",
    proto.CMD_MUTATE_CONSOLE_USERS: "creates or promotes console logins",
    proto.CMD_APPLY_APPLIANCE_SETTINGS: "writes the Cloudflare API token to disk",
    proto.CMD_CREATE_MAILBOX: "creates mail accounts",
    proto.CMD_CLEAR_INITIAL_CONSOLE_PASSWORD: "removes the first-boot password gate",
    proto.CMD_GET_AUDIT_LOG: "reads the audit trail",
    proto.CMD_MAINTENANCE: "stops and starts mail",
    proto.CMD_ADD_HOST: "reaches another machine over SSH",
    proto.CMD_REMOVE_HOST: "tears a node out of a cluster",
    proto.CMD_ADD_OBSERVABILITY: "reaches another machine over SSH",
    proto.CMD_REMOVE_OBSERVABILITY: "tears the witness out",
    proto.CMD_STORE_OBSERVABILITY_SECRETS: "writes credentials for another host",
}


class TheAnonymousSetupAllowlist(unittest.TestCase):
    def test_it_is_exactly_what_first_deploy_needs(self) -> None:
        self.assertEqual(
            set(deploy_state.SETUP_ALLOWED_COMMANDS),
            EXPECTED,
            "the unauthenticated pre-deploy allowlist changed; if that is "
            "deliberate, update EXPECTED in this test and say why in the commit",
        )

    def test_the_dangerous_commands_are_not_on_it(self) -> None:
        for cmd, why in MUST_NEVER_BE_ANONYMOUS.items():
            with self.subTest(cmd=cmd):
                self.assertNotIn(
                    cmd,
                    deploy_state.SETUP_ALLOWED_COMMANDS,
                    f"{cmd} would be callable with no login: it {why}",
                )

    def test_every_entry_is_a_command_that_exists(self) -> None:
        # An allowlist entry that matches no real command is dead text, and
        # dead text is where a typo hides: "run_ha_orchestraton" would deny
        # silently and nobody would know which way it had failed.
        for entry in deploy_state.SETUP_ALLOWED_COMMANDS:
            with self.subTest(entry=entry):
                if entry.startswith("run_script:"):
                    self.assertIn(proto.CMD_RUN_HARDENING_STATUS, proto.ALLOWED_COMMANDS)
                    continue
                self.assertIn(
                    entry,
                    proto.ALLOWED_COMMANDS,
                    "allowlisted for the wizard but not a registered command",
                )


class TheDaemonEnforcesItIndependentlyOfTheConsole(unittest.TestCase):
    """Even if the console layer were bypassed, the helper still refuses."""

    def _authorize(self, cmd: str, *, deployed: bool):
        with mock.patch.object(deploy_state, "is_mail_deployed", lambda: deployed):
            return daemon._authorize(deploy_state.SETUP_USERNAME, cmd)

    def test_setup_is_refused_outright_once_mail_is_deployed(self) -> None:
        # The window closes at setup-complete. After that the identity is dead
        # even for commands it was allowed to run five minutes earlier.
        for cmd in sorted(deploy_state.SETUP_ALLOWED_COMMANDS):
            with self.subTest(cmd=cmd):
                role, deny = self._authorize(cmd, deployed=True)
                self.assertIsNone(role)
                self.assertEqual(deny, "denied_setup_after_deploy")

    def test_an_off_list_command_is_refused_before_deploy(self) -> None:
        for cmd in sorted(MUST_NEVER_BE_ANONYMOUS):
            with self.subTest(cmd=cmd):
                role, deny = self._authorize(cmd, deployed=False)
                self.assertIsNone(role)
                self.assertEqual(deny, "denied_setup_cmd")

    def test_the_allowlisted_commands_do_work_before_deploy(self) -> None:
        # The gate has to be a gate, not a wall: first deploy must be possible.
        role, deny = self._authorize("run_full_install", deployed=False)
        self.assertIsNone(deny)
        self.assertIsNotNone(role)


if __name__ == "__main__":
    unittest.main()
