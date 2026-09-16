#!/usr/bin/env bash
#
# Tests for the generated Zimbra defaults files.
#
# These files ARE the install: the installer sources one, installs whatever
# INSTALL_PACKAGES names, then hands the same file to zmsetup.pl. A wrong value
# is not a syntax error - it produces a machine that installs cleanly, reports
# success, and is the wrong node. The expensive ones are silent: an edge that
# mints its own LDAP password binds to nothing, and the symptom is mail not
# moving, hours later and on a different machine.
#
# Key names are from zcs-10.1.20's own zmsetup.pl, not from documentation.
# shellcheck disable=SC2034
set -u

LIB="$(cd "$(dirname "$0")" && pwd)/zcs-defaults.sh"
[ -f "$LIB" ] || { echo "missing $LIB" >&2; exit 1; }
# shellcheck disable=SC1090
. "$LIB"

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/kin-zcs-defaults.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

# Fixture deployment. Documentation ranges and example.test throughout.
MAIL_DOMAIN="example.test"
MAILBOX_HOST="store.example.test"
EDGE_HOST="mail.example.test"
ADMIN_PASS="AdminPw12345"
ZIMBRA_TZ_NAME="Asia/Bangkok"
KIN_LDAP_ADMIN_PASS="ldapadminsecret"
KIN_LDAP_ROOT_PASS="ldaprootsecret"
KIN_LDAP_REP_PASS="ldaprepsecret"
KIN_LDAP_POST_PASS="ldappostsecret"
KIN_LDAP_AMAVIS_PASS="ldapamavissecret"
KIN_LDAP_NGINX_PASS="ldapnginxsecret"

MBOX="$TMP/mailbox.cfg"
EDGE="$TMP/edge.cfg"
kin_zcs_defaults_mailbox "$MBOX"
kin_zcs_defaults_edge "$EDGE"

has()    { grep -q "^$2=" "$1" && ok "$3" || bad "$3 (missing $2)"; }
val()    { sed -n "s/^$2=\"\(.*\)\"$/\1/p" "$1"; }
eq()     { [ "$(val "$1" "$2")" = "$3" ] && ok "$4" || bad "$4: $2 is '$(val "$1" "$2")', want '$3'"; }
lacks()  { grep -q "^$2=" "$1" && bad "$3 (found $2)" || ok "$3"; }

# --- the file must be a shell file the installer can source ------------------
for f in "$MBOX" "$EDGE"; do
  bash -n "$f" 2>/dev/null && ok "$(basename "$f") is valid shell" \
    || bad "$(basename "$f") is not sourceable - the installer would die on it"
done

# --- the file must not be silently truncated ---------------------------------
# The generator writes through unquoted heredocs, so a bare $word anywhere in
# them - including inside a comment - is expanded. Under `set -u`, which every
# stage runs with, an unset one aborts the heredoc mid-write and leaves a file
# that is still valid shell and still looks plausible, just missing everything
# after that point. A comment mentioning "$newinstall" cost exactly that.
#
# LDAPHOST is the canary: it sits after the long explanatory comment block, so
# a truncated file loses it while keeping the lines above.
for f in "$MBOX" "$EDGE"; do
  keys=$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$f")
  if [ "$keys" -ge 25 ]; then
    ok "$(basename "$f") has ${keys} settings, so it was not truncated"
  else
    bad "$(basename "$f") has only ${keys} settings - the heredoc stopped early"
  fi
done
if grep -qE '^\s*#.*[^\\$]\$[A-Za-z_]' "$LIB"; then
  bad "a comment in the generator contains an unescaped \$word; set -u will truncate the output"
else
  ok "no unescaped \$word in the generator's comments"
fi

# --- secrets must not be world readable -------------------------------------
# The installer sources this as root and it carries the directory's passwords.
for f in "$MBOX" "$EDGE"; do
  mode=$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")
  [ "$mode" = 600 ] && ok "$(basename "$f") is mode 600" \
    || bad "$(basename "$f") is mode ${mode}, and it holds LDAP passwords"
done

# --- roles must differ in the one place that decides the role ---------------
case "$(val "$MBOX" INSTALL_PACKAGES)" in
  *zimbra-ldap*) ok "mailbox installs the directory" ;;
  *) bad "mailbox has no zimbra-ldap" ;;
esac
case "$(val "$MBOX" INSTALL_PACKAGES)" in
  *zimbra-store*) ok "mailbox installs the store" ;;
  *) bad "mailbox has no zimbra-store" ;;
esac
case "$(val "$MBOX" INSTALL_PACKAGES)" in
  *zimbra-mta*) bad "mailbox installs an MTA - that is the edge's job" ;;
  *) ok "mailbox installs no MTA" ;;
esac
case "$(val "$EDGE" INSTALL_PACKAGES)" in
  *zimbra-mta*) ok "edge installs the MTA" ;;
  *) bad "edge has no zimbra-mta" ;;
