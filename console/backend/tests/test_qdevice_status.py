"""corosync-qdevice-tool -s parsers (no live qdevice)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.qdevice_status import (
    qdevice_tool_is_connected,
    qdevice_tool_state,
)

# Ubuntu jammy man page sample (State: Connected plus Connected since:).
JAMMY_CONNECTED = """\
Qdevice information
-------------------
Model:                  Net
Node ID:                1
Qdevice-net information
----------------------
Cluster name:           kin-mail
QNetd host:             192.0.2.12:5403
Algorithm:              Fifty-Fifty split
Tie-breaker:            Node with lowest node ID
State:                  Connected
TLS active:             Yes (client certificate sent)
Connected since:        2016-06-24T17:02:35
"""

DISCONNECTED = """\
Qdevice-net information
----------------------
QNetd host:             192.0.2.12:5403
State:                  Disconnected
"""

WAITING = """\
Qdevice-net information
----------------------
QNetd host:             192.0.2.12:5403
State:                  Waiting for connect
"""


class QdeviceStatusTests(unittest.TestCase):
    def test_jammy_connected_sample(self) -> None:
        self.assertEqual(qdevice_tool_state(JAMMY_CONNECTED), "Connected")
        self.assertTrue(qdevice_tool_is_connected(JAMMY_CONNECTED, "192.0.2.12"))

    def test_disconnected_is_not_connected(self) -> None:
        # Case-sensitive 'Connected' is not a substring of Disconnected.
        # Still refuse: the State: field is Disconnected, not Connected.
        self.assertNotIn("Connected", DISCONNECTED)
        self.assertEqual(qdevice_tool_state(DISCONNECTED), "Disconnected")
        self.assertFalse(qdevice_tool_is_connected(DISCONNECTED, "192.0.2.12"))

    def test_connected_since_alone_is_not_enough(self) -> None:
        blob = (
            "QNetd host:             192.0.2.12:5403\n"
            "State:                  Waiting for connect\n"
            "Connected since:        2016-06-24T17:02:35\n"
        )
        self.assertIn("Connected", blob)
        self.assertFalse(qdevice_tool_is_connected(blob, "192.0.2.12"))

    def test_waiting_for_connect_is_not_connected(self) -> None:
        self.assertFalse(qdevice_tool_is_connected(WAITING, "192.0.2.12"))

    def test_wrong_qnetd_ip_is_not_connected(self) -> None:
        self.assertFalse(qdevice_tool_is_connected(JAMMY_CONNECTED, "192.0.2.99"))

    def test_ansible_filter_is_the_same_function(self) -> None:
        path = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "corosync_qdevice"
            / "filter_plugins"
            / "qdevice_status.py"
        )
        spec = importlib.util.spec_from_file_location("ansible_qdevice_status_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.qdevice_tool_is_connected, qdevice_tool_is_connected)
        self.assertTrue(
            mod.FilterModule().filters()["kin_qdevice_is_connected"](
                JAMMY_CONNECTED, "192.0.2.12"
            )
        )

    def test_role_uses_the_filter(self) -> None:
        tasks = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "corosync_qdevice"
            / "tasks"
            / "verify.yml"
        )
        text = tasks.read_text(encoding="utf-8")
        self.assertIn("kin_qdevice_is_connected", text)
        self.assertNotIn("'Connected' not in", text)


if __name__ == "__main__":
    unittest.main()
