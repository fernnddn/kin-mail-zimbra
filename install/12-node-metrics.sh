#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 12 NODE METRICS
#
# Makes this machine's CPU, memory, disk and network readable by the console's
# Monitoring tab.
#
# The console runs on the edge and the operator never logs into the mailbox, so
# the mailbox's own utilisation was invisible: the one machine holding every
# message was the one nobody could see. Prometheus lives on the edge and scrapes
# node_exporter; this installs node_exporter here and binds it where the edge
# can reach it.
#
# WHAT IT LISTENS ON, AND WHY THAT IS SAFE
#
# On a single appliance, and on the edge, node_exporter stays on 127.0.0.1 -
# Prometheus is on the same machine and nothing else needs it.
#
# On the MAILBOX it binds to that node's own LAN address, because Prometheus is
# on the other machine. That is a routable port, so the question is who can
# reach it: 10-host-firewall.sh gives the mailbox one rule - allow everything
# from the edge, nothing from anywhere else - so the edge can scrape it and the
# internet cannot see that the port exists. It binds to ONE address rather than
# 0.0.0.0 so that holds even in the window before ufw is applied.
#
# Metrics are machine utilisation only. No mail content, no addresses, no
# credentials: node_exporter reads /proc and /sys.
#
#   sudo ./12-node-metrics.sh
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

# shellcheck source=lib/apt-lock.sh
. ./lib/apt-lock.sh

PKG="prometheus-node-exporter"
SVC="prometheus-node-exporter"
DEFAULTS="/etc/default/prometheus-node-exporter"
PORT="${KIN_NODE_EXPORTER_PORT:-9100}"

echo
say "Node metrics for the console Monitoring tab"

# Which address should this listen on?
#
# Decided from the role, not from a flag, because the answer is a property of
# the deployment: the mailbox is the only node whose metrics have to cross a
# network to reach Prometheus.
BIND="127.0.0.1"
ROLE="all"
if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
  ROLE=$(kin_node_role) || {
    fail "TOPOLOGY=split but this host's role cannot be determined."
    info "Check EDGE_IP and MAILBOX_IP in ${CONF_FILE} against this machine's addresses."
    exit 1
  }
  if [ "$ROLE" = "mailbox" ]; then
    BIND=$(kin_mailbox_ip) || BIND=""
    if [ -z "$BIND" ]; then
      fail "MAILBOX_IP is not set, so there is no address to publish metrics on."
      exit 1
    fi
  fi
fi
info "Role: ${ROLE}; listening on ${BIND}:${PORT}"

say "1. Package"
if dpkg -s "$PKG" >/dev/null 2>&1; then
  ok "${PKG} already installed"
else
  export DEBIAN_FRONTEND=noninteractive
  kin_apt -qq update
  if ! kin_apt -y install "$PKG" >/dev/null; then
    fail "Could not install ${PKG}"
    exit 1
  fi
  ok "${PKG} installed"
fi

say "2. Listen address"
# Whole file, not an edit: the packaged default ships ARGS="" and an appended
# second ARGS= line would be silently overridden by the first one sourced.
TMP=$(mktemp)
cat >"$TMP" <<EOF
# Managed by KIN Mail (install/12-node-metrics.sh). Edits are overwritten.
#
# Bound to one address on purpose. On the mailbox of a multi deployment that
# address is reachable from the edge, which is where Prometheus runs, and the
# host firewall allows the edge and nothing else.
ARGS="--web.listen-address=${BIND}:${PORT}"
EOF
if [ -f "$DEFAULTS" ] && cmp -s "$TMP" "$DEFAULTS"; then
  rm -f "$TMP"
  ok "Listen address already ${BIND}:${PORT}"
else
  install -o root -g root -m 0644 "$TMP" "$DEFAULTS"
  rm -f "$TMP"
  ok "Wrote ${DEFAULTS}"
fi

say "3. Service"
systemctl enable "$SVC" >/dev/null 2>&1 || true
if ! systemctl restart "$SVC"; then
  fail "${SVC} did not start"
  info "Look at: journalctl -u ${SVC} -n 50"
  exit 1
fi

say "4. Verify"
# Poll rather than sleep: the unit returns before the socket is up, and a fixed
# sleep is either too short on a loaded mailbox or wasted time on an idle one.
UP=0
for _try in 1 2 3 4 5 6 7 8 9 10; do
  if timeout 3 bash -c "exec 3<>/dev/tcp/${BIND}/${PORT}" 2>/dev/null; then
    UP=1
    break
  fi
  sleep 1
done
if [ "$UP" -ne 1 ]; then
  fail "Nothing is listening on ${BIND}:${PORT}"
  info "Look at: journalctl -u ${SVC} -n 50"
  exit 1
fi
ok "Metrics answering on ${BIND}:${PORT}"

if [ "$ROLE" = "mailbox" ]; then
  info "The edge scrapes this address. Nothing else can reach it: the host"
  info "firewall on this node allows the edge and refuses everything else."
fi

echo
say "DONE"
exit 0
