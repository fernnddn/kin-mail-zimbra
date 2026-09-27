#!/usr/bin/env bash
# A skipped host firewall must reach the deploy summary, not just the scrollback.
#
# 10-host-firewall.sh can be skipped only by an operator who declines it at the
# prompt. It used to be skipped by an empty Admin IPs field as well, on the
# reading that a blank optional field meant the same thing - it does not, and
# that reading left internet-facing hosts with no firewall at all. What was
# wrong in the first place is that the skip was invisible afterwards: the stage
# returned 0, nothing was added to soft_failed_stages, and the install ended on
# "Full install complete / All selected pipeline stages exited 0".
#
# For a single internet-facing mail node that is the wrong last word. The
# console treats that exact string as proof nothing needs attention, so an
# appliance with no host firewall reported itself as a clean install, with the
# one warning that said otherwise twenty minutes up a scrolling transcript.
#
# The function is extracted rather than sourced: kin-mail.sh runs main_menu at
# the bottom, so sourcing it would launch the wizard.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

KIN_MAIL_SH="$(pwd)/../kin-mail.sh"
[ -f "$KIN_MAIL_SH" ] || { printf 'FAIL kin-mail.sh not found at %s\n' "$KIN_MAIL_SH"; exit 1; }

FUNC_SRC="$(sed -n '/^run_firewall_stage_interactive() {/,/^}/p' "$KIN_MAIL_SH")"
if [ -z "$FUNC_SRC" ]; then
  printf 'FAIL could not extract run_firewall_stage_interactive from kin-mail.sh\n'
  exit 1
fi

# Drive the real function with the surrounding script stubbed out.
# $1 = KIN_ADMIN_IPS, $2 = answer for the interactive prompt ("y"/"n").
# Prints "rc=<code> skipped=<0|1> applied=<0|1>".
run_case() {
  local admin_ips="$1" answer="${2:-y}" confirmed="${3:-1}"
  local work
  work="$(mktemp -d)" || return 1
  # The accept path calls ./10-host-firewall.sh cancel-deadman directly rather
  # than through run_stage, so it needs a real file to find.
  printf '#!/usr/bin/env bash\nexit 0\n' > "$work/10-host-firewall.sh"
  chmod +x "$work/10-host-firewall.sh"
  (
    cd "$work" || exit 1
    KIN_ADMIN_IPS="$admin_ips" ANSWER="$answer" CONSOLE_CONFIRMED="$confirmed" \
    FUNC_SRC="$FUNC_SRC" bash <<'INNER'
set -u
BLD=""; RST=""; DIM=""; YLW=""; RED=""
say()  { :; }
warn() { :; }
info() { :; }
ok()   { :; }
fail() { :; }
hr()   { :; }
DRY_RUN=0
APPLIED=0
ufw_is_active() { return 1; }
ask_yn() { [ "$ANSWER" = "y" ]; }
ensure_admin_ips_for_firewall() { return 0; }
run_stage() { APPLIED=1; return 0; }
eval "$FUNC_SRC"
run_firewall_stage_interactive
rc=$?
printf 'rc=%s skipped=%s applied=%s\n' "$rc" "${FIREWALL_STAGE_SKIPPED:-unset}" "$APPLIED"
INNER
  ) | tail -n 1
  rm -rf "$work"
}

# 1. The console path that produced a silently unfirewalled appliance.
#
# It used to be asserted the other way round - skipped=1, applied=0 - on the
# reasoning that a blank optional field meant the operator had declined. It did
# not. The firewall now applies, with admin access falling back to this host's
# own subnet, which is the only outcome that is not worse than leaving every
# port open on an internet-facing mail server.
out="$(run_case "" y 1)"
case "$out" in
  "rc=0 skipped=0 applied=1")
    pass "console install with no admin IPs still applies ufw" ;;
  *) bad "console install with no admin IPs: got '$out'" ;;
esac

# 2. Admin IPs present: the firewall is applied and nothing is flagged.
out="$(run_case "203.0.113.5" y 1)"
case "$out" in
  "rc=0 skipped=0 applied=1")
    pass "console install with admin IPs applies ufw and flags nothing" ;;
  *) bad "console install with admin IPs: got '$out'" ;;
esac

# 3. Interactive operator declining is still a skip worth reporting.
out="$(run_case "203.0.113.5" n 0)"
case "$out" in
  "rc=0 skipped=1 applied=0")
    pass "an operator declining the firewall is recorded as a skip" ;;
  *) bad "interactive decline: got '$out'" ;;
