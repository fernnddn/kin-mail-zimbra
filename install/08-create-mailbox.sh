#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 08 CREATE MAILBOX (manual provisioning — quota-gated)
#
# The only CLI create path that must run the shared seat gate BEFORE zmprov ca.
# Future admin-console self-service must call the same lib/quota-gate.sh helpers
# (kin_quota_gate_allow_new_mailbox) — do not reimplement counting or the gate.
#
#   sudo ./08-create-mailbox.sh                  # interactive
#   sudo ./08-create-mailbox.sh user@domain 'Pass' ['Display Name']
#   sudo ./08-create-mailbox.sh --status         # seat usage only (no create)
#
# SCOPE: new mailbox creation only. Password reset / quota edits are out of
# scope for this script and must NOT be routed through the quota gate.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# shellcheck disable=SC1091
. ./lib/quota-gate.sh
need_root

if [ ! -d /opt/zimbra ]; then
  fail "Zimbra is not installed yet - run 03-install-zimbra.sh first"
  exit 1
fi

usage() {
  cat <<EOF
Usage:
  sudo ./08-create-mailbox.sh --status
  sudo ./08-create-mailbox.sh <email> <password> [displayName]
  sudo ./08-create-mailbox.sh          # interactive prompts

Creates one mailbox on ${MAIL_DOMAIN:-<MAIL_DOMAIN>} only after the shared
quota gate allows it (NEW CREATE only — not for password/quota changes).
EOF
}

show_status() {
  echo
  say "Seat / quota status (${MAIL_DOMAIN})"
  if kin_quota_status "$MAIL_DOMAIN"; then
    ok "Under seat limit — new mailbox create would be allowed"
    return 0
  fi
  local rc=$?
  if [ "$rc" -eq 1 ]; then
    fail "${KIN_QUOTA_MESSAGE}"
    return 1
  fi
  fail "${KIN_QUOTA_MESSAGE}"
  return "$rc"
}

# Create path: gate FIRST, then zmprov ca. Never ca before the gate returns 0.
create_mailbox_gated() {
  local email="$1" pass="$2" dname="${3:-}"
  local local_part domain_part out

  case "$email" in
    *@*) ;;
    *)
      fail "Email must be user@${MAIL_DOMAIN}"
      return 2
      ;;
  esac
  local_part="${email%@*}"
  domain_part="${email#*@}"
  if [ "$domain_part" != "$MAIL_DOMAIN" ]; then
    fail "Refusing create outside configured domain (${email} vs ${MAIL_DOMAIN})"
    return 2
  fi
  if [ -z "$local_part" ] || [ -z "$pass" ]; then
    fail "Email and password are required"
    return 2
  fi
  if [ ${#pass} -lt 8 ]; then
    fail "Password must be at least 8 characters"
    return 2
  fi

  echo
  say "Quota gate (new mailbox create only)"
  if ! kin_quota_gate_allow_new_mailbox "$MAIL_DOMAIN"; then
    fail "${KIN_QUOTA_MESSAGE}"
    info "No zmprov ca was run."
    return 1
  fi
  ok "${KIN_QUOTA_MESSAGE}"

  if zimbra_cmd zmprov -l ga "$email" >/dev/null 2>&1; then
    fail "Account already exists: ${email}"
    return 1
  fi

  say "Creating mailbox ${email}"
  if [ -n "$dname" ]; then
    if ! out=$(zimbra_cmd zmprov ca "$email" "$pass" displayName "$dname" 2>&1); then
      fail "zmprov ca failed"
      printf '%s\n' "$out" | sed 's/^/    /' >&2
      return 1
    fi
  else
    if ! out=$(zimbra_cmd zmprov ca "$email" "$pass" 2>&1); then
      fail "zmprov ca failed"
      printf '%s\n' "$out" | sed 's/^/    /' >&2
      return 1
    fi
  fi

  if ! zimbra_user_auth_ok "$email" "$pass"; then
    fail "Account created but auth probe failed for ${email}"
    return 1
  fi
  ok "Created and authenticated: ${email}"
  kin_quota_status "$MAIL_DOMAIN" >/dev/null || true
  info "Seats now: ${KIN_QUOTA_USED}/${KIN_QUOTA_LIMIT}"
  return 0
}

# --- entry -------------------------------------------------------------------
case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --status)
    show_status
    exit $?
    ;;
esac

if [ "$#" -ge 2 ]; then
  create_mailbox_gated "$1" "$2" "${3:-}"
  exit $?
fi

if [ "$#" -eq 1 ]; then
  usage
  exit 2
fi

# Interactive
echo
say "Create mailbox (quota-gated)"
info "Domain: ${MAIL_DOMAIN}"
info "This path calls kin_quota_gate_allow_new_mailbox before zmprov ca."
show_status || {
  warn "Create is blocked until seats are available / CONTRACTED_SEATS is set."
  exit 1
}

ask EMAIL "Full email address" ""
while :; do
  ask_secret PASS "Password (min 8 characters)"
  [ ${#PASS} -ge 8 ] && break
  warn "Too short."
done
ask DNAME "Display name (optional)" ""

create_mailbox_gated "$EMAIL" "$PASS" "$DNAME"
exit $?
