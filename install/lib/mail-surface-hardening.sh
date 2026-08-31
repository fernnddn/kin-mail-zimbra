#!/usr/bin/env bash
# KIN Mail - decisions behind stage 09's mail-surface hardening.
#
# Kept out of the stage itself so they can be tested without running any of it:
# both answers gate a change that is easy to get wrong in opposite directions.
# Too eager and the appliance locks its own operator out or bounces legitimate
# mail; too shy and it ships an open relay with no headers.

# relay_scope_too_wide <space-separated networks>
#
# True when Postfix would relay for a range far wider than a host and its LAN.
# Anything listed in zimbraMtaMyNetworks can send through this server WITHOUT
# authenticating, so one over-broad entry is the whole difference between a
# mail server and somebody else's spam cannon.
#
# Deliberately conservative: /8 and wider for IPv4, /32 and wider for IPv6, or
# a default route. A /16 lab range is not flagged, because plenty of real
# deployments relay for one, and an alert that cries wolf gets ignored.
relay_scope_too_wide() {
  local nets="$1" entry prefix
  for entry in $nets; do
    case "$entry" in
    0.0.0.0/0 | ::/0) return 0 ;;
    */*) ;;
    *) continue ;;
    esac
    prefix=${entry##*/}
    case "$prefix" in '' | *[!0-9]*) continue ;; esac
    case "$entry" in
    *:*) [ "$prefix" -lt 32 ] && return 0 ;;
    *) [ "$prefix" -lt 8 ] && return 0 ;;
    esac
  done
  return 1
}

# cert_is_trusted
#
# Is the certificate on :443 issued by somebody other than itself? HSTS on a
# self-signed certificate is a trap: the browser stops offering "proceed
# anyway", and an operator who has not finished stage 04 can no longer reach
# the console at all.
cert_is_trusted() {
  local out issuer subject
  out=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 \
    -servername "${MAIL_HOST:-localhost}" 2>/dev/null |
    openssl x509 -noout -subject -issuer 2>/dev/null) || return 1
  [ -n "$out" ] || return 1
  issuer=$(printf '%s' "$out" | sed -n 's/^issuer=//p' | head -1)
  subject=$(printf '%s' "$out" | sed -n 's/^subject=//p' | head -1)
  [ -n "$issuer" ] && [ -n "$subject" ] && [ "$issuer" != "$subject" ]
}
