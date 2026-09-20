#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 13 LOG RELAY (the logger host's syslog receiver)
#
# Makes Zimbra Administration -> Monitor -> Server Status tell the truth about
# every node, instead of drawing the edge's services as down while mail flows.
#
# HOW THAT PAGE GETS ITS ANSWER
#
# Each node runs zmstatuslog from cron every two minutes. It does not talk to
# the directory or to the admin console: it writes one syslog line per service
# to local1, and the ONLY way those lines reach the machine that answers the
# page is syslog forwarding. zimbraLogHostname names the receiver, zmsyslogsetup
# writes the forwarding rules on each sender, zmlogprocess on the receiver reads
# what arrived, and GetServiceStatusRequest serves it.
#
# WHY THIS STAGE HAS TO EXIST
#
# zmsyslogsetup's Ubuntu path (updateRsyslogd) writes the SENDER side and
# nothing else. There is no branch in it that makes a logger host listen - the
# syslog-ng path defines a source, the rsyslog path never does. So on Ubuntu a
# multi-server Zimbra forwards its status to a receiver that was never switched
# on, and stock /etc/rsyslog.conf ships imudp commented out.
#
# The failure is silent and it reads as an outage. The edge's services are
# Running, its ports are listening, mail is being delivered, and the admin
# console shows every one of them with a red X - because the last status it
# heard from that host is from never. An operator seeing that page has no
# reason to suspect syslog (20 Sep 2026: they reported it as services being
# down, which was the opposite of true).
#
# WHAT IT LISTENS ON, AND WHY THAT IS SAFE
#
# UDP 514 on the mailbox's own LAN address, not 0.0.0.0, for the same reason
# 12-node-metrics.sh binds one address: the only sender is the edge, and
# binding narrowly holds even in the window before ufw is applied.
# 10-host-firewall.sh then gives this node one allow rule - everything from the
# edge, nothing from anywhere else - which covers this port without naming it.
#
# Nothing here parses or stores mail. Status lines are service names and the
# word Running.
#
#   sudo ./13-log-relay.sh
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

CONF="/etc/rsyslog.d/55-kin-log-relay.conf"
PORT="${KIN_LOG_RELAY_PORT:-514}"

echo
say "Log relay for Zimbra's Server Status page"

# Single-server appliances have one node, and its status lines never leave the
# machine. Opening a network receiver there would be a port with no sender.
if ! { declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; }; then
  ok "Single deployment: status is written and read on this machine. Nothing to do."
  exit 0
fi

ROLE=$(kin_node_role) || {
  fail "TOPOLOGY=split but this host's role cannot be determined."
  info "Check EDGE_IP and MAILBOX_IP in ${CONF_FILE} against this machine's addresses."
  exit 1
}

# The receiver belongs on the logger, and on this product's split the logger is
# the mailbox - zimbra-logger is in the mailbox package set and nowhere else.
# The edge is a sender; zmsyslogsetup has already given it its forwarding rules.
if [ "$ROLE" != "mailbox" ]; then
  ok "Role ${ROLE}: this node sends its status to the mailbox. Nothing to receive."
  exit 0
fi

BIND=$(kin_mailbox_ip) || BIND=""
if [ -z "$BIND" ]; then
  fail "MAILBOX_IP is not set, so there is no address to receive status on."
  info "Server Status would keep showing the edge as down."
  exit 1
fi
EDGE=$(kin_edge_ip) || EDGE=""

say "1. Receiver"
TMP=$(mktemp)
cat >"$TMP" <<EOF
# Managed by KIN Mail (install/13-log-relay.sh). Edits are overwritten.
#
# The edge forwards its Zimbra status here over UDP 514 and nothing else does.
# Bound to one address on purpose: the host firewall on this node allows the
# edge and refuses everything else, and a narrow bind holds even before it is
# applied.
module(load="imudp")
input(type="imudp" port="${PORT}" address="${BIND}")
EOF
if [ -f "$CONF" ] && cmp -s "$TMP" "$CONF"; then
  rm -f "$TMP"
  ok "Already receiving on ${BIND}:${PORT}"
else
  # Checked, not announced. Verify below would catch a failure here anyway, but
  # it would report it as "nothing is listening" - which sends whoever reads it
  # looking at rsyslog rather than at the write that never happened.
  if ! install -o root -g root -m 0644 "$TMP" "$CONF"; then
    rm -f "$TMP"
    fail "Could not write ${CONF}"
    info "Server Status will keep showing the edge's services as down."
    exit 1
  fi
  rm -f "$TMP"
  ok "Wrote ${CONF}"
fi

say "2. Service"
if ! systemctl restart rsyslog; then
  fail "rsyslog did not restart"
  info "Look at: journalctl -u rsyslog -n 50"
  exit 1
fi

say "3. Verify"
# Checked, not announced. A config file on disk is not a socket, and a receiver
# that failed to bind looks exactly like one that was never installed - which is
# the failure this whole stage exists to end.
#
# There is no /dev/udp equivalent of the TCP connect 12-node-metrics.sh uses -
# a UDP send succeeds whether or not anything is listening - so this asks the
# kernel for the socket instead. If ss is somehow absent, say that rather than
# failing a stage that probably worked: an unverifiable result is not a failed
# one, and aborting here would leave the mailbox unhardened for a missing tool.
if ! command -v ss >/dev/null 2>&1; then
  warn "ss is not available, so the receiver could not be verified."
  info "Check by hand: ss -lnu | grep ${PORT}"
  echo
  say "DONE"
  exit 0
fi
UP=0
for _try in 1 2 3 4 5; do
  if ss -lnu 2>/dev/null | grep -q "${BIND}:${PORT}"; then
    UP=1
    break
  fi
  sleep 1
done
if [ "$UP" -ne 1 ]; then
  fail "Nothing is listening on UDP ${BIND}:${PORT}"
  info "Server Status will keep drawing the edge's services as down."
  info "Look at: journalctl -u rsyslog -n 50"
  exit 1
fi
ok "Receiving status on UDP ${BIND}:${PORT}"

if [ -n "$EDGE" ]; then
  info "The edge (${EDGE}) forwards its status here every two minutes."
  info "Zimbra Administration -> Monitor -> Server Status picks it up within ten."
fi

echo
say "DONE"
exit 0
