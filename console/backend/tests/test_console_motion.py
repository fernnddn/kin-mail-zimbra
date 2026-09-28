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


class ThePagesThatShouldMoveDo(unittest.TestCase):
    """Motion that was added and then quietly lost is the same as none.

    The console grew four pages - the deployment topology, the cluster's node
    cards, the account list and the disk targets - that render a *set* and
    stood completely still. The operator's report was not "the animation is
    broken", it was "I cannot feel any difference", which is what a console
    with a motion library and four static pages feels like.

    Each entry below is a list or a diagram where the sequence carries meaning:
    how many machines there are, which order mail travels in, whether the
    people expected from the directory actually arrived. Decoration is still
    not wanted anywhere; these are the places where it is not decoration.
    """

    CASES = {
        "src/pages/ClusterTopology.tsx": ("useStaggerIn", "useDrawIn"),
        "src/pages/Cluster.tsx": ("useStaggerIn",),
        "src/pages/Users.tsx": ("useStaggerIn",),
        "src/pages/ActivityCenter.tsx": ("useStaggerIn",),
        "src/monitoring/DiskExtend.tsx": ("useStaggerIn",),
        # Already animated before this; kept so they cannot regress either.
        "src/monitoring/MonitoringTab.tsx": ("useStaggerIn",),
        "src/monitoring/ReportsTab.tsx": ("useStaggerIn", "useCountUp"),
        "src/pages/MailGateway.tsx": ("useStaggerIn",),
        "src/ConsoleChrome.tsx": ("slideTo",),
    }

    def test_each_page_that_renders_a_set_animates_it(self) -> None:
        missing = []
        for rel, helpers in self.CASES.items():
            path = FRONTEND / rel
            self.assertTrue(path.is_file(), f"missing {rel}")
            src = path.read_text(encoding="utf-8")
            for helper in helpers:
                if helper not in src:
                    missing.append(f"{rel}: {helper}")
        self.assertEqual(missing, [], f"these stopped animating: {missing}")

    def test_the_animated_container_is_actually_wired_to_a_ref(self) -> None:
        """A hook with no ref on an element animates nothing and fails silently."""
        for rel in self.CASES:
            src = (FRONTEND / rel).read_text(encoding="utf-8")
            for match in re.finditer(r"use(?:StaggerIn|DrawIn)\(\s*([A-Za-z0-9_]+)", src):
                ref = match.group(1)
                # assertIn on a short string, not assertRegex on the file: a
                # failure here should name the ref, not print the component.
                self.assertTrue(
                    f"ref={{{ref}}}" in src,
                    f"{rel}: {ref} is passed to a motion hook but never attached "
                    f"to an element, so it animates nothing and says nothing",
                )

    def test_the_topology_draws_its_connectors_in_reading_order(self) -> None:
        """The lines are the point: a link that draws downwards says which way
        mail travels, and that is the question the page exists to answer."""
        src = (FRONTEND / "src/pages/ClusterTopology.tsx").read_text(encoding="utf-8")
        self.assertIn("useDrawIn", src)
        motion = MOTION.read_text(encoding="utf-8")
        body = motion[motion.index("export function useDrawIn("):]
        body = body[: body.index("\n}\n")]
        # Reduced motion must leave the connectors VISIBLE, not half-drawn.
        self.assertIn("prefersReducedMotion()", body)
        self.assertIn('strokeDasharray = ""', body)
        # A dashed line means "not present". Restoring the attribute afterwards
        # is what keeps it from being silently promoted to a solid link.
        self.assertIn("dashed", body)
        # The selector is explicit. "every line and path under here" would draw
        # the first icon anybody adds to a node card, and the cause would look
        # like their component rather than this hook.
        self.assertIn("[data-draw]", body)
        self.assertTrue(
            'data-draw=""' in src,
            "the topology's connectors are not marked, so useDrawIn finds nothing",
        )
