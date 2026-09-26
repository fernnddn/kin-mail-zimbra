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
# shellcheck source=lib/zimbra-store.sh
. ./lib/zimbra-store.sh
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
# zimbraAuthMech=ad needs a bind template, and the wizard calls it optional.
#
# With mech=ad Zimbra binds to the directory AS THE USER, forming the bind name
# from this template. The search base, filter and bind DN set above are how the
# `ldap` mechanism finds people; `ad` still needs to know what to bind as. With
# the template left blank - which is what the wizard tells the operator to do -
# there is nothing to bind with, and every single sign-in fails while every
# setting on the page looks correct.
#
# That is what happened on 25 Sep 2026: the service account bound fine by hand
# and could find the user, the certificate was trusted, and nobody could log in.
#
# Derived from the search base rather than asked for again: DC=kampretos,DC=my,
# DC=id is the same domain, written the other way round, and asking twice for
# one fact is how the two come to disagree.
if [ "$AD_APPLY_OK" -eq 1 ]; then
  _bind_tpl="${AD_BIND_DN_TEMPLATE}"
  if [ -z "$_bind_tpl" ]; then
    # Only the dc= components, joined with dots. A search base narrowed to an
    # organisational unit - OU=Staff,DC=corp,DC=local - is perfectly normal, and
    # treating the whole string as the domain would produce "ou=staff.corp.local".
    _ad_domain=$(printf '%s' "$AD_SEARCH_BASE" | tr 'A-Z' 'a-z' | tr -d '[:space:]' |
      grep -oE 'dc=[^,]+' | sed 's/^dc=//' | paste -sd. -)
    case "$_ad_domain" in
      *.*)
        _bind_tpl="%u@${_ad_domain}"
        info "No bind template given; using ${_bind_tpl} from the search base."
        ;;
      *)
        warn "Could not derive an AD domain from AD_SEARCH_BASE (${AD_SEARCH_BASE})."
        warn "Set AD_BIND_DN_TEMPLATE in ${CONF_FILE}, or sign-in will fail."
        ;;
    esac
  fi
  if [ -n "$_bind_tpl" ]; then
    zm md "$MAIL_DOMAIN" zimbraAuthLdapBindDn "$_bind_tpl" \
      || warn "zmprov failed to set zimbraAuthLdapBindDn (continuing)"
  fi
fi
if [ "$AD_APPLY_OK" -eq 1 ]; then
  ok "zimbraAuthMech=ad + local fallback TRUE"
fi

# Show non-secret attrs for the progress/audit trail.
su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAuthMech zimbraAuthLdapURL zimbraAuthLdapSearchBase zimbraAuthLdapSearchFilter zimbraAuthLdapSearchBindDn zimbraAuthFallbackToLocal zimbraAuthLdapBindDn" 2>/dev/null \
  | sed 's/^/    /'

# -----------------------------------------------------------------------------
# Mailboxes created from Active Directory.
#
# zimbraAuthMech=ad, applied above, only delegates the password check. It does
# not read the directory's account list, so someone who exists in AD still has
# no mailbox and cannot receive mail. To an operator that looks like AD failing
# to sync - reported as exactly that in QA on 24 Sep 2026, against a
# configuration that was entirely correct.
#
# Zimbra's own answer is auto-provisioning. Off unless asked for: creating
# mailboxes spends contracted seats.
#
# Every attribute is checked to exist on THIS Zimbra build before it is set.
# These names are stable across Zimbra 8-10, but "stable" is not "verified",
# and a name this build lacks would half-configure the directory with no clue
# left behind as to why.
autoprov_supported() {
  su - zimbra -c "zmprov desc -a $1" >/dev/null 2>&1
}

