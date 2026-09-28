#!/usr/bin/env bash
# =============================================================================
# No installer script may change the permissions of a path another component
# owns.
#
# WHAT THIS EXISTS FOR
#
# lib/ad-ldap.sh wanted a tmpfs directory for the file holding the directory
# service account's password. It used /run/kin-mail and ran `chmod 0700` on it.
#
# /run/kin-mail is the PRIVHELPER's directory. It holds privhelper.sock, and
# kin_privhelper/daemon.py sets it root:kin-console 0750 for one reason: the
# console runs as kin-console and needs the group execute bit to traverse that
# directory and reach the socket. Taking the group bit off cut the console off
# from its own helper, so every privileged action - Deploy, the mail gateway,
# all of it - failed with a permission error. Reported from live QA on
# 28 September 2026, after a deployment.
#
# The failure had nothing to do with the code that caused it, which is what
# makes this class expensive: the operator sees "permission denied" on Deploy
# and has no reason to look at an LDAP helper.
#
# So: a component may create and chmod a directory of its OWN. It may not
# chmod a directory that belongs to something else.
# =============================================================================
set -u
cd "$(dirname "$0")/.." || exit 1

fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad() {
  printf 'FAIL %s\n' "$1"
  fails=$((fails + 1))
  shift
  for l in "$@"; do printf '       %s\n' "$l"; done
}

# Paths owned by another component, with who owns them and why it matters.
# Add to this list; never remove from it without moving the ownership too.
#
# Each entry: <path>|<owner>|<what breaks>
OWNED="
/run/kin-mail|the privhelper (daemon.py sets it root:kin-console 0750)|the console loses its socket and every privileged action fails with a permission error
/var/lib/kin-mail-console|the console service|the console cannot read its own users, session secret or TLS material
/etc/kin-mail-console|the console service|the console cannot read console.env
/opt/kin-mail-console|the console install|the console's venv and static files become unreadable
"

scripts=$(find . -maxdepth 2 -name '*.sh' -not -name 'test-*' -type f | sort)
[ -n "$scripts" ] || {
  printf 'FAIL no installer scripts found - refusing a silent pass\n'
  exit 1
}

found_any=0
printf '%s\n' "$OWNED" | while IFS='|' read -r path owner breaks; do
  [ -n "$path" ] || continue
  # Two shapes, because the one that shipped used the second.
  #
  #   chmod 0700 /run/kin-mail        the literal, caught by the first pattern
  #   rundir=/run/kin-mail            assigned first, chmod "$rundir" later -
  #                                   which is what was actually written, and
  #                                   what a literal-only check walks past
  #
  # A trailing character rules out longer paths that are a different directory:
  # /run/kin-ad-bind is ours, /run/kin-mail is not.
  hits=$(grep -nE "(chmod|chown|install -d)[^|;&]*[\"' ]${path}[\"'/ ]*(\$|[;&)])" $scripts 2>/dev/null || true)
  # The path may be followed by more of the same declaration ("local a=x b=y"),
  # so anchor on what must NOT follow it: another path character. That is what
  # separates /run/kin-mail from /run/kin-ad-bind, which is ours.
  hits="${hits}$(grep -nE "^[^#]*=[\"']?${path}([\"']|[[:space:]]|\$)" $scripts 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    printf 'FAIL a script changes permissions on %s\n' "$path"
    printf '       owned by: %s\n' "$owner"
    printf '       breaks:   %s\n' "$breaks"
    printf '%s\n' "$hits" | sed 's/^/       /'
    echo "FAILMARK" >> /tmp/kin-owned-path-fail.$$
  fi
done
if [ -f "/tmp/kin-owned-path-fail.$$" ]; then
  fails=$((fails + $(grep -c FAILMARK "/tmp/kin-owned-path-fail.$$")))
  rm -f "/tmp/kin-owned-path-fail.$$"
else
  pass "no installer script changes permissions on a path another component owns"
fi

# The specific one, spelled out, because it is the one that actually shipped.
if grep -rn 'chmod[^|;&]*/run/kin-mail[^-]' $scripts 2>/dev/null | grep -v 'kin-mail-'; then
  bad "something still chmods the privhelper's socket directory"
else
  pass "the privhelper's socket directory is left alone"
fi

# And the helper that caused it must use a directory of its own.
if [ -f lib/ad-ldap.sh ]; then
  if grep -q 'rundir=/run/kin-ad-bind' lib/ad-ldap.sh; then
    pass "ad-ldap.sh uses a run directory of its own"
  else
    bad "ad-ldap.sh no longer names its own run directory" \
      "whatever it uses instead must not belong to another component"
  fi
  # The password file still has to be 0600 wherever it lands.
  if grep -q 'chmod 0600 "\$pwfile"' lib/ad-ldap.sh; then
    pass "the bind password file is still 0600"
  else
    bad "the bind password file is no longer explicitly 0600"
  fi
fi

if [ "$fails" -eq 0 ]; then
  printf 'All owned-path tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
