"""Build HA pair must do nothing at all on a single-server appliance.

1vm is the product this company sells. The 2vm path stays in the tree, and the
console still offers Build HA pair only for a 2vm topology, but "the button is
hidden" is not a guarantee: the command is reachable by any logged-in ops user
and the topology can change under a stale page.

The orchestration already refused a single node, and refused for the right
reason, but it refused too late. resolve_topology raises "Second server IP is
missing" well after the preamble has re-enabled password SSH on this host and
written cluster secrets to the vault. On 1vm that quietly undoes the
--revert-ssh-password step the single-node install performs at the very end, so
a stray call left an internet-facing mail server accepting SSH passwords again
while the operator read a refusal and reasonably concluded nothing had
happened.

Order is the whole fix: decide before touching anything.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest import mock

from kin_privhelper import orchestration


def _drain(gen) -> list[dict]:
    async def go() -> list[dict]:
        return [ev async for ev in gen]

    return asyncio.run(go())


class ASingleServerIsRefusedBeforeAnythingHappens(unittest.TestCase):
    def test_it_refuses_and_spawns_no_process(self) -> None:
        spawned: list[object] = []

        async def never(*a: object, **k: object):
            spawned.append(a)
            yield {"type": "done", "exit_code": 0}

        from kin_privhelper import commands, deploy_state

        # Patched on deploy_state, not on orchestration: the import is inside
        # the function, so it binds from the source module every call. Patching
        # the wrong target with create=True silently does nothing and the test
        # passes for the wrong reason.
        with mock.patch.object(
            deploy_state, "saved_wizard_topology", lambda: "1vm"
        ), mock.patch.object(commands, "_stream_subprocess", never):
            events = _drain(orchestration.cmd_run_ha_orchestration({"join_mode": "apply"}))

        self.assertEqual(spawned, [], "a single node must not run anything")
        done = [e for e in events if e.get("type") == "done"]
        self.assertTrue(done)
        self.assertNotEqual(done[-1].get("exit_code"), 0, "refusal must not report success")
        text = " ".join(str(e.get("data") or e.get("text") or "") for e in events)
        self.assertIn("single server", text)
        self.assertIn("Nothing on this host has been changed", text)

    def test_the_refusal_is_ahead_of_the_ssh_enable_in_source_order(self) -> None:
        # The behavioural test above proves it for the topology it mocks. This
        # proves the ordering itself, which is the property that actually
        # matters and the one a refactor would quietly lose.
        src = inspect.getsource(orchestration.cmd_run_ha_orchestration)
        refuse_at = src.index('saved_wizard_topology() == "1vm"')
        ssh_at = src.index("--ensure-ssh-password")
        vault_at = src.index("provisioning vault is empty")
        self.assertLess(refuse_at, ssh_at, "password SSH is enabled before the topology check")
        self.assertLess(refuse_at, vault_at, "secrets are written before the topology check")


class TheSecondLineOfDefenceStays(unittest.TestCase):
    """Even with the topology marker unreadable, no peer means no pair."""

    def test_an_empty_peer_ip_is_still_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            orchestration.resolve_topology(
                draft={},
                config={"MAIL_HOST": "mail.example.test", "SERVER_IP": "203.0.113.10"},
            )
        self.assertIn("Second server IP", str(ctx.exception))

    def test_a_two_node_topology_is_not_blocked(self) -> None:
        # The guard has to be a guard, not a wall: 2vm must still work.
        src = inspect.getsource(orchestration.cmd_run_ha_orchestration)
        self.assertIn('saved_wizard_topology() == "1vm"', src)
        self.assertNotIn('saved_wizard_topology() != "2vm"', src)


if __name__ == "__main__":
    unittest.main()
