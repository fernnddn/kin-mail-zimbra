"""Mail flow collector: log parsing, queue depth, and reading a rotating log.

The Monitoring tab could say the machine was busy. It could not say whether
mail was moving, which is the question a mail appliance exists to answer. This
collector reads the Postfix log incrementally and the spool directly.

The incremental read is the part worth testing hardest. Getting it wrong is
silent in both directions: too eager and every pass re-counts the whole file,
too cautious and the graph flatlines after the nightly logrotate while mail
keeps flowing.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
COLLECTOR = REPO / "monitoring/mailflow/kin-mail-flow-metrics.py"


def _load() -> Any:
    importlib.invalidate_caches()
    spec = importlib.util.spec_from_file_location("kin_mail_flow", COLLECTOR)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mf = _load()
_qid = itertools.count(1)


def delivered(transport: str = "lmtp") -> str:
    q = f"{next(_qid):010X}"
    return (
        f"Sep  5 09:14:04 mail postfix/{transport}[2860]: {q}: to=<user@example.test>, "
        f"relay=127.0.0.1[127.0.0.1]:7025, delay=1.8, delays=0.5/0/0/1.3, "
        f"dsn=2.1.5, status=sent (250 2.1.5 Delivery OK)\n"
    )


class LogParsing(unittest.TestCase):
    def test_a_realistic_log_excerpt_counts_correctly(self) -> None:
        lines = """
