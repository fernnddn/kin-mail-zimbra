#!/usr/bin/env bash
# Bringing Active Directory's people into Zimbra, and what that must never do.
#
# THE REPORT THIS EXISTS FOR
#
# QA, 24 September 2026, with a screenshot of AD beside a screenshot of the
# Zimbra admin console: most of the staff are in one and not the other.
#
# zimbraAuthMech=ad checks passwords; it does not read the account list. Of
# Zimbra's three auto-provisioning modes, none closes the gap on this build:
#
#   LAZY    creates the mailbox only when that person first signs in, so the
#           two lists stay different until everybody has logged in once
#   EAGER   would sweep the directory on a timer - mailbox.log says "auto
#           provision thread is not running" and the class is absent
#   MANUAL  searchAutoProvDirectory is not in this build's zmprov
#
# So the sweep lives here, where it can be read, tested and fixed.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/../15-ad-sync.sh"
[ -f "$S" ] || { printf 'FAIL 15-ad-sync.sh not found\n'; exit 1; }
bash -n "$S" && pass "parses" || bad "syntax error"
[ -x "$S" ] && pass "is executable" || bad "not executable"

# --- it only ever creates -----------------------------------------------------
# A person vanishing from an LDAP search - a filter typo, a moved OU, a
# directory that answered slowly - must never be able to destroy their mail.
if grep -qE 'zmprov (-l )?(da|ra|ma .*zimbraAccountStatus (closed|locked))' "$S"; then
  bad "the sync can delete, rename or disable a mailbox"
else
  pass "never deletes, renames or disables anything"
fi
if grep -q 'Removing people is never done here' "$S"; then
  pass "and says so where an operator will read it"
else
  bad "the one-way rule is not stated"
fi
# An empty result is the dangerous case: it reads as "everyone has left".
if grep -q "Refusing to treat that as 'everyone has left'" "$S"; then
  pass "an empty directory result is refused, not acted on"
else
  bad "a directory that returned nothing would be treated as authoritative"
fi

# --- the seat gate holds ------------------------------------------------------
# Contracted seats are a commercial limit. A directory sync must not be a way
# around it: the point of the gate is that it holds no matter who is asking.
if grep -q 'kin_quota_gate_allow_new_mailbox' "$S"; then
  pass "asks the seat gate before every create"
else
  bad "creates mailboxes without consulting the seat gate"
fi
if sed -n '/Creating the missing mailboxes/,/^fi$/p' "$S" | grep -q 'break'; then
  pass "stops at the limit rather than pushing past it"
else
  bad "does not stop when the seat limit is reached"
fi

# --- who it leaves out --------------------------------------------------------
# Disabled leavers and Windows' own accounts are not mailboxes, and each one
# would spend a contracted seat.
if grep -q 'userAccountControl:1.2.840.113556.1.4.803:=2' "$S"; then
  pass "excludes disabled AD accounts"
else
  bad "would create mailboxes for disabled leavers"
fi
if grep -q 'AD_SYNC_SKIP.*administrator guest krbtgt' "$S"; then
  pass "skips Windows' own accounts"
else
  bad "would create a mailbox for krbtgt"
fi

# --- safe to run twice, and to preview ---------------------------------------
if grep -q 'grep -qxF "\$email"' "$S"; then
  pass "skips people who already have a mailbox"
else
  bad "would try to recreate existing accounts on every run"
fi
if grep -q -- '--dry-run' "$S" && grep -q 'nothing changed' "$S"; then
  pass "can show what it would do without doing it"
else
  bad "has no way to preview"
fi

# --- checked, not announced ---------------------------------------------------
if sed -n '/zmprov ca "\$email"/,/^    fi$/p' "$S" | grep -q 'zmprov -l ga "\$email"'; then
  pass "confirms each account exists before counting it as created"
else
  bad "counts a create that zmprov only claimed to do"
fi

# --- the password it sets -----------------------------------------------------
# AD holds the real one. This exists because Zimbra requires the field, and
# nobody - including this script's author - should be able to guess it.
if grep -q '/dev/urandom' "$S"; then
  pass "the placeholder password is random"
else
  bad "the placeholder password is predictable"
fi

# --- runs where accounts live -------------------------------------------------
if grep -q 'kin_mailboxd_is_local' "$S"; then
  pass "runs on the node that holds the directory"
else
  bad "would run anywhere, including a node with no mail store"
fi

# --- keeps itself in step -----------------------------------------------------
if grep -q 'kin-ad-sync.timer' "$S" && grep -q 'OnUnitActiveSec=${AD_SYNC_INTERVAL}' "$S"; then
  pass "can install a timer so new people are picked up without anyone asking"
else
  bad "has no way to stay in step after the first run"
fi
# How often is the operator's call, not a number baked in here. One minute
# makes a new joiner appear almost at once; an hour is plenty for batches.
C="$(pwd)/../00-config.sh"
if grep -q ': "${AD_SYNC_INTERVAL:=1h}"' "$C"; then
  pass "the interval is configurable, with a sane default"
else
  bad "the sync interval is hardcoded"
fi
# A timer that silently fell back to a default nobody asked for would run at
# the wrong rate forever, and nothing would say so.
if grep -q 'is not a systemd interval' "$S"; then
  pass "an interval systemd cannot parse is refused, not guessed at"
else
  bad "a malformed interval would be written into the timer"
fi
if grep -q 'AD_AUTH_ENABLED.*!= "yes"' "$S"; then
  pass "does nothing on a deployment with no AD"
else
  bad "runs even where there is no directory to read"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All AD sync tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
