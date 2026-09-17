#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 06 HYBRID AUTH (AD LDAP + local fallback)
#
# Applies per-domain external Active Directory authentication when the operator
# supplied AD settings in 00-config.sh. Local Zimbra passwords remain usable
# via zimbraAuthFallbackToLocal=TRUE for accounts that are not in AD.
#
#   ./06-hybrid-auth.sh
#
# If AD_AUTH_ENABLED is not "yes", this script exits 0 after stating that
# local-only auth remains in effect - it must not fail a single-node install.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

# shellcheck disable=SC1091
. ./lib/zimbra-data-disk-probe.sh
if zimbra_is_mid_handoff; then
  warn "Mid-handoff: /opt/zimbra is unmounted; skipping hybrid auth (zmprov not reachable)."
  exit 0
fi

if [ ! -d /opt/zimbra ]; then
  fail "Zimbra is not installed yet - run 03-install-zimbra.sh first"
  exit 1
fi

echo
say "Hybrid authentication"

if [ "${AD_AUTH_ENABLED}" != "yes" ]; then
  info "AD_AUTH_ENABLED=${AD_AUTH_ENABLED:-no} - skipped."
  info "Domain uses local Zimbra auth. Re-run 00-config.sh --reset"
  info "then this script when the customer AD is ready."
  # Still prove local auth works (same probe as 05 - not zmprov auth).
  ensure_kin_test_mailbox "${TEST_USER_1}" "${TEST_PASS_1}" 2>/dev/null; _auth_rc=$?
  case "$_auth_rc" in
    0)
      ok "Local auth verified: ${TEST_USER_1}"
      ;;
    2)
      ok "Test account ready: ${TEST_USER_1}"
      info "Logging in is a mailboxd operation, and this is the edge. The"
      info "account was created in the directory from here, which is where"
      info "accounts live; the login proof belongs on the mailbox."
      ;;
    *)
      fail "Local auth FAILED: ${TEST_USER_1} - run 05-healthcheck.sh for details"
      exit 1
      ;;
  esac
  ok "Local auth remains active (no domain changes)"
  exit 0
fi

for req in AD_LDAP_URL AD_SEARCH_BASE AD_SEARCH_BIND_DN AD_SEARCH_BIND_PASSWORD AD_TEST_USER AD_TEST_PASS; do
  eval "val=\${$req}"
  if [ -z "$val" ]; then
    fail "$req is empty even though AD_AUTH_ENABLED=yes - fix /etc/kin-mail/config"
    exit 1
  fi
done

# Official Zimbra domain attrs (config guide / admin LDAP chapter):
#   zimbraAuthMech=ad|ldap|zimbra
#   zimbraAuthLdapURL, zimbraAuthLdapSearchBase, zimbraAuthLdapSearchFilter
#   zimbraAuthLdapSearchBindDn, zimbraAuthLdapSearchBindPassword
#   zimbraAuthLdapBindDn (optional template, e.g. %u@corp.local)
#   zimbraAuthFallbackToLocal=TRUE  → try local password if external auth fails
# KIN needs TRUE: mixed AD + local mailboxes in one domain per customer contract.
say "1. Applying domain attributes ${MAIL_DOMAIN}"

zm() {
  # Run a single zmprov line as the zimbra user. Arguments are shell-quoted.
  local args=() a
  for a in "$@"; do
    args+=("$(printf '%q' "$a")")
  done
  # shellcheck disable=SC2029
  su - zimbra -c "zmprov ${args[*]}"
}

AD_APPLY_OK=1
if ! zm md "$MAIL_DOMAIN" zimbraAuthMech ad \
    zimbraAuthLdapURL "$AD_LDAP_URL" \
    zimbraAuthLdapSearchBase "$AD_SEARCH_BASE" \
    zimbraAuthLdapSearchFilter "$AD_SEARCH_FILTER" \
    zimbraAuthLdapSearchBindDn "$AD_SEARCH_BIND_DN" \
    zimbraAuthLdapSearchBindPassword "$AD_SEARCH_BIND_PASSWORD" \
    zimbraAuthFallbackToLocal TRUE; then
  warn "zmprov failed to apply domain auth (URL/search/bind) - local Zimbra auth stays active"
  AD_APPLY_OK=0
fi
if [ "$AD_APPLY_OK" -eq 1 ] && [ -n "${AD_BIND_DN_TEMPLATE}" ]; then
  zm md "$MAIL_DOMAIN" zimbraAuthLdapBindDn "$AD_BIND_DN_TEMPLATE" \
    || warn "zmprov failed to set zimbraAuthLdapBindDn (continuing)"