esac

# 4. Interactive accept applies it.
out="$(run_case "203.0.113.5" y 0)"
case "$out" in
  "rc=0 skipped=0 applied=1")
    pass "an operator accepting the firewall applies it and flags nothing" ;;
  *) bad "interactive accept: got '$out'" ;;
esac

# 5. run_full_install must actually consume the flag. A stage function that
#    sets a variable nobody reads is the same bug wearing a new hat.
if grep -q 'FIREWALL_STAGE_SKIPPED:-0' "$KIN_MAIL_SH" &&
   grep -q 'soft_failed_stages+=("10-host-firewall.sh apply' "$KIN_MAIL_SH"; then
  pass "run_full_install turns the skip into a named summary warning"
else
  bad "run_full_install does not add the skipped firewall to soft_failed_stages"
fi

# 6. soft_failed_stages is what flips the closing line the console reads.
if grep -q 'Full install complete, with warnings' "$KIN_MAIL_SH"; then
  pass "a soft-failed stage still flips the transcript to 'with warnings'"
else
  bad "the 'with warnings' summary line is gone"
fi

# An empty admin list must not stop the firewall, anywhere.
#
# This decision existed in FOUR places: the console branch of the stage runner,
# ensure_admin_ips_for_firewall's console path, its interactive path, and the
# firewall stage itself. Three were fixed across two rounds and the fourth ran
# first, so the edge - the internet-facing half of a split - still finished a
# "complete" deploy with ufw off and one warning to show for it.
#
# Counting the places is the guard. The policy lives in 10-host-firewall.sh; no
# caller gets to pre-empt it.
# Same path the rest of this file uses; a second spelling of it is how the two
# drift apart.
MAIN="$KIN_MAIL_SH"
if ! grep -q 'Host firewall (ufw) not applied - no admin IPs' "$MAIN"; then
  pass "the stage runner no longer skips the firewall over a blank optional field"
else
  bad "an empty admin list still skips stage 10 before the stage can decide"
fi
if ! grep -q 'Cannot apply firewall without KIN_ADMIN_IPS' "$MAIN"; then
  pass "the interactive path no longer refuses either"
else
  bad "answering the admin-IP prompt with nothing still cancels the firewall"
fi
# FIREWALL_STAGE_SKIPPED is for an operator who actually declined. If an empty
# admin list can still reach it, the skip is back under another name.
skip_sites=$(grep -c 'FIREWALL_STAGE_SKIPPED=1' "$MAIN")
if [ "$skip_sites" -eq 1 ]; then
  pass "only a real decline marks the firewall as skipped"
else
  bad "${skip_sites} places mark the firewall skipped; one of them is not a decline"
fi
if grep -B3 'FIREWALL_STAGE_SKIPPED=1' "$MAIN" | grep -q 'ask_yn'; then
  pass "the one that remains is the operator answering no"
else
  bad "the remaining skip is not gated on the operator declining"
fi

# --- the dead man an operator never sees ------------------------------------
# From the console the firewall stage runs near the end of a 20-40 minute
# deploy. A five-minute window to cancel is one the operator is not watching
# for, and twice it ended with an internet-facing edge running no firewall at
# all. The window has to be long enough to be noticed.
K="$(pwd)/../kin-mail.sh"
if grep -q 'KIN_UFW_DEADMAN_SEC=1800' "$K"; then
  pass "a console deploy arms a dead man long enough to be cancelled"
else
  bad "the console deploy still uses a window the operator cannot act within"
fi
# An operator who chose their own window keeps it.
if grep -q 'if \[ -z "${KIN_UFW_DEADMAN_SEC:-}" \]; then' "$K"; then
  pass "an explicit KIN_UFW_DEADMAN_SEC is not overridden"
else
  bad "the stage overrides a window the operator set deliberately"
fi
# And the consequence is stated where it is armed, not only in a design note.
if grep -q 'NO host firewall, facing the internet' "$K"; then
  pass "the cost of not cancelling is spelled out"
else
  bad "the operator is not told what happens if they do nothing"
fi

