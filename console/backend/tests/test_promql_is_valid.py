"""Every PromQL this console sends must actually parse.

The mail-flow freshness query contained `\\.` inside a double-quoted PromQL
string. PromQL string literals use Go escaping, where `\\.` is not an escaped
dot but an *invalid escape sequence*, so Prometheus rejected the whole query
and the panel whose job is to warn about stale figures printed

    invalid parameter "query": 1:44: parse error: unknown escape sequence U+002E '.'

across the Monitoring tab and the top of every report (QA Phase 16, 12
September 2026).

A string-equality test would not have caught that, because the string was
exactly what was intended - it was the escaping rules that were misunderstood.
So this validates the query the way a parser does: it walks the literals and
rejects any escape sequence Prometheus would reject.
"""

from __future__ import annotations

import re
import unittest

from kin_console import monitoring as M

# Go's escape sequences, which is what PromQL accepts inside a quoted string.
GO_ESCAPES = set('abfnrtv\\\'"')


def promql_string_literals(query: str) -> list[tuple[str, str]]:
    """Every string literal in a query, as (quote character, raw body)."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(query):
        ch = query[i]
        if ch not in "\"'`":
            i += 1
            continue
        quote = ch
        i += 1
        body = []
        while i < len(query) and query[i] != quote:
            # A backtick string is raw: a backslash never escapes the quote.
            if quote != "`" and query[i] == "\\" and i + 1 < len(query):
                body.append(query[i])
                body.append(query[i + 1])
                i += 2
                continue
            body.append(query[i])
            i += 1
        i += 1
        out.append((quote, "".join(body)))
    return out


def invalid_escapes(query: str) -> list[str]:
    """Escape sequences Prometheus would refuse.

    Backtick literals are raw, so nothing in them is an escape at all - which
    is exactly why the fix uses one.
    """
    bad: list[str] = []
    for quote, body in promql_string_literals(query):
        if quote == "`":
            continue
        for m in re.finditer(r"\\(.)", body):
            ch = m.group(1)
            if ch in GO_ESCAPES or ch in "xuU01234567":
                continue
            bad.append("\\" + ch)
    return bad


class TheFreshnessQueryParses(unittest.TestCase):
    def _query(self) -> str:
        captured: dict[str, str] = {}

        def fake_get(path, params=None):
            captured["query"] = (params or {}).get("query", "")
            raise M.MonitoringError("not calling a real Prometheus")

        original = M._get
        M._get = fake_get  # type: ignore[assignment]
        try:
            M.mail_flow_freshness()
        finally:
            M._get = original  # type: ignore[assignment]
        self.assertIn("query", captured, "mail_flow_freshness sent no query")
        return captured["query"]

    def test_it_contains_no_escape_prometheus_would_reject(self) -> None:
        query = self._query()
        self.assertEqual(
            invalid_escapes(query),
            [],
            f"Prometheus would refuse this whole query: {query}",
        )

    def test_the_literal_is_raw_so_the_question_cannot_come_back(self) -> None:
        # A backtick string has no escape processing, which is the reason it
        # was chosen over getting the doubling right once.
        self.assertIn("`", self._query())

    def test_the_pattern_still_matches_what_node_exporter_labels(self) -> None:
        """A query that parses and matches nothing is not an improvement."""
        query = self._query()
        literals = [body for quote, body in promql_string_literals(query) if quote == "`"]
        self.assertTrue(literals, "no raw literal found")
        pattern = literals[0]
        # node_exporter has labelled this with both shapes across versions.
        self.assertRegex("kin_mail_flow.prom", f"^{pattern}$")
        self.assertRegex(
            "/var/lib/node_exporter/textfile/kin_mail_flow.prom", f"^{pattern}$"
        )

    def test_the_pattern_cannot_match_another_collector(self) -> None:
        query = self._query()
        pattern = [b for q, b in promql_string_literals(query) if q == "`"][0]
        self.assertIsNone(re.fullmatch(pattern, "kin_mail_gateway.prom"))
        self.assertIsNone(re.fullmatch(pattern, "kin_mail_flowXprom.prom"))

    def test_the_escape_checker_itself_works(self) -> None:
        """A checker that passes everything would have passed the bug."""
        self.assertEqual(invalid_escapes('foo{f=~"a\\.b"}'), ["\\."])
        self.assertEqual(invalid_escapes('foo{f=~"a\\\\.b"}'), [])
        self.assertEqual(invalid_escapes("foo{f=~`a\\.b`}"), [])
        self.assertEqual(invalid_escapes('foo{f=~"a\\nb"}'), [])


class EveryCataloguedQueryParses(unittest.TestCase):
    def test_no_catalogue_entry_has_an_invalid_escape(self) -> None:
        bad = {}
        for name, (_label, _unit, promql) in M.CATALOGUE.items():
            found = invalid_escapes(promql)
            if found:
                bad[name] = found
        self.assertEqual(bad, {}, f"invalid escapes in the metric catalogue: {bad}")


if __name__ == "__main__":
    unittest.main()