Sep  5 09:14:02 mail postfix/smtpd[2841]: connect from unknown[198.51.100.7]
Sep  5 09:14:02 mail postfix/smtpd[2841]: A1B2C3D4E5: client=unknown[198.51.100.7]
Sep  5 09:14:03 mail postfix/cleanup[2850]: A1B2C3D4E5: message-id=<x@example.test>
Sep  5 09:14:03 mail postfix/qmgr[1200]: A1B2C3D4E5: from=<a@example.test>, size=2048, nrcpt=1 (queue active)
Sep  5 09:14:04 mail postfix/lmtp[2860]: A1B2C3D4E5: to=<user@example.test>, relay=127.0.0.1[127.0.0.1]:7025, delay=1.8, status=sent (250 2.1.5 Delivery OK)
Sep  5 09:15:10 mail postfix/smtpd[2841]: NOQUEUE: reject: RCPT from bad[203.0.113.9]: 554 5.7.1 Service unavailable
Sep  5 09:16:00 mail postfix/smtp[2900]: BB11CC22: to=<out@remote.test>, relay=mx.remote.test[203.0.113.5]:25, delay=2.1, status=sent (250 OK)
Sep  5 09:17:00 mail postfix/smtp[2901]: CC33DD44: to=<x@down.test>, relay=none, delay=30, status=deferred (connect timed out)
Sep  5 09:18:00 mail postfix/smtp[2902]: DD55EE66: to=<no@remote.test>, relay=mx.remote.test[203.0.113.5]:25, status=bounced (550 5.1.1 unknown)
Sep  5 09:19:00 mail postfix/smtpd[2841]: lost connection after EHLO from scanner[203.0.113.44]
Sep  5 09:20:01 mail postfix/smtp[2903]: EE77FF88: to=<x@y.test>, relay=none, delay=432000, status=expired (message expired)
Sep  5 09:21:00 mail dovecot: something unrelated
Sep  5 09:21:01 mail postfix/smtpd[2841]: timeout after DATA from slow[203.0.113.77]
""".strip().splitlines()
        c = mf.parse_lines(lines, mf.new_counters())
        self.assertEqual(c["accepted"], 1)
        self.assertEqual(c["rejected"], 1)
        self.assertEqual(c["lost"], 2)          # lost connection + timeout
        self.assertEqual(c["delivered_lmtp"], 1)   # inbound, into a mailbox
        self.assertEqual(c["delivered_smtp"], 1)   # outbound, to the internet
        self.assertEqual(c["deferred"], 1)
        self.assertEqual(c["bounced"], 1)
        self.assertEqual(c["expired"], 1)

    def test_inbound_and_outbound_are_separated_by_transport(self) -> None:
        # Zimbra delivers into mailboxes over lmtp and relays out over smtp;
        # that is what tells the two directions apart without parsing
        # addresses and guessing which domains are local.
        c = mf.parse_lines([delivered("lmtp"), delivered("smtp")], mf.new_counters())
        self.assertEqual(c["delivered_lmtp"], 1)
        self.assertEqual(c["delivered_smtp"], 1)

    def test_non_postfix_lines_are_ignored(self) -> None:
        noise = [
            "Sep  5 09:00:00 mail dovecot: imap-login: Login: user=<a@example.test>\n",
            "Sep  5 09:00:01 mail systemd[1]: Started something.\n",
            "Sep  5 09:00:02 mail kernel: [12345.6] eth0: link up\n",
            "\n",
        ]
        self.assertEqual(mf.parse_lines(noise, mf.new_counters()), mf.new_counters())

    def test_a_rejection_is_not_counted_as_an_acceptance(self) -> None:
        # NOQUEUE lines carry no queue id; that is the whole distinction.
        c = mf.parse_lines(
            ["Sep  5 09:15:10 mail postfix/smtpd[1]: NOQUEUE: reject: RCPT from x[203.0.113.9]: 554\n"],
            mf.new_counters(),
        )
        self.assertEqual(c["rejected"], 1)
        self.assertEqual(c["accepted"], 0)


class QueueDepth(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.spool = self.root / "spool"
        for q in mf.QUEUES:
            (self.spool / q).mkdir(parents=True)

    def test_counts_messages_per_queue(self) -> None:
        for i in range(3):
            (self.spool / "deferred" / f"m{i}").write_text("x")
        (self.spool / "active" / "a1").write_text("x")
        depths = mf.queue_depths(self.spool)
        self.assertEqual(depths["deferred"], 3)
        self.assertEqual(depths["active"], 1)
        self.assertEqual(depths["incoming"], 0)

    def test_counts_messages_in_postfix_hash_subdirectories(self) -> None:
        # Postfix shards large queues into single-character subdirectories.
        nested = self.spool / "deferred" / "A" / "B"
        nested.mkdir(parents=True)
        (nested / "AABBCCDD").write_text("x")
        self.assertEqual(mf.queue_depths(self.spool)["deferred"], 1)

    def test_an_empty_spool_is_zero_not_an_error(self) -> None:
        self.assertEqual(mf.queue_depths(self.root / "nothing-here")["deferred"], 0)

    def test_oldest_age_is_what_says_mail_is_stuck(self) -> None:
        # Depth alone cannot distinguish ten messages that arrived a second
        # ago from ten that have been retrying since 03:00.
        now = time.time()
        (self.spool / "deferred" / "new").write_text("x")
        old = self.spool / "deferred" / "old"
        old.write_text("x")
        os.utime(old, (now - 7200, now - 7200))
        self.assertAlmostEqual(
            mf.oldest_queued_seconds(self.spool, now=now), 7200, delta=5
        )

    def test_an_empty_queue_reports_zero_age(self) -> None:
        self.assertEqual(mf.oldest_queued_seconds(self.spool), 0.0)


class IncrementalRead(unittest.TestCase):
    """Too eager re-counts the whole file; too cautious flatlines after logrotate."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.log = self.root / "zimbra.log"
        self.state: dict[str, Any] = {
            "inode": 0, "offset": 0, "head": "", "head_len": 0,
            "counters": mf.new_counters(),
        }

    def cycle(self) -> int:
        lines, pos = mf.read_new_lines(self.log, self.state)
        counters = mf.parse_lines(lines, self.state["counters"])
        self.state = {**pos, "counters": counters}
        return counters["delivered_lmtp"]

    def _run_lifecycle(self, first: int) -> None:
        self.log.write_text("".join(delivered() for _ in range(first)))
        self.assertEqual(self.cycle(), first, "first read")
        self.assertEqual(self.cycle(), first, "a second pass must add nothing")

        n = first
        for _ in range(3):
            with self.log.open("a") as fh:
                fh.write(delivered())
            n += 1
            self.assertEqual(self.cycle(), n, "append")

        # logrotate's default: rename away, recreate.
        os.rename(self.log, self.root / f"rotated-{first}")
        self.log.write_text(delivered())
        n += 1
        self.assertEqual(self.cycle(), n, "rotation")

        # logrotate copytruncate, replaced with the SAME length. Invisible to
        # inode and to size; only a prefix hash catches it.
        self.log.write_text(delivered())
        n += 1
        self.assertEqual(self.cycle(), n, "truncate to the same length")

        self.log.write_text(delivered() + delivered())
        n += 2
        self.assertEqual(self.cycle(), n, "truncate and grow")

        os.remove(self.log)
        self.assertEqual(self.cycle(), n, "a missing log must not lose counters")

        self.log.write_text(delivered())
        n += 1
        self.assertEqual(self.cycle(), n, "log reappears")

    def test_lifecycle_on_a_log_larger_than_the_prefix(self) -> None:
        self._run_lifecycle(first=6)

    def test_lifecycle_on_a_log_smaller_than_the_prefix(self) -> None:
        # The first attempt hashed "256 bytes, or the whole file if shorter",
        # so on a young log every append changed the hash, was read as a
        # truncation, and re-counted the entire file every 30 seconds.
        self._run_lifecycle(first=1)

    def test_a_half_written_line_is_left_for_the_next_pass(self) -> None:
        self.log.write_text(delivered())
        self.assertEqual(self.cycle(), 1)
        partial = delivered()
        with self.log.open("a") as fh:
            fh.write(partial[:40])
        self.assertEqual(self.cycle(), 1, "half a line is not an event")
        with self.log.open("a") as fh:
            fh.write(partial[40:])
        self.assertEqual(self.cycle(), 2, "and is counted once it completes")

    def test_a_hugely_grown_log_is_tailed_not_replayed(self) -> None:
        # A collector that was not running for a week comes back to a log far
        # bigger than its saved offset. It must skip to the tail rather than
        # block a live mail node on a multi-GB read.
        self.log.write_text(delivered())
        self.cycle()
        self.state["offset"] = 0
        self.state["head"] = ""          # force the "way behind" path
        filler = ("Sep  5 09:00:00 mail dovecot: noise\n" * 4096)
        with self.log.open("a") as fh:
            while self.log.stat().st_size < mf.MAX_READ_BYTES + 1_000_000:
                fh.write(filler)
                fh.flush()
        size = self.log.stat().st_size
        _lines, pos = mf.read_new_lines(self.log, self.state)
        self.assertGreaterEqual(
            pos["offset"],
            size - mf.MAX_READ_BYTES,
            "should have skipped ahead instead of replaying from the start",
        )
        self.assertLessEqual(pos["offset"], size)


