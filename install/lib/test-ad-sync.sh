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
# Skipping them silently produced the report "6 enabled people in the
# directory / 5 already have a mailbox; 0 do not" - which does not add up, and
# is indistinguishable from the sync losing somebody. That arithmetic is the
# exact shape of the complaint this feature was built to answer, so the counts
# must reconcile against the directory total.
if grep -q 'POLICY_SKIPPED' "$S"; then
  pass "accounts dropped by policy are counted, not dropped silently"
else
  bad "the numbers cannot add up to the directory total"
fi
if grep -q 'accounted for' "$S"; then
  pass "a total that does not reconcile is reported rather than printed as fact"
else
  bad "somebody could be dropped uncounted and the report would look clean"
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
if grep -qE ': "\$\{AD_SYNC_INTERVAL:=[0-9]+(s|min)\}"' "$C"; then
  pass "the interval is configurable, and defaults to seconds or minutes"
else
  bad "the sync interval is hardcoded, or defaults to hours"
fi
# Hours specifically: the default was 1h, and an hour between adding someone in
# AD and seeing them in Zimbra is indistinguishable from the sync being broken.
# The operator then goes looking for a fault that does not exist. A no-op sweep
# was measured at 3 seconds, so the hour was never buying anything.
if grep -qE ': "\$\{AD_SYNC_INTERVAL:=[0-9]+(h|hour)' "$C"; then
  bad "a new AD account would take up to an hour to appear, which reads as broken"
else
  pass "a new AD account appears in minutes, not hours"
fi
# systemd spends its default one-minute accuracy by firing LATE. On an hourly
# timer that is invisible; on a two-minute one the operator is timing it.
if grep -q 'AccuracySec=' "$S"; then
  pass "the timer sets its own accuracy rather than taking systemd's minute"
else
  bad "a short interval would drift by up to a minute against what it promises"
fi
# This is a monotonic timer (OnUnitActiveSec), so systemd leaves its realtime
# columns empty and `list-timers` prints "n/a  n/a" under NEXT and LEFT. That
# was the command this script told the operator to check it with - measured on
# the lab mailbox, 25 Sep 2026 - and it reads as a dead timer.
if grep -q 'systemctl list-timers kin-ad-sync' "$S"; then
  bad "sends the operator to a command that shows n/a for this kind of timer"
else
  pass "does not send the operator to a command that reports n/a here"
fi
# An enabled timer with nothing scheduled fires once and never again, and is
# indistinguishable from a working one until somebody is not created.
if grep -q 'NextElapseUSecMonotonic' "$S"; then
  pass "confirms a next sweep is actually scheduled before claiming it is installed"
else
  bad "reports the timer installed without checking anything is scheduled"
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

# --- a sync that created nobody is not a success ------------------------------
# 25 Sep 2026: the sync ran, the timer was installed, the stage exited 0, and
# the admin console stayed empty - the contracted seat count had never been set,
# so the gate refused every create. Nothing anywhere said so.
if grep -q 'exit 4' "$S"; then
  pass "being blocked before creating anybody exits non-zero"
else
  bad "a run that created nobody still reports success"
fi
if grep -q 'Nobody was brought across from the directory' "$S"; then
  pass "and says so in the words an operator needs"
else
  bad "the failure is a number with no explanation"
fi
# "Not configured" and "limit reached" need different answers: the first is a
# wizard step that was skipped, the second is a contract that is full.
if grep -q 'PLACEHOLDER_UNSET' "$S"; then
  pass "an unset seat count is distinguished from a full one"
else
  bad "a skipped wizard step is reported as a contract limit"
fi
H="$(pwd)/../06-hybrid-auth.sh"
if sed -n '/_sync_rc/,/esac/p' "$H" | grep -q '4)'; then
  pass "the deploy surfaces that exit code specifically"
else
  bad "the deploy treats it as a generic failure"
fi
# 06 exits 0 whatever the sync did, deliberately - stopping the pipeline here
# would leave the host unhardened and unfirewalled over a mailbox problem. The
# cost is that the deploy then ends "all selected pipeline stages exited 0"
# with an admin console that lists nobody, and the FAIL lines forty minutes up
# the log. Live deploy, 26 Sep 2026.
M="$(pwd)/../kin-mail.sh"
# One name, spelled the same in three places, or the marker is written where
# nothing looks and the deploy reports a clean run anyway. Grepping for the
# name "somewhere in the file" is not enough: it passes while the write uses
# one spelling and the read another.
_written=$(grep -oE '>/etc/kin-mail/\.[a-z-]+' "$H" | head -1 | sed 's/^>//')
_cleared=$(grep -oE 'rm -f /etc/kin-mail/\.[a-z-]+' "$H" | head -1 | sed 's/^rm -f //')
_read=$(grep -oE '\[ -f /etc/kin-mail/\.[a-z-]+ \]' "$M" | head -1 | sed -e 's/^\[ -f //' -e 's/ \]$//')
if [ -n "$_written" ]; then
  pass "an unsynchronised directory is recorded for the end-of-deploy summary"
else
  bad "the only trace is three FAIL lines in the middle of a long transcript"
fi
if [ -n "$_read" ] && grep -q 'soft_failed_stages+=("06-hybrid-auth.sh' "$M"; then
  pass "and the deploy summary names it rather than reporting a clean run"
else
  bad "a deploy where nobody can sign in still reports every stage exited 0"
fi
if [ -n "$_written" ] && [ "$_written" = "$_read" ]; then
  pass "the stage writes the same marker the deploy reads"
else
  bad "written as '${_written}' but read as '${_read}' - the warning never appears"