apply_autoprov() {
  local mode="$1" attr
  say "1b. Creating mailboxes from AD (auto-provisioning: ${mode})"
  for attr in zimbraAutoProvMode zimbraAutoProvAuthMech zimbraAutoProvLdapURL \
    zimbraAutoProvLdapAdminBindDn zimbraAutoProvLdapAdminBindPassword \
    zimbraAutoProvLdapSearchBase zimbraAutoProvLdapSearchFilter \
    zimbraAutoProvAccountNameMap; do
    if ! autoprov_supported "$attr"; then
      warn "This Zimbra build has no ${attr} - skipping auto-provisioning."
      info "Mailboxes stay manual. AD still checks passwords for accounts that exist."
      return 0
    fi
  done

  # The same URL, base, bind and filter the auth path already uses. A second
  # copy would be a second thing to get wrong and a second thing to keep in step.
  if ! zm md "$MAIL_DOMAIN" \
    zimbraAutoProvMode "$mode" \
    zimbraAutoProvAuthMech LDAP \
    zimbraAutoProvLdapURL "$AD_LDAP_URL" \
    zimbraAutoProvLdapAdminBindDn "$AD_SEARCH_BIND_DN" \
    zimbraAutoProvLdapAdminBindPassword "$AD_SEARCH_BIND_PASSWORD" \
    zimbraAutoProvLdapSearchBase "$AD_SEARCH_BASE" \
    zimbraAutoProvLdapSearchFilter "$AD_SEARCH_FILTER" \
    zimbraAutoProvAccountNameMap sAMAccountName; then
    warn "Could not apply auto-provisioning - mailboxes stay manual."
    info "Mail is unaffected; AD still checks passwords for accounts that exist."
    return 0
  fi

  # Carry the person's name across, so a provisioned mailbox is not a bare
  # address with nothing in the address book.
  if autoprov_supported zimbraAutoProvAttrMap; then
    zm md "$MAIL_DOMAIN" \
      zimbraAutoProvAttrMap displayName=displayName \
      zimbraAutoProvAttrMap givenName=givenName \
      zimbraAutoProvAttrMap sn=sn >/dev/null 2>&1 \
      || warn "Name attributes were not mapped; mailboxes will have no display name."
  fi

  # Checked, not announced.
  local got
  got=$(su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAutoProvMode" 2>/dev/null |
    sed -n 's/^zimbraAutoProvMode: //p' | tr '\n' ',' | sed 's/,$//')
  if [ -z "$got" ]; then
    warn "The directory does not report an auto-provisioning mode; treating it as not applied."
    return 0
  fi
  ok "Auto-provisioning active: ${got}"
  case ",${got}," in
    *,LAZY,*) info "A mailbox is created the first time someone signs in with their AD password." ;;
  esac
  case ",${got}," in
    *,MANUAL,*) info "AD accounts can now be searched and created from the Zimbra admin console." ;;
  esac
  warn "Each mailbox created this way spends one contracted seat."
}

if [ "$AD_APPLY_OK" -eq 1 ] && [ -n "${AD_AUTOPROV_MODE}" ]; then
  apply_autoprov "$AD_AUTOPROV_MODE"
elif [ "$AD_APPLY_OK" -eq 1 ]; then
  info "AD_AUTOPROV_MODE is empty: mailboxes are created by hand."
  info "AD checks passwords for accounts that exist; it does not create them."
  info "Set AD_AUTOPROV_MODE=LAZY,MANUAL in ${CONF_FILE} to let Zimbra create them."
fi

# An ldaps:// directory signs its certificate with the domain's own private CA,
# which Zimbra does not trust until it is told to. Left to the operator that is
# four steps across two operating systems; done here it is none. Never fatal:
# the stage below reports an untrusted certificate clearly enough either way.
if [ "$AD_APPLY_OK" -eq 1 ]; then
  case "${AD_LDAP_URL}" in
    ldaps://*)
      if [ -x ./14-ad-trust.sh ]; then
        ./14-ad-trust.sh || warn "Certificate trust step did not complete; AD sign-in may fail on TLS."
      else
        warn "14-ad-trust.sh is missing; the directory certificate was not checked."
      fi
      ;;
  esac
fi

