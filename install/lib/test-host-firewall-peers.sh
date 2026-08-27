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

# With nothing configured it must still produce something usable rather than
# empty rules, which ufw would reject.
got3=$(derive 192.0.2.30 "" "")
case "$got3" in
  "192.0.2.0/24|192.0.2.30|"*[0-9]"|"*[0-9]) pass "falls back to a guess only when config is silent" ;;
  *) bad "empty fallback would produce invalid ufw rules: $got3" ;;
esac

if [ "$fails" -eq 0 ]; then
  printf 'All host-firewall peer tests passed\n'
  exit 0
fi
printf '%s failure(s)\n' "$fails"
exit 1
