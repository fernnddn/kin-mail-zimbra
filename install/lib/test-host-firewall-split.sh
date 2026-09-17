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

# The trust between the two halves has to be symmetric and by ADDRESS.
#
# The mailbox names EDGE_IP. The edge named nothing: the only thing letting the
# mailbox reach it was CLUSTER_NET, which is "the /24 this host happens to be
# on". That is true in a lab where both VMs share a subnet, and false the
# moment anyone deploys the split the way a split is meant to be deployed -
# edge in a DMZ, mailbox on an internal VLAN. Outbound mail would then queue on
# the mailbox with nothing in any log on the edge to explain it, because the
# packets never arrived.
if grep -q 'ufw allow from "\$MAILBOX_IP"' "$SCRIPT"; then
  pass "the edge trusts the mailbox by address, not by subnet"
else
  bad "the edge never names MAILBOX_IP; cross-subnet splits lose outbound mail"
fi
if grep -q 'ufw allow from "\$EDGE_IP"' "$SCRIPT"; then
  pass "the mailbox trusts the edge by address, not by subnet"
else
  bad "the mailbox never names EDGE_IP"
fi
# ...and each refuses rather than falling back to the subnet when it does not
# know its peer. A silent fallback is the failure this pair of rules exists to
# prevent, arriving later and quieter.
mbip_line=$(grep -n 'MAILBOX_IP is not set' "$SCRIPT" | head -1 | cut -d: -f1)
edip_line=$(grep -n 'EDGE_IP is not set' "$SCRIPT" | head -1 | cut -d: -f1)
if [ -n "$mbip_line" ] && [ -n "$edip_line" ]; then
  pass "each half refuses to firewall itself without knowing its peer"
else
  bad "a missing peer address is tolerated (mailbox=$mbip_line edge=$edip_line)"
fi
# The edge rule must be gated on being the edge. Applied on a single appliance
# it would reference a variable that deployment has no reason to set.
#
# Ordered by line number rather than a -B window: a fixed window is a distance,
# and distances go stale the next time a comment is added between the gate and
# the rule it guards.
edge_gate=$(grep -n 'node_role" = "edge"' "$SCRIPT" | head -1 | cut -d: -f1)
mbip_rule=$(grep -n 'ufw allow from "\$MAILBOX_IP"' "$SCRIPT" | head -1 | cut -d: -f1)
if [ -n "$edge_gate" ] && [ -n "$mbip_rule" ] && [ -n "$mbip_line" ] \
   && [ "$edge_gate" -lt "$mbip_line" ] && [ "$mbip_line" -lt "$mbip_rule" ]; then
  pass "the mailbox trust rule only applies on an edge, and only once it knows the address"
else
  bad "MAILBOX_IP rule is not inside the edge gate (gate=$edge_gate guard=$mbip_line rule=$mbip_rule)"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All host-firewall split tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
