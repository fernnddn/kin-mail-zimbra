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
# local-only auth remains in effect — it must not fail a single-node install.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

if [ ! -d /opt/zimbra ]; then
  fail "Zimbra belum terpasang - jalankan 03-install-zimbra.sh dulu"
  exit 1
fi

echo
say "Hybrid authentication"

if [ "${AD_AUTH_ENABLED}" != "yes" ]; then
  info "AD_AUTH_ENABLED=${AD_AUTH_ENABLED:-no} - dilewati."
  info "Domain memakai auth lokal Zimbra. Jalankan ulang 00-config.sh --reset"
  info "lalu skrip ini bila AD customer sudah siap."
  # Still prove local auth works (same probe as 05 — not zmprov auth).
  if ensure_kin_test_mailbox "${TEST_USER_1}" "${TEST_PASS_1}" 2>/dev/null \
     && zimbra_user_auth_ok "${TEST_USER_1}" "${TEST_PASS_1}"; then
    ok "Auth lokal terverifikasi: ${TEST_USER_1}"
  else
    fail "Auth lokal GAGAL: ${TEST_USER_1} — jalankan 05-healthcheck.sh untuk detail"
    exit 1
  fi
  ok "Auth lokal tetap aktif (tidak ada perubahan domain)"
  exit 0
fi

for req in AD_LDAP_URL AD_SEARCH_BASE AD_SEARCH_BIND_DN AD_SEARCH_BIND_PASSWORD AD_TEST_USER AD_TEST_PASS; do
  eval "val=\${$req}"
  if [ -z "$val" ]; then
    fail "$req kosong meskipun AD_AUTH_ENABLED=yes - perbaiki /etc/kin-mail/config"
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
say "1. Menerapkan atribut domain ${MAIL_DOMAIN}"

zm() {
  # Run a single zmprov line as the zimbra user. Arguments are shell-quoted.
  local args=() a
  for a in "$@"; do
    args+=("$(printf '%q' "$a")")
  done
  # shellcheck disable=SC2029
  su - zimbra -c "zmprov ${args[*]}"
}

if ! zm md "$MAIL_DOMAIN" zimbraAuthMech ad \
    zimbraAuthLdapURL "$AD_LDAP_URL" \
    zimbraAuthLdapSearchBase "$AD_SEARCH_BASE" \
    zimbraAuthLdapSearchFilter "$AD_SEARCH_FILTER" \
    zimbraAuthLdapSearchBindDn "$AD_SEARCH_BIND_DN" \
    zimbraAuthLdapSearchBindPassword "$AD_SEARCH_BIND_PASSWORD" \
    zimbraAuthFallbackToLocal TRUE; then
  fail "zmprov gagal menerapkan auth domain (URL/search/bind)"
  exit 1
fi
if [ -n "${AD_BIND_DN_TEMPLATE}" ]; then
  zm md "$MAIL_DOMAIN" zimbraAuthLdapBindDn "$AD_BIND_DN_TEMPLATE" \
    || { fail "zmprov gagal set zimbraAuthLdapBindDn"; exit 1; }
fi
ok "zimbraAuthMech=ad + fallback lokal TRUE"

# Show non-secret attrs for the progress/audit trail.
su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAuthMech zimbraAuthLdapURL zimbraAuthLdapSearchBase zimbraAuthLdapSearchFilter zimbraAuthLdapSearchBindDn zimbraAuthFallbackToLocal zimbraAuthLdapBindDn" 2>/dev/null \
  | sed 's/^/    /'

say "2. Akun uji — lokal murni + AD-backed"

# Local-only mailbox: not expected to exist in AD, so external bind fails and
# zimbraAuthFallbackToLocal accepts the Zimbra password.
LOCAL_USER="${TEST_USER_1}"
LOCAL_PASS="${TEST_PASS_1}"
if ensure_kin_test_mailbox "$LOCAL_USER" "$LOCAL_PASS" 2>/dev/null; then
  ok "Akun lokal siap: ${LOCAL_USER}"
else
  fail "Gagal menyiapkan akun lokal: ${LOCAL_USER}"
  exit 1
fi

# AD-backed mailbox: must already exist in Active Directory. Zimbra still needs
# a local account record; password below is a placeholder — login proof uses AD.
AD_PLACEHOLDER="KinAdPlaceholder-$(hostname -s)"
if zimbra_cmd zmprov ga "$AD_TEST_USER" >/dev/null 2>&1; then
  info "Akun AD-backed sudah ada di Zimbra: ${AD_TEST_USER}"
else
  if zimbra_cmd zmprov ca "$AD_TEST_USER" "$AD_PLACEHOLDER" displayName 'KIN AD auth test' >/dev/null; then
    ok "Akun AD-backed dibuat di Zimbra: ${AD_TEST_USER}"
  else
    fail "Gagal membuat akun AD-backed: ${AD_TEST_USER}"
    exit 1
  fi
fi

say "3. Verifikasi kedua jalur auth (zmmailbox / zimbra_user_auth_ok)"

auth_ok() {
  local user="$1" pass="$2" label="$3"
  if zimbra_user_auth_ok "$user" "$pass"; then
    ok "${label}: ${user}"
    return 0
  fi
  fail "${label} GAGAL: ${user}"
  # Surface a real auth error (zmmailbox), not obsolete zmprov auth usage text.
  zimbra_cmd zmmailbox -m "$user" -p "$pass" gaf 2>&1 | sed 's/^/    /' | tail -5 || true
  return 1
}

FAILS=0
auth_ok "$LOCAL_USER" "$LOCAL_PASS" "Auth lokal (fallback)" || FAILS=$((FAILS + 1))
auth_ok "$AD_TEST_USER" "$AD_TEST_PASS" "Auth AD LDAP" || FAILS=$((FAILS + 1))

echo
if [ "$FAILS" -eq 0 ]; then
  say "SELESAI — kedua jalur auth terverifikasi"
  exit 0
fi
fail "${FAILS} jalur auth gagal — periksa URL/bind DN/filter dan keanggotaan akun di AD"
exit 1
