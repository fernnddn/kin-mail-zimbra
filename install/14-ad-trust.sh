#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 14 AD TRUST (make Zimbra trust the directory's LDAPS certificate)
#
#   sudo ./14-ad-trust.sh
#
# WHY THIS EXISTS
#
# Active Directory refuses a plain LDAP bind once "LDAP server signing
# requirements" is set to Require - the Windows default since the 2020
# hardening advisory. The supported answer is ldaps://, and the certificate
# behind it is issued by the domain's OWN private CA.
#
# Zimbra's LDAP client validates that certificate against Java's trust store,
# which has never heard of that CA. So the bind fails on TLS before any
# password is considered, and what the operator sees is an authentication
# failure - sending them to check the bind DN, the search base and the filter,
# all of which are correct. Measured on the lab AD, 24 Sep 2026:
#
#   against the system store   unable to get local issuer certificate
#   with the domain CA given   Verify return code: 0 (ok)
#
# Fixing it by hand meant exporting a certificate on Windows, copying it to the
# right Linux machine, and running zmcertmgr - four steps, on two operating
# systems, in a terminal. This does all of it from what the wizard already
# collected.
#
# WHICH MACHINE
#
# The trust store that matters belongs to mailboxd, which on a multi deployment
# is the mailbox. Run from the edge, this carries the work there over the same
# SSH channel the orchestrator uses.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# shellcheck source=lib/zimbra-store.sh
. ./lib/zimbra-store.sh
need_root

echo
say "Trusting the directory's certificate"

if [ "${AD_AUTH_ENABLED}" != "yes" ]; then
  ok "AD_AUTH_ENABLED=${AD_AUTH_ENABLED:-no} - nothing to trust."
  exit 0
fi
case "${AD_LDAP_URL}" in
  ldaps://*) ;;
  *)
    ok "AD_LDAP_URL is not ldaps:// - no certificate is involved."
    info "Plain ldap:// carries passwords unprotected and modern AD refuses it."
    exit 0
    ;;
esac

