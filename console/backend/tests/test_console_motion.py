"""Motion in the console has to be safe before it is nice.

Two failures matter more than any animation. One: somebody who has asked their
system for less movement gets movement anyway. Two: an animation that fades an
element in is skipped, and the element is left at opacity 0 - "reduced motion"
becomes "blank page", which is far worse than the animation would have been.

These are source-level guards, because the alternative is a browser test rig
this product does not have and should not grow for this.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FRONTEND = REPO / "console/frontend"
MOTION = FRONTEND / "src/lib/motion.ts"
GLOBAL_CSS = FRONTEND / "src/styles/global.css"


class MotionRespectsTheSystemSetting(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(MOTION.is_file(), f"missing {MOTION}")
        self.src = MOTION.read_text(encoding="utf-8")

    def test_it_asks_the_system_before_moving_anything(self) -> None:
        self.assertIn("prefers-reduced-motion", self.src)

    def test_reduced_motion_applies_the_end_state_rather_than_skipping(self) -> None:
        """Skipping a fade-in leaves the element invisible."""
        self.assertIn("utils.set", self.src)
        # The guard has to run before the animation, in the shared runner, so
        # no individual helper can forget it.
        runner = self.src[self.src.index("function run("):]
        runner = runner[: runner.index("\n}")]
        self.assertIn("prefersReducedMotion()", runner)
        self.assertIn("utils.set", runner)

    def test_every_helper_goes_through_that_runner(self) -> None:
        # A helper calling animate() directly would bypass the guard entirely.
        body = self.src[self.src.index("function run("):]
        direct = [
            ln
            for ln in body.splitlines()
            if re.search(r"(?<![A-Za-z.])animate\(", ln) and "return animate(" not in ln
        ]
        self.assertEqual(direct, [], f"these bypass the reduced-motion guard: {direct}")

    def test_completion_callbacks_still_fire_when_motion_is_off(self) -> None:
        # A component that reveals something in onComplete would otherwise
        # never reveal it.
        self.assertIn("params.onComplete?.(", self.src)

    def test_the_global_stylesheet_neutralises_css_animation_too(self) -> None:
        css = GLOBAL_CSS.read_text(encoding="utf-8")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("animation-duration: 0.01ms !important", css)

    def test_a_missing_target_does_not_throw(self) -> None:
        """React unmounts things while an animation is being set up."""
        self.assertIn("catch", self.src)


class TheLibraryIsDeclaredAndPinned(unittest.TestCase):
    def test_animejs_is_a_real_dependency(self) -> None:
        pkg = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
        self.assertIn("animejs", pkg.get("dependencies", {}))

    def test_it_is_vendored_into_the_committed_bundle(self) -> None:
        """The appliance installs offline; nothing is fetched at runtime."""
        dist = FRONTEND / "dist" / "assets"
        self.assertTrue(dist.is_dir(), "no built bundle committed")
        bundles = list(dist.glob("index-*.js"))
        self.assertTrue(bundles, "no javascript bundle in dist")
        blob = bundles[0].read_text(encoding="utf-8", errors="replace")
        # If it were loaded from a CDN the appliance would have no animation
        # and, worse, would try to reach the internet from a mail server.
        self.assertNotIn("cdn.jsdelivr.net", blob)
        self.assertNotIn("unpkg.com", blob)


class MotionIsUsedForMeaningNotDecoration(unittest.TestCase):
    """Each use has to be defensible in one sentence."""

    def test_it_is_applied_where_a_set_arrives(self) -> None:
        gateway = (FRONTEND / "src/pages/MailGateway.tsx").read_text(encoding="utf-8")
        monitoring = (FRONTEND / "src/monitoring/MonitoringTab.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("useStaggerIn", gateway)
        self.assertIn("useStaggerIn", monitoring)

    def test_the_transcript_does_not_re_animate_on_every_chunk(self) -> None:
        # A stream appends line by line. Re-running the stagger on each append
        # makes the panel flicker for the whole of an apply.
        gateway = (FRONTEND / "src/pages/MailGateway.tsx").read_text(encoding="utf-8")
        self.assertIn("running === null ? lines.length : 0", gateway)


if __name__ == "__main__":
    unittest.main()