class PromOutput(unittest.TestCase):
    def _render(self) -> str:
        counters = mf.new_counters()
        counters.update(delivered_lmtp=12, delivered_smtp=3, deferred=2, bounced=1)
        return mf.render(
            counters,
            {"incoming": 0, "active": 1, "deferred": 2, "hold": 0, "corrupt": 0},
            3600.0,
            ok=True,
            now=1_757_000_000.0,
        )

    def test_every_series_carries_help_and_type(self) -> None:
        body = self._render()
        for name in (
            "kin_mail_events_total",
            "kin_mail_delivered_total",
            "kin_mail_undelivered_total",
            "kin_mail_queue_messages",
            "kin_mail_queue_oldest_seconds",
            "kin_mail_flow_up",
            "kin_mail_flow_mta",
        ):
            self.assertIn(f"# HELP {name} ", body, name)
            self.assertIn(f"# TYPE {name} ", body, name)

    def test_counters_are_declared_as_counters(self) -> None:
        # A counter declared as a gauge cannot be rate()d meaningfully.
        body = self._render()
        self.assertIn("# TYPE kin_mail_delivered_total counter", body)
        self.assertIn("# TYPE kin_mail_undelivered_total counter", body)
        self.assertIn("# TYPE kin_mail_queue_messages gauge", body)

    def test_values_render_as_plain_numbers(self) -> None:
        body = self._render()
        self.assertIn('kin_mail_delivered_total{transport="lmtp"} 12', body)
        self.assertIn('kin_mail_queue_messages{queue="deferred"} 2', body)
        self.assertIn("kin_mail_queue_oldest_seconds 3600", body)
        self.assertIn("kin_mail_flow_up 1", body)
        self.assertIn("kin_mail_flow_mta 1", body)
        # No scientific notation or stray decimals that would fail a parse.
        for line in body.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            value = line.rsplit(" ", 1)[1]
            float(value)  # raises if unparseable

    def test_a_failed_read_reports_itself(self) -> None:
        body = mf.render(mf.new_counters(), {}, 0.0, ok=False, now=1.0)
        self.assertIn("kin_mail_flow_up 0", body)

    def test_a_node_without_postfix_says_so(self) -> None:
        """Zeros from a mailbox node are not 'no mail is moving'."""
        body = mf.render(mf.new_counters(), {}, 0.0, ok=True, now=1.0, mta=False)
        self.assertRegex(body, r"(?m)^kin_mail_flow_mta 0$")
        self.assertNotRegex(body, r"(?m)^kin_mail_flow_mta 1$")


