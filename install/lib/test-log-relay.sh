#!/usr/bin/env bash
# What install/13-log-relay.sh has to get right, and what 10-host-firewall.sh
# has to stop getting wrong about the admin console.
#
# Both are about the same class of fault: a deployment that finishes cleanly
# and leaves an operator looking at a screen that lies. The edge's services
# drawn as down while mail flows, and an admin console that refuses every
# address the operator owns. Neither surfaces an error anywhere.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/../13-log-relay.sh"
FW="$(pwd)/../10-host-firewall.sh"
SPLIT="$(pwd)/../kin-mail-split.sh"
for f in "$S" "$FW" "$SPLIT"; do
  [ -f "$f" ] || { printf 'FAIL %s not found\n' "$f"; exit 1; }
done

# Comments in these files describe the faults under test, so a naive grep for
# a symptom matches the explanation of it. Strip them first.
no_comments() { grep -vE '^[[:space:]]*#' "$1"; }

bash -n "$S" && pass "13-log-relay parses" || bad "13-log-relay syntax error"
[ -x "$S" ] && pass "13-log-relay is executable" || bad "13-log-relay is not executable"

# --- the receiver Zimbra never switches on ------------------------------------
# zmsyslogsetup's Ubuntu path writes the sender side only: it has no branch
# that makes a logger host listen. Stock rsyslog.conf ships imudp commented
# out, so the forwarded status arrives nowhere.
if no_comments "$S" | grep -q 'module(load="imudp")'; then
  pass "loads imudp, which nothing in Zimbra does on Ubuntu"
else
  bad "never loads imudp; the logger has no receiver and Server Status stays wrong"
fi
if no_comments "$S" | grep -q 'input(type="imudp"'; then
  pass "opens an imudp input"
else
  bad "loads the module without opening an input"
fi

# --- bound narrowly, like every other cross-node port this product opens ------
if no_comments "$S" | grep -q 'address="${BIND}"'; then
  pass "binds one address"
else
  bad "does not bind a specific address"
fi
if no_comments "$S" | grep -q '0\.0\.0\.0'; then
  bad "binds every interface"
else
  pass "never binds every interface"
fi

# --- only where it makes sense ------------------------------------------------
# A single appliance has one node and its status never leaves the machine; the
# edge is a sender. A receiver on either is a port with no sender.
if grep -q 'kin_topology_is_split' "$S"; then
  pass "does nothing on a single deployment"
else
  bad "opens a receiver on a single deployment, where nothing sends"
fi
if grep -q 'ROLE" != "mailbox"' "$S"; then
  pass "does nothing on the edge, which is a sender"
else
  bad "does not distinguish the logger host from a sender"
fi

# --- checked, not announced ---------------------------------------------------
# A config file on disk is not a socket. A receiver that failed to bind looks
# exactly like one that was never installed, which is the failure this stage
# exists to end - and claiming success without checking is a pattern this
# repository has already had to fix more than once.
if grep -q 'ss -lnu' "$S" && grep -q 'Nothing is listening' "$S"; then
  pass "verifies the socket exists before claiming success"
else
  bad "reports success without checking that anything is listening"
fi
if grep -q 'systemctl restart rsyslog' "$S"; then
  pass "restarts rsyslog, without which the file changes nothing"
else
  bad "writes a config and never reloads the daemon"
fi
if grep -q 'cmp -s "\$TMP" "\$CONF"' "$S"; then
  pass "leaves an unchanged config alone instead of rewriting it"
else
  bad "rewrites the config on every run"
fi
# The write is the step the verify below would misattribute: a config that was
# never written reports as "nothing is listening", which sends whoever reads it
# to rsyslog rather than to the write that failed.
if grep -q 'if ! install .* "\$CONF"' "$S"; then
  pass "checks the write instead of announcing it"
else
  bad "claims it wrote the config without checking"
fi

# --- it has to actually run ---------------------------------------------------
# The mailbox is only ever reached over SSH from the orchestrator; a stage that
# is not called there is a stage that never runs on that machine.
if grep -q '13-log-relay.sh' "$SPLIT"; then
  pass "the split orchestrator runs it on the mailbox"
else
  bad "nothing ever runs it on the mailbox"
fi
# A reporting gap is not an outage, and must not fail a build that delivers
# mail - the same call already made for node metrics.
if grep -A2 'mailbox-log-relay' "$SPLIT" | grep -q 'warn '; then
  pass "a failure warns rather than failing the build"
else
  bad "a reporting gap can abort a build that delivers mail"
fi

# --- the admin console after a deploy that filled in nothing ------------------
# Trusted console IPs is optional in the wizard and therefore usually empty.
# While the only rules opening 7071 came from that list, a clean deploy left
# the one console an operator needs on day one reachable from the edge and
# nowhere else.
mailbox_rules=$(sed -n '/node_role" = "mailbox"/,/^  fi$/p' "$FW")
if printf '%s' "$mailbox_rules" | grep -q 'port 7071 proto tcp comment .Zimbra admin private net'; then
  pass "the mailbox opens 7071 without waiting for an admin list"
else
  bad "7071 on the mailbox still depends on a field the wizard calls optional"
fi
for net in '10\.0\.0\.0/8' '172\.16\.0\.0/12' '192\.168\.0\.0/16'; do
  if printf '%s' "$mailbox_rules" | grep -q "$net"; then
    pass "covers ${net}"
  else
    bad "does not cover ${net}, so an operator on it cannot reach the console"
  fi
done
# Private SOURCES, never a destination of any. An internet host cannot hold one
# of these addresses, so this opens the operator's own networks and nothing else.
if printf '%s' "$mailbox_rules" | grep -q 'ufw allow .*to any port 7071 proto tcp' \
  && ! printf '%s' "$mailbox_rules" | grep -q 'ufw allow 7071'; then
  pass "7071 is opened by source, never to the world"
else
  bad "7071 is opened without a source restriction"
fi
# The admin list must still narrow it: its rules are what an operator tightens
# down to after dropping the private-network one.
if printf '%s' "$mailbox_rules" | grep -q "comment 'Zimbra admin admin-IP'"; then
  pass "an admin list still gets its own rules"
else
  bad "the admin list no longer has any effect on 7071"
fi

bash -n "$FW" && pass "10-host-firewall still parses" || bad "10-host-firewall syntax error"

if [ "$fails" -eq 0 ]; then
  printf 'All log-relay tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
