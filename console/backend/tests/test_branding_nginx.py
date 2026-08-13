"""Branding nginx patcher + dummy placeholder assets (no live Zimbra)."""

from __future__ import annotations

import importlib.util
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATCHER = ROOT / "install" / "lib" / "kin_brand_nginx.py"
DUMMY = ROOT / "branding" / "dummy"

CATCHALL = """
    location /
    {
        return 200;
    }
"""


def _load_patcher():
    spec = importlib.util.spec_from_file_location("kin_brand_nginx", PATCHER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path} is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


class DummyBrandAssetsTests(unittest.TestCase):
    def test_placeholder_copy_is_explicit(self) -> None:
        conf = (DUMMY / "brand.conf").read_text()
        readme = (DUMMY / "README.md").read_text()
        self.assertIn("PLACEHOLDER", conf)
        self.assertIn("NOT A CUSTOMER", conf)
        self.assertIn("#C45C26", conf)
        self.assertIn("placeholder", readme.lower())
        self.assertNotIn("10.10.", conf)
        self.assertNotIn("gits-it", conf)

    def test_login_and_app_banners_are_png(self) -> None:
        self.assertEqual(_png_size(DUMMY / "login-banner.png"), (450, 60))
        self.assertEqual(_png_size(DUMMY / "app-banner.png"), (200, 35))
        ico = (DUMMY / "favicon.ico").read_bytes()
        self.assertGreater(len(ico), 22)
        self.assertEqual(ico[:4], b"\x00\x00\x01\x00")


class NginxPatcherTests(unittest.TestCase):
    def test_inserts_before_catchall_and_is_idempotent(self) -> None:
        mod = _load_patcher()
        original = (
            "server {\n"
            "    listen 443 ssl;\n"
            "    location ^~ /zimbraAdmin {\n"
            "        return 404;\n"
            "    }\n"
            f"{CATCHALL}"
            "}\n"
        )
        snippet = "/etc/kin-mail/brand/nginx-kin-brand.conf"
        once, changed = mod.patch_https_template(original, snippet)
        self.assertTrue(changed)
        self.assertIn("location ^~ /kin-brand/", once)
        self.assertIn(f"include {snippet};", once)
        self.assertLess(once.find("location ^~ /kin-brand/"), once.find("location /"))
        twice, changed_again = mod.patch_https_template(once, snippet)
        self.assertFalse(changed_again)
        self.assertEqual(once, twice)

    def test_repairs_half_applied_block(self) -> None:
        mod = _load_patcher()
        stale = (
            "server {\n"
            f"    {mod.MARKER_BEGIN}\n"
            "    location ^~ /kin-brand/ { return 404; }\n"
            f"    {mod.MARKER_END}\n"
            f"{CATCHALL}"
            "}\n"
        )
        snippet = "/etc/kin-mail/brand/nginx-kin-brand.conf"
        text, changed = mod.patch_https_template(stale, snippet)
        self.assertTrue(changed)
        self.assertEqual(text.count("location ^~ /kin-brand/"), 1)
        self.assertIn(f"include {snippet};", text)
        self.assertNotIn("return 404", text.split("kin-brand")[1].split("location /")[0])


if __name__ == "__main__":
    unittest.main()