class EndToEnd(unittest.TestCase):
    def test_it_writes_a_prom_file_and_resumes_from_state(self) -> None:
        root = Path(tempfile.mkdtemp())
        log = root / "zimbra.log"
        spool = root / "spool"
        (spool / "deferred").mkdir(parents=True)
        out = root / "textfile"
        log.write_text(delivered() + delivered())
        argv = ["--log", str(log), "--spool", str(spool), "--textfile-dir", str(out)]

        self.assertEqual(mf.main(argv), 0)
        body = (out / mf.PROM_NAME).read_text()
        self.assertIn('kin_mail_delivered_total{transport="lmtp"} 2', body)

        # A second run with no new log lines must not double the counter.
        self.assertEqual(mf.main(argv), 0)
        self.assertIn(
            'kin_mail_delivered_total{transport="lmtp"} 2',
            (out / mf.PROM_NAME).read_text(),
        )

        with log.open("a") as fh:
            fh.write(delivered())
        self.assertEqual(mf.main(argv), 0)
        self.assertIn(
            'kin_mail_delivered_total{transport="lmtp"} 3',
            (out / mf.PROM_NAME).read_text(),
        )

    def test_reset_starts_the_counters_again(self) -> None:
        root = Path(tempfile.mkdtemp())
        log = root / "zimbra.log"
        spool = root / "spool"
        spool.mkdir()
        out = root / "textfile"
        log.write_text(delivered())
        argv = ["--log", str(log), "--spool", str(spool), "--textfile-dir", str(out)]
        mf.main(argv)
        mf.main(argv + ["--reset"])
        self.assertIn(
            'kin_mail_delivered_total{transport="lmtp"} 1',
            (out / mf.PROM_NAME).read_text(),
        )

    def test_no_temp_file_is_left_behind(self) -> None:
        # node_exporter reads this directory continuously; a half-written file
        # would be parsed as a broken metric set.
        root = Path(tempfile.mkdtemp())
        log = root / "zimbra.log"
        log.write_text(delivered())
        spool = root / "spool"
        spool.mkdir()
        out = root / "textfile"
        mf.main(["--log", str(log), "--spool", str(spool), "--textfile-dir", str(out)])
        self.assertEqual([p.name for p in out.glob("*.tmp")], [])

    def test_a_missing_log_still_produces_a_readable_file(self) -> None:
        root = Path(tempfile.mkdtemp())
        out = root / "textfile"
        spool = root / "spool"
        spool.mkdir()
        mf.main([
            "--log", str(root / "absent.log"),
            "--spool", str(spool),
            "--textfile-dir", str(out),
        ])
        self.assertIn("kin_mail_flow_up 0", (out / mf.PROM_NAME).read_text())


if __name__ == "__main__":
    unittest.main()