# --- the dead man must not be a step nobody remembers ------------------------
#
# THE REPORT THIS EXISTS FOR
#
# 27 Sep 2026, on the cluster page of a finished deploy:
#
#   Edge: The host firewall is not running on this machine. Applying it arms a
#   dead man that switches ufw off again after five minutes unless it is
#   cancelled, and it was not.
#
# The cancel is a button on a page the operator is not looking at, during a
# forty-minute install, with a five-minute timer. It was missed on every
# console deploy this product has done, and each time the edge - the machine
# facing the internet - finished with no host firewall.
#
# The dead man is not the problem; leaving it to be remembered is. It asks one
# question, and that question can be answered without a human.
F="$(pwd)/../10-host-firewall.sh"
if grep -q '^  verify-access) verify_access ;;' "$F"; then
  pass "the firewall stage can answer whether current sessions survive its rules"
else
  bad "nothing can check access, so cancelling is either manual or blind"
fi
V=$(sed -n '/^verify_access() {/,/^}$/p' "$F")
# Loopback always reaches the port and an established session survives a new
# rule. Neither says anything about the NEXT connection, which is the only
# thing the dead man is protecting.
if printf '%s' "$V" | grep -q 'fe80:' && printf '%s' "$V" | grep -q '127'; then
  pass "loopback and link-local are not treated as evidence"
else
  bad "a connection that was never filtered would count as proof"
fi
if printf '%s' "$V" | grep -q 'ipaddress.ip_network'; then
  pass "coverage is decided by real network arithmetic, not string matching"
else
  bad "a /24 rule would not be seen to cover an address inside it"
fi
# No sessions means nothing to prove - which is not the same as proven.
if printf '%s' "$V" | grep -q 'There is nothing to prove access for'; then
  pass "an empty session list leaves the dead man armed"
else
  bad "a machine nobody is connected to would have its dead man cancelled"
fi
if printf '%s' "$V" | grep -q 'would be shut out'; then
  pass "an address that would be locked out is named, not just counted"
else
  bad "the operator is told it failed but not who loses access"
fi

# --- and the deploy has to use it -------------------------------------------
if grep -q 'run_stage 10-host-firewall.sh verify-access' "$K"; then
  pass "a console deploy checks access instead of leaving a button to press"
else
  bad "the console path still depends on the operator noticing in five minutes"
fi
if sed -n '/verify-access/,/^  fi$/p' "$K" | grep -q 'cancel-deadman'; then
  pass "and cancels only after that check passes"
else
  bad "the cancel is not gated on the check"
fi
# Still reported when it could not be proven: a firewall about to switch itself
# off is not a firewall, and the closing summary is the last place anyone reads.
if grep -q 'FIREWALL_DEADMAN_ARMED' "$K"; then
  pass "an armed dead man reaches the closing summary"
else
  bad "the transcript would end clean over a firewall that is about to revert"
fi
if grep -q 'dead-man armed: ufw switches off unless cancelled' "$K"; then
  pass "and the summary line says what will happen, not just which stage"
else
  bad "the summary names a stage without naming the consequence"
fi

# --- the transcript must not contradict itself -------------------------------
# 27 Sep 2026, in one deploy log:
#
#   [WARN] Dead-man will be armed for 1800s. Console will NOT auto-cancel it.
#   ...
#   [ OK ] Dead-man cancelled: the firewall stays on, and your access is proved.
#
# The second line is the one that is true. The first was written when nothing
# cancelled it automatically, and was never updated. A log that contradicts
# itself is worse than a quiet one: the operator stops trusting the parts that
# are correct, and this transcript is what gets captured for handover.
# Scoped to lines that actually print. The phrase survives in a comment above
# the fix, quoting what it used to say, and a test that cannot tell a comment
# from output would force that context to be deleted to stay green.
if grep -E '^[[:space:]]*(warn|info|say|ok|fail|echo|printf)' "$K" |
     grep -q 'Console will NOT auto-cancel'; then
  bad "the transcript still promises no auto-cancel, then auto-cancels"
else
  pass "the armed message does not promise an outcome it cannot know yet"
fi
if grep -q 'Dead-man armed for .* while access is checked' "$K"; then
  pass "and says what is about to decide it"
else
  bad "the operator is told a timer is armed with no idea what happens next"
fi
# The manual escape has to stay: verify-access can fail to prove anything.
if sed -n '/Dead-man armed for/,+4p' "$K" | grep -q 'cancel-deadman'; then
  pass "the manual cancel is still named for when the check cannot prove access"
else
  bad "an operator whose access could not be proved has no instruction"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All firewall-skip-reporting tests passed\n'
else
  printf '%s test(s) failed\n' "$fails"
fi
exit "$fails"