fi
if [ "$AD_APPLY_OK" -eq 1 ]; then
  ok "zimbraAuthMech=ad + local fallback TRUE"
fi

# Show non-secret attrs for the progress/audit trail.
su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAuthMech zimbraAuthLdapURL zimbraAuthLdapSearchBase zimbraAuthLdapSearchFilter zimbraAuthLdapSearchBindDn zimbraAuthFallbackToLocal zimbraAuthLdapBindDn" 2>/dev/null \
  | sed 's/^/    /'

say "2. Test accounts - pure local + AD-backed"

# Local-only mailbox: not expected to exist in AD, so external bind fails and
# zimbraAuthFallbackToLocal accepts the Zimbra password.
LOCAL_USER="${TEST_USER_1}"
LOCAL_PASS="${TEST_PASS_1}"
ensure_kin_test_mailbox "$LOCAL_USER" "$LOCAL_PASS" 2>/dev/null; LOCAL_PREP_RC=$?
if [ "$LOCAL_PREP_RC" -eq 0 ] || [ "$LOCAL_PREP_RC" -eq 2 ]; then
  ok "Local account ready: ${LOCAL_USER}"
else
  fail "Failed to prepare local account: ${LOCAL_USER}"
  exit 1
fi

# AD-backed mailbox: must already exist in Active Directory. Zimbra still needs
# a local account record; password below is a placeholder - login proof uses AD.
AD_PLACEHOLDER="KinAdPlaceholder-$(hostname -s)"
if [ "$AD_APPLY_OK" -eq 1 ]; then
  if zimbra_cmd zmprov ga "$AD_TEST_USER" >/dev/null 2>&1; then
    info "AD-backed account already exists in Zimbra: ${AD_TEST_USER}"
  else
    if zimbra_cmd zmprov ca "$AD_TEST_USER" "$AD_PLACEHOLDER" displayName 'KIN AD auth test' >/dev/null; then
      ok "AD-backed account created in Zimbra: ${AD_TEST_USER}"
    else
      warn "Failed to create AD-backed account: ${AD_TEST_USER} (mail install continues on local auth)"
      AD_APPLY_OK=0
    fi
  fi
fi

say "3. Verify auth paths (zmmailbox / zimbra_user_auth_ok)"

# Both probes below log in, and logging in needs mailboxd. On a split edge
# there is none, so they would fail on a working deployment and the stage would
# exit 1 on the line that says "mailboxd is not usable" - which is true, and
# which is not a fault. The domain attributes set above are directory-wide and
# already applied; what cannot be done here is the proof.
if ! kin_auth_provable_here; then
  info "Auth probes need mailboxd, which on a split runs on the mailbox node."
  info "The domain attributes above are directory-wide and already in effect."
  info "Prove both paths from the mailbox:"
  info "  su - zimbra -c \"zmmailbox -m ${LOCAL_USER} -p '<password>' gaf\""
  echo
  say "DONE - domain attributes applied; auth proof belongs on the mailbox"
  exit 0
fi

auth_ok() {
  local user="$1" pass="$2" label="$3"
  if zimbra_user_auth_ok "$user" "$pass"; then
    ok "${label}: ${user}"
    return 0
  fi
  warn "${label} FAILED: ${user}"
  # Surface a real auth error (zmmailbox), not obsolete zmprov auth usage text.
  zimbra_cmd zmmailbox -m "$user" -p "$pass" gaf 2>&1 | sed 's/^/    /' | tail -5 || true
  return 1
}

LOCAL_FAIL=0
AD_FAIL=0
auth_ok "$LOCAL_USER" "$LOCAL_PASS" "Local auth (fallback)" || LOCAL_FAIL=1
if [ "$AD_APPLY_OK" -eq 1 ]; then
  auth_ok "$AD_TEST_USER" "$AD_TEST_PASS" "AD LDAP auth" || AD_FAIL=1
fi

echo
if [ "$LOCAL_FAIL" -ne 0 ]; then
  fail "Local Zimbra auth failed - mailboxd is not usable"
  exit 1
fi
if [ "$AD_APPLY_OK" -eq 0 ] || [ "$AD_FAIL" -ne 0 ]; then
  warn "AD path not verified. Local mail still works. Fix URL/bind DN/filter and re-run 06-hybrid-auth.sh"
  exit 0
fi
say "DONE - both auth paths verified"
exit 0
