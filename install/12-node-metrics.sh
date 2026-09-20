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
# Same directory the monitoring_stack role uses on the edge, so both
# machines publish through one mechanism.
TEXTFILE_DIR="${KIN_TEXTFILE_DIR:-/var/lib/node_exporter/textfile}"
FACTS_SCRIPT="/usr/local/libexec/kin-node-facts.sh"

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

say "2. Listen address and textfile collector"
# Whole file, not an edit: the packaged default ships ARGS="" and an appended
# second ARGS= line would be silently overridden by the first one sourced.
#
# The textfile directory goes in the SAME line, because writing this file at
# all replaces whatever was there. Leaving it out is how the mailbox ended up
# with a node_exporter that published nothing but machine counters: the
# collector was never enabled there, so anything written for it was scraped by
# nobody, and the console showed a blank processor for that machine.
install -d -o root -g root -m 0755 "$TEXTFILE_DIR"
TMP=$(mktemp)
cat >"$TMP" <<EOF
# Managed by KIN Mail (install/12-node-metrics.sh). Edits are overwritten.
#
# Bound to one address on purpose. On the mailbox of a multi deployment that
# address is reachable from the edge, which is where Prometheus runs, and the
# host firewall allows the edge and nothing else.
ARGS="--web.listen-address=${BIND}:${PORT} --collector.textfile.directory=${TEXTFILE_DIR}"
EOF
if [ -f "$DEFAULTS" ] && cmp -s "$TMP" "$DEFAULTS"; then
  rm -f "$TMP"
  ok "Listen address already ${BIND}:${PORT}, textfile collector on"
else
  install -o root -g root -m 0644 "$TMP" "$DEFAULTS"
  rm -f "$TMP"
  ok "Wrote ${DEFAULTS}"
fi

say "3. Machine facts a scraper cannot see"
# What a CPU is called is in /proc/cpuinfo and nowhere in node_exporter.
#
# node_cpu_info would carry it, but jammy ships node_exporter 1.3 and that
# collector arrived in 1.4 behind a flag. /proc belongs to the machine it is
# on, so the console reading it over Prometheus has no way to learn the model
# of any machine but its own - and a Mailbox view with four cores and a blank
# processor is what an operator reported (20 Sep 2026).
#
# The textfile collector is the supported way to publish something node_exporter
# does not collect, and this appliance already uses it for mail flow and the
# gateway. One more file, written by a unit that runs at boot, because the only
# way these values change is a reconfigure and a restart.
# /usr/local/libexec exists on the edge because the monitoring role creates it
# for its own collectors. On a mailbox nothing has, and install(1) does not make
# parent directories - it failed there while the line below still said "Wrote".
install -d -o root -g root -m 0755 "$(dirname "$FACTS_SCRIPT")"
install -o root -g root -m 0755 /dev/stdin "$FACTS_SCRIPT" <<'FACTS'
#!/usr/bin/env bash
# Publish what /proc knows and node_exporter does not. Written by KIN Mail.
set -u
DIR="${KIN_TEXTFILE_DIR:-/var/lib/node_exporter/textfile}"
OUT="${DIR}/kin_node_facts.prom"
[ -d "$DIR" ] || exit 0

model=$(sed -n 's/^model name[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo | head -1)
[ -n "$model" ] || model=$(sed -n 's/^Model[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo | head -1)
threads=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || printf '0')
cores=$(sed -n 's/^cpu cores[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo | head -1)
[ -n "$cores" ] || cores="$threads"

# A label value is escaped for backslash, double quote and newline. A CPU model
# has none of those today; a future one with a quote in it would corrupt the
# file for every metric in it, not just this one.
esc=$(printf '%s' "$model" | sed 's/\\/\\\\/g; s/"/\\"/g')

tmp=$(mktemp "${OUT}.XXXXXX") || exit 1
{
  printf '# HELP kin_node_cpu_info Processor of this machine, carried as a label.\n'
  printf '# TYPE kin_node_cpu_info gauge\n'
  printf 'kin_node_cpu_info{model="%s"} 1\n' "$esc"
  printf '# HELP kin_node_cpu_threads Logical processors.\n'
  printf '# TYPE kin_node_cpu_threads gauge\n'
  printf 'kin_node_cpu_threads %s\n' "$threads"
  printf '# HELP kin_node_cpu_cores Physical cores.\n'
  printf '# TYPE kin_node_cpu_cores gauge\n'
  printf 'kin_node_cpu_cores %s\n' "$cores"
} >"$tmp"
# Rename, never write in place: node_exporter reads this directory on every
# scrape and a half-written file is a parse error for the whole scrape.
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT"
FACTS
# Checked, not announced. Saying "Wrote" whatever happened is how a missing
# parent directory became a warning three lines later about something else.
if [ ! -x "$FACTS_SCRIPT" ]; then
  fail "Could not install ${FACTS_SCRIPT}"
  info "The Monitoring tab will show a blank processor for this node."
  exit 1
fi
ok "Wrote ${FACTS_SCRIPT}"

cat >/etc/systemd/system/kin-node-facts.service <<EOF
[Unit]
Description=KIN Mail node facts for the console Monitoring tab
After=local-fs.target

[Service]
Type=oneshot
ExecStart=${FACTS_SCRIPT}
Environment=KIN_TEXTFILE_DIR=${TEXTFILE_DIR}

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/kin-node-facts.service
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable kin-node-facts.service >/dev/null 2>&1 || true
if KIN_TEXTFILE_DIR="$TEXTFILE_DIR" "$FACTS_SCRIPT"; then
  ok "Published: $(sed -n 's/^kin_node_cpu_info{model="\(.*\)"} 1$/\1/p' "${TEXTFILE_DIR}/kin_node_facts.prom" | head -1)"
else
  warn "Could not publish machine facts; the Monitoring tab will show a blank processor for this node."
fi

say "4. Service"
systemctl enable "$SVC" >/dev/null 2>&1 || true
if ! systemctl restart "$SVC"; then
  fail "${SVC} did not start"
  info "Look at: journalctl -u ${SVC} -n 50"
  exit 1
fi

say "5. Verify"
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
