#!/usr/bin/env bash
# =============================================================================
# Classify healthcheck DNS / deliverability outcomes.
#
# FAIL in 05-healthcheck.sh stops the full-install pipeline and skips
# setup-complete. Public DNS, SPF, PTR, and unpublished DKIM are operator /
# ISP work, not a broken mail server. Those must be BLOCKED (exit 0) so a
# healthy Zimbra install is not rolled back at the last stage.
#
#   KIN_HEALTHCHECK_DNS_SOURCE_ONLY=1  define functions only (tests)
# =============================================================================

# opendkim-testkey -vvv stdout/stderr -> pass | blocked_missing | blocked_other
healthcheck_opendkim_classify() {
  local out="$1"
  if printf '%s' "$out" | grep -qi "key OK"; then
    printf '%s\n' "pass"
    return 0
  fi
  if printf '%s' "$out" | grep -qi "record not found"; then
    printf '%s\n' "blocked_missing"
    return 0
  fi
  printf '%s\n' "blocked_other"
}

# Authentication-Results snippet -> pass | fail | blocked
healthcheck_auth_results_dkim() {
  local ar="$1"
  case "$ar" in
    *"dkim=pass"*) printf '%s\n' "pass" ;;
    *"dkim=fail"*) printf '%s\n' "fail" ;;
    *) printf '%s\n' "blocked" ;;
  esac
}

# Published A vs SMTP sending IP -> pass | blocked_missing | blocked_mismatch
healthcheck_a_vs_send() {
  local a_rec="$1"
  local send_ip="$2"
  if [ -z "$a_rec" ]; then
    printf '%s\n' "blocked_missing"
    return 0
  fi
  if [ "$a_rec" = "$send_ip" ]; then
    printf '%s\n' "pass"
    return 0
  fi
  printf '%s\n' "blocked_mismatch"
}

# SPF text vs sending IP and A of MAIL_HOST
# -> pass_explicit | pass_mx | blocked_mx | blocked_other
healthcheck_spf_cover() {
  local spf="$1"
  local send_ip="$2"
  local a_rec="$3"
  case "$spf" in
    *"ip4:${send_ip}"*)
      printf '%s\n' "pass_explicit"
      ;;
    *mx*|*MX*)
      if [ "$a_rec" = "$send_ip" ]; then
        printf '%s\n' "pass_mx"
      else
        printf '%s\n' "blocked_mx"
      fi
      ;;
    *)
      printf '%s\n' "blocked_other"
      ;;
  esac
}

# PTR name vs MAIL_HOST and A of that PTR name
# ptr_dot: dig -x output (may include trailing dot)
# -> pass_hostname | pass_other_name | blocked_missing | blocked_chain
healthcheck_ptr_chain() {
  local send_ip="$1"
  local mail_host="$2"
  local ptr_dot="$3"
  local ptr_a="$4"
  if [ -z "$ptr_dot" ]; then
    printf '%s\n' "blocked_missing"
    return 0
  fi
  if [ "$ptr_dot" = "${mail_host}." ]; then
    if [ "$ptr_a" = "$send_ip" ]; then
      printf '%s\n' "pass_hostname"
    else
      printf '%s\n' "blocked_chain"
    fi
    return 0
  fi
  if [ "$ptr_a" = "$send_ip" ]; then
    printf '%s\n' "pass_other_name"
    return 0
  fi
  printf '%s\n' "blocked_chain"
}

# True when this host is not the published MX (HA peer / mail2). A local DKIM
# key cannot match the primary's public TXT.
# mx_host: lowest-preference MX hostname, no trailing dot. Empty = unknown.
healthcheck_host_is_not_published_mx() {
  local mail_host="$1"
  local mx_host="$2"
  mail_host=$(printf '%s' "$mail_host" | tr '[:upper:]' '[:lower:]' | sed 's/\.$//')
  mx_host=$(printf '%s' "$mx_host" | tr '[:upper:]' '[:lower:]' | sed 's/\.$//')
  [ -n "$mx_host" ] && [ "$mail_host" != "$mx_host" ]
}

# True when public TXT for selector._domainkey.domain looks like DKIM.
healthcheck_dkim_txt_visible() {
  local domain="$1"
  local sel="$2"
  local resolver="$3"
  local raw
  raw=$(dig +short +time=5 @"$resolver" TXT "${sel}._domainkey.${domain}" 2>/dev/null \
    | tr -d '"\n ')
  printf '%s' "$raw" | grep -qi 'v=DKIM1'
}

if [ "${KIN_HEALTHCHECK_DNS_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi
