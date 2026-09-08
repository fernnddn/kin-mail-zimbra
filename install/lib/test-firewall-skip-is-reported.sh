#!/usr/bin/env bash
# A skipped host firewall must reach the deploy summary, not just the scrollback.
#
# 10-host-firewall.sh is allowed to be skipped: the wizard marks Admin IPs
# optional, so a console-confirmed full install with KIN_ADMIN_IPS empty
# returns success without applying ufw. That is a deliberate product decision
# and it stays. What was wrong is that the skip was invisible afterwards: the
# stage returned 0, nothing was added to soft_failed_stages, and the install
# ended on "Full install complete / All selected pipeline stages exited 0".
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
out="$(run_case "" y 1)"
case "$out" in
  "rc=0 skipped=1 applied=0")
    pass "console install with no admin IPs skips ufw and records the skip" ;;
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

if [ "$fails" -eq 0 ]; then
  printf 'All firewall-skip-reporting tests passed\n'
else
  printf '%s test(s) failed\n' "$fails"
fi
exit "$fails"
