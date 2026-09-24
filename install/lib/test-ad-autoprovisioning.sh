#!/usr/bin/env bash
# Mailboxes created from Active Directory, and why they were not being.
#
# THE REPORT THIS EXISTS FOR
#
# QA, 24 September 2026: "Akun yang ada di AD sebelum deploy tidak ada di
# console 7071 - Tidak sesuai." The operator had configured AD correctly and
# concluded, reasonably, that the sync was broken.
#
# Nothing was broken. zimbraAuthMech=ad delegates the PASSWORD CHECK and
# nothing else - it never reads the directory's account list. Someone who
# exists in AD therefore has no mailbox and cannot receive mail, and no setting
# in the auth path changes that, because account creation is not part of it.
#
# Zimbra's own answer is auto-provisioning, and this product was not using it.
# These pin the shape of that: off unless asked for, every attribute checked to
# exist before it is written, and never fatal - a mailbox-creation convenience
# must not be able to stop mail being installed.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/../06-hybrid-auth.sh"
C="$(pwd)/../00-config.sh"
for f in "$S" "$C"; do
  [ -f "$f" ] || { printf 'FAIL %s not found\n' "$f"; exit 1; }
done
bash -n "$S" && pass "06-hybrid-auth parses" || bad "06 syntax error"
bash -n "$C" && pass "00-config parses" || bad "00-config syntax error"

# --- off unless asked for -----------------------------------------------------
# Creating mailboxes spends contracted seats. That is not a side effect to hand
# anyone who merely turned AD authentication on.
if grep -q ': "${AD_AUTOPROV_MODE:=}"' "$C"; then
  pass "AD_AUTOPROV_MODE defaults to empty"
else
  bad "auto-provisioning has no empty default; existing installs would change behaviour"
fi
if grep -q 'if \[ "$AD_APPLY_OK" -eq 1 \] && \[ -n "${AD_AUTOPROV_MODE}" \]; then' "$S"; then
  pass "it only runs when a mode was chosen"
else
  bad "it can run without the operator choosing a mode"
fi
# And when it is off, the operator is told WHY accounts do not appear - the
# whole point of the QA report.
if grep -q 'it does not create them' "$S"; then
  pass "an empty mode explains that AD does not create accounts"
else
  bad "silence leaves the operator with the same wrong conclusion"
fi

# --- every attribute checked before it is written -----------------------------
# These names are stable across Zimbra 8-10. "Stable" is not "verified", and a
# name this build does not have would half-configure the directory.
if grep -q 'autoprov_supported() {' "$S" && grep -q 'zmprov desc -a' "$S"; then
  pass "each attribute is checked to exist on this build"
else
  bad "attributes are written without checking the build supports them"
fi
for attr in zimbraAutoProvMode zimbraAutoProvAuthMech zimbraAutoProvLdapURL \
  zimbraAutoProvLdapAdminBindDn zimbraAutoProvLdapAdminBindPassword \
  zimbraAutoProvLdapSearchBase zimbraAutoProvLdapSearchFilter \
  zimbraAutoProvAccountNameMap; do
  if grep -q "$attr" "$S"; then
    pass "sets ${attr}"
  else
    bad "does not set ${attr}"
  fi
done
# The check list and the write list must not drift apart.
checked=$(sed -n '/for attr in zimbraAutoProvMode/,/done/p' "$S" | grep -c 'zimbraAutoProv')
if [ "$checked" -ge 3 ]; then
  pass "the pre-flight checks more than one attribute"
else
  bad "only a token attribute is checked before writing the rest"
fi

# --- reuses the auth settings rather than asking twice ------------------------
# A second copy of the AD URL, base and bind credentials is a second thing to
# get wrong and a second thing to keep in step.
for v in AD_LDAP_URL AD_SEARCH_BASE AD_SEARCH_BIND_DN AD_SEARCH_BIND_PASSWORD AD_SEARCH_FILTER; do
  if sed -n '/^apply_autoprov() {/,/^}/p' "$S" | grep -q "\$$v"; then
    pass "reuses ${v}"
  else
    bad "does not reuse ${v}; it would have to be configured twice"
  fi
done

# --- never fatal --------------------------------------------------------------
# Mail delivery does not depend on this. A convenience that can abort the
# install is a worse trade than a convenience that does not happen.
block=$(sed -n '/^apply_autoprov() {/,/^}/p' "$S")
if printf '%s' "$block" | grep -qE '^\s*exit [1-9]'; then
  bad "auto-provisioning can abort the stage"
else
  pass "a failure here never aborts the stage"
fi
if printf '%s' "$block" | grep -q 'warn "Could not apply auto-provisioning'; then
  pass "a failed apply is reported rather than swallowed"
else
  bad "a failed apply is silent"
fi

# --- checked, not announced ---------------------------------------------------
if printf '%s' "$block" | grep -q 'zmprov gd ${MAIL_DOMAIN} zimbraAutoProvMode'; then
  pass "reads the mode back from the directory before claiming it is on"
else
  bad "claims auto-provisioning is active without checking"
fi

# --- the seat cost is stated --------------------------------------------------
# Contracted seats are a commercial limit. Spending them automatically without
# saying so is how a deployment quietly breaches its own contract.
if printf '%s' "$block" | grep -q 'spends one contracted seat'; then
  pass "the seat cost is stated where it is enabled"
else
  bad "mailboxes are created automatically with no mention of the seat cost"
fi

# --- the failure an ldaps:// URL adds ----------------------------------------
# AD signs its LDAPS certificate with the domain's own private CA. Zimbra
# validates against Java's trust store, which has never heard of it, so the
# bind fails on TLS before the password is considered - and surfaces as an auth
# failure that sends the reader to check the bind DN and filter, which are fine.
if grep -q 'ldaps://\*)' "$S"; then
  pass "an ldaps:// URL gets its own explanation when the bind fails"
else
  bad "an ldaps:// TLS failure is reported as a generic auth failure"
fi
if grep -q 'zmcertmgr addcacert' "$S"; then
  pass "the fix names the command that imports the CA"
else
  bad "the operator is not told how to make Zimbra trust the CA"
fi
if grep -q 'on the MAILBOX node' "$S"; then
  pass "and says which machine to run it on"
else
  bad "does not say which node holds the trust store"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All AD auto-provisioning tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
