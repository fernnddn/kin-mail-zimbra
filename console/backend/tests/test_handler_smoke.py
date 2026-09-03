"""Every privhelper command must fail cleanly, never crash.

The console applies a license by calling one of these handlers. On 31 Aug 2026
that call raised

    TypeError: OrchHost.__init__() missing 1 required positional argument

straight into the browser, on both nodes, because no test had ever invoked the
handler - only the pure functions around it. A programming error reaching the
UI as a stack trace is a different failure from an operation that could not
complete, and only one of them is acceptable.

These run each handler in a sandbox where nothing it looks for exists. They are
all expected to FAIL; what is asserted is HOW. A missing file, an absent `pcs`,
a rejected argument: fine. TypeError, NameError or AttributeError: a bug.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

CRASHES = (TypeError, NameError, AttributeError, KeyError, IndexError, UnboundLocalError)

# Args that get each handler past argument validation and into its own logic,
# without asking it to do anything to a real host.
PROBE_ARGS: dict[str, dict] = {
    "maintenance": {"op": "status"},
    "remove_host": {"op": "probe", "target": "mail2.example.test"},
    "add_host": {"op": "probe", "new_name": "mail2.example.test", "new_ip": "192.0.2.9"},
    # seats, not status: this is the path that reaches the peer push, which is
    # where the license TypeError lived. `status` only reads and would have
    # let that bug through again.
    "apply_appliance_settings": {"section": "seats", "seats": "10"},
    "ha_disk_preflight": {},
    "run_ha_orchestration": {"join_mode": "check", "skip_remote_install": "1"},
}


class HandlerSmokeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "etc").mkdir()
        (root / "var").mkdir()
        # Point every writable/readable path at the sandbox so a handler that
        # does reach the filesystem cannot touch this machine.
        cls._env = {
            "KIN_MAIL_CONFIG": str(root / "etc/config"),
            "KIN_TOPOLOGY_MARKER": str(root / "etc/topology"),
            "KIN_SERVER_ID_FILE": str(root / "etc/server-id"),
            "KIN_SETUP_COMPLETE_MARKER": str(root / "etc/setup-complete"),
            "KIN_HA_SETUP_COMPLETE_MARKER": str(root / "etc/ha-setup-complete"),
            "KIN_CONSOLE_DRAFT": str(root / "var/draft.json"),
            "KIN_DEPLOY_LAST_LOG": str(root / "var/deploy.log"),
            "KIN_CLUSTER_OPS_LOG": str(root / "var/cluster-ops.log"),
            "KIN_LAST_REMOVED_PEER": str(root / "var/last-removed"),
            "KIN_MAINT_LOCK": str(root / "var/maint.lock"),
            "KIN_USERS_MUTATION_LOCK": str(root / "var/users.lock"),
            "KIN_ZIMBRA_ROOT": str(root / "opt/zimbra"),
            "KIN_MAIL_DEPLOY_DIR": str(root / "opt/deploy"),
            "KIN_ANSIBLE_DIR": str(root / "opt/ansible"),
            "KIN_ORCH_WORK_DIR": str(root / "var/orch"),
            "KIN_MAIL_CONSOLE_OPT": str(root / "opt/console"),
            # Nothing should wait minutes inside a unit test.
            "KIN_FAILBACK_TIMEOUT": "1",
            "KIN_MAINT_RESYNC_TIMEOUT": "1",
            "PATH": "/nonexistent-so-pcs-and-drbdadm-are-absent",
        }
        cls._saved = {k: os.environ.get(k) for k in cls._env}
        os.environ.update(cls._env)
        # A believable two-node appliance. Without this every handler takes its
        # single-node shortcut and the code that talks to a peer - the code
        # that crashed in production - is never reached.
        (root / "etc/config").write_text(
            'MAIL_HOST="mail.example.test"\n'
            'SERVER_IP="192.0.2.7"\n'
            'MAIL_DOMAIN="example.test"\n'
            'PEER_HOST_NAME="mail2.example.test"\n'
            'PEER_HOST_IP="192.0.2.9"\n'
            'CONTRACTED_SEATS="5"\n'
            'TOPOLOGY="2vm"\n',
            encoding="utf-8",
        )
        (root / "etc/topology").write_text("2vm\n", encoding="utf-8")
        (root / "etc/server-id").write_text("00000000-0000-0000-0000-000000000000\n", encoding="utf-8")

        # deploy_state binds these Paths at import. Setting the environment
        # only works if this module happens to import first, which it does
        # alone and does not inside the full suite - the test then passed for
        # the wrong reason. Rebind the attributes instead.
        from kin_privhelper import deploy_state

        cls._patched = {
            "KIN_MAIL_CONFIG": root / "etc/config",
            "TOPOLOGY_MARKER": root / "etc/topology",
            "SERVER_ID_FILE": root / "etc/server-id",
            "WIZARD_DRAFT_FILE": root / "var/draft.json",
            "DEPLOY_LAST_LOG": root / "var/deploy.log",
        }
        cls._deploy_state_saved = {
            k: getattr(deploy_state, k) for k in cls._patched if hasattr(deploy_state, k)
        }
        for k, v in cls._patched.items():
            if hasattr(deploy_state, k):
                setattr(deploy_state, k, v)

    @classmethod
    def tearDownClass(cls) -> None:
        from kin_privhelper import deploy_state

        for k, v in cls._deploy_state_saved.items():
            setattr(deploy_state, k, v)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmp.cleanup()

    async def _drain(self, name: str, handler, args: dict):
        events = []
        gen = handler(args)
        try:
            async for ev in gen:
                events.append(ev)
                if len(events) > 400:  # a handler that will not stop is its own bug
                    break
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                await aclose()
        return events

    async def test_no_handler_raises_a_programming_error(self) -> None:
        from kin_privhelper.commands import HANDLERS

        self.assertGreaterEqual(len(HANDLERS), 20, "handler table shrank unexpectedly")
        crashed = []
        for name, handler in sorted(HANDLERS.items()):
            args = PROBE_ARGS.get(name, {})
            try:
                await asyncio.wait_for(self._drain(name, handler, args), timeout=25)
            except CRASHES as exc:
                crashed.append(f"{name}: {type(exc).__name__}: {exc}")
            except asyncio.TimeoutError:
                crashed.append(f"{name}: did not finish in 25s")
            except Exception:  # noqa: BLE001
                # Anything else is the handler reporting that it cannot do the
                # job here, which is the correct outcome in this sandbox.
                pass
        self.assertEqual(crashed, [], "; ".join(crashed))

    async def test_the_peer_path_is_actually_reached(self) -> None:
        """Otherwise this whole file passes while the peer code is untouched."""
        from kin_privhelper import license_sync

        plan = await license_sync.resolve_peer()
        self.assertEqual(
            plan["action"],
            "push",
            "sandbox does not look like a pair, so no handler exercises the peer code",
        )
        ok, lines = await license_sync.push_license_to_peer(
            seats="10", token="", server_id="sid"
        )
        # It gets as far as trying to log in, and reports that it could not.
        self.assertFalse(ok)
        self.assertTrue(lines)

    async def test_a_handler_that_runs_reports_through_events(self) -> None:
        # Proves the harness above really executes handler bodies rather than
        # bailing out before them.
        from kin_privhelper.commands import HANDLERS

        events = await asyncio.wait_for(
            self._drain("get_status", HANDLERS["get_status"], {}), timeout=25
        )
        self.assertTrue(events, "get_status produced no events at all")
        self.assertTrue(
            any(e.get("type") in ("stdout", "stderr", "done") for e in events),
            f"unexpected event shapes: {events[:3]}",
        )


if __name__ == "__main__":
    unittest.main()
