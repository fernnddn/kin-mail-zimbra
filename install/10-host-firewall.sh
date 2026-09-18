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
# apt on a freshly booted host: wait out apt-daily / unattended-upgrades
# instead of failing on a lock that clears itself.
# shellcheck source=lib/apt-lock.sh
. ./lib/apt-lock.sh
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
    # Real addresses first: the wizard writes PEER_HOST_IP and
    # OBSERVABILITY_VM_IP into /etc/kin-mail/config, and SERVER_IP is this
    # host. The .13/.12/.14 values below are a last-resort guess from an old
    # lab layout, and on any other deployment they name machines that do not
    # exist - a live Phase 6 pair on .30/.31/.29 got per-peer corosync, DRBD
    # and pcsd rules pointing at .13/.12/.14. Nothing broke only because the
    # /24 LAN rules alongside them still matched; tighten those and the
    # cluster would have lost its ring on the next apply.
    PEER_A="${KIN_PEER_A_IP:-${SERVER_IP}}"
    PEER_B="${KIN_PEER_B_IP:-${PEER_HOST_IP:-}}"
    MON_IP="${KIN_MON_IP:-${OBSERVABILITY_VM_IP:-}}"
    # No guessing. An unknown peer stays empty and its per-host rule is simply
    # skipped: a single-node install has no peer and no witness, and inventing
    # an address there would put rules for a machine that does not exist into
    # the live ruleset. Real peers are still covered by the CLUSTER_NET rules
    # below either way.
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
  if [ -z "$CLUSTER_NET" ]; then
    fail "SERVER_IP unset - cannot derive cluster LAN"
    exit 2
  fi

  # Which half of a split is this, decided BEFORE any firewall change.
  #
  # A single appliance answers "all" and everything below behaves as it always
  # has. A split is two very different machines:
  #
  #   edge     runs the MTA and the proxy. It faces the internet exactly as a
  #            single appliance does, and it holds no mail.
  #   mailbox  runs the directory and the mail store. Nothing on the internet
  #            has any business reaching it. Opening 110/143/993/995 there is
  #            the precise exposure a split exists to remove, so the mailbox
  #            gets no public ports at all.
  #
  # The role comes from this host's own addresses, never from its hostname -
  # see lib/topology.sh. If it cannot be determined we refuse, because guessing
  # "edge" on a mailbox publishes every mailbox port to the internet and
  # guessing "mailbox" on an edge stops all mail.
  CONSOLE_PORT="${CONSOLE_PORT:-${KIN_CONSOLE_PORT:-9443}}"
  local node_role="all"
  if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
    node_role=$(kin_node_role) || {
      fail "TOPOLOGY=split, but this host's role cannot be determined."
      info "Check EDGE_IP and MAILBOX_IP in ${CONF_FILE} against this machine's addresses."
      info "Refusing rather than guessing: one wrong guess publishes every mailbox"
      info "port to the internet, the other stops all mail."
      exit 2
    }
    ok "Split deployment; this host is the ${node_role} node"
  fi

  # Admin IPs are required on a machine that opens public ports: without them
  # the console and SSH would be reachable from the internet the moment ufw
  # is enabled. The mailbox opens no public ports. Its security model is
  # "the edge, and nothing else from the internet". An empty admin list there
  # is therefore not a reason to refuse - refusing is how a split deploy
  # printed SPLIT BUILD INCOMPLETE over a store that was still on the
  # internet, because the wizard treats Admin IPs as optional and the
  # orchestrator still has to firewall that machine.
  #
  # Trade: without an admin IP, a workstation cannot SSH to the mailbox
  # except via the edge or the LAN. If the edge is down that is inconvenient.
  # Leaving the store on the internet is worse. Re-run with KIN_ADMIN_IPS to
  # add a workstation later.
  # The same reasoning reaches the same answer on a node that DOES open public
  # ports, and refusing there was worse, not safer.
  #
  # Refusing means no ufw at all: every port on the machine open to the
  # internet, including SSH, the console and Zimbra's admin port. Applying
  # without an admin list means those three are reachable from this host's own
  # subnet and from nowhere else, while the public ports are the mail ports and
  # nothing more. There is no deployment in which the first is the safer of the
  # two - and on a split it is the EDGE, the machine that faces the internet by
  # design, that was being left unfirewalled.
  #
  # The wizard also calls Admin IPs optional and promises they can be narrowed
  # "later from the console after mail is up". That promise was false while
  # leaving the field blank meant the firewall never ran. It is true now.
  #
  # Trade: on a node whose address is public, its /24 is public too, so admin
  # access is open to its neighbours rather than to the whole internet. That is
  # a real weakening against a properly filled admin list, and it is still an
  # enormous improvement on no firewall. The dead man covers the other
  # direction: an operator whose own address is outside that subnet gets ufw
  # switched back off in five minutes rather than being locked out.
  if [ "$node_role" != "mailbox" ] && [ -z "$ADMIN_IPS" ]; then
    warn "No admin IPs configured. This host will still be firewalled."
    info "SSH, the console and Zimbra admin are opened to ${CLUSTER_NET:-the local subnet} only."
    info "Public ports are the mail ports and nothing else."
    warn "Narrow this properly: set Admin IPs in the console, then re-run this stage."
  elif [ "$node_role" = "mailbox" ] && [ -z "$ADMIN_IPS" ]; then
    warn "No admin IPs configured. This mailbox will still be firewalled."
    info "SSH from a workstation is not opened. Reach it via the edge, or from the LAN."
    info "Re-run with KIN_ADMIN_IPS set when you want a workstation path."
  fi

  say "Installing ufw (if needed)"
  export DEBIAN_FRONTEND=noninteractive
  kin_apt -qq update
  kin_apt -y install ufw >/dev/null

  say "Resetting ufw to a clean deny-incoming policy"
  ufw --force reset >/dev/null
  ufw default deny incoming
  ufw default allow outgoing
  ufw default deny routed

  # Established
  ufw allow in on lo
  # SSH - admin IPs (may be empty on a mailbox) + cluster LAN (break-glass)
  local ip
  for ip in $ADMIN_IPS; do
    ufw allow from "$ip" to any port 22 proto tcp comment 'SSH admin'
  done
  ufw allow from "$CLUSTER_NET" to any port 22 proto tcp comment 'SSH cluster LAN'

  if [ "$node_role" = "mailbox" ]; then
    if [ -z "${EDGE_IP:-}" ]; then
      fail "EDGE_IP is not set, so the mailbox cannot be told who may reach it."
      info "Without it this node would be firewalled off from its own edge."
      exit 2
    fi
    # The edge is the only machine that speaks to this one, and it speaks a lot
    # of Zimbra: LDAP, LMTP, mailboxd, the proxy's IMAP and POP back ends,
    # milter, and more between releases. Enumerating that set is where a wrong
    # guess breaks mail in a way nobody traces back to a firewall, so the edge
    # is trusted wholesale and the internet is trusted with nothing.
    ufw allow from "$EDGE_IP" comment 'edge node of this split'
    ok "Everything from the edge (${EDGE_IP}); nothing from the internet"
    for ip in $ADMIN_IPS; do
      ufw allow from "$ip" to any port 22 proto tcp comment 'SSH admin'
      ufw allow from "$ip" to any port "$CONSOLE_PORT" proto tcp comment 'KIN console admin-IP'
      ufw allow from "$ip" to any port 7071 proto tcp comment 'Zimbra admin admin-IP'
    done
    warn "No public mail ports on this node. Users reach mail through the edge."
    info "The directory and the mail store are not reachable from the internet."
    info "Admin IPs: ${ADMIN_IPS}"
    start_deadman
    say "Enabling ufw NOW"
    ufw --force enable
    ufw_status
    ok "ufw enabled with dead-man (${DEADMAN_SEC}s). Verify SSH, then cancel-deadman."
    return 0
  fi

  # The edge half of the same trust the mailbox already extends to us.
  #
  # Without this the only thing letting the mailbox reach this machine is
  # CLUSTER_NET - "whatever /24 this host happens to be on". That holds in a
  # lab where both VMs share a subnet and stops holding the moment anyone
  # deploys the split the way a split is meant to be deployed: the edge in a
  # DMZ, the mailbox on an internal VLAN. Then the mailbox cannot reach port 25
  # here, every outbound message queues on the mailbox, and nothing in any log
  # on this machine says why - because the packets never arrived.
  #
  # Named addresses, not a subnet, for the same reason the mailbox names this
  # one: on a split "same /24" is no longer the same question as "is the peer".
  # Trusted wholesale and symmetrically - the mailbox already does this for the
  # edge, and enumerating Zimbra's inter-node port set is where a wrong guess
  # breaks mail in a way nobody traces back to a firewall.
  if [ "$node_role" = "edge" ]; then
    if [ -z "${MAILBOX_IP:-}" ]; then
      fail "MAILBOX_IP is not set, so the edge cannot be told which machine its mailbox is."
      info "Without it the mailbox can only reach this host by being on the same"
      info "subnet, and outbound mail stops the day it is not."
      exit 2
    fi
    ufw allow from "$MAILBOX_IP" comment 'mailbox node of this split'
    ok "Everything from the mailbox (${MAILBOX_IP}) - it relays outbound mail through here"
  fi

  # Public mail (internet)
  #
  # Port 25 is the exception. From 0.1.10 a Proxmox Mail Gateway sits in front
  # of this appliance and is the MX; inbound SMTP arrives from the gateway and
  # from nowhere else. Narrowing it is the whole point of putting a gateway
  # there: an attacker who breaks the SMTP surface lands on a filtering box
  # with no mailboxes on it, not on the machine holding every message.
  #
  # It narrows only when a gateway is actually configured AND applied. An
  # appliance that has not been linked yet keeps the open rule, because
  # closing port 25 on a server that is still its own MX stops all inbound
  # mail, and a firewall stage is not the place to discover that.
  local gw_host="" gw_enabled="" gw_phase="" gw_addr=""
  if [ -f /etc/kin-mail/mail-gateway.conf ]; then
    gw_host=$(sed -n 's/^[[:space:]]*GATEWAY_HOST=//p' /etc/kin-mail/mail-gateway.conf |
              tail -1 | tr -d "\"' \r")
    gw_enabled=$(sed -n 's/^[[:space:]]*GATEWAY_ENABLED=//p' /etc/kin-mail/mail-gateway.conf |
                 tail -1 | tr -d "\"' \r")
  fi
  if [ -f /var/lib/kin-mail-console/mail-gateway-state.json ]; then
    gw_phase=$(sed -n 's/.*"phase"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
               /var/lib/kin-mail-console/mail-gateway-state.json | head -1)
  fi

  # ufw takes an address here, not a name. A gateway recorded by hostname has
  # to be resolved now, and if it cannot be, the safe answer is to leave port
  # 25 open rather than to silently stop accepting mail.
  if [ -n "$gw_host" ]; then
    case "$gw_host" in
      *[!0-9.]*)
        gw_addr=$(getent ahostsv4 "$gw_host" 2>/dev/null | awk 'NR==1{print $1}')
        [ -n "$gw_addr" ] || warn "Could not resolve the gateway '${gw_host}' to an address."
        ;;
      *) gw_addr="$gw_host" ;;
    esac
  fi

  for p in 110 143 443 465 587 993 995; do
    ufw allow "$p"/tcp comment "mail public ${p}"
  done

  if [ -n "$gw_addr" ] && [ "$gw_enabled" = "1" ] && [ "$gw_phase" = "applied" ]; then
    ufw allow from "$gw_addr" to any port 25 proto tcp comment 'SMTP from mail gateway'
    ufw allow from "$CLUSTER_NET" to any port 25 proto tcp comment 'SMTP cluster LAN'
    ok "Port 25 accepts mail from the gateway (${gw_addr}) and the LAN only"
  else
    ufw allow 25/tcp comment 'mail public 25 (no gateway configured)'
    warn "Port 25 is open to the internet: no mail gateway is linked yet."
    info "Console > Mail Gateway, then re-run this stage to narrow it."
  fi

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
  # allow_from <addr> <port-spec> <proto> <comment>: silently skips an address
  # the config never supplied, so a single-node install does not get rules for
  # a peer it does not have.
  allow_from() {
    local addr="$1" ports="$2" proto="$3" note="$4"
    [ -n "$addr" ] || return 0
    ufw allow from "$addr" to any port "$ports" proto "$proto" comment "$note"
  }

  # Corosync kronosnet
  allow_from "$PEER_A" 5404:5405 udp 'corosync knet'
  allow_from "$PEER_B" 5404:5405 udp 'corosync knet'
  allow_from "$MON_IP" 5404:5405 udp 'corosync/mon'
  # Also allow whole LAN for corosync in case VIP/host moves - still not internet
  allow_from "$CLUSTER_NET" 5404:5405 udp 'corosync LAN'

  # DRBD replication (live config uses 7788)
  allow_from "$PEER_A" 7788 tcp 'DRBD'
  allow_from "$PEER_B" 7788 tcp 'DRBD'
  allow_from "$CLUSTER_NET" 7788 tcp 'DRBD LAN'

  # pcsd
  allow_from "$PEER_A" 2224 tcp 'pcsd'
  allow_from "$PEER_B" 2224 tcp 'pcsd'
  allow_from "$CLUSTER_NET" 2224 tcp 'pcsd LAN'

  # qnetd/iSCSI are outbound to mon (default allow outgoing). If this host ever
  # needs inbound from mon for diagnostics, mon is on CLUSTER_NET already.

  info "Admin IPs: ${ADMIN_IPS}"
  info "Cluster net: ${CLUSTER_NET} peers ${PEER_A}/${PEER_B:-(none)} mon ${MON_IP:-(none)}"

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
