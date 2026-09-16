#!/usr/bin/env bash
# Tests for the unattended-install capability check.
#
# That check is the gate the whole split topology stands on: if it says USABLE
# and is wrong, a multi-server install gets built on a path that does not exist,
# and the discovery happens halfway through installing Zimbra on a customer's
# two machines. So the check itself is tested here against built fixtures - a
# build that supports the unattended path and several that do not - because a
# gate that always says yes is indistinguishable from no gate at all.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK="${DIR}/../check-unattended-install.sh"
[ -f "$CHECK" ] || { echo "missing $CHECK" >&2; exit 1; }

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "SKIP: dpkg-deb not available, cannot build package fixtures"
  exit 0
fi

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; }

ROOT=$(mktemp -d "${TMPDIR:-/tmp}/kin-unattended-fixture.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT

# build_fixture <dir> <zmsetup-flavour> [omit-package...]
#
# Produces a tree shaped like an extracted Zimbra tarball. The flavour decides
# whether zmsetup.pl looks like one that can be driven from a config file.
build_fixture() {
  local dest="$1" flavour="$2"; shift 2
  local omit=" $* "
  mkdir -p "$dest/packages" "$dest/util"

  # Shaped like the real installer's contract: a defaults file switches it to
  # non-interactive, and the same file is handed to zmsetup.pl afterwards.
  cat > "$dest/install.sh" <<'SH'
#!/bin/bash
# Fixture standing in for Zimbra's installer.
usage() { echo "usage: install.sh [-s -x -h] [defaultsfile]"; }
SOFTWAREONLY="no"
while [ $# -gt 0 ]; do
  case "$1" in
    -s) SOFTWAREONLY="yes" ;;
    --platform-override) ALLOW_PLATFORM_OVERRIDE=yes ;;
    *) DEFAULTFILE=$1 ;;
  esac
  shift
done
if [ "x$DEFAULTFILE" != "x" ]; then
  AUTOINSTALL="yes"
else
  AUTOINSTALL="no"
fi
if [ $AUTOINSTALL = "no" ]; then
  getInstallPackages
fi
if [ $SOFTWAREONLY = "yes" ]; then
  exit 0
fi
if [ "x$DEFAULTFILE" != "x" ]; then
	/opt/zimbra/libexec/zmsetup.pl -c $DEFAULTFILE
else
	/opt/zimbra/libexec/zmsetup.pl
fi
SH
  chmod +x "$dest/install.sh"

  # INSTALL_PACKAGES lives here, in the installer's shell, not among the
  # zmsetup.pl config keys.
  cat > "$dest/util/globals.sh" <<'SH'
CORE_PACKAGES="zimbra-core"
INSTALL_PACKAGES="zimbra-core"
SH

  local build="$ROOT/build.$$"
  rm -rf "$build"
  mkdir -p "$build/DEBIAN" "$build/opt/zimbra/libexec"
  cat > "$build/DEBIAN/control" <<'CTL'
Package: zimbra-core
Version: 1.0
Architecture: all
Maintainer: fixture <fixture@example.test>
Description: fixture core package
CTL

  case "$flavour" in
  good)
    cat > "$build/opt/zimbra/libexec/zmsetup.pl" <<'PL'
#!/usr/bin/perl
use Getopt::Long;
GetOptions("c=s" => \$configFile, "h" => \$help);
my %config = ();
$config{INSTALL_PACKAGES};
$config{HOSTNAME};
$config{LDAPHOST};
$config{LDAPPORT};
$config{LDAPADMINPASS};
$config{LDAPROOTPASS};
$config{LDAPREPPASS};
$config{LDAPPOSTPASS};
$config{LDAPAMAVISPASS};
$config{ldap_nginx_password};
$config{CREATEADMIN};
$config{CREATEADMINPASS};
$config{TIMEZONEID};
$config{CREATEDOMAIN};
$config{SMTPHOST};
PL
    ;;
  no_config_opt)
    # Configuration exists but can only be reached interactively.
    cat > "$build/opt/zimbra/libexec/zmsetup.pl" <<'PL'
#!/usr/bin/perl
my %config = ();
$config{INSTALL_PACKAGES};
$config{HOSTNAME};
$config{LDAPHOST};
$config{LDAPPORT};
$config{LDAPADMINPASS};
$config{LDAPROOTPASS};
$config{LDAPREPPASS};
$config{LDAPPOSTPASS};
$config{LDAPAMAVISPASS};
$config{LDAPNGINXPASS};
$config{CREATEADMIN};
$config{TIMEZONEID};
PL
    ;;
  no_ldap_keys)
    # Takes a config file, but has no notion of joining another node's LDAP.
    cat > "$build/opt/zimbra/libexec/zmsetup.pl" <<'PL'
#!/usr/bin/perl
use Getopt::Long;
GetOptions("c=s" => \$configFile);
my %config = ();
$config{INSTALL_PACKAGES};
$config{HOSTNAME};
$config{CREATEADMIN};
$config{CREATEADMINPASS};
$config{TIMEZONEID};
$config{CREATEDOMAIN};
$config{SMTPHOST};
$config{SNMPTRAPHOST};
$config{AVDOMAIN};
$config{AVUSER};
$config{MAILBOXD_MEMORY};
$config{MYSQLMEMORYPERCENT};
PL
    ;;
  absent)
    rm -f "$build/opt/zimbra/libexec/zmsetup.pl"
    ;;
  esac

  dpkg-deb -b "$build" "$dest/packages/zimbra-core_1.0_all.deb" >/dev/null 2>&1
  rm -rf "$build"

  # Role packages. Content is irrelevant; the check only asks whether they exist.
  local p
  for p in zimbra-ldap zimbra-store zimbra-apache zimbra-spell \
           zimbra-mta zimbra-proxy zimbra-memcached; do
    case "$omit" in *" $p "*) continue ;; esac
    : > "$dest/packages/${p}_1.0_all.deb"
  done
}