# host:port out of ldaps://host:port
_hp=${AD_LDAP_URL#ldaps://}
_hp=${_hp%%/*}
AD_HOST=${_hp%%:*}
AD_PORT=${_hp##*:}
[ "$AD_PORT" = "$AD_HOST" ] && AD_PORT=636
if [ -z "$AD_HOST" ]; then
  fail "Could not read a host out of AD_LDAP_URL (${AD_LDAP_URL})"
  exit 1
fi
info "Directory: ${AD_HOST}:${AD_PORT}"

# --- is it already trusted? ---------------------------------------------------
# Asked before anything is changed. A directory whose certificate comes from a
# public CA - or one already imported - needs nothing done, and importing again
# would be noise in the trust store.
trusted_here() {
  echo | timeout 15 openssl s_client -connect "${AD_HOST}:${AD_PORT}" 2>/dev/null |
    grep -q "Verify return code: 0"
}

say "1. Is the certificate already trusted?"
if trusted_here; then
  ok "Yes - the certificate chains to a CA this machine already trusts."
  info "Nothing to import. A public certificate on the domain controller"
  info "removes this step entirely."
  exit 0
fi
info "Not yet - the certificate is signed by a CA nothing here knows."

# --- fetch the CA -------------------------------------------------------------
# From the directory itself. Active Directory publishes its own CA certificate
# under Public Key Services, so the operator does not have to export anything
# on Windows and copy it across.
# An operator who was handed a CA file - their own internal root, an offline
# root that was never published, a certificate from a PKI that is not AD CS -
# supplies it here instead. Nothing about the proof below changes: whatever the
# source, it still has to verify what the directory actually presents.
say "2. Where the CA comes from"
CA_PEM="/opt/zimbra/conf/kin-ad-ca.cer"
TMP_CA=$(mktemp)
trap 'rm -f "$TMP_CA"' EXIT

# Zimbra ships its own OpenLDAP client, so this needs nothing installed. The
# machine has no ldap3 and no system ldapsearch, and adding a package to a mail
# server for one read would be the wrong trade.
#
# LDAPTLS_REQCERT=never for this one read on purpose: the whole point is that
# the certificate is not trusted yet. What comes back is a public certificate,
# and it is checked below to actually sign what the server presents - that is
# the proof, not the transport it arrived over.
if [ -n "${AD_CA_FILE}" ]; then
  if [ ! -r "$AD_CA_FILE" ]; then
    fail "AD_CA_FILE is set to ${AD_CA_FILE}, which cannot be read."
    info "Put the file where this machine can reach it, or clear AD_CA_FILE to"
    info "read the CA out of the directory instead."
    exit 1
  fi
  info "Using the file the operator supplied: ${AD_CA_FILE}"
  # PEM or DER - accept whichever they were given, rather than making the
  # difference their problem.
  if openssl x509 -in "$AD_CA_FILE" -out "$TMP_CA" 2>/dev/null ||
    openssl x509 -inform DER -in "$AD_CA_FILE" -out "$TMP_CA" 2>/dev/null; then
    ok "Read: $(openssl x509 -in "$TMP_CA" -noout -subject 2>/dev/null)"
  else
    fail "${AD_CA_FILE} is not a certificate this can read (tried PEM and DER)."
    exit 1
  fi
else
  info "No AD_CA_FILE set - reading the CA out of the directory."

LDAPSEARCH=/opt/zimbra/common/bin/ldapsearch
if [ ! -x "$LDAPSEARCH" ]; then
  fail "Zimbra's ldapsearch is not at ${LDAPSEARCH}"
  exit 1
fi
CA_BASE="CN=Certification Authorities,CN=Public Key Services,CN=Services,CN=Configuration,${AD_SEARCH_BASE}"
# LDIF folds long values onto continuation lines that begin with a space.
# Unfold first, or the base64 comes back in pieces.
CA_B64=$(LDAPTLS_REQCERT=never "$LDAPSEARCH" -LLL \
  -H "ldaps://${AD_HOST}:${AD_PORT}" \
  -D "$AD_SEARCH_BIND_DN" -w "$AD_SEARCH_BIND_PASSWORD" \
  -b "$CA_BASE" "(objectClass=certificationAuthority)" cACertificate 2>/dev/null |
  sed -e ':a' -e 'N' -e '$!ba' -e 's/\n //g' |
  sed -n 's/^cACertificate:: //p' | head -1)

if [ -z "$CA_B64" ]; then
  fail "Could not read the CA certificate out of the directory."
  info "Check AD_SEARCH_BIND_DN and AD_SEARCH_BIND_PASSWORD in ${CONF_FILE}."
  info "Or export the CA on Windows and import it on the mailbox:"
  info "  su - zimbra -c 'zmcertmgr addcacert /path/to/ca.cer'"
  exit 1
fi
if ! printf '%s' "$CA_B64" | base64 -d 2>/dev/null |
  openssl x509 -inform DER -out "$TMP_CA" 2>/dev/null; then
  fail "The directory returned something that is not a certificate."
  exit 1
fi

if [ ! -s "$TMP_CA" ]; then
  fail "The directory returned no CA certificate."
  exit 1
fi
  ok "Read: $(openssl x509 -in "$TMP_CA" -noout -subject 2>/dev/null || echo 'unreadable')"
fi

# --- does it actually sign what the server presents? --------------------------
# Checked, not assumed. A CA read from a connection that was not validated
# proves nothing on its own; this is what makes it evidence.
say "3. Does it sign the certificate the directory presents?"
if echo | timeout 15 openssl s_client -connect "${AD_HOST}:${AD_PORT}" -CAfile "$TMP_CA" 2>/dev/null |
  grep -q "Verify return code: 0"; then
  ok "Yes - this CA verifies the directory's certificate."
else
  fail "It does not. Refusing to import a certificate that proves nothing."
  info "The directory may present a certificate from a different CA."
  exit 1
fi

# --- import where mailboxd reads it -------------------------------------------
say "4. Importing into Zimbra's trust store"
import_locally() {
  install -o zimbra -g zimbra -m 0644 "$1" "$CA_PEM" || return 1
  su - zimbra -c "zmcertmgr addcacert ${CA_PEM}" || return 1
  su - zimbra -c "zmmailboxdctl restart" >/dev/null 2>&1 || true
  return 0
}

if kin_mailboxd_is_local; then
  if import_locally "$TMP_CA"; then
    ok "Imported and mailboxd restarted."
  else
    fail "zmcertmgr could not import the certificate."
    exit 1
  fi
else
  # The trust store belongs to mailboxd, which is on the other machine.
  #
  # Carried by the shared helper rather than by an SSH invocation of its own.
  # This stage had its own copy, and that copy tried MAILBOX_SSH_PASS and
  # stopped - the password the mailbox had BEFORE 02-prepare-os.sh changed it.
  # It was refused, the warning went past in a long deploy log, and the
  # certificate was never imported: authentication was configured against a
  # directory nothing trusted, so every sign-in failed (25 Sep 2026). The
  # helper tries both passwords, as the orchestrator always has.
  info "The trust store is on the mailbox; carrying this there."
  kin_run_stage_on_store 14-ad-trust.sh
  _rc=$?
  case "$_rc" in
    0) ok "Imported on the mailbox." ;;
    3)
      fail "Could not reach the mailbox, so the certificate was NOT imported."
      warn "Active Directory sign-in will fail until it is: nothing trusts the"
      warn "certificate the directory presents."
      info "Check MAILBOX_SSH_PASS and KIN_USER_PASS in ${CONF_FILE}, then run"
      info "ON THE MAILBOX:  sudo ${KIN_MAIL_INSTALL_DIR}/14-ad-trust.sh"
      exit 1
      ;;
    *)
      fail "The certificate was not imported on the mailbox (exit ${_rc})."
      warn "Active Directory sign-in will fail until it is."
      exit 1
      ;;
  esac
fi

echo
say "DONE"
info "AD sign-in can now complete its TLS handshake. If a login still fails,"
info "it is the password, the bind template or the filter - not the certificate."
exit 0
