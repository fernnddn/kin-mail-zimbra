"""The deploy log has to be readable, and it must not lie about failure."""

from __future__ import annotations

import unittest

from kin_privhelper.log_format import (
    format_command_banner,
    tidy_ansible_line,
    tidy_ansible_text,
)


class RetryLineTests(unittest.TestCase):
    """`until:` polling is not failure, and must not print the word."""

    LINE = (
        "FAILED - RETRYING: [mail.example.test]: Wait until kin-fs, kin-vip, "
        "and kin-zimbra are Started (90 retries left)."
    )

    def test_the_word_failed_is_gone(self) -> None:
        out = tidy_ansible_line(self.LINE)
        self.assertNotIn("FAILED", out)
        self.assertNotIn("RETRYING", out)

    def test_it_says_what_is_being_waited_for(self) -> None:
        out = tidy_ansible_line(self.LINE)
        self.assertIn("waiting", out)
        self.assertIn("Wait until kin-fs, kin-vip, and kin-zimbra are Started", out)
        self.assertIn("90 more attempts", out)

    def test_the_last_attempt_reads_correctly(self) -> None:
        line = "FAILED - RETRYING: [h]: Wait for it (1 retries left)."
        self.assertIn("1 more attempt)", tidy_ansible_line(line))

    def test_a_real_failure_is_left_alone(self) -> None:
        # This one IS a failure and must keep every alarming word it has.
        line = 'fatal: [mail.example.test]: FAILED! => {'
        self.assertEqual(tidy_ansible_line(line), line)


class BannerTests(unittest.TestCase):
    def test_task_banners_lose_the_asterisks(self) -> None:
        line = "TASK [drbd_disk_prep : Collect lsblk JSON] " + "*" * 40
        out = tidy_ansible_line(line)
        self.assertEqual(out, "TASK  drbd_disk_prep: Collect lsblk JSON")

    def test_play_banners_too(self) -> None:
        out = tidy_ansible_line("PLAY [Set up DRBD] " + "*" * 60)
        self.assertEqual(out, "PLAY  Set up DRBD")

    def test_handlers_are_labelled_as_handlers(self) -> None:
        out = tidy_ansible_line("RUNNING HANDLER [role : Reload systemd] ****")
        self.assertEqual(out, "HANDLER  role: Reload systemd")

    def test_the_recap_header_is_just_the_words(self) -> None:
        self.assertEqual(tidy_ansible_line("PLAY RECAP " + "*" * 60), "PLAY RECAP")

    def test_a_colon_inside_a_task_name_survives(self) -> None:
        # Only the "role : task" separator is rewritten, not punctuation in the
        # task's own name.
        out = tidy_ansible_line("TASK [r : Check A: then B] ***")
        self.assertEqual(out, "TASK  r: Check A: then B")


class PassthroughTests(unittest.TestCase):
    """Nothing is dropped. The ask was less decoration, not less information."""

    def test_result_lines_are_untouched(self) -> None:
        for line in (
            "ok: [mail2.example.test]",
            "changed: [mail.example.test]",
            "skipping: [mail.example.test]",
            "mail.example.test : ok=80 changed=8 unreachable=0 failed=0",
        ):
            with self.subTest(line=line):
                self.assertEqual(tidy_ansible_line(line), line)

    def test_blank_lines_survive(self) -> None:
        self.assertEqual(tidy_ansible_line("\n"), "\n")

    def test_newlines_are_preserved(self) -> None:
        self.assertTrue(tidy_ansible_line("PLAY RECAP ****\n").endswith("\n"))
        self.assertFalse(tidy_ansible_line("PLAY RECAP ****").endswith("\n"))

    def test_a_chunk_keeps_its_line_count(self) -> None:
        chunk = "TASK [a : b] ***\nok: [h]\n\nPLAY RECAP ****\n"
        self.assertEqual(
            len(tidy_ansible_text(chunk).splitlines()), len(chunk.splitlines())
        )

    def test_installer_output_is_not_mangled(self) -> None:
        # The same filter sees shell-stage output; it must only touch ansible's.
        for line in ("[ OK ] dnsmasq active", "==> 3. Packages", "[WARN] something"):
            with self.subTest(line=line):
                self.assertEqual(tidy_ansible_line(line), line)


class CommandBannerTests(unittest.TestCase):
    PLAYBOOK = [
        "/usr/bin/ansible-playbook",
        "-i",
        "/var/lib/kin-mail-privhelper/orchestration/inventory.yml",
        "--tags",
        "disk_prep",
        "/opt/kin-mail-console/ansible/playbooks/mail-drbd.yml",
    ]

    def test_a_playbook_run_is_one_short_line(self) -> None:
        out = format_command_banner(self.PLAYBOOK, "2026-08-29T04:08:27.760616+00:00")
        self.assertEqual(out, "> mail-drbd.yml  tags=disk_prep  04:08:27")

    def test_absolute_paths_and_microseconds_are_gone(self) -> None:
        out = format_command_banner(self.PLAYBOOK, "2026-08-29T04:08:27.760616+00:00")
        self.assertNotIn("/var/lib", out)
        self.assertNotIn("/opt/kin-mail-console", out)
        self.assertNotIn("760616", out)

    def test_an_ssh_run_names_the_target_not_its_options(self) -> None:
        argv = [
            "/usr/bin/sshpass", "-e", "ssh",
            "-o", "PreferredAuthentications=password",
            "-o", "StrictHostKeyChecking=accept-new",
            "kin@192.0.2.9", "long remote command here",
        ]
        out = format_command_banner(argv, "2026-08-29T07:17:48.101246+00:00")
        self.assertEqual(out, "> ssh kin@192.0.2.9  07:17:48")

    def test_anything_else_keeps_its_full_argv(self) -> None:
        # One-off commands are where the exact flags matter and where they do
        # not repeat, so they are left complete.
        out = format_command_banner(["/usr/sbin/pcs", "resource", "config"], "2026-08-29T01:02:03+00:00")
        self.assertIn("/usr/sbin/pcs resource config", out)

    def test_check_mode_is_visible(self) -> None:
        out = format_command_banner(self.PLAYBOOK + ["--check"], "2026-08-29T04:08:27+00:00")
        self.assertIn("check-mode", out)


class WiringTests(unittest.TestCase):
    def test_the_orchestrator_filters_what_it_streams(self) -> None:
        from pathlib import Path

        orch = (
            Path(__file__).resolve().parents[1]
            / "kin_privhelper/orchestration.py"
        ).read_text(encoding="utf-8")
        self.assertIn("line_filter=tidy_ansible_text", orch)

    def test_the_filter_runs_before_the_transcript_is_written(self) -> None:
        from pathlib import Path

        cmds = (
            Path(__file__).resolve().parents[1] / "kin_privhelper/commands.py"
        ).read_text(encoding="utf-8")
        # Otherwise the log read afterwards would not match what was watched.
        filt = cmds.index("if line_filter is not None:")
        tee = cmds.index("if kind == \"tee_only\":", filt - 400)
        self.assertLess(filt, tee)


if __name__ == "__main__":
    unittest.main()