# Runs the check with a scratch cwd, so the key list it writes never lands in
# the repository.
run_check() {
  local fixture="$1" out rc
  local cwd="$ROOT/cwd.$$"
  mkdir -p "$cwd"
  out=$(cd "$cwd" && KIN_MAIL_CONFIG=/nonexistent bash "$CHECK" "$fixture" 2>&1)
  rc=$?
  rm -rf "$cwd"
  printf '%s\n' "$out"
  return $rc
}

# --- a build that supports the unattended path -------------------------------
build_fixture "$ROOT/good" good
if out=$(run_check "$ROOT/good"); then
  ok "a build with -s and zmsetup.pl -c is reported USABLE"
else
  bad "a usable build was rejected"
  printf '%s\n' "$out" | sed 's/^/          /'
fi
printf '%s\n' "$out" | grep -q 'USABLE' && ok "verdict line is printed" || bad "no verdict line"

# The key list is the deliverable: Phase 2 generates the config file from it.
cwd="$ROOT/keys"; mkdir -p "$cwd"
(cd "$cwd" && KIN_MAIL_CONFIG=/nonexistent bash "$CHECK" "$ROOT/good" >/dev/null 2>&1)
if [ -s "$cwd/zmsetup-config-keys.txt" ] &&
   grep -qx 'LDAPHOST' "$cwd/zmsetup-config-keys.txt" &&
   grep -qx 'INSTALL_PACKAGES' "$cwd/zmsetup-config-keys.txt"; then
  ok "the config keys are written out for the generator to use"
else
  bad "no usable key list was produced"
fi

# --- builds that cannot be driven from a file --------------------------------
# Each of these must FAIL. A check that passes them is worse than no check: it
# would authorise building the multi-server install on a path that is not there.
refuses() {
  local label="$1" fixture="$2" want="$3" out
  if out=$(run_check "$fixture"); then
    bad "accepted a build that $label"
    printf '%s\n' "$out" | tail -6 | sed 's/^/          /'
  else
    if printf '%s\n' "$out" | grep -q "$want"; then
      ok "refuses a build that $label"
    else
      bad "refused a build that $label, but not for the stated reason"
      printf '%s\n' "$out" | tail -6 | sed 's/^/          /'
    fi
  fi
}

build_fixture "$ROOT/nocfg" no_config_opt
refuses "cannot take a config file" "$ROOT/nocfg" 'no -c <config file> option'

build_fixture "$ROOT/nozmsetup" absent
refuses "has no zmsetup.pl at all" "$ROOT/nozmsetup" 'zmsetup.pl is not in the core package'

build_fixture "$ROOT/noldap" no_ldap_keys
refuses "cannot join another node's LDAP" "$ROOT/noldap" 'LDAPHOST'

build_fixture "$ROOT/nomta" good zimbra-mta zimbra-proxy
refuses "is missing the edge packages" "$ROOT/nomta" 'edge'

build_fixture "$ROOT/nostore" good zimbra-store
refuses "is missing the mailbox packages" "$ROOT/nostore" 'mailbox'

# An installer that cannot be driven from a file always stops at its own menu.
build_fixture "$ROOT/nodefaults" good
cat > "$ROOT/nodefaults/install.sh" <<'SH'
#!/bin/bash
echo "interactive only"
SH
chmod +x "$ROOT/nodefaults/install.sh"
refuses "cannot take a defaults file" "$ROOT/nodefaults" 'no defaults-file argument'

# The handoff is the half that is easy to lose: packages get installed and the
# machine is left unconfigured, which is worse than refusing to start.
build_fixture "$ROOT/nohandoff" good
sed -i 's|/opt/zimbra/libexec/zmsetup.pl -c \$DEFAULTFILE|true|' "$ROOT/nohandoff/install.sh"
refuses "installs packages but never configures them" "$ROOT/nohandoff" 'does not pass the defaults file'

# memcached is not in the tarball on any real build. Reporting that as missing
# is what made an earlier revision call a working build unusable.
build_fixture "$ROOT/nomemcached" good
if out=$(run_check "$ROOT/nomemcached"); then
  ok "a build without zimbra-memcached in the tarball still passes"
else
  bad "rejected a build for not shipping memcached in the tarball"
  printf '%s\n' "$out" | tail -6 | sed 's/^/          /'
fi

# INSTALL_PACKAGES belongs to install.sh, not to zmsetup.pl's config hash.
# Looking for it in the wrong place is the other half of that same mistake.
build_fixture "$ROOT/nopkgvar" good
rm -f "$ROOT/nopkgvar/util/globals.sh"
refuses "has no INSTALL_PACKAGES to select a role with" "$ROOT/nopkgvar" 'INSTALL_PACKAGES'

# --- it must never claim success without a tarball ---------------------------
if out=$(KIN_MAIL_CONFIG=/nonexistent bash "$CHECK" "$ROOT/does-not-exist" 2>&1); then
  bad "reported success for a directory that does not exist"
else
  ok "a missing tarball is refused, not passed"
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
