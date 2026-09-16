#!/usr/bin/env bash
# KIN Mail - which deployment is this, and which machine am I in it?
#
# Every stage before 0.1.11 could assume the answer: one host, every role, and
# SERVER_IP was both "me" and "the mail server". The split topology breaks that
# apart, and the cost of a stage guessing wrong is not cosmetic - a mailbox node
# that believes it is an edge opens 110/143/993/995 to the internet, which is
# the exact exposure the split exists to remove.
#
# So the answer is computed in one place, from addresses rather than names, and
# it refuses rather than guesses. Sourced by the numbered stages; no side
# effects at source time, so it can be tested without running any of them.

# kin_valid_ipv4 <string>
#
# Four dotted decimal octets, each 0-255, nothing trailing.
kin_valid_ipv4() {
  local o1 o2 o3 o4 rest o
  IFS=. read -r o1 o2 o3 o4 rest <<EOF
${1:-}
EOF
  [ -n "${o4:-}" ] && [ -z "${rest:-}" ] || return 1
  for o in "$o1" "$o2" "$o3" "$o4"; do
    case "$o" in '' | *[!0-9]*) return 1 ;; esac
    [ "$o" -le 255 ] || return 1
  done
  return 0
}

# kin_topology
#
# Prints 1vm | 2vm | split. These are the console's own literals (the marker
# file /etc/kin-mail/topology, the TOPOLOGY key in /etc/kin-mail/config, and the
# wizard draft all use them), so a shell stage and a Python handler can never
# disagree about what this deployment is.
#
# Empty means a config written before topology existed, and those are all single
# hosts - that is what install/kin-mail.sh has always assumed with
# "${TOPOLOGY:-1vm}". An unrecognised value is NOT defaulted: it means somebody
# hand-edited the config or a newer release wrote something this code does not
# understand, and quietly treating that as 1vm is how a split deployment gets
# firewalled as a single appliance.
kin_topology() {
  case "${TOPOLOGY:-}" in
  '') printf '1vm\n' ;;
  1vm | 2vm | split) printf '%s\n' "$TOPOLOGY" ;;
  *)
    printf 'unrecognised TOPOLOGY=%s in the appliance config\n' "$TOPOLOGY" >&2
    return 1
    ;;
  esac
}

kin_topology_is_split() {
  [ "$(kin_topology 2>/dev/null)" = split ]
}

# kin_local_ipv4s
#
# Every global IPv4 address on this host, one per line. KIN_LOCAL_IPV4S
# overrides it, which is how the tests exercise role resolution without needing
# interfaces to exist.
kin_local_ipv4s() {
  if [ -n "${KIN_LOCAL_IPV4S:-}" ]; then
    # Deliberately unquoted: the override is a space-separated list and each
    # address has to land on its own line, the way ip(8) reports them.
    # shellcheck disable=SC2086
    printf '%s\n' $KIN_LOCAL_IPV4S
    return 0
  fi
  ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
}

kin_host_has_ipv4() {
  local want="${1:-}" addr
  [ -n "$want" ] || return 1
  while IFS= read -r addr; do
    [ "$addr" = "$want" ] && return 0
  done <<EOF
$(kin_local_ipv4s)
EOF
  return 1
}

# kin_node_role
#
# Prints all | edge | mailbox, and returns non-zero without printing a role when
# it cannot tell.
#
# Resolved by matching this host's addresses against EDGE_IP and MAILBOX_IP,
# never by hostname: hostnames are set by stage 02, are wrong for the whole
# window before it runs, and on a half-built node are frequently still the
# installer's default. An address is a fact about the machine.
#
# Both matching, or neither, is a configuration error and is reported as one.
# There is no sensible fallback here - see the header.
kin_node_role() {
  local topo edge mailbox is_edge=0 is_mailbox=0
  topo=$(kin_topology) || return 1
  [ "$topo" = split ] || { printf 'all\n'; return 0; }

  edge=$(kin_edge_ip)
  mailbox=$(kin_mailbox_ip)
  if [ -z "$edge" ] || [ -z "$mailbox" ]; then
    printf 'TOPOLOGY=split but EDGE_IP/MAILBOX_IP are not both set\n' >&2
    return 1
  fi

  kin_host_has_ipv4 "$edge" && is_edge=1
  kin_host_has_ipv4 "$mailbox" && is_mailbox=1

  if [ "$is_edge" = 1 ] && [ "$is_mailbox" = 1 ]; then
    printf 'this host holds both EDGE_IP (%s) and MAILBOX_IP (%s); a split needs two machines\n' \
      "$edge" "$mailbox" >&2
    return 1
  fi
  if [ "$is_edge" = 1 ]; then printf 'edge\n'; return 0; fi
  if [ "$is_mailbox" = 1 ]; then printf 'mailbox\n'; return 0; fi

  printf 'this host has neither EDGE_IP (%s) nor MAILBOX_IP (%s); it is not part of this deployment\n' \
    "$edge" "$mailbox" >&2
  return 1
}

