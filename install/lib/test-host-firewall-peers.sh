#!/usr/bin/env bash
# The firewall's per-peer rules must name the REAL peers.
#
# 10-host-firewall.sh used to derive them from a hardcoded lab convention
# (.13/.12/.14 relative to this host's /24). A live Phase 6 pair on
# a .30/.31/.29 layout therefore got corosync, DRBD and pcsd rules pointing at machines
# that do not exist; it only kept working because the broader /24 LAN rules
# alongside them still matched.
# Addresses here are RFC 5737 documentation ranges, never real deployment IPs:
# this repo is public and the hygiene gate rejects live addresses.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

SCRIPT="../10-host-firewall.sh"

# Re-run just the derivation block the script uses, with the same inputs
# /etc/kin-mail/config would have provided.
derive() {
  local server_ip="$1" peer_ip="$2" mon_ip="$3"
  SERVER_IP="$server_ip" PEER_HOST_IP="$peer_ip" OBSERVABILITY_VM_IP="$mon_ip" \
  bash -c '
    set -u
    SERVER_IP="${SERVER_IP:-}"; PEER_HOST_IP="${PEER_HOST_IP:-}"; OBSERVABILITY_VM_IP="${OBSERVABILITY_VM_IP:-}"
    CLUSTER_NET=""; PEER_A=""; PEER_B=""; MON_IP="${KIN_MON_IP:-}"
    '"$(sed -n '/^# Derive LAN \/24/,/^esac$/p' "$SCRIPT")"'
    printf "%s|%s|%s|%s\n" "$CLUSTER_NET" "$PEER_A" "$PEER_B" "$MON_IP"
  '
}

got=$(derive 192.0.2.30 192.0.2.31 192.0.2.29)
if [ "$got" = "192.0.2.0/24|192.0.2.30|192.0.2.31|192.0.2.29" ]; then
  pass "real peer and observability addresses are used"
else
  bad "expected the configured addresses, got: $got"
fi

# The old lab numbers must not appear when the config knows better.
case "$got" in
  *192.0.2.13*|*192.0.2.12*|*192.0.2.14*) bad "stale lab guesses leaked into the rules: $got" ;;
  *) pass "no stale .13/.12/.14 guesses when config is populated" ;;
esac

# A different /24 must follow the host, not a memorised subnet.
got2=$(derive 198.51.100.10 198.51.100.11 198.51.100.12)
if [ "$got2" = "198.51.100.0/24|198.51.100.10|198.51.100.11|198.51.100.12" ]; then
  pass "derivation follows the host's own subnet"
else
  bad "wrong derivation on a different subnet: $got2"
fi

# A single-node install has no peer and no witness. Those must stay EMPTY so
# the rule for them is skipped, rather than being invented - a rule naming a
# machine that does not exist is worse than no rule, and the CLUSTER_NET rules
# still cover any real peer.
got3=$(derive 192.0.2.30 "" "")
if [ "$got3" = "192.0.2.0/24|192.0.2.30||" ]; then
  pass "an unknown peer/witness stays empty instead of being invented"
else
  bad "expected empty peer and mon on a single-node host, got: $got3"
fi
case "$got3" in
  *192.0.2.12*|*192.0.2.14*) bad "invented a peer address: $got3" ;;
  *) pass "no fabricated addresses when config is silent" ;;
esac

# And the script must actually skip an empty address rather than calling ufw
# with a blank source.
if grep -q 'allow_from()' ../10-host-firewall.sh   && grep -q '\[ -n "\$addr" \] || return 0' ../10-host-firewall.sh; then
  pass "per-host rules are skipped when the address is unknown"
else
  bad "10-host-firewall.sh does not guard empty addresses"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All host-firewall peer tests passed\n'
  exit 0
fi
printf '%s failure(s)\n' "$fails"
exit 1
