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
# Would a browser or a mail client accept the certificate on :443?
#
# This used to ask "is the issuer different from the subject", and that is not
# the same question. Zimbra issues its own certificate from its own private CA
# - on a split, the CA lives on the mailbox - so subject and issuer differ on
# every appliance that has not finished stage 04, and the old test called that
# trusted. It was wrong in exactly the two places where being wrong costs the
# most:
#
#   05-healthcheck reported "Trusted certificate installed" and "Valid for 1824
#   days" on a deployment with no public certificate at all, which is the one
#   line an operator reads to decide TLS is done.
#
#   This file's caller turned HSTS on. HSTS on an untrusted certificate is not
#   a weak header, it is a denial of service against your own users: RFC 6797
#   requires the browser to refuse the connection and forbids offering "proceed
#   anyway", for a year, for everyone who loaded the page once. The comment at
#   that call site describes this exact trap. The guard was right; its predicate
#   was not.
#
# The real question is whether the chain validates against the system trust
# store, which is what openssl reports as "Verify return code: 0 (ok)". No
# hostname check: s_client connects to 127.0.0.1 and only the chain is in
# question here.
cert_is_trusted() {
  local out code
  out=$(echo | timeout 15 openssl s_client -connect "${KIN_TLS_PROBE_ADDR:-127.0.0.1:443}" \
    -servername "${MAIL_HOST:-localhost}" 2>/dev/null) || return 1
  [ -n "$out" ] || return 1
  # The last one: s_client prints a verify result per handshake, and a renegotiated
  # or resumed session prints more than one.
  code=$(printf '%s\n' "$out" |
    sed -n 's/^ *Verify return code: *\([0-9][0-9]*\).*/\1/p' | tail -1)
  [ -n "$code" ] || return 1
  [ "$code" = "0" ]
}

# cert_trust_reason
#
# What openssl said, for a message the operator can act on. Empty when it could
# not be read at all.
cert_trust_reason() {
  local out
  out=$(echo | timeout 15 openssl s_client -connect "${KIN_TLS_PROBE_ADDR:-127.0.0.1:443}" \
    -servername "${MAIL_HOST:-localhost}" 2>/dev/null) || return 0
  printf '%s\n' "$out" |
    sed -n 's/^ *Verify return code: *//p' | tail -1
}