esac
case "$(val "$EDGE" INSTALL_PACKAGES)" in
  *zimbra-proxy*) ok "edge installs the proxy" ;;
  *) bad "edge has no zimbra-proxy" ;;
esac
# The whole point of the split: the exposed machine holds no mail and no accounts.
case "$(val "$EDGE" INSTALL_PACKAGES)" in
  *zimbra-store*) bad "edge installs a store - it would hold mailboxes" ;;
  *) ok "edge installs no store" ;;
esac
case "$(val "$EDGE" INSTALL_PACKAGES)" in
  *zimbra-ldap*) bad "edge installs the directory - it would hold every account" ;;
  *) ok "edge installs no directory" ;;
esac
# memcached is not in the tarball; naming it makes the installer fetch it.
case "$(val "$EDGE" INSTALL_PACKAGES)" in
  *zimbra-memcached*) ok "edge names memcached so the installer fetches it from the repo" ;;
  *) bad "edge has no zimbra-memcached; the proxy needs it" ;;
esac

# --- each machine must be told who it is ------------------------------------
eq "$MBOX" HOSTNAME "store.example.test" "mailbox knows its own name"
eq "$EDGE" HOSTNAME "mail.example.test"  "edge knows its own name"

# --- both must point at the same directory ----------------------------------
eq "$MBOX" LDAPHOST "store.example.test" "mailbox is its own LDAP host"
eq "$EDGE" LDAPHOST "store.example.test" "edge points at the mailbox for LDAP"
eq "$EDGE" LDAPPORT "389"                "edge uses the standard LDAP port"

# --- the join secrets, and the flags that make them count -------------------
# Without the *SET flag zmsetup treats the value as unset and generates its own.
# The node then installs cleanly and cannot bind.
eq "$EDGE" LDAPADMINPASS  "ldapadminsecret"  "edge carries the directory admin password"
eq "$EDGE" LDAPROOTPASS   "ldaprootsecret"   "edge carries the root password"
eq "$EDGE" LDAPREPPASS    "ldaprepsecret"    "edge carries the replication password"
eq "$EDGE" LDAPPOSTPASS   "ldappostsecret"   "edge carries the postfix password"
eq "$EDGE" LDAPAMAVISPASS "ldapamavissecret" "edge carries the amavis password"
eq "$EDGE" ldap_nginx_password "ldapnginxsecret" "edge carries the nginx password (lower case, as this build spells it)"
for flag in LDAPADMINPASSSET LDAPROOTPASSSET LDAPREPPASSSET LDAPPOSTPASSSET LDAPAMAVISPASSSET LDAPNGINXPASSSET; do
  eq "$EDGE" "$flag" "yes" "edge marks ${flag}"
done

# The mailbox must carry them too. zmsetup hands LDAPROOTPASS and LDAPADMINPASS
# straight to zmldapinit when it creates the directory (zmsetup.pl:5451), so
# leaving them empty does not mean "generate one" - it creates a directory with
# an empty password, and the edge then has nothing to join with.
eq "$MBOX" LDAPADMINPASS "ldapadminsecret" "mailbox sets the directory admin password"
eq "$MBOX" LDAPROOTPASS  "ldaprootsecret"  "mailbox sets the directory root password"
eq "$MBOX" LDAPADMINPASSSET "yes" "mailbox marks LDAPADMINPASSSET"

# Both machines must present the SAME values or the bind fails.
for k in LDAPADMINPASS LDAPROOTPASS LDAPREPPASS LDAPPOSTPASS LDAPAMAVISPASS ldap_nginx_password; do
  if [ "$(val "$MBOX" "$k")" = "$(val "$EDGE" "$k")" ]; then
    ok "both nodes agree on ${k}"
  else
    bad "${k} differs between the two nodes - the edge could not bind"
  fi
done

# --- the secret store ---------------------------------------------------------
# Generated once and reused: the edge is built minutes after the mailbox, and a
# second set of passwords is one the directory has never heard of.
STORE="$TMP/ldap-secrets"
( unset KIN_LDAP_ADMIN_PASS KIN_LDAP_ROOT_PASS KIN_LDAP_REP_PASS \
        KIN_LDAP_POST_PASS KIN_LDAP_AMAVIS_PASS KIN_LDAP_NGINX_PASS
  kin_zcs_ensure_ldap_secrets "$STORE"
  first="$KIN_LDAP_ADMIN_PASS"
  unset KIN_LDAP_ADMIN_PASS
  kin_zcs_ensure_ldap_secrets "$STORE"
  [ "$first" = "$KIN_LDAP_ADMIN_PASS" ] && echo SAME || echo DIFFERENT
) > "$TMP/reuse" 2>/dev/null
grep -q SAME "$TMP/reuse" && ok "a second call reuses the stored passwords" \
  || bad "the passwords changed between calls - the edge would not bind"

mode=$(stat -c %a "$STORE" 2>/dev/null || stat -f %Lp "$STORE")
[ "$mode" = 600 ] && ok "the secret store is mode 600" || bad "secret store is mode ${mode}"

