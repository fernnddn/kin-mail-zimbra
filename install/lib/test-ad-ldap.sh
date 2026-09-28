#!/usr/bin/env bash
# =============================================================================
# lib/ad-ldap.sh - the directory read must not leak the password or trust
#                  anything that answers
#
# Two regressions this pins down, both of which shipped:
#
#   ldapsearch -w <password>      argv is world-readable in /proc, and the sync
#                                 timer reopened that window every few minutes
#   LDAPTLS_REQCERT=allow         "continue even if the certificate is wrong",
#                                 on the read that carries the service account's
#                                 password, after 14-ad-trust.sh had gone to the
#                                 trouble of proving which CA to trust
#
# The stubs record exactly what would have been executed, which is the only way
# to assert on argv and environment without a directory to talk to.
# =============================================================================
set -u
cd "$(dirname "$0")" || exit 1

PASS=0
FAIL=0
ok() {
  PASS=$((PASS + 1))
  printf 'ok    %s\n' "$1"
}
no() {
  FAIL=$((FAIL + 1))
  printf 'FAIL  %s\n' "$1"
  shift
  for l in "$@"; do printf '        %s\n' "$l"; done
}
eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "expected: $3" "actual:   $2"; fi
}
contains() {
  case "$2" in *"$3"*) ok "$1" ;; *) no "$1" "expected to contain: $3" "actual: $2" ;; esac
}
lacks() {
  case "$2" in *"$3"*) no "$1" "must NOT contain: $3" "actual: $2" ;; *) ok "$1" ;; esac
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- stubs -------------------------------------------------------------------
# ldapsearch records its argv and the LDAPTLS_* environment it was handed.
cat >"$WORK/ldapsearch" <<'STUB'
#!/usr/bin/env bash
{
  printf 'ARGV:'
  for a in "$@"; do printf ' [%s]' "$a"; done
  printf '\n'
  printf 'REQCERT:%s\n' "${LDAPTLS_REQCERT:-<unset>}"
  printf 'CACERT:%s\n' "${LDAPTLS_CACERT:-<unset>}"
} >>"$STUB_LOG"
# Echo back the password file's contents so a test can prove the bind actually
# receives the right bytes, and that no newline was appended.
for ((i = 1; i <= $#; i++)); do
  if [ "${!i}" = "-y" ]; then
    j=$((i + 1))
    printf 'PWFILE_BYTES:%s\n' "$(cat "${!j}")" >>"$STUB_LOG"
    printf 'PWFILE_MODE:%s\n' "$(stat -c '%a' "${!j}")" >>"$STUB_LOG"
    printf 'PWFILE_PATH:%s\n' "${!j}" >>"$STUB_LOG"
  fi
done
printf '%s' "${STUB_STDOUT:-}"
exit "${STUB_RC:-0}"
STUB
chmod +x "$WORK/ldapsearch"

# openssl s_client: CHAIN_OK / NAME_OK decide what it reports.
cat >"$WORK/openssl" <<'STUB'
#!/usr/bin/env bash
want_hostname=0
for a in "$@"; do [ "$a" = "-verify_hostname" ] && want_hostname=1; done
if [ "$want_hostname" = 1 ]; then
  # -verify_return_error: exit status is the answer.
  [ "${NAME_OK:-1}" = 1 ] && exit 0
  exit 1
fi
if [ "${CHAIN_OK:-1}" = 1 ]; then
  echo "    Verify return code: 0 (ok)"
else
  echo "    Verify return code: 21 (unable to verify the first certificate)"
fi
exit 0
STUB
chmod +x "$WORK/openssl"

export STUB_LOG="$WORK/log"
KIN_AD_LDAPSEARCH="$WORK/ldapsearch"
KIN_AD_OPENSSL="$WORK/openssl"
KIN_AD_CA_PEM="$WORK/ca.pem"
export KIN_AD_LDAPSEARCH KIN_AD_OPENSSL KIN_AD_CA_PEM
# shellcheck source=ad-ldap.sh
. ./ad-ldap.sh

reset() { : >"$STUB_LOG"; }
logged() { cat "$STUB_LOG" 2>/dev/null; }

echo "=== kin_ad_host_port ==="
eq "ldaps with explicit port"  "$(kin_ad_host_port 'ldaps://dc.example.test:636')" "dc.example.test 636"
eq "ldaps defaults to 636"     "$(kin_ad_host_port 'ldaps://dc.example.test')"     "dc.example.test 636"
eq "ldap defaults to 389"      "$(kin_ad_host_port 'ldap://dc.example.test')"      "dc.example.test 389"
eq "ldap with explicit port"   "$(kin_ad_host_port 'ldap://dc.example.test:3268')" "dc.example.test 3268"
eq "a trailing path is ignored" "$(kin_ad_host_port 'ldaps://dc.example.test:636/dc=x')" "dc.example.test 636"
# An IPv6 literal split on ":" would have become "[2001" - a host that resolves
# to nothing and a port that is not a number.
eq "IPv6 literal with port"    "$(kin_ad_host_port 'ldaps://[2001:db8::5]:636')"   "2001:db8::5 636"
eq "IPv6 literal without port" "$(kin_ad_host_port 'ldaps://[2001:db8::5]')"       "2001:db8::5 636"

echo
echo "=== the password never reaches argv ==="
reset
CHAIN_OK=1 NAME_OK=1 : >"$KIN_AD_CA_PEM"
export CHAIN_OK=1 NAME_OK=1
kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc,DC=example,DC=test' 'S3cret!#pw' verify \
  -LLL -b 'DC=example,DC=test' '(objectClass=user)' >/dev/null 2>&1
L=$(logged)
# Scoped to the ARGV line: the stub logs the password FILE's contents
# deliberately, a few assertions down, and a whole-log search finds that.
ARGV_LINE=$(sed -n 's/^ARGV://p' "$STUB_LOG" | head -1)
lacks    "the password is absent from argv"          "$ARGV_LINE" "S3cret!#pw"
lacks    "-w is not used at all"                     "$ARGV_LINE" "[-w]"
contains "the bind DN is passed"                     "$L" "[CN=svc,DC=example,DC=test]"
contains "-y names a password file"                  "$L" "[-y]"
contains "the file holds exactly the password"       "$L" "PWFILE_BYTES:S3cret!#pw"
contains "the file is 0600"                          "$L" "PWFILE_MODE:600"
contains "the caller's own arguments survive"        "$L" "[(objectClass=user)]"

# A trailing newline in the file becomes part of the password, and every bind
# fails with "invalid credentials" - which reads as a wrong password, not as a
# file-format mistake.
eq "no newline was appended to the password" \
  "$(grep -c '^PWFILE_BYTES:S3cret!#pw$' "$STUB_LOG")" "1"

pw_path=$(sed -n 's/^PWFILE_PATH://p' "$STUB_LOG" | head -1)
if [ -n "$pw_path" ] && [ ! -e "$pw_path" ]; then
  ok "the password file is removed afterwards"
else
  no "the password file is removed afterwards" "still present: $pw_path"
fi

echo
echo "=== an ldaps:// certificate is verified before the password is sent ==="
reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=1 NAME_OK=1
kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc' 'pw' verify -LLL >/dev/null 2>&1
L=$(logged)
eq       "pinned CA, name matches -> mode pinned"   "$KIN_AD_TLS_MODE" "pinned"
contains "REQCERT is require"                        "$L" "REQCERT:require"
contains "the pinned CA is the one used"             "$L" "CACERT:$KIN_AD_CA_PEM"

reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=1 NAME_OK=0
# RFC 5737 documentation space, deliberately, rather than an RFC1918 address.
# The repository hygiene gate strips the three private range bases - each with
# its own prefix length attached - and then fails the build on whatever private
# address is left. A range base with a port after it instead of a prefix length
# is not the base, so it survives the strip and goes red.
kin_ad_ldapsearch 'ldaps://192.0.2.10:636' 'CN=svc' 'pw' verify -LLL >/dev/null 2>&1
L=$(logged)
eq       "chain good, name wrong -> mode chain"      "$KIN_AD_TLS_MODE" "chain"
contains "the read still happens"                    "$L" "ARGV:"
contains "the pinned CA is still named"              "$L" "CACERT:$KIN_AD_CA_PEM"
contains "the operator is told which name to use"    "$KIN_AD_TLS_NOTE" "does not name"

reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=0 NAME_OK=0
# Decided in this shell first, exactly as 15-ad-sync.sh does it. Calling it
# only inside $( ) below would leave KIN_AD_TLS_MODE holding the PREVIOUS
# answer - which is how a "refuse" was read as the earlier "chain", and under
# `set -u` on a first call would have aborted the stage outright.
KIN_AD_TLS_FOR=""
kin_ad_tls_decision 'ldaps://dc.example.test:636' verify
out=$(kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc' 'pw' verify -LLL 2>&1)
rc=$?
eq  "chain bad -> exit 77"                           "$rc" "77"
eq  "chain bad -> mode refuse"                       "$KIN_AD_TLS_MODE" "refuse"
eq  "chain bad -> ldapsearch never ran"              "$(logged)" ""
contains "the refusal says what to do"               "$out" "14-ad-trust.sh"

echo
echo "=== no pinned CA: the system store, or a refusal ==="
rm -f "$KIN_AD_CA_PEM"
reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=1 NAME_OK=1
kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc' 'pw' verify -LLL >/dev/null 2>&1
eq       "a publicly-trusted certificate -> system"  "$KIN_AD_TLS_MODE" "system"
contains "REQCERT is still require"                  "$(logged)" "REQCERT:require"

reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=0
kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc' 'pw' verify -LLL >/dev/null 2>&1
rc=$?
eq  "nothing verifies it -> refuse"                  "$KIN_AD_TLS_MODE" "refuse"
eq  "nothing verifies it -> exit 77"                 "$rc" "77"
eq  "nothing verifies it -> nothing was sent"        "$(logged)" ""

echo
echo "=== plain ldap:// has no certificate to check ==="
reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=0 NAME_OK=0
kin_ad_ldapsearch 'ldap://dc.example.test:389' 'CN=svc' 'pw' verify -LLL >/dev/null 2>&1
L=$(logged)
eq       "ldap:// -> mode none"                      "$KIN_AD_TLS_MODE" "none"
eq       "ldap:// is not refused"                    "$(printf '%s' "$L" | grep -c 'ARGV:')" "1"
contains "REQCERT is left alone"                     "$L" "REQCERT:<unset>"
contains "the operator is warned it is unencrypted"  "$KIN_AD_TLS_NOTE" "unencrypted"
lacks    "and the password is still not in argv"     "$L" "[pw]"

echo
echo "=== bootstrap is the only unverified read, and only for the CA fetch ==="
reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=0 NAME_OK=0
kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc' 'pw' bootstrap -LLL >/dev/null 2>&1
L=$(logged)
eq       "bootstrap -> mode bootstrap"               "$KIN_AD_TLS_MODE" "bootstrap"
contains "bootstrap uses REQCERT=never"              "$L" "REQCERT:never"
lacks    "bootstrap still keeps the password out of argv" "$L" "[pw]"
contains "bootstrap says why it is unverified"       "$KIN_AD_TLS_NOTE" "proved afterwards"

echo
echo "=== the exit status of the real search is passed through ==="
reset
KIN_AD_TLS_FOR=""
export CHAIN_OK=1 NAME_OK=1
: >"$KIN_AD_CA_PEM"
STUB_RC=49 kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc' 'pw' verify -LLL >/dev/null 2>&1
eq "an LDAP error is not swallowed" "$?" "49"

STUB_STDOUT='dn: CN=Person' kin_ad_ldapsearch 'ldaps://dc.example.test:636' 'CN=svc' 'pw' verify -LLL 2>/dev/null |
  grep -q 'dn: CN=Person' && ok "the search output reaches the caller" ||
  no "the search output reaches the caller"

echo
echo "=== a missing ldapsearch is reported, not ignored ==="
KIN_AD_LDAPSEARCH="$WORK/does-not-exist"
kin_ad_ldapsearch 'ldap://dc.example.test' 'CN=svc' 'pw' verify >/dev/null 2>&1
eq "a missing client is exit 127" "$?" "127"
KIN_AD_LDAPSEARCH="$WORK/ldapsearch"

echo
echo "=== the callers actually use it ==="
cd ..
for f in 14-ad-trust.sh 15-ad-sync.sh; do
  if grep -q 'kin_ad_ldapsearch' "$f"; then
    ok "$f reads the directory through the helper"
  else
    no "$f reads the directory through the helper"
  fi
  if grep -qE '\-w "\$AD_SEARCH_BIND_PASSWORD"' "$f"; then
    no "$f no longer puts the password in argv"
  else
    ok "$f no longer puts the password in argv"
  fi
done
if grep -q 'REQCERT=allow' 15-ad-sync.sh; then
  no "15-ad-sync.sh does not accept any certificate"
else
  ok "15-ad-sync.sh does not accept any certificate"
fi
# Exactly one place in the product may skip verification, and it must be the
# CA fetch. Any other bootstrap caller is a hole.
n=$(grep -rl 'bootstrap' --include='1[0-9]-*.sh' . 2>/dev/null | wc -l)
eq "only one stage reads the directory unverified" "$n" "1"

# The bug class, not just the bug. Reading a helper's output variable after a
# command substitution has now silently lost the answer twice in this product -
# once as API_ERROR in the mail-gateway link, once as KIN_AD_TLS_MODE here -
# and the second time `set -u` turned it from a wrong message into an aborted
# stage. Any caller that captures kin_ad_ldapsearch must decide first.
for f in 14-ad-trust.sh 15-ad-sync.sh; do
  if grep -qE '=\$\(kin_ad_ldapsearch' "$f"; then
    if awk '/kin_ad_tls_decision/ { d = NR } /=\$\(kin_ad_ldapsearch/ { exit !(d && d < NR) }' "$f"; then
      ok "$f decides before it captures the search"
    else
      no "$f decides before it captures the search" \
        "KIN_AD_TLS_MODE set inside \$( ) is discarded, and reading it" \
        "afterwards under set -u aborts the stage"
    fi
  fi
done

echo
printf '%s\n' "----------------------------------------"
printf 'ad-ldap: %s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
