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

if [ "$fails" -eq 0 ]; then
  printf 'All firewall-skip-reporting tests passed\n'
else
  printf '%s test(s) failed\n' "$fails"
fi
exit "$fails"
