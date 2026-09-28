#!/usr/bin/env bash
# =============================================================================
# A stage that exists but nothing knows about is a stage that silently does not
# run.
#
# kin-mail.sh keeps REQUIRED_SCRIPTS, the list that decides whether a clone is
# complete: if anything on it is missing, the installer pulls before it starts.
# Four stages were added over the last fortnight - node metrics, the log relay,
# AD certificate trust and the AD sync - and none of them reached that list.
#
# That is not a cosmetic omission. 06-hybrid-auth.sh calls the two AD stages
# with `if [ -x ./14-ad-trust.sh ]`, so an absent file produces one warning in
# the middle of a forty-minute log and carries on; the directory's certificate
# is never trusted, and the failure surfaces later as "nobody can sign in",
# pointing at authentication rather than at a file that was not there.
#
# This is a list that must not be maintained by hand, so it is checked against
# the stages on disk instead.
# =============================================================================
set -u
cd "$(dirname "$0")/.." || exit 1

fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad() {
  printf 'FAIL %s\n' "$1"
  fails=$((fails + 1))
}

M=kin-mail.sh
[ -f "$M" ] || {
  printf 'FAIL kin-mail.sh not found\n'
  exit 1
}

listed=$(sed -n '/^REQUIRED_SCRIPTS=(/,/^)/p' "$M" |
  grep -v '^[[:space:]]*#' | grep -oE '[0-9A-Za-z._-]+\.sh' | sort -u)
if [ -z "$listed" ]; then
  bad "REQUIRED_SCRIPTS could not be read out of kin-mail.sh"
  printf '%s test(s) failed\n' "$fails"
  exit 1
fi

# --- every numbered stage, plus the split builder -----------------------------
missing=""
for f in [0-9]*.sh kin-mail-split.sh; do
  [ -f "$f" ] || continue
  printf '%s\n' "$listed" | grep -qxF "$f" || missing="${missing}${f} "
done
if [ -n "$missing" ]; then
  bad "stages on disk that REQUIRED_SCRIPTS does not know about: ${missing}"
  printf '     Add them to REQUIRED_SCRIPTS in kin-mail.sh. Without that, a\n'
  printf '     short clone is not detected and the stage is simply absent when\n'
  printf '     something calls it.\n'
else
  pass "every stage on disk is in REQUIRED_SCRIPTS"
fi

# --- and nothing listed that does not exist -----------------------------------
# The opposite mistake sends ensure_scripts into a pull loop that can never
# satisfy itself, on every run, for a file that was renamed or removed.
ghosts=""
while IFS= read -r name; do
  [ -n "$name" ] || continue
  [ -f "$name" ] || ghosts="${ghosts}${name} "
done <<EOF
$(printf '%s\n' "$listed")
EOF
if [ -n "$ghosts" ]; then
  bad "REQUIRED_SCRIPTS names files that do not exist: ${ghosts}"
else
  pass "every name in REQUIRED_SCRIPTS exists"
fi

# --- each stage is reachable from some pipeline -------------------------------
# Presence is not the same as being run. A stage nothing calls is dead weight
# that reads, to anyone auditing the product, like a feature that ships.
#
# REQUIRED_SCRIPTS is stripped out of kin-mail.sh first. It names every stage
# by definition, so searching it for callers makes this assertion vacuous - it
# passed on a stage whose only remaining mention was the completeness list that
# had just been fixed above.
stripped=$(mktemp)
trap 'rm -f "$stripped"' EXIT
sed '/^REQUIRED_SCRIPTS=(/,/^)/d' kin-mail.sh >"$stripped"

unreached=""
for f in [0-9]*.sh; do
  [ -f "$f" ] || continue
  # 00-config.sh is sourced by name in every stage, not invoked as one.
  case "$f" in 00-config.sh) continue ;; esac
  callers=$(grep -lF "$f" "$stripped" kin-mail-split.sh [0-9]*.sh \
    ../console/backend/kin_privhelper/*.py 2>/dev/null |
    grep -v "/${f}$" | grep -v "^${f}$")
  [ -n "$callers" ] || unreached="${unreached}${f} "
done
if [ -n "$unreached" ]; then
  bad "stages nothing invokes: ${unreached}"
else
  pass "every stage is invoked from a pipeline or a caller"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All stage-accounting tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
