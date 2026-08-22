"""Helper single-flight lock: wait/steal so Deploy can retry after a failed 05."""

from __future__ import annotations

import asyncio
import unittest

from kin_privhelper import protocol as proto
from kin_privhelper.daemon import (
    acquire_run_slot,
    mark_run_slot_busy_for_tests,
    release_run_slot,
    reset_run_slot_for_tests,
)


class HelperRunSlotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_run_slot_for_tests()

    def tearDown(self) -> None:
        reset_run_slot_for_tests()

    async def test_idle_acquire_and_release(self) -> None:
        token = await acquire_run_slot(proto.CMD_GET_STATUS, wait_sec=0, is_installing=lambda: False)
        self.assertIsNotNone(token)
        other = await acquire_run_slot(proto.CMD_GET_STATUS, wait_sec=0, is_installing=lambda: False)
        self.assertIsNone(other)
        await release_run_slot(token)
        again = await acquire_run_slot(proto.CMD_GET_STATUS, wait_sec=0, is_installing=lambda: False)
        self.assertIsNotNone(again)
        await release_run_slot(again)

    async def test_pipeline_steals_stale_lock_when_install_is_not_running(self) -> None:
        stale = mark_run_slot_busy_for_tests()
        stolen = await acquire_run_slot(
            proto.CMD_APPLY_WIZARD_DRAFT,
            wait_sec=0,
            is_installing=lambda: False,
        )
        self.assertIsNotNone(stolen)
        self.assertNotEqual(stolen, stale)
        await release_run_slot(stale)
        held = await acquire_run_slot(
            proto.CMD_RUN_FULL_INSTALL,
            wait_sec=0,
            is_installing=lambda: False,
        )
        self.assertIsNone(held)
        await release_run_slot(stolen)
        free = await acquire_run_slot(
            proto.CMD_RUN_FULL_INSTALL,
            wait_sec=0,
            is_installing=lambda: False,
        )
        self.assertIsNotNone(free)
        await release_run_slot(free)

    async def test_no_steal_while_install_process_is_live(self) -> None:
        mark_run_slot_busy_for_tests()
        token = await acquire_run_slot(
            proto.CMD_RUN_FULL_INSTALL,
            wait_sec=0,
            is_installing=lambda: True,
        )
        self.assertIsNone(token)

    async def test_waits_until_previous_job_releases(self) -> None:
        stale = mark_run_slot_busy_for_tests()

        async def _release_soon() -> None:
            await asyncio.sleep(0.15)
            await release_run_slot(stale)

        asyncio.create_task(_release_soon())
        token = await acquire_run_slot(
            proto.CMD_APPLY_WIZARD_DRAFT,
            wait_sec=2,
            is_installing=lambda: False,
        )
        self.assertIsNotNone(token)
        await release_run_slot(token)


if __name__ == "__main__":
    unittest.main()
