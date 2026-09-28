"""The console must start when /etc/kin-mail-console/console.env cannot be read.

The console runs as ``kin-console`` and reads console.env only through group
ownership (``root:kin-console 0640``). pydantic-settings opens every path in
``env_file`` while the class body is evaluated, so for a long time an existing
but unreadable file raised PermissionError during *import* - taking the whole
console down before any logging existed, with a traceback that named
python-dotenv rather than the file to fix. The docstring in settings.py claimed
the read was best-effort; the code was not.

That is reachable without anybody making a mistake: a restore that lands the
file as root:root, a hand-typed ``chmod 600``, or a useradd that recreated the
group with a different gid. Under systemd the values arrive through
EnvironmentFile= anyway, so skipping an unreadable file costs nothing and
failing costs the appliance its admin console.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from kin_console import settings as S


class ReadableEnvFilesTests(unittest.TestCase):
    def _candidates(self, *paths: str):
        return unittest.mock.patch.object(S, "ENV_FILE_CANDIDATES", tuple(paths))

    def test_a_readable_file_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "console.env"
            p.write_text("CONSOLE_PORT=9999\n", encoding="utf-8")
            with self._candidates(str(p)):
                self.assertEqual(S.readable_env_files(), (str(p),))

    def test_a_missing_file_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self._candidates(str(Path(td) / "absent.env")):
                self.assertEqual(S.readable_env_files(), ())

    @unittest.skipIf(os.geteuid() == 0, "root can read any mode; run as a normal user")
    def test_an_unreadable_file_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "console.env"
            p.write_text("CONSOLE_PORT=9999\n", encoding="utf-8")
            p.chmod(0o000)
            try:
                with self._candidates(str(p)):
                    self.assertEqual(
                        S.readable_env_files(),
                        (),
                        "an unreadable console.env must be skipped, not opened",
                    )
            finally:
                p.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_a_directory_in_place_of_the_file_is_skipped(self) -> None:
        # A bind mount or a botched restore can leave a directory here. dotenv
        # would raise IsADirectoryError, which is also an OSError.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "console.env"
            d.mkdir()
            with self._candidates(str(d)):
                self.assertEqual(S.readable_env_files(), ())

    def test_a_dangling_symlink_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "console.env"
            link.symlink_to(Path(td) / "nowhere.env")
            with self._candidates(str(link)):
                self.assertEqual(S.readable_env_files(), ())

    @unittest.skipIf(os.geteuid() == 0, "root can read any mode; run as a normal user")
    def test_one_bad_candidate_does_not_discard_a_good_one(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.env"
            bad.write_text("X=1\n", encoding="utf-8")
            bad.chmod(0o000)
            good = Path(td) / "good.env"
            good.write_text("Y=2\n", encoding="utf-8")
            try:
                with self._candidates(str(bad), str(good)):
                    self.assertEqual(S.readable_env_files(), (str(good),))
            finally:
                bad.chmod(stat.S_IRUSR | stat.S_IWUSR)


class SettingsImportContractTests(unittest.TestCase):
    def test_settings_module_hands_pydantic_only_readable_paths(self) -> None:
        # The whole point: env_file must come from readable_env_files(), never
        # from a literal path. A literal reintroduces the import-time crash.
        src = Path(S.__file__).read_text(encoding="utf-8")
        self.assertIn("env_file=readable_env_files()", src)
        self.assertNotIn(
            'env_file=("/etc/kin-mail-console/console.env",)',
            src,
            "env_file must not name the path directly - dotenv opens it eagerly",
        )

    def test_the_real_candidate_path_is_still_the_one_we_ship(self) -> None:
        # Guards against the fix quietly changing which file is honoured.
        self.assertIn("/etc/kin-mail-console/console.env", S.ENV_FILE_CANDIDATES)

    def test_importing_settings_did_not_require_reading_that_file(self) -> None:
        # This test file only runs because the import above succeeded; on a
        # workstation where /etc/kin-mail-console/console.env exists root-only
        # the old code raised PermissionError here and 125 unrelated tests
        # could not be collected at all.
        self.assertIsInstance(S.settings.console_port, int)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401

    unittest.main()
