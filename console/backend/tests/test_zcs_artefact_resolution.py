"""The Zimbra tarball name is upstream's to change, and upstream changed it.

27 September 2026. A deploy ran ten minutes of package updates and disk
preparation on the mailbox, then:

    ==> 1. Downloading zcs-10.1.18_GA_4200001.UBUNTU22_64.20260801175919.tgz
    curl: (22) The requested URL returned error: 404

The 10.1.18 artefact had been rebuilt and the build stamp in its filename moved
from ...175919 to ...175925. Six seconds. Every config carrying the old name
pointed at a file that no longer existed, and the operator read the failure as
something they had misconfigured - they were mid-way through testing a
Cloudflare token and reasonably assumed it was related.

Two routes are supposed to prevent that, and the first one had quietly stopped
working: api.github.com allows 60 unauthenticated requests an hour per address,
so an office behind one NAT gets 403 and the console falls through to a
hardcoded name with no way of knowing it is stale.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kin_privhelper import apply_config as ac

# What /releases/expanded_assets/<tag> actually serves, trimmed.
ASSETS_HTML = """
<li><a href="/maldua/zimbra-foss/releases/download/zimbra-foss-build-ubuntu-22.04/10.1.20.p1/zcs-10.1.20_GA_4200001.UBUNTU22_64.20260820122323.tgz">
zcs-10.1.20_GA_4200001.UBUNTU22_64.20260820122323.tgz</a></li>
<li><a>zcs-10.1.20_GA_4200001.UBUNTU22_64.20260820122323.tgz.sha256</a></li>
"""


class _Resp:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class WhenTheApiIsRateLimited(unittest.TestCase):
    """403 from api.github.com must not mean "use whatever was hardcoded"."""

    def test_the_asset_list_is_read_from_the_plain_page(self) -> None:
        with patch.object(ac, "_os_version_id", return_value="22.04"), patch(
            "urllib.request.urlopen", return_value=_Resp(ASSETS_HTML)
        ):
            got = ac.resolve_zcs_artefacts_without_api()
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(
            got["ZCS_FILE"], "zcs-10.1.20_GA_4200001.UBUNTU22_64.20260820122323.tgz"
        )
        self.assertEqual(got["ZCS_VERSION"], "10.1.20.p1")
        self.assertTrue(got["ZCS_BASE"].endswith("/10.1.20.p1"))

    def test_the_sha256_is_not_mistaken_for_the_tarball(self) -> None:
        # Both names are on the page and one is a prefix of the other.
        with patch.object(ac, "_os_version_id", return_value="22.04"), patch(
            "urllib.request.urlopen", return_value=_Resp(ASSETS_HTML)
        ):
            got = ac.resolve_zcs_artefacts_without_api()
        assert got is not None
        self.assertTrue(got["ZCS_FILE"].endswith(".tgz"))
        self.assertNotIn(".sha256", got["ZCS_FILE"])

    def test_the_platform_decides_which_asset(self) -> None:
        html = ASSETS_HTML + (
            "<li>zcs-10.1.20_GA_4200001.UBUNTU24_64.20260820122323.tgz</li>"
        )
        with patch.object(ac, "_os_version_id", return_value="24.04"), patch(
            "urllib.request.urlopen", return_value=_Resp(html)
        ):
            got = ac.resolve_zcs_artefacts_without_api()
        assert got is not None
        self.assertIn("UBUNTU24_64", got["ZCS_FILE"])

    def test_an_unreachable_page_is_not_an_answer(self) -> None:
        with patch.object(ac, "_os_version_id", return_value="22.04"), patch(
            "urllib.request.urlopen", side_effect=OSError("no route")
        ):
            self.assertIsNone(ac.resolve_zcs_artefacts_without_api())

    def test_it_is_tried_before_the_hardcoded_name(self) -> None:
        with patch.object(ac, "resolve_latest_zcs_artefacts", return_value=None), patch.object(
            ac, "resolve_zcs_artefacts_without_api", return_value={"ZCS_FILE": "from-page.tgz"}
        ):
            self.assertEqual(ac.default_zcs_artefacts()["ZCS_FILE"], "from-page.tgz")


class TheHardcodedFallback(unittest.TestCase):
    """It is a last resort with a shelf life, so it has to be checked."""

    def _fallback(self, version_id: str) -> dict[str, str]:
        with patch.object(ac, "_os_version_id", return_value=version_id), patch.object(
            ac, "resolve_latest_zcs_artefacts", return_value=None
        ), patch.object(ac, "resolve_zcs_artefacts_without_api", return_value=None):
            return ac.default_zcs_artefacts()

    def test_it_is_not_the_name_that_404d(self) -> None:
        for version_id in ("22.04", "24.04"):
            with self.subTest(version_id=version_id):
                self.assertNotIn("20260801175919", self._fallback(version_id)["ZCS_FILE"])

    def test_it_still_carries_a_build_stamp(self) -> None:
        # A short name without the stamp 404s too, which is the failure this
        # fallback existed to avoid in the first place.
        for version_id in ("22.04", "24.04"):
            with self.subTest(version_id=version_id):
                name = self._fallback(version_id)["ZCS_FILE"]
                self.assertRegex(name, r"\.\d{14}\.tgz$")

    def test_the_version_and_the_filename_agree(self) -> None:
        for version_id in ("22.04", "24.04"):
            with self.subTest(version_id=version_id):
                got = self._fallback(version_id)
                base_version = got["ZCS_VERSION"].split(".p")[0]
                self.assertIn(base_version.replace(".", "."), got["ZCS_FILE"])
                self.assertTrue(got["ZCS_BASE"].endswith("/" + got["ZCS_VERSION"]))

    def test_the_platform_matches_the_host(self) -> None:
        self.assertIn("UBUNTU22_64", self._fallback("22.04")["ZCS_FILE"])
        self.assertIn("UBUNTU24_64", self._fallback("24.04")["ZCS_FILE"])


if __name__ == "__main__":
    unittest.main()
