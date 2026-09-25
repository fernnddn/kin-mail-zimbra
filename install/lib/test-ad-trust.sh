#!/usr/bin/env bash
# Making Zimbra trust the directory's LDAPS certificate, without a terminal.
#
# WHAT THIS REPLACED
#
# Active Directory refuses a plain LDAP bind once signing is required - the
# Windows default since 2020 - so the URL has to be ldaps://. The certificate
# behind it is issued by the domain's own private CA, which Java's trust store
# has never heard of, so the bind fails on TLS before any password is
# considered. The operator sees an authentication failure and goes to check the
# bind DN, the search base and the filter, all of which are correct.
#
# Fixing it by hand was: export a certificate on Windows, copy it to the right
# Linux machine, run zmcertmgr, restart mailboxd. Four steps, two operating
# systems, and the operator asked not to have to touch a Linux terminal at all.
#
# Measured on the lab AD, 24 Sep 2026: the domain controller sends only its own
# certificate - no chain - so the CA cannot be scraped off the TLS handshake.
# It is read out of the directory instead, where AD publishes it.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/../14-ad-trust.sh"
H="$(pwd)/../06-hybrid-auth.sh"
[ -f "$S" ] || { printf 'FAIL 14-ad-trust.sh not found\n'; exit 1; }
bash -n "$S" && pass "14-ad-trust parses" || bad "syntax error"
[ -x "$S" ] && pass "is executable" || bad "is not executable"

# --- does nothing it should not ----------------------------------------------
if grep -q 'AD_AUTH_ENABLED.*!= "yes"' "$S"; then
  pass "does nothing when AD is not configured"
else
  bad "runs even when the deployment has no AD"
fi
if grep -q 'ldaps://\*) ;;' "$S"; then
  pass "does nothing for a plain ldap:// URL"
else
  bad "tries to import a certificate where none is involved"
fi
# A public certificate needs no import at all - and the operator asked whether
# their own SSL could be used, which is exactly this path.
if grep -q 'trusted_here' "$S" && grep -q 'Verify return code: 0' "$S"; then
  pass "checks whether the certificate is already trusted before changing anything"
else
  bad "imports without asking whether it was needed"
fi

# --- no new dependency --------------------------------------------------------
# The mail store has neither ldap3 nor a system ldapsearch. Adding a package to
# a mail server for one read would be the wrong trade; Zimbra ships its own.
if grep -q '/opt/zimbra/common/bin/ldapsearch' "$S"; then
  pass "uses the OpenLDAP client Zimbra already ships"
else
  bad "does not use Zimbra's bundled ldapsearch"
fi
if grep -qE 'import ldap3|apt-get install|apt install' "$S"; then
  bad "pulls in a dependency the mail store does not have"
else
  pass "installs nothing"
fi
# LDIF folds long values across continuation lines; base64 read naively comes
# back in pieces and decodes to nothing.
if grep -q "s/\\\\n //g" "$S"; then
  pass "unfolds LDIF before reading the base64"
else
  bad "reads folded LDIF, so the certificate would arrive truncated"
fi

# --- the certificate is proved, not trusted on sight --------------------------
# It is fetched over a connection that was deliberately not validated, so on its
# own it proves nothing. What makes it evidence is that it verifies the
# certificate the server actually presents.
if grep -q -- '-CAfile "\$TMP_CA"' "$S"; then
  pass "checks the CA actually signs what the directory presents"
else
  bad "imports a certificate without proving it belongs to this directory"
fi
if grep -q 'Refusing to import a certificate that proves nothing' "$S"; then
  pass "refuses rather than importing on faith"
else
  bad "no refusal path when the CA does not match"
fi

# --- lands where mailboxd reads it -------------------------------------------
# The trust store belongs to mailboxd. On a multi deployment that is the other
# machine, and an import on the edge would change nothing while reporting success.
if grep -q 'kin_mailboxd_is_local' "$S"; then
  pass "knows which machine holds the trust store"
else
  bad "imports wherever it happens to run"
fi
if grep -q 'zmcertmgr addcacert' "$S" && grep -q 'zmmailboxdctl restart' "$S"; then
  pass "imports and restarts mailboxd, which is what makes it take effect"
else
  bad "imports without restarting, so nothing changes until something else does"
fi
if sed -n '/^else$/,$p' "$S" | grep -q 'sshpass'; then
  pass "carries the work to the mailbox when run from the edge"
else
  bad "run from the edge it would silently do nothing useful"
fi

# --- wired in so nobody has to know it exists --------------------------------
# The whole point is that the operator never runs this by hand.
if grep -q '14-ad-trust.sh' "$H"; then
  pass "the AD stage runs it automatically"
else
  bad "nothing runs it; the operator is back in a terminal"
fi
if sed -n '/14-ad-trust.sh/,+2p' "$H" | grep -q 'warn '; then
  pass "a failure warns rather than stopping the AD stage"
else
  bad "a certificate step can abort authentication setup"
fi

# --- three shapes of customer -------------------------------------------------
# A public certificate on the DC needs nothing. AD CS publishes its CA in the
# directory. A customer's own internal root may be in neither - and that is the
# one the operator asked about, having an existing SSL of their own.
if grep -q 'AD_CA_FILE' "$S"; then
  pass "an operator-supplied CA file is accepted"
else
  bad "a customer's own root CA cannot be used"
fi
if grep -q 'openssl x509 -inform DER' "$S"; then
  pass "takes the file as PEM or DER, rather than making the difference their problem"
else
  bad "only one certificate encoding is accepted"
fi
if grep -q 'AD_CA_FILE is set to' "$S"; then
  pass "an unreadable AD_CA_FILE is refused with the path named"
else
  bad "a missing CA file fails without saying which file"
fi
# Whatever the source, the proof is the same.
if sed -n '/does it actually sign/,/^fi$/p' "$S" | grep -q -- '-CAfile'; then
  pass "a supplied file is proved against the directory too, not trusted on sight"
else
  bad "a supplied CA is imported without checking it belongs to this directory"
fi
C="$(pwd)/../00-config.sh"
if grep -q ': "${AD_CA_FILE:=}"' "$C"; then
  pass "AD_CA_FILE defaults to empty, so nothing changes for anyone else"
else
  bad "AD_CA_FILE has no default"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All AD trust tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