# Zimbra's installer rejects several metacharacters, and these values also pass
# through shell single quotes inside zmsetup.
secret=$(kin_zcs_ldap_secret)
case "$secret" in
  *[!A-Za-z0-9]*) bad "generated password contains a character the installer may reject: $secret" ;;
  *) ok "generated passwords are letters and digits only" ;;
esac
[ "${#secret}" -ge 20 ] && ok "generated passwords are at least 20 characters" \
  || bad "generated password is only ${#secret} characters"

# --- domain and admin are created once, on the mailbox only -----------------
eq "$MBOX" CREATEDOMAIN "example.test"        "mailbox creates the domain"
eq "$MBOX" CREATEADMIN  "admin@example.test"  "mailbox creates the admin account"
eq "$EDGE" CREATEDOMAIN "no"                  "edge does not create a second domain"
eq "$EDGE" CREATEADMIN  "no"                  "edge does not create a second admin"

# --- who answers route lookups, and who does not ----------------------------
eq "$MBOX" zimbraReverseProxyLookupTarget "TRUE"  "mailbox answers proxy route lookups"
eq "$EDGE" zimbraReverseProxyLookupTarget "FALSE" "edge does not answer its own lookups"
eq "$MBOX" zimbraMtaAuthTarget "TRUE"  "mailbox is the auth target"
eq "$EDGE" zimbraMtaAuthTarget "FALSE" "edge is not"

# --- install.sh's own findings must not be overridden ------------------------
# UPGRADE and REMOVE read like answers but they are what utilfunc.sh works out
# by inspecting the machine. Setting UPGRADE="yes" here made a clean install log
# every package as UPGRADED instead of INSTALLED; zmsetup then derived
# $newinstall = 0 from zimbra-core's history entry, and $newinstall is what
# gates zmldapinit. The directory was never created and every later step failed
# with an LDAP "Connection refused" that had nothing to do with LDAP.
for f in "$MBOX" "$EDGE"; do
  lacks "$f" UPGRADE "$(basename "$f") does not override the upgrade detection"
  lacks "$f" REMOVE  "$(basename "$f") does not override the removal detection"
done

# --- telemetry stays off ----------------------------------------------------
# The interactive installer asks this as "Notify Zimbra of your installation?"
# and answering yes mails the admin address to Zimbra.
eq "$MBOX" VERSIONUPDATECHECKS "FALSE" "mailbox does not phone home"
eq "$EDGE" VERSIONUPDATECHECKS "FALSE" "edge does not phone home"

# --- the admin password must reach the file intact --------------------------
eq "$MBOX" CREATEADMINPASS "AdminPw12345" "the admin password is written verbatim"

# --- the validator must catch the failures that are silent ------------------
refuses() {
  local label="$1" f="$2" want="$3" got
  if got=$(kin_zcs_defaults_problem "$f"); then
    case "$got" in
      *"$want"*) ok "validator refuses: $label" ;;
      *) bad "validator refused $label for the wrong reason: $got" ;;
    esac
  else
    bad "validator accepted a broken file: $label"
  fi
}

if kin_zcs_defaults_problem "$MBOX" >/dev/null; then
  bad "validator rejected a good mailbox file: $(kin_zcs_defaults_problem "$MBOX")"
else
  ok "validator accepts the mailbox file"
fi
if kin_zcs_defaults_problem "$EDGE" >/dev/null; then
  bad "validator rejected a good edge file: $(kin_zcs_defaults_problem "$EDGE")"
else
  ok "validator accepts the edge file"
fi

cp "$EDGE" "$TMP/nopass.cfg"
sed -i 's/^LDAPADMINPASSSET=.*/LDAPADMINPASSSET="no"/' "$TMP/nopass.cfg"
refuses "an edge that will mint its own LDAP password" "$TMP/nopass.cfg" "LDAPADMINPASSSET"

cp "$EDGE" "$TMP/emptypass.cfg"
sed -i 's/^LDAPADMINPASS=.*/LDAPADMINPASS=""/' "$TMP/emptypass.cfg"
refuses "an edge with an empty LDAP password" "$TMP/emptypass.cfg" "empty LDAPADMINPASS"

cp "$MBOX" "$TMP/both.cfg"
sed -i 's/^INSTALL_PACKAGES=.*/INSTALL_PACKAGES="zimbra-core zimbra-ldap zimbra-mta"/' "$TMP/both.cfg"
refuses "a file that is a single appliance, not a split node" "$TMP/both.cfg" "single appliance"

cp "$MBOX" "$TMP/nopkgs.cfg"
sed -i '/^INSTALL_PACKAGES=/d' "$TMP/nopkgs.cfg"
refuses "a file with no package list" "$TMP/nopkgs.cfg" "package menu"

cp "$MBOX" "$TMP/nohost.cfg"
sed -i '/^HOSTNAME=/d' "$TMP/nohost.cfg"
refuses "a file that does not say which machine it is for" "$TMP/nohost.cfg" "HOSTNAME"

refuses "a file that does not exist" "$TMP/absent.cfg" "not readable"

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
