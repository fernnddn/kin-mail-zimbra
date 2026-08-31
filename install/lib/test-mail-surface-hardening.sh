#!/usr/bin/env bash
# Tests for stage 09's mail-surface decisions.
#
# Both of these gate a change that is wrong in opposite directions if it
# misfires: an over-eager HSTS locks the operator out of their own console, and
# a missed relay range turns the appliance into an open relay.
set -u

LIB="$(cd "$(dirname "$0")" && pwd)/mail-surface-hardening.sh"
[ -f "$LIB" ] || { echo "missing $LIB" >&2; exit 1; }
# shellcheck disable=SC1090
. "$LIB"

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; }

wide()   { relay_scope_too_wide "$1" && ok   "flags: $1" || bad "should flag: $1"; }
narrow() { relay_scope_too_wide "$1" && bad "should NOT flag: $1" || ok "allows: $1"; }

# --- ranges that let strangers relay ---------------------------------------
# Documentation and reserved ranges throughout: a test fixture must never carry
# a real deployment's addressing, and the width is the only thing under test.
wide "0.0.0.0/0"
wide "127.0.0.0/8 0.0.0.0/0"
wide "::/0"
wide "240.0.0.0/4"
wide "192.0.2.0/24 0.0.0.0/0 127.0.0.0/8"

# --- ranges a real deployment actually uses ---------------------------------
# /8 is where it stops being a LAN. 127.0.0.0/8 is the loopback Zimbra always
# lists, and flagging it would make the warning useless.
narrow "127.0.0.0/8"
narrow "127.0.0.0/8 192.0.2.0/24"
narrow "127.0.0.0/8 198.51.100.0/16"
narrow "203.0.113.0/12"
narrow "127.0.0.0/8 [::1]/128"
narrow "fd00::/64"

# --- input that is not a network at all -------------------------------------
narrow ""
narrow "   "
narrow "not-a-network"
narrow "192.0.2.5"
narrow "192.0.2.0/notanumber"
narrow "192.0.2.0/"

# --- the loopback-only default must never be flagged ------------------------
narrow "127.0.0.0/8 ::1/128"

# --- cert_is_trusted must fail closed when it cannot tell -------------------
# No listener on :443 here, so it must say "not trusted" and the caller skips
# HSTS. Failing open would be the lockout.
if cert_is_trusted 2>/dev/null; then
  bad "cert_is_trusted returned true with nothing listening on :443"
else
  ok "cert_is_trusted fails closed when it cannot read a certificate"
fi

# --- the stage must gate HSTS on that answer --------------------------------
STAGE="$(cd "$(dirname "$0")/.." && pwd)/09-hardening.sh"
if grep -q "if cert_is_trusted; then" "$STAGE" &&
   grep -A3 "if cert_is_trusted; then" "$STAGE" | grep -q "Strict-Transport-Security"; then
  ok "HSTS is only set when the certificate is trusted"
else
  bad "HSTS is not gated on cert_is_trusted"
fi

# Response headers are appended, never assigned: the attribute is multi-valued
# and Zimbra puts its own entries in it.
if grep -q 'zmprov mcf +zimbraResponseHeader' "$STAGE" &&
   ! grep -q 'zmprov mcf zimbraResponseHeader ' "$STAGE"; then
  ok "response headers are appended, not assigned over Zimbra's own"
else
  bad "response headers overwrite the multi-valued attribute"
fi

# Relay scope is reported, never rewritten: it is a deployment decision, and
# narrowing it silently would cut off a customer relaying for an app subnet.
# Anchor on the RUN path (zimbra_cmd ...), not on the string: the stage also
# prints the command as advice, and matching that made this test fail on its
# own help text.
if grep -qE '^[[:space:]]*zimbra_cmd zmprov mcf zimbraMtaMyNetworks' "$STAGE"; then
  bad "the stage rewrites zimbraMtaMyNetworks instead of reporting it"
else
  ok "relay scope is reported, not silently narrowed"
fi
if grep -q 'zmprov mcf zimbraMtaMyNetworks' "$STAGE"; then
  ok "the stage still shows the operator the command to run themselves"
else
  bad "no guidance printed for a too-wide relay scope"
fi

# Every zmproxy restart in the stage must be wrapped in unmanage/remanage.
# Pacemaker watches that proxy: an unguarded restart looks like kin-zimbra
# failing, which tears down the VIP or fences the node. A hardening step that
# takes mail down is worse than the weakness it closes.
# Count invocations, not mentions: the failure messages name the command too,
# and matching those made this test read 4 restarts where there are 2.
restarts=$(grep -cE 'zimbra_cmd zmproxyctl restart' "$STAGE")
unmanages=$(grep -c 'kin_zimbra_unmanage' "$STAGE")
if [ "$restarts" -gt 0 ] && [ "$unmanages" -ge "$restarts" ]; then
  ok "every zmproxy restart (${restarts}) is paired with an unmanage"
else
  bad "zmproxy restarts=${restarts} but unmanage calls=${unmanages}"
fi
if grep -q 'trap kin_zimbra_remanage EXIT' "$STAGE"; then
  ok "kin-zimbra is remanaged even if the stage exits early"
else
  bad "no EXIT trap remanaging kin-zimbra"
fi
# And it must not restart the proxy when nothing changed.
if grep -q 'headers_changed' "$STAGE"; then
  ok "the proxy is only restarted when a header actually changed"
else
  bad "the proxy is restarted unconditionally"
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
