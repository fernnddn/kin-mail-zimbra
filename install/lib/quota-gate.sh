#!/usr/bin/env bash
# =============================================================================
# KIN Mail — shared quota / seat gate (NEW MAILBOX CREATION ONLY)
#
# Source this after 00-config.sh (needs zimbra_cmd / MAIL_DOMAIN / CONTRACTED_SEATS):
#
#   . ./00-config.sh
#   . ./lib/quota-gate.sh
#
# SCOPE (explicit design decision — do not widen without a new decision):
#   • YES:  creating a new mailbox (before `zmprov ca`)
#   • NO:   password reset, per-mailbox quota change, rename, alias, COS,
#           status change, or any other modify path
#
# Callers (today and later):
#   • install/08-create-mailbox.sh  (manual CLI provisioning)
#   • future admin-console self-service form (must call these functions — not reimplement)
#   • future directory sync (same — create path only)
#
# Active mailbox definition (counted against CONTRACTED_SEATS):
#   • Account is on the target mail domain (zmprov -l gaa <domain>)
#   • zimbraAccountStatus == active
#     (excludes closed / maintenance / locked / pending / …)
#   • NOT zimbraIsSystemAccount=TRUE
#   • NOT zimbraIsSystemResource=TRUE
#   • NOT zimbraIsAdminAccount=TRUE   (global admin; e.g. admin@)
#   • NOT zimbraIsExternalVirtualAccount=TRUE
#   Local Zimbra auth and AD-LDAP-backed accounts both appear in LDAP via
#   `zmprov -l`, so both are counted the same way.
# =============================================================================

# Exported by kin_quota_gate_allow_new_mailbox / kin_quota_status:
#   KIN_QUOTA_USED  KIN_QUOTA_LIMIT  KIN_QUOTA_REMAINING  KIN_QUOTA_MESSAGE

# -----------------------------------------------------------------------------
# Count active (billable) mailboxes on a domain.
# Prints an integer on stdout. Return 0 on success, non-zero on probe failure.
# -----------------------------------------------------------------------------
kin_quota_count_active_mailboxes() {
  local domain="${1:-${MAIL_DOMAIN:-}}"
  local raw

  if [ -z "$domain" ]; then
    printf 'kin_quota_count_active_mailboxes: domain is empty\n' >&2
    return 2
  fi
  if ! command -v zimbra_cmd >/dev/null 2>&1; then
    printf 'kin_quota_count_active_mailboxes: zimbra_cmd unavailable (source 00-config.sh)\n' >&2
    return 2
  fi

  # -l/--ldap: required for gaa; sees local + AD-provisioned accounts alike.
  if ! raw=$(zimbra_cmd zmprov -l gaa -v "$domain" 2>/dev/null); then
    printf 'kin_quota_count_active_mailboxes: zmprov -l gaa -v %s failed\n' "$domain" >&2
    return 1
  fi

  printf '%s\n' "$raw" | awk '
    BEGIN { count = 0; acct = ""; status = ""; sys = ""; sres = ""; admin = ""; ext = "" }
    function flush() {
      if (acct == "") return
      if (tolower(sys) == "true") return
      if (tolower(sres) == "true") return
      if (tolower(admin) == "true") return
      if (tolower(ext) == "true") return
      if (status != "active") return
      count++
    }
    /^# name / {
      flush()
      acct = $3
      status = ""; sys = ""; sres = ""; admin = ""; ext = ""
      next
    }
    /^zimbraAccountStatus:/            { status = $2; next }
    /^zimbraIsSystemAccount:/          { sys = $2; next }
    /^zimbraIsSystemResource:/         { sres = $2; next }
    /^zimbraIsAdminAccount:/           { admin = $2; next }
    /^zimbraIsExternalVirtualAccount:/ { ext = $2; next }
    END { flush(); print count }
  '
}

# -----------------------------------------------------------------------------
# NEW-MAILBOX-CREATE GATE ONLY.
#
# Exit 0 = allow one new create. Exit 1 = deny. Exit 2 = misconfiguration.
# Sets KIN_QUOTA_* and KIN_QUOTA_MESSAGE (human-readable; deny reasons are clear).
#
# Do NOT call from password-reset / quota-edit / other modify scripts.
# -----------------------------------------------------------------------------
kin_quota_gate_allow_new_mailbox() {
  local domain="${1:-${MAIL_DOMAIN:-}}"
  local used limit remaining
  local seats_raw="${CONTRACTED_SEATS:-}"

  KIN_QUOTA_USED=""
  KIN_QUOTA_LIMIT=""
  KIN_QUOTA_REMAINING=""
  KIN_QUOTA_MESSAGE=""

  if [ -z "$domain" ]; then
    KIN_QUOTA_MESSAGE="quota gate: MAIL_DOMAIN / domain is empty"
    return 2
  fi

  # Placeholder / unset — never invent a production seat count.
  if [ -z "$seats_raw" ] || [ "$seats_raw" = "PLACEHOLDER_UNSET" ]; then
    KIN_QUOTA_USED=""
    KIN_QUOTA_LIMIT=""
    KIN_QUOTA_REMAINING=""
    KIN_QUOTA_MESSAGE="seat limit not configured (CONTRACTED_SEATS is PLACEHOLDER_UNSET) — set a positive integer in /etc/kin-mail/config before creating mailboxes"
    return 1
  fi

  case "$seats_raw" in
    ''|*[!0-9]*)
      KIN_QUOTA_MESSAGE="seat limit misconfigured (CONTRACTED_SEATS='${seats_raw}' is not a non-negative integer)"
      return 2
      ;;
  esac
  limit=$seats_raw

  if ! used=$(kin_quota_count_active_mailboxes "$domain"); then
    KIN_QUOTA_MESSAGE="quota gate: failed to count active mailboxes on ${domain}"
    return 2
  fi

  KIN_QUOTA_USED=$used
  KIN_QUOTA_LIMIT=$limit
  remaining=$((limit - used))
  KIN_QUOTA_REMAINING=$remaining

  if [ "$used" -ge "$limit" ]; then
    KIN_QUOTA_MESSAGE="seat limit reached: ${used}/${limit} — contact KIN to add seats"
    return 1
  fi

  KIN_QUOTA_MESSAGE="seat available: ${used}/${limit} (${remaining} remaining) — new mailbox create allowed"
  return 0
}

# -----------------------------------------------------------------------------
# Read-only status helper (safe to call anytime — does not create anything).
# Prints a one-line summary; exit mirrors kin_quota_gate_allow_new_mailbox.
# -----------------------------------------------------------------------------
kin_quota_status() {
  local domain="${1:-${MAIL_DOMAIN:-}}"
  local rc=0
  kin_quota_gate_allow_new_mailbox "$domain" || rc=$?
  printf '%s\n' "${KIN_QUOTA_MESSAGE}"
  if [ -n "${KIN_QUOTA_USED}" ] && [ -n "${KIN_QUOTA_LIMIT}" ]; then
    printf 'used=%s limit=%s remaining=%s domain=%s\n' \
      "$KIN_QUOTA_USED" "$KIN_QUOTA_LIMIT" "$KIN_QUOTA_REMAINING" "$domain"
  fi
  return "$rc"
}