fi
# The marker must not outlive the problem: a later run that succeeds has to
# clear it, or every deploy on that host warns for ever.
if [ -n "$_cleared" ] && [ "$_cleared" = "$_written" ]; then
  pass "a successful sync clears the same marker it would have written"
else
  bad "cleared '${_cleared}' but writes '${_written}' - the warning would persist"
fi

# --- the seat count stops being optional once there is a directory -----------
# Three QA reports in three days (24-26 Sep 2026), each one a screenshot of AD
# beside an almost-empty Zimbra admin console. Every time the cause was this
# field left blank: the sync spends a contracted seat per person, so an unset
# count refuses all of them, and the deploy still finishes.
#
# The CLI wizard offered PLACEHOLDER_UNSET as the default answer and advised
# taking it. That is right with no directory and wrong with one.
if sed -n '/Licensing . mailbox seats/,/^  echo; say "Host firewall/p' "$C" |
     grep -q 'AD_AUTH_ENABLED" = "yes"'; then
  pass "the CLI wizard asks differently once a directory is configured"
else
  bad "the CLI wizard still offers PLACEHOLDER_UNSET when AD is enabled"
fi
if sed -n '/AD_AUTH_ENABLED" = "yes"/,/^  else$/p' "$C" | grep -q 'while :; do'; then
  pass "and will not move on until a number is given"
else
  bad "the operator can still press Enter past it with AD enabled"
fi
# Zero passes "is it a whole number" and then refuses every create - the same
# empty console by a different route.
if sed -n '/AD_AUTH_ENABLED" = "yes"/,/^  else$/p' "$C" | grep -qE '^ +0\)'; then
  pass "zero is refused too, not just blank"
else
  bad "zero seats would be accepted and create nobody"
fi
# With no directory this is a commercial figure that is often unknown on build
# day. Demanding it from every deploy would be the wrong trade.
if sed -n '/^  else$/,/^  fi$/p' "$C" | grep -q 'PLACEHOLDER_UNSET'; then
  pass "and it stays optional when there is no directory"
else
  bad "every deploy now has to invent a seat count"
fi

# --- caught in the first minute, not the fortieth ----------------------------
# The wizard refuses an unset seat count when AD is on, in three places. None of
# them covers a config carried over from an earlier build or edited by hand, and
# that path ends the same way: forty minutes of install, then an empty console.
P="$(pwd)/../01-preflight.sh"
if grep -q 'CONTRACTED_SEATS' "$P" && grep -q 'AD_AUTH_ENABLED:-no}" = "yes"' "$P"; then
  pass "preflight checks the seat count against AD before anything is installed"
else
  bad "a hand-edited config still gets found out at the end of the deploy"
fi
# Scoped to the seat case, not the whole section: counting every FATAL in
# section 8 broke the moment a second check was added beside it, which is a
# test reporting on its own arithmetic rather than on the behaviour.
_seatcase=$(sed -n '/case "\${CONTRACTED_SEATS:-}" in/,/^  esac$/p' "$P")
if [ "$(printf '%s' "$_seatcase" | grep -c 'FATAL=$((FATAL + 1))')" -eq 3 ]; then
  pass "unset, non-numeric and zero are all refused"
else
  bad "one of unset/non-numeric/zero would be allowed through"
fi
if sed -n '/8. Directory sync has seats/,/^fi$/p' "$P" | grep -q 'no AD user will be able to sign in'; then
  pass "and it says what the consequence would have been"
else
  bad "the refusal is a rule with no reason attached"
fi

# --- one character in the directory address --------------------------------
# AD_LDAP_URL="daps://<host>:636", one short of ldaps://, passed the wizard,
# passed validate_draft, reached the Zimbra domain, and cost the whole feature
# three ways: nobody created, 14-ad-trust skipped itself because the scheme was
# not ldaps://, and no AD sign-in worked (26 Sep 2026). Only emptiness had ever
# been checked.
if sed -n '/8. Directory sync has seats/,/^fi$/p' "$P" | grep -q 'is not an LDAP address'; then
  pass "preflight refuses a directory address that is not an LDAP URL"
else
  bad "a mistyped scheme still gets found out forty minutes in"
fi
if sed -n '/8. Directory sync has seats/,/^fi$/p' "$P" | grep -q 'ldaps://?\*|ldap://?\*'; then
  pass "and requires a host after the scheme, not just the scheme"
else
  bad "'ldaps://' with nothing after it would be accepted"
fi
# Refusing plain ldap:// outright would be a policy decision wearing a typo
# check: some estates genuinely run it on an isolated segment.
if sed -n '/8. Directory sync has seats/,/^fi$/p' "$P" | grep -q 'refuses unsigned binds'; then
  pass "plain ldap:// is allowed but flagged, not silently blessed"
else
  bad "ldap:// is either refused outright or passed without comment"
fi

# --- the lockdown stage must not fail on a proxy that is still starting -------
# It regenerates nginx and restarts zmproxy, then sampled once. nginx takes a
# few seconds to bind; that sample returned 000, the stage failed, and the
# deploy stopped with the health check and monitoring steps never run - on a
# machine where nothing was wrong (25 Sep 2026).
A="$(pwd)/../11-admin-path-lockdown.sh"
if grep -q '_root_ready' "$A"; then
  pass "the lockdown waits for the proxy before judging it"
else
  bad "the lockdown samples once, immediately after restarting the proxy"
fi
if sed -n '/_root_ready=0/,/^fi$/p' "$A" | grep -q 'sleep 2'; then
  pass "it polls rather than sleeping a fixed guess"
else
  bad "it uses a fixed sleep, which is wrong on both fast and slow machines"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All AD sync tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