# Bring the directory's people across, and keep doing it.
#
# Without this the deploy ends with authentication configured and an empty
# admin console: AD holds 130 people, Zimbra holds none of them, and the
# operator is told to run something on the other machine. That is the step this
# release exists to remove. Never fatal - mail does not depend on it.
if [ "$AD_APPLY_OK" -eq 1 ]; then
  if [ -x ./15-ad-sync.sh ] || [ "$(kin_mailboxd_is_local; echo $?)" -ne 0 ]; then
    say "1c. Bringing Active Directory's people into Zimbra"
    kin_run_stage_on_store 15-ad-sync.sh --schedule
      _sync_rc=$?
      case "$_sync_rc" in
        0) ;;
        3)
          echo
          fail "THE DIRECTORY WAS NOT SYNCHRONISED - the mailbox could not be reached."
          warn "Authentication is configured, but the admin console will show no AD users"
          warn "and nobody can sign in until their mailbox exists."
          info "Neither SSH password worked. Check MAILBOX_SSH_PASS and KIN_USER_PASS"
          info "in ${CONF_FILE}, then run ON THE MAILBOX:"
          info "  sudo /opt/kin-mail-deploy/install/15-ad-sync.sh --schedule"
          echo
          ;;
        4)
          echo
          fail "THE DIRECTORY WAS NOT SYNCHRONISED - no mailboxes could be created."
          warn "Authentication works, but the admin console will list no AD users"
          warn "and those people cannot sign in until their mailbox exists."
          info "The usual cause is that the contracted seat count was never set."
          info "Set Mailbox seats in the console, then run ON THE MAILBOX:"
          info "  sudo /opt/kin-mail-deploy/install/15-ad-sync.sh"
          echo
          ;;
        *)
          echo
          fail "THE DIRECTORY WAS NOT SYNCHRONISED (exit ${_sync_rc})."
          warn "Authentication is configured, but the admin console will show no AD users."
          info "Run on the mailbox to see why:"
          info "  sudo /opt/kin-mail-deploy/install/15-ad-sync.sh --dry-run"
          echo
          ;;
      esac
      # Carry this to the end of the deploy.
      #
      # This stage exits 0 on purpose: authentication itself is configured, and
      # stopping the pipeline here would leave the host unhardened and
      # unfirewalled over a mailbox-creation problem. But exiting 0 also meant
      # the run finished "complete, all stages exited 0" while the admin console
      # listed nobody and the whole point of the directory integration was
      # missing. The three FAIL lines above had scrolled past by then.
      #
      # kin-mail.sh reads this marker after the stage loop and names it in the
      # summary, so the deploy ends saying what is wrong and what to do.
      if [ "$_sync_rc" -eq 0 ]; then
        rm -f /etc/kin-mail/.ad-sync-incomplete 2>/dev/null || true
      else
        mkdir -p /etc/kin-mail 2>/dev/null || true
        printf '%s\n' "$_sync_rc" >/etc/kin-mail/.ad-sync-incomplete 2>/dev/null || true
      fi
  fi
fi

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
  warn "No Active Directory users were synchronised, so the admin console lists only local accounts."
  # An ldaps:// URL adds a failure the message above does not describe, and it
  # is the likely one the first time anyone points this at a Windows CA.
  #
  # Active Directory's LDAPS certificate is issued by the domain's own private
  # CA. Zimbra's LDAP client validates it against Java's trust store, which has
  # never heard of that CA, so the bind fails on TLS - before any password is
  # even considered. The error surfaces as an auth failure and sends whoever
  # reads it to check the bind DN and the filter, which are fine.
  #
  # Measured on the lab AD, 24 Sep 2026: openssl reports
  # "unable to get local issuer certificate" against the system store and
  # "Verify return code: 0" once the domain CA is supplied.
  case "${AD_LDAP_URL}" in
    ldaps://*)
      echo
      warn "This is an ldaps:// URL. If the bind failed on TLS rather than on the"
      warn "password, Zimbra does not trust the certificate the domain controller"
      warn "presents - Active Directory signs it with the domain's own private CA."
      info "Check it from this node:"
      info "  openssl s_client -connect <dc-host>:636 </dev/null 2>&1 | grep 'Verify return'"
      info "A non-zero code means the CA has to be imported, on the MAILBOX node:"
      info "  su - zimbra -c 'zmcertmgr addcacert /path/to/domain-ca.cer'"
      info "  su - zimbra -c 'zmmailboxdctl restart'"
      info "Export that file from the CA, or read it out of the directory itself:"
      info "  CN=Certification Authorities,CN=Public Key Services,CN=Services,CN=Configuration,<base>"
      ;;
  esac
  exit 0
fi
say "DONE - both auth paths verified"
exit 0
