#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 10 HOST FIREWALL (ufw) - high risk; dead-man's switch required
#
# Applies ONLY after SSH key-only lockdown (09 / 7.2 Stage 1).
#
#   sudo KIN_ADMIN_IPS='x.x.x.x' ./10-host-firewall.sh apply
#   sudo ./10-host-firewall.sh status
#   sudo ./10-host-firewall.sh disable
#
# Dead man's switch: on apply, schedules `ufw disable` in 300s unless cancelled
# after verification (KIN_UFW_DEADMAN_SEC override).
#
# Does NOT change SSH port. Does NOT open 7071 to the world.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

DEADMAN_SEC="${KIN_UFW_DEADMAN_SEC:-300}"
# Prefer env override, else value from /etc/kin-mail/config (via 00-config.sh).
ADMIN_IPS="${KIN_ADMIN_IPS:-}"
CLUSTER_NET=""
PEER_A=""
PEER_B=""
MON_IP="${KIN_MON_IP:-}"

# Derive LAN /24 and default peer/mon guesses from SERVER_IP without hardcoding lab CIDR in git.
case "${SERVER_IP:-}" in
  [0-9]*.[0-9]*.[0-9]*.[0-9]*)
    IFS=. read -r _a _b _c _d <<EOF
${SERVER_IP}
EOF
    CLUSTER_NET="${_a}.${_b}.${_c}.0/24"
    # Conventional KIN Mail lab layout relative to this host's /24 - override via env if different.
    PEER_A="${KIN_PEER_A_IP:-${_a}.${_b}.${_c}.13}"
    PEER_B="${KIN_PEER_B_IP:-${_a}.${_b}.${_c}.12}"
    MON_IP="${KIN_MON_IP:-${_a}.${_b}.${_c}.14}"
    ;;
esac

usage() {
  cat <<EOF
Usage: sudo KIN_ADMIN_IPS='203.0.113.10' $0 apply|status|disable|cancel-deadman
EOF
}

ufw_status() {
  echo
  say "ufw status"
  ufw status verbose | sed 's/^/    /' || true
}

cancel_deadman() {
  # Prefer cancel flag (survives failed signals) + hard-kill + verify process gone.
  touch /run/kin-ufw-deadman.cancel
  local pid=""
  if [ -f /run/kin-ufw-deadman.pid ]; then
    pid=$(tr -d ' \n' < /run/kin-ufw-deadman.pid 2>/dev/null || true)
  fi
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    # Kill whole process group if started with setsid; also try direct PID.
    kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    local i
    for i in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
      sleep 1
    fi
    if kill -0 "$pid" 2>/dev/null; then
      fail "Dead-man PID ${pid} still alive after SIGKILL - NOT safe to assume ufw stays on"
      exit 1
    fi
    ok "Cancelled dead-man PID ${pid} (process confirmed dead)"
  else
    info "No live dead-man process (cancel flag set)"
  fi
  rm -f /run/kin-ufw-deadman.pid
  # Keep cancel flag so a racing sleep loop still sees it before disable.
}

