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
if sed -n '/Creating the missing mailboxes/,/done <"\$MISSING_FILE"/p' "$S" | grep -q 'zmprov -l ga "\$email"'; then
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

# --- it has to run without anyone remembering it ------------------------------
# A deploy that finishes with authentication configured and an empty admin
# console is the failure this release exists to remove: AD holds 130 people,
# Zimbra holds none, and the operator is told to go and run something on the
# other machine.
H="$(pwd)/../06-hybrid-auth.sh"
if grep -q '15-ad-sync.sh' "$H"; then
  pass "the deploy runs the sync itself"
else
  bad "nothing runs the sync; the operator is back in a terminal"
fi
if grep -q 'kin_run_stage_on_store 15-ad-sync.sh --schedule' "$H"; then
  pass "and installs the timer, so it keeps up afterwards"
else
  bad "runs once at most, with nothing to catch later joiners"
fi
if sed -n '/15-ad-sync.sh --schedule/,+8p' "$H" | grep -q 'warn '; then
  pass "a sync failure never stops the deploy"
else
  bad "a mailbox-creation convenience can abort the install"
fi
Z="$(pwd)/zimbra-store.sh"
if grep -q 'kin_run_stage_on_store' "$Z"; then
  pass "running a stage on the mail store has one spelling, in the library"
else
  bad "the SSH carry-over is duplicated per stage and will drift"
fi

# --- the person comes across, not just the address ---------------------------
# A mailbox that is only an address has nobody's name on it - not in the
# address book, not in the From line, nowhere a colleague would look.
if grep -q 'givenName sn displayName' "$S"; then
  pass "asks the directory for the name fields"
else
  bad "only reads the account name"
fi
for a in givenName sn displayName; do
  if sed -n '/attrs=()/,/^    fi$/p' "$S" | grep -q "attrs+=($a"; then
    pass "carries ${a} onto the new mailbox"
  else
    bad "does not carry ${a}"
  fi
done
# A directory is not obliged to be tidy: a missing surname must not shift the
# other fields along.
if grep -q 'function flush' "$S"; then
  pass "parses LDIF per entry, so a missing field cannot shift the others"
else
  bad "reads the fields as parallel lists, which desynchronise"
fi

# --- reaching the mailbox at all ---------------------------------------------
# MAILBOX_SSH_PASS is the password that machine had BEFORE anything was
# installed. 02-prepare-os.sh then changes the account to KIN_USER_PASS. Trying
# only the first is how the synchronisation silently never ran: ssh refused,
# one warning in a forty-minute log, and an empty admin console (QA, 25 Sep).
Z="$(pwd)/zimbra-store.sh"
if grep -q 'for pw in "${MAILBOX_SSH_PASS:-}" "${KIN_USER_PASS:-}"' "$Z"; then
  pass "both SSH passwords are tried, as the orchestrator already does"
else
  bad "only one SSH password is tried; a stale one silently kills the sync"
fi
if sed -n '/^kin_run_stage_on_store/,/^}/p' "$Z" | grep -q 'true >/dev/null 2>&1'; then
  pass "the password is proven with a cheap probe before the real run"
else
  bad "the stage is run blind, so a refusal reads as the stage failing"
fi
# Not reaching the mailbox and the stage failing are different problems with
# different fixes, and must not arrive as the same message.
if grep -q 'return 3' "$Z"; then
  pass "unreachable is its own return code, distinct from a stage failure"
else
  bad "unreachable is indistinguishable from a failed stage"
fi
H="$(pwd)/../06-hybrid-auth.sh"
if grep -q 'THE DIRECTORY WAS NOT SYNCHRONISED' "$H"; then
  pass "a sync that did not happen is stated loudly, not warned about in passing"
else
  bad "a silent sync failure is easy to miss in a long deploy log"
fi
if grep -q 'No Active Directory users were synchronised' "$H"; then
  pass "a failed AD apply also says the account list will be empty"
else
  bad "an operator is left to work out why the console is empty"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All AD sync tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
