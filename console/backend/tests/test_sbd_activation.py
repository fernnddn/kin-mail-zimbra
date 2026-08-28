"""SBD activation plan and fence_sbd timeout helpers (no live cluster)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from kin_privhelper.sbd_activation import (
    fence_sbd_power_timeout_ok,
    pcs_status_shows_local_mail_busy,
    sbd_activation_plan,
    sbd_device_needs_create,
    sbd_fresh_lun_plan,
    stonith_config_has_attr,
)

PCS_PRIMARY = """\
Clone Set: kin-drbd-clone [kin-drbd] (promotable):
     kin-drbd\t(ocf::linbit:drbd):\t Promoted mail.example.test
     kin-drbd\t(ocf::linbit:drbd):\t Unpromoted mail2.example.test
 Resource Group: kin-mail-svc:
     kin-fs\t(ocf::heartbeat:Filesystem):\t Started mail.example.test
     kin-zimbra\t(ocf::kin:zimbra):\t Started mail.example.test
     kin-vip\t(ocf::heartbeat:IPaddr2):\t Started mail.example.test
 Daemon Status:
  corosync: active/enabled
  pacemaker: active/enabled
  pcsd: active/enabled
  sbd: inactive/enabled
"""

PCS_IDLE_PEER = """\
Clone Set: kin-drbd-clone [kin-drbd] (promotable):
     kin-drbd\t(ocf::linbit:drbd):\t Promoted mail.example.test
     kin-drbd\t(ocf::linbit:drbd):\t Unpromoted mail2.example.test
 Resource Group: kin-mail-svc:
     kin-fs\t(ocf::heartbeat:Filesystem):\t Started mail.example.test
     kin-zimbra\t(ocf::kin:zimbra):\t Started mail.example.test
     kin-vip\t(ocf::heartbeat:IPaddr2):\t Started mail.example.test
"""

PCS_GREENFIELD = """\
Daemon Status:
  corosync: active/enabled
  pacemaker: active/enabled
  pcsd: active/enabled
  sbd: inactive/enabled
"""

STONITH_WITHOUT_PT = """\
 Resource: sbd (class=stonith type=fence_sbd)
  Attributes: devices=/dev/disk/by-id/scsi-3600 pcmk_delay_max=5s
"""

STONITH_WITH_PT = """\
 Resource: sbd (class=stonith type=fence_sbd)
  Attributes: devices=/dev/disk/by-id/scsi-3600 power_timeout=150 pcmk_delay_max=5s
