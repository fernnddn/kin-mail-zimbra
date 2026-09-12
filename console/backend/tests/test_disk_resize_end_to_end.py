"""Disk resize: the console and the helper must agree, marker by marker.

The console reads the helper's output with regular expressions. If the helper
renames a marker, nothing fails anywhere - the console simply stops seeing it,
the button that should appear does not, and an operator concludes the feature
is broken. That is close to what happened: space was added in the hypervisor,
Check for unused space found nothing, and there was no hint why.

Two causes, both fixed and both pinned here. The kernel was never asked to
re-read the disk before the free space was measured. And on the mail disk the
partition reserved for a future second server sits behind the data partition,
so the free space landed on the far side of it and nothing could reach it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "install/lib/grow-disk.sh"
UI = REPO / "console/frontend/src/monitoring/DiskExtend.tsx"
APP = REPO / "console/backend/kin_console/app.py"
CMDS = REPO / "console/backend/kin_privhelper/commands.py"


def _code(path: Path) -> str:
    return "\n".join(
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )


class TheRescanHappensBeforeTheMeasuring(unittest.TestCase):
    """A disk enlarged while the guest is running still reports its old size."""

    def setUp(self) -> None:
        self.code = _code(HELPER)

    def test_there_is_a_rescan_at_all(self) -> None:
        self.assertIn("rescan_disk()", self.code)

    def test_plan_rescans_and_not_only_apply(self) -> None:
        # Check for unused space is the button an operator presses first. If
        # only apply rescans, plan reports nothing and apply then skips the
        # growpart because plan already decided there was nothing to do.
        plan = self.code[self.code.index("grow_plan() {") :]
        plan = plan[: plan.index("\n}")]
        self.assertIn("rescan_disk", plan)

    def test_it_happens_before_the_size_is_read(self) -> None:
        plan = self.code[self.code.index("grow_plan() {") :]
        plan = plan[: plan.index("\n}")]
        self.assertLess(
            plan.index("rescan_disk"),
            plan.index("fs_source"),
            "the rescan must come before anything is measured",
        )

    def test_a_dry_run_still_touches_nothing(self) -> None:
        """plan promises to change nothing on disk; dry run promises more."""
        self.assertIn('KIN_GROW_DRY_RUN:-0}" != "1"', self.code)


class TheMarkersTheConsoleReadsAreTheOnesEmitted(unittest.TestCase):
    """Rename one end and the other silently stops working."""

    def setUp(self) -> None:
        self.helper = HELPER.read_text(encoding="utf-8")
        self.ui = UI.read_text(encoding="utf-8")

    def _ui_markers(self) -> set[str]:
        return set(re.findall(r"KIN_GROW_[A-Z_]+", self.ui))

    def _emitted(self) -> set[str]:
        """Markers the helper can actually print.

        Some are written out, and some are assembled: `mark FOO=1` prints
        `KIN_GROW_FOO=1`, because mark() adds the prefix. A test that only
        looked for literals would report a marker as missing while the helper
        emits it perfectly - and, worse, would miss the real failure, which is
        a marker the console greps for that nothing ever prints.
        """
        out = set(re.findall(r"KIN_GROW_[A-Z_]+", self.helper))
        for name in re.findall(r'^\s*mark\s+"?([A-Z_]+)', self.helper, re.M):
            out.add("KIN_GROW_" + name)
        return out

    def test_the_prefix_convention_still_holds(self) -> None:
        # If mark() stops adding KIN_GROW_, every assembled marker changes name
        # and the console silently stops seeing any of them.
        self.assertIn("mark() { printf 'KIN_GROW_%s", self.helper)

    def test_every_marker_the_console_looks_for_is_emitted(self) -> None:
        emitted = self._emitted()
        missing = sorted(m for m in self._ui_markers() if m not in emitted)
        self.assertEqual(
            missing, [], f"the console watches for markers nothing emits: {missing}"
        )

    def test_the_console_watches_for_the_ones_that_matter(self) -> None:
        emitted = self._emitted()
        for marker in (
            "KIN_GROW_FREE_BYTES",
            "KIN_GROW_REFUSED",
            "KIN_GROW_RECLAIMABLE_RESERVED",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.ui, "the console does not look for it")
                self.assertIn(marker, emitted, "the helper never prints it")


class ReclaimingIsOptInAllTheWayDown(unittest.TestCase):
    """Deleting a partition must be asked for at every layer, never inferred."""

    def test_the_api_refuses_reclaim_on_a_plan(self) -> None:
        app = _code(APP)
        self.assertIn("grow_disk reclaim only applies to apply", app)
        self.assertIn("grow_disk reclaim must be 0 or 1", app)

    def test_the_privhelper_only_passes_the_flag_when_asked(self) -> None:
        cmds = _code(CMDS)
        self.assertIn('args.get("reclaim")', cmds)
        self.assertIn("--reclaim-reserved", cmds)
        # The flag is appended, never part of the base argv.
        self.assertIn('argv = [str(script), "apply", mountpoint]', cmds)

    def test_the_console_asks_separately_before_deleting_anything(self) -> None:
        ui = UI.read_text(encoding="utf-8")
        # Two dialogs, not one with a checkbox: somebody must not delete a
        # partition while intending only to grow a disk.
        self.assertEqual(ui.count("<ConfirmModal"), 2)
        self.assertIn("Free the reserved partition?", ui)
        self.assertIn('variant="danger"', ui)

    def test_the_console_explains_what_is_lost(self) -> None:
        ui = UI.read_text(encoding="utf-8")
        self.assertIn("separate disk", ui)
        self.assertIn("cannot be undone", ui)

    def test_a_blocker_we_will_not_touch_is_reported_rather_than_ignored(self) -> None:
        """Otherwise Check again gets pressed for ever."""
        ui = UI.read_text(encoding="utf-8")
        self.assertIn("KIN_GROW_RESERVED_NOT_RECLAIMABLE", ui)
        # Assembled by mark(), which adds the KIN_GROW_ prefix.
        self.assertIn('mark "RESERVED_NOT_RECLAIMABLE=', HELPER.read_text(encoding="utf-8"))


class TheEncryptedPathCannotHang(unittest.TestCase):
    """cryptsetup prompts when it cannot find a key.

    Inside a console stream a prompt is not an error the operator sees. It is a
    spinner that never stops until the helper times out.
    """

    def setUp(self) -> None:
        self.code = _code(HELPER)

    def test_cryptsetup_runs_in_batch_mode(self) -> None:
        for line in self.code.splitlines():
            if '"$CRYPTSETUP" resize' in line:
                with self.subTest(line=line.strip()[:70]):
                    self.assertIn("--batch-mode", line)

    def test_its_stdin_is_closed(self) -> None:
        self.assertRegex(self.code, r'"\$CRYPTSETUP" resize[^\n]*</dev/null')

    def test_the_appliance_keyfile_is_used_when_there_is_one(self) -> None:
        self.assertIn("luks-keyfile", self.code)

    def test_the_failure_says_the_filesystem_was_not_touched(self) -> None:
        """A half-done resize is the thing an operator most needs to know about."""
        self.assertIn("the filesystem was not touched", self.code)


if __name__ == "__main__":
    unittest.main()
