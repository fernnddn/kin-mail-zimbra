#!/usr/bin/env bash
# =============================================================================
# Ensure the OS resolver can resolve public names during install.
#
# Blank Ubuntu VMs often ship with broken / empty systemd-resolved DNS while
# dig @$DNS_UPSTREAM still works. 01-preflight then FAILs curl to GitHub /
# Ubuntu / Zimbra ("unreachable") even though the network path is fine.
# 02-prepare-os later installs dnsmasq; this helper only bridges that gap so
# preflight and apt can run.
# =============================================================================
set -u

kin_ensure_outbound_dns() {
  local u1="${DNS_UPSTREAM_1:-1.1.1.1}"
  local u2="${DNS_UPSTREAM_2:-8.8.8.8}"
  local probe_host="${KIN_DNS_PROBE_HOST:-github.com}"
  local ns_line="" ns worked=0 iface=""

  _kin_dns_system_ok() {
    getent ahostsv4 "$probe_host" >/dev/null 2>&1 && return 0
    getent hosts "$probe_host" >/dev/null 2>&1 && return 0
    [ -n "$(dig +short +time=3 +tries=1 A "$probe_host" 2>/dev/null | head -1)" ]
  }

  _kin_dns_upstream_ok() {
    local server="$1"
    [ -n "$server" ] || return 1
    [ -n "$(dig +short +time=3 +tries=1 @"$server" A "$probe_host" 2>/dev/null | head -1)" ]
  }

  if _kin_dns_system_ok; then
    return 0
  fi

  if ! command -v dig >/dev/null 2>&1; then
    # kin_apt when the caller has sourced lib/apt-lock.sh, plain otherwise:
    # this library is also sourced by contexts that do not need apt at all.
    if command -v kin_apt >/dev/null 2>&1; then
      kin_apt -qq -y install dnsutils >/dev/null 2>&1 || true
    else
      apt-get -qq -y install dnsutils >/dev/null 2>&1 || true
    fi
  fi
  if ! command -v dig >/dev/null 2>&1; then
    echo "KIN_DNS: dig unavailable; cannot probe upstream resolvers" >&2
    return 1
  fi

  for ns in "$u1" "$u2" "1.1.1.1" "8.8.8.8"; do
    if _kin_dns_upstream_ok "$ns"; then
      worked=1
      case " $ns_line " in
        *" nameserver ${ns} "*) ;;
        *) ns_line="${ns_line}nameserver ${ns}"$'\n' ;;
      esac
    fi
  done

  if [ "$worked" -eq 0 ]; then
    echo "KIN_DNS: no upstream resolved ${probe_host} (firewall, no route, or DNS blocked)" >&2
    return 1
  fi

  # Prefer resolvectl when the stub resolver owns /etc/resolv.conf.
  if command -v resolvectl >/dev/null 2>&1 \
    && { [ -L /etc/resolv.conf ] || grep -qE 'nameserver[[:space:]]+127\.0\.0\.(53|1)' /etc/resolv.conf 2>/dev/null; }
  then
    iface="$(ip -4 route show default 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
    if [ -n "$iface" ]; then
      # shellcheck disable=SC2086
      resolvectl dns "$iface" $u1 $u2 >/dev/null 2>&1 || true
      resolvectl domain "$iface" '~.' >/dev/null 2>&1 || true
      sleep 1
      if _kin_dns_system_ok; then
        echo "KIN_DNS: fixed systemd-resolved on ${iface} using ${u1} / ${u2}"
        return 0
      fi
    fi
  fi

  # Direct resolv.conf bootstrap (same approach 02 uses before dnsmasq owns DNS).
  printf '%s' "$ns_line" > /etc/resolv.conf.kin-bootstrap
  if [ -L /etc/resolv.conf ] || [ -e /etc/resolv.conf ]; then
    cp -a /etc/resolv.conf /etc/resolv.conf.kin-preflight.bak 2>/dev/null || true
  fi
  ln -sfn /etc/resolv.conf.kin-bootstrap /etc/resolv.conf

  if _kin_dns_system_ok; then
    echo "KIN_DNS: wrote /etc/resolv.conf from upstream ${u1} / ${u2} (system resolver was broken)"
    return 0
  fi

  echo "KIN_DNS: upstream works but system resolver still cannot resolve ${probe_host}" >&2
  return 1
}

if [ "${BASH_SOURCE[0]-}" = "${0}" ]; then
  kin_ensure_outbound_dns
fi