"""


class SbdActivationPlanTests(unittest.TestCase):
    def test_already_active(self) -> None:
        self.assertEqual(
            sbd_activation_plan(sbd_active=True, local_mail_busy=True),
            "already_active",
        )
        self.assertEqual(
            sbd_activation_plan(sbd_active=True, local_mail_busy=False),
            "already_active",
        )

    def test_recycle_when_idle(self) -> None:
        self.assertEqual(
            sbd_activation_plan(sbd_active=False, local_mail_busy=False),
            "recycle_local",
        )

    def test_refuse_when_busy(self) -> None:
        self.assertEqual(
            sbd_activation_plan(sbd_active=False, local_mail_busy=True),
            "refuse_busy",
        )


class PcsStatusBusyTests(unittest.TestCase):
    def test_primary_fqdn_and_short(self) -> None:
        self.assertTrue(
            pcs_status_shows_local_mail_busy(
                PCS_PRIMARY, ["mail.example.test", "mail"]
            )
        )
        self.assertTrue(pcs_status_shows_local_mail_busy(PCS_PRIMARY, ["mail"]))

    def test_idle_peer_not_busy(self) -> None:
        self.assertFalse(
            pcs_status_shows_local_mail_busy(
                PCS_IDLE_PEER, ["mail2.example.test", "mail2"]
            )
        )

    def test_greenfield_not_busy(self) -> None:
        self.assertFalse(
            pcs_status_shows_local_mail_busy(PCS_GREENFIELD, ["mail", "mail2"])
        )


class PowerTimeoutTests(unittest.TestCase):
    def test_default_fence_sbd_is_too_low_for_msgwait_70(self) -> None:
        self.assertFalse(fence_sbd_power_timeout_ok(30, 70))
        self.assertFalse(fence_sbd_power_timeout_ok(70, 70))

    def test_role_default_is_ok(self) -> None:
        self.assertTrue(fence_sbd_power_timeout_ok(150, 70))
        self.assertTrue(fence_sbd_power_timeout_ok("150s", "70"))


class StonithConfigAttrTests(unittest.TestCase):
    def test_missing_power_timeout(self) -> None:
        self.assertFalse(stonith_config_has_attr(STONITH_WITHOUT_PT, "power_timeout", "150"))

    def test_present_power_timeout(self) -> None:
        self.assertTrue(stonith_config_has_attr(STONITH_WITH_PT, "power_timeout", "150"))


class RoleWiringTests(unittest.TestCase):
    def test_ansible_filter_is_the_same_function(self) -> None:
        path = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "sbd_stonith"
            / "filter_plugins"
            / "sbd_activation.py"
        )
        spec = importlib.util.spec_from_file_location("ansible_sbd_activation_filter", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        filters = mod.FilterModule().filters()
        self.assertEqual(
            filters["kin_sbd_activation_plan"](False, True),
            "refuse_busy",
        )
        self.assertTrue(
            filters["kin_pcs_status_local_mail_busy"](PCS_PRIMARY, ["mail"])
        )
        self.assertTrue(filters["kin_fence_sbd_power_timeout_ok"](150, 70))
        self.assertTrue(
            filters["kin_stonith_config_has_attr"](STONITH_WITH_PT, "power_timeout", "150")
        )

    def test_role_wires_activation_and_power_timeout(self) -> None:
        root = (
            Path(__file__).resolve().parents[3]
            / "ansible"
            / "roles"
            / "sbd_stonith"
        )
        main = (root / "tasks" / "main.yml").read_text(encoding="utf-8")
        activate = (root / "tasks" / "activate.yml").read_text(encoding="utf-8")
        stonith = (root / "tasks" / "stonith.yml").read_text(encoding="utf-8")
        defaults = (root / "defaults" / "main.yml").read_text(encoding="utf-8")
        verify = (root / "tasks" / "verify.yml").read_text(encoding="utf-8")
        self.assertIn("activate.yml", main)
        self.assertIn("kin_sbd_activation_plan", activate)
        self.assertIn("pcs cluster stop", activate)
        self.assertIn("request-timeout", activate)
        self.assertIn("throttle: 1", activate)
        self.assertIn("sbd_stonith_pcs_request_timeout", activate)
        self.assertIn("refuse_busy", activate)
        self.assertIn("power_timeout=", stonith)
        self.assertIn("sbd_stonith_power_timeout", defaults)
        self.assertIn("sbd_stonith_power_timeout: 150", defaults)
        self.assertIn("sbd_stonith_pcs_request_timeout", defaults)
        self.assertIn("is-active sbd", verify)
        self.assertIn("kin_stonith_config_has_attr", verify)
        # Do not fight RefuseManualStart.
        self.assertNotIn("state: started", (root / "tasks" / "install.yml").read_text(encoding="utf-8"))
        ordering = (root / "templates" / "kin-ordering.conf.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn("Requires=kin-softdog.service open-iscsi.service", ordering)
        self.assertNotIn("Wants=open-iscsi.service", ordering)
        initialize = (root / "tasks" / "initialize.yml").read_text(encoding="utf-8")
        self.assertIn("observability_expect_fresh_sbd", initialize)
        self.assertIn("kin_sbd_fresh_lun_plan", (root / "filter_plugins" / "sbd_activation.py").read_text(encoding="utf-8"))


class SbdFreshLunTests(unittest.TestCase):
    def test_dump_failure_means_create(self) -> None:
        self.assertTrue(sbd_device_needs_create(dump_rc=1))
        self.assertEqual(sbd_fresh_lun_plan(dump_rc=1, expect_fresh=True), "create")
        self.assertEqual(sbd_fresh_lun_plan(dump_rc=1, expect_fresh=False), "create")

    def test_existing_header_skipped_unless_rebuild(self) -> None:
        self.assertFalse(sbd_device_needs_create(dump_rc=0))
        self.assertEqual(sbd_fresh_lun_plan(dump_rc=0, expect_fresh=False), "skip_existing")
        self.assertEqual(sbd_fresh_lun_plan(dump_rc=0, expect_fresh=True), "refuse_existing")


if __name__ == "__main__":
    unittest.main()