kin_edge_ip()      { printf '%s\n' "${EDGE_IP:-}"; }
kin_edge_host()    { printf '%s\n' "${EDGE_HOST:-}"; }
kin_mailbox_ip()   { printf '%s\n' "${MAILBOX_IP:-}"; }
kin_mailbox_host() { printf '%s\n' "${MAILBOX_HOST:-}"; }

# kin_node_ip / kin_node_host
#
# This machine's own address and name for the role it is playing.
#
# On a single appliance those are SERVER_IP and MAIL_HOST, which is what every
# stage has always used. On a split they are not: one config file is written on
# the edge and then carried to the mailbox, so SERVER_IP on the mailbox names
# the wrong machine. A stage that writes /etc/hosts or a certificate subject
# from SERVER_IP would configure the mailbox as if it were the edge.
#
# Fails rather than falling back, for the same reason kin_node_role does.
kin_node_ip() {
  local role
  role=$(kin_node_role) || return 1
  case "$role" in
  edge)    printf '%s\n' "${EDGE_IP:-}" ;;
  mailbox) printf '%s\n' "${MAILBOX_IP:-}" ;;
  *)       printf '%s\n' "${SERVER_IP:-}" ;;
  esac
}

kin_node_host() {
  local role
  role=$(kin_node_role) || return 1
  case "$role" in
  edge)    printf '%s\n' "${EDGE_HOST:-}" ;;
  mailbox) printf '%s\n' "${MAILBOX_HOST:-}" ;;
  *)       printf '%s\n' "${MAIL_HOST:-}" ;;
  esac
}

# kin_mta_ip
#
# The address that receives filtered mail from the gateway and relays outbound
# mail to it. On a single appliance that is the appliance; on a split it is the
# edge, because the mailbox node has no MTA and never speaks to the gateway.
#
# This exists so the gateway code can stop caring which topology it is in:
# lib/mail-gateway.sh uses SERVER_IP in about twenty places to mean exactly
# this, and every one of them is wrong on a split.
kin_mta_ip() {
  if kin_topology_is_split; then
    printf '%s\n' "${EDGE_IP:-}"
  else
    printf '%s\n' "${SERVER_IP:-}"
  fi
}

# kin_split_config_problem
#
# Prints the first reason this split config cannot work and returns 0; returns 1
# when it is usable. Called by the wizard before writing the config and by the
# orchestrator before touching a machine, because every one of these is cheap to
# catch now and expensive to find halfway through installing Zimbra on two
# hosts.
kin_split_config_problem() {
  local edge="${EDGE_IP:-}" mailbox="${MAILBOX_IP:-}"
  local edge_host="${EDGE_HOST:-}" mailbox_host="${MAILBOX_HOST:-}"
  local gw="${GATEWAY_HOST:-}"

  [ -n "$edge" ]    || { printf 'EDGE_IP is not set\n'; return 0; }
  [ -n "$mailbox" ] || { printf 'MAILBOX_IP is not set\n'; return 0; }
  kin_valid_ipv4 "$edge"    || { printf 'EDGE_IP is %s, which is not an IPv4 address\n' "$edge"; return 0; }
  kin_valid_ipv4 "$mailbox" || { printf 'MAILBOX_IP is %s, which is not an IPv4 address\n' "$mailbox"; return 0; }

  if [ "$edge" = "$mailbox" ]; then
    printf 'EDGE_IP and MAILBOX_IP are both %s; a split needs two machines\n' "$edge"
    return 0
  fi

  # The gateway is recorded as a host, which may be a name. Only an address can
  # be compared, and a name that happens to resolve to one of these is caught by
  # the gateway's own probe rather than guessed at here.
  if kin_valid_ipv4 "$gw"; then
    if [ "$gw" = "$edge" ]; then
      printf 'GATEWAY_HOST and EDGE_IP are both %s; the gateway cannot relay to itself\n' "$gw"
      return 0
    fi
    if [ "$gw" = "$mailbox" ]; then
      printf 'GATEWAY_HOST and MAILBOX_IP are both %s\n' "$gw"
      return 0
    fi
  fi

  [ -n "$edge_host" ]    || { printf 'EDGE_HOST is not set\n'; return 0; }
  [ -n "$mailbox_host" ] || { printf 'MAILBOX_HOST is not set\n'; return 0; }
  if [ "$edge_host" = "$mailbox_host" ]; then
    printf 'EDGE_HOST and MAILBOX_HOST are both %s; Zimbra needs a distinct name per server\n' "$edge_host"
    return 0
  fi

  # A bare address where Zimbra wants a server name. zmsetup writes this into
  # the server entry and every later zmprov gs/ms is keyed on it.
  if kin_valid_ipv4 "$edge_host"; then
    printf 'EDGE_HOST is %s; Zimbra needs a hostname there, not an address\n' "$edge_host"
    return 0
  fi
  if kin_valid_ipv4 "$mailbox_host"; then
    printf 'MAILBOX_HOST is %s; Zimbra needs a hostname there, not an address\n' "$mailbox_host"
    return 0
  fi

  return 1
}
