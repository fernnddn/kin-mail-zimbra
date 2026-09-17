#!/usr/bin/env bash
# The mailbox firewall must still apply when KIN_ADMIN_IPS is empty.
#
# The wizard treats Admin IPs as optional. The orchestrator still has to
# firewall the mailbox over SSH. A guard that required admin IPs before it
# knew the role exited 2 on that machine and printed SPLIT BUILD INCOMPLETE
# over a store that was still on the internet.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

SCRIPT="../10-host-firewall.sh"
[ -f "$SCRIPT" ] || { echo "missing $SCRIPT" >&2; exit 1; }

role_line=$(grep -n 'node_role=$(kin_node_role)' "$SCRIPT" | head -1 | cut -d: -f1)
admin_line=$(grep -n 'KIN_ADMIN_IPS is required' "$SCRIPT" | head -1 | cut -d: -f1)
exempt_line=$(grep -n 'node_role" != "mailbox"' "$SCRIPT" | head -1 | cut -d: -f1)
keep_line=$(grep -n 'This mailbox will still be firewalled' "$SCRIPT" | head -1 | cut -d: -f1)

if [ -n "$role_line" ] && [ -n "$admin_line" ] && [ "$role_line" -lt "$admin_line" ]; then
  pass "role is decided before admin IPs are required"
else
  bad "ADMIN_IPS guard runs before the role is known (role=$role_line admin=$admin_line)"
fi
if [ -n "$exempt_line" ] && [ -n "$admin_line" ] && [ "$exempt_line" -le "$admin_line" ]; then
  pass "the mailbox is exempt from the admin-IP requirement"
else
  bad "mailbox is not exempt (exempt=$exempt_line admin=$admin_line)"
fi
if [ -n "$keep_line" ]; then
  pass "an empty admin list still firewalled the mailbox, and says so"
else
  bad "mailbox empty-admin path does not say the firewall still applies"
fi

# The exemption is the mailbox, not every split node. An edge without admin
# IPs would publish SSH and the console the moment ufw is enabled.
if grep -A2 'node_role" != "mailbox"' "$SCRIPT" | grep -q 'KIN_ADMIN_IPS is required'; then
  pass "edge and single-appliance still require admin IPs"
else
  bad "admin-IP requirement is no longer tied to non-mailbox nodes"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All host-firewall split tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