start_deadman() {
  cancel_deadman
  rm -f /run/kin-ufw-deadman.cancel
  : >/var/log/kin-ufw-deadman.log
  # Flag-polled loop (not one long sleep): cancel file stops disable even if kill races.
  # setsid → dedicated process group for reliable kill.
  setsid bash -c "
    marker=/run/kin-ufw-deadman.cancel
    log=/var/log/kin-ufw-deadman.log
    sec=${DEADMAN_SEC}
    i=0
    while [ \"\$i\" -lt \"\$sec\" ]; do
      if [ -f \"\$marker\" ]; then
        echo \"dead-man cancelled at \${i}s/\${sec}s (\$(date -Is))\" | tee -a \"\$log\"
        logger -t kin-ufw \"dead-man cancelled at \${i}s\"
        exit 0
      fi
      sleep 1
      i=\$((i + 1))
    done
    if [ -f \"\$marker\" ]; then
      echo \"dead-man cancelled at deadline (\$(date -Is))\" | tee -a \"\$log\"
      logger -t kin-ufw 'dead-man cancelled at deadline'
      exit 0
    fi
    echo \"dead-man disabling ufw after \${sec}s (\$(date -Is))\" | tee -a \"\$log\"
    logger -t kin-ufw \"dead-man disabled ufw after \${sec}s\"
    ufw --force disable
  " >>/var/log/kin-ufw-deadman.log 2>&1 &
  echo $! > /run/kin-ufw-deadman.pid
  ok "Dead-man armed: ufw will auto-disable in ${DEADMAN_SEC}s (pid $(cat /run/kin-ufw-deadman.pid))"
  warn "After verification: $0 cancel-deadman   (keeps ufw enabled)"
}

apply_rules() {
  if [ -z "$ADMIN_IPS" ]; then
    fail "KIN_ADMIN_IPS is required (set in /etc/kin-mail/config via 00-config wizard, or export KIN_ADMIN_IPS=…)"
    exit 2
  fi
  if [ -z "$CLUSTER_NET" ]; then
    fail "SERVER_IP unset - cannot derive cluster LAN"
    exit 2
  fi

  say "Installing ufw (if needed)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get -qq update
  apt-get -y install ufw >/dev/null

  say "Resetting ufw to a clean deny-incoming policy"
  ufw --force reset >/dev/null
  ufw default deny incoming
  ufw default allow outgoing
  ufw default deny routed

  # Established
  ufw allow in on lo
  # SSH - admin IPs + cluster LAN (break-glass from peers)
  local ip
  for ip in $ADMIN_IPS; do
    ufw allow from "$ip" to any port 22 proto tcp comment 'SSH admin'
  done
  ufw allow from "$CLUSTER_NET" to any port 22 proto tcp comment 'SSH cluster LAN'

  # Public mail (internet)
  for p in 25 110 143 443 465 587 993 995; do
    ufw allow "$p"/tcp comment "mail public ${p}"
  done

  # Admin console - NOT any (Zimbra :7071 + KIN console :CONSOLE_PORT)
  CONSOLE_PORT="${CONSOLE_PORT:-${KIN_CONSOLE_PORT:-9443}}"
  for ip in $ADMIN_IPS; do
    ufw allow from "$ip" to any port 7071 proto tcp comment 'Zimbra admin admin-IP'
  done
  ufw allow from "$CLUSTER_NET" to any port 7071 proto tcp comment 'Zimbra admin LAN'
  for ip in $ADMIN_IPS; do
    ufw allow from "$ip" to any port "$CONSOLE_PORT" proto tcp comment 'KIN console admin-IP'
  done
  ufw allow from "$CLUSTER_NET" to any port "$CONSOLE_PORT" proto tcp comment 'KIN console LAN'

  # Cluster / HA - peers + mon only (never any)
  # Corosync kronosnet
  ufw allow from "$PEER_A" to any port 5404:5405 proto udp comment 'corosync knet'
  ufw allow from "$PEER_B" to any port 5404:5405 proto udp comment 'corosync knet'
  ufw allow from "$MON_IP" to any port 5404:5405 proto udp comment 'corosync/mon'
  # Also allow whole LAN for corosync in case VIP/host moves - still not internet
  ufw allow from "$CLUSTER_NET" to any port 5404:5405 proto udp comment 'corosync LAN'

  # DRBD replication (live config uses 7788)
  ufw allow from "$PEER_A" to any port 7788 proto tcp comment 'DRBD'
  ufw allow from "$PEER_B" to any port 7788 proto tcp comment 'DRBD'
  ufw allow from "$CLUSTER_NET" to any port 7788 proto tcp comment 'DRBD LAN'

  # pcsd
  ufw allow from "$PEER_A" to any port 2224 proto tcp comment 'pcsd'
  ufw allow from "$PEER_B" to any port 2224 proto tcp comment 'pcsd'
  ufw allow from "$CLUSTER_NET" to any port 2224 proto tcp comment 'pcsd LAN'

  # qnetd/iSCSI are outbound to mon (default allow outgoing). If this host ever
  # needs inbound from mon for diagnostics, mon is on CLUSTER_NET already.

  info "Admin IPs: ${ADMIN_IPS}"
  info "Cluster net: ${CLUSTER_NET} peers ${PEER_A}/${PEER_B} mon ${MON_IP}"

  start_deadman
  say "Enabling ufw NOW"
  ufw --force enable
  ufw_status
  ok "ufw enabled with dead-man (${DEADMAN_SEC}s). Verify SSH + cluster, then cancel-deadman."
}

case "${1:-}" in
  apply) apply_rules ;;
  status) ufw_status ;;
  disable)
    cancel_deadman
    ufw --force disable
    ok "ufw disabled"
    ;;
  cancel-deadman) cancel_deadman ;;
  *) usage; exit 2 ;;
esac
