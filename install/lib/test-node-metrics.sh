#!/usr/bin/env bash
# What install/12-node-metrics.sh has to get right on a machine nobody logs into.
#
# The console runs on the edge. Everything it can ever know about the mailbox
# arrives through this stage, so a thing this stage forgets is a thing that is
# simply absent from the page with no error anywhere.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/../12-node-metrics.sh"
[ -f "$S" ] || { printf 'FAIL 12-node-metrics.sh not found\n'; exit 1; }

bash -n "$S" && pass "parses" || bad "syntax error"

# --- the textfile collector ---------------------------------------------------
# This stage writes /etc/default/prometheus-node-exporter WHOLE, so anything it
# leaves out is switched off. It left out the textfile directory, and the
# mailbox ran a node_exporter that could publish nothing but built-in counters:
# the facts written for it were scraped by nobody and the Monitoring tab showed
# that machine with a blank processor.
if grep -q -- '--collector.textfile.directory=' "$S"; then
  pass "keeps the textfile collector on when it rewrites ARGS"
else
  bad "rewrites ARGS without the textfile collector, silencing every published fact"
fi
if grep -q 'install -d .* "\$TEXTFILE_DIR"' "$S"; then
  pass "creates the textfile directory"
else
  bad "enables a collector pointed at a directory it never creates"
fi

# --- the facts it publishes ---------------------------------------------------
# node_exporter 1.3 is what jammy ships and it has no collector for the CPU
# model; /proc belongs to the machine it is on, so a console reading another
# machine over Prometheus cannot learn what its processor is called.
for metric in kin_node_cpu_info kin_node_cpu_threads kin_node_cpu_cores; do
  if grep -q "$metric" "$S"; then
    pass "publishes ${metric}"
  else
    bad "does not publish ${metric}"
  fi
done
if grep -q 'mv -f "\$tmp" "\$OUT"' "$S"; then
  pass "renames into place, so a scrape never reads a half-written file"
else
  bad "writes the .prom file in place; a scrape can catch it half written"
fi
if grep -q 's/"/\\\\"/g' "$S"; then
  pass "escapes the label value"
else
  bad "a quote in a CPU model would corrupt every metric in the file"
fi

# --- the failures it must not paper over --------------------------------------
# /usr/local/libexec exists on the edge because the monitoring role makes it.
# On a mailbox nothing has, install(1) does not create parents, and the line
# after it said "Wrote" regardless - so the real error surfaced three lines
# later as a warning about something else (live mailbox, 20 Sep 2026).
if grep -q 'install -d .*dirname "\$FACTS_SCRIPT"' "$S"; then
  pass "creates the parent directory before installing into it"
else
  bad "installs into a directory it does not create"
fi
if grep -q 'if \[ ! -x "\$FACTS_SCRIPT" \]' "$S"; then
  pass "checks the script is there before claiming it wrote one"
else
  bad "announces success without checking"
fi

# --- the listen address -------------------------------------------------------
# One address, not 0.0.0.0: the mailbox's port has to be reachable from the edge
# and from nowhere else, and that has to hold before ufw is applied.
if grep -q 'BIND="127.0.0.1"' "$S" && grep -q 'kin_mailbox_ip' "$S"; then
  pass "binds loopback by default and the mailbox's own address on a split"
else
  bad "does not decide the listen address from the node's role"
fi
# Code only: the comment above the bind explains why 0.0.0.0 is NOT used, and
# matching that would fail the file for documenting the decision under test.
if grep -vE '^[[:space:]]*#' "$S" | grep -q '0\.0\.0\.0'; then
  bad "binds every interface"
else
  pass "never binds every interface"
fi

# --- refreshed when it can change ---------------------------------------------
if grep -q 'kin-node-facts.service' "$S" && grep -q 'WantedBy=multi-user.target' "$S"; then
  pass "re-publishes at boot, which is when these values can change"
else
  bad "publishes once and never again"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All node-metrics tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
