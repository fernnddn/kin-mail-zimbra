#!/usr/bin/env bash
# Tests for backup retention.
#
# Retention decides which backups stop existing, so a mistake here does not
# show up as a bug in a log - it shows up on the morning somebody needs a
# restore and the set they needed was pruned to make room for one that had
# never finished. These run the real functions against real directories.
set -u

SRC="$(cd "$(dirname "$0")/../.." && pwd)/backup/kin-mail-backup.sh"
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

t_pass=0
t_fail=0
t_ok()  { t_pass=$((t_pass + 1)); echo "ok  $1"; }
t_bad() { t_fail=$((t_fail + 1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; return 0; }

# Lift the retention functions out of the script and run them for real. The
# script needs root, a config and a live cluster; these four functions do not.
# The script's own say/ok/warn/fail are stubbed - note they are deliberately
# NOT named like the reporters above, because an earlier version of this file
# defined ok() for both and the sourced stub silently disabled every
# assertion. It printed "ALL OK (0 checks)".
LIB=$(mktemp "${TMPDIR:-/tmp}/kin-retention.XXXXXX")
{
  echo 'say(){ :; }; ok(){ :; }; warn(){ :; }; fail(){ echo "script-fail: $*" >&2; }'
  awk '/^verified_sets\(\) \{/,/^\}/'   "$SRC"
  awk '/^unverified_sets\(\) \{/,/^\}/' "$SRC"
  awk '/^prune_all_but\(\) \{/,/^\}/'   "$SRC"
  awk '/^apply_retention\(\) \{/,/^\}/' "$SRC"
} > "$LIB"
# shellcheck disable=SC1090
. "$LIB"
rm -f "$LIB"

# Prove the extraction actually produced callable functions before trusting a
# single result below.
for fn in verified_sets unverified_sets prune_all_but apply_retention; do
  if ! declare -F "$fn" >/dev/null 2>&1; then
    echo "FAILED  could not extract $fn from $SRC - the tests below would be vacuous" >&2
    exit 1
  fi
done
t_ok "the retention functions were extracted and are callable"

names()  { for d in "$1"/*/; do [ -d "$d" ] && basename "${d%/}"; done | sort | tr '\n' ' '; }
count()  { "$1" "$2" | grep -c . | tr -d '[:space:]'; }
make_set() {  # <dir> <name> <verified 1|0>
  mkdir -p "$1/$2"
  printf 'data\n' > "$1/$2/MANIFEST.txt"
  if [ "$3" = "1" ]; then touch "$1/$2/.backup-ok"; fi
}
new_root() {
  ROOT=$(mktemp -d "${TMPDIR:-/tmp}/kin-bk.XXXXXX")
  BACKUP_ROOT="$ROOT"
  mkdir -p "$ROOT/daily" "$ROOT/weekly"
}

# --- 1. an interrupted run must not evict a good backup ---------------------
new_root
DAILY_KEEP=3; WEEKLY_KEEP=4; UNVERIFIED_KEEP=2
for n in 20260101T020000Z 20260102T020000Z 20260103T020000Z; do
  make_set "$ROOT/daily" "$n" 1
done
make_set "$ROOT/daily" "20260104T020000Z" 0   # died between pull and checksum
make_set "$ROOT/daily" "20260105T020000Z" 1   # newest good set
apply_retention "$ROOT/daily/20260105T020000Z" >/dev/null 2>&1
got=$(names "$ROOT/daily")

case "$got" in
  *20260103T020000Z*) t_ok "a verified set survives alongside an interrupted run" ;;
  *) t_bad "retention pruned a verified set to make room for a partial one" "$got" ;;
esac
case "$got" in
  *20260101T020000Z*) t_bad "kept more verified sets than DAILY_KEEP" "$got" ;;
  *) t_ok "the oldest verified set beyond DAILY_KEEP is pruned" ;;
esac
v=$(count verified_sets "$ROOT/daily")
[ "$v" = "3" ] && t_ok "exactly DAILY_KEEP verified sets remain" \
               || t_bad "verified count is $v, want 3" "$got"
case "$got" in
  *20260104T020000Z*) t_ok "the partial set is kept for inspection, not counted" ;;
  *) t_bad "the partial set was removed while under UNVERIFIED_KEEP" "$got" ;;
esac
rm -rf "$ROOT"

# --- 2. the old mtime rule really did have the bug --------------------------
# Negative test: reintroduce the previous behaviour and show it fails the
# assertion above. Without this, test 1 could be passing for another reason.
new_root
DAILY_KEEP=3
for n in 20260101T020000Z 20260102T020000Z 20260103T020000Z; do
  make_set "$ROOT/daily" "$n" 1
done
make_set "$ROOT/daily" "20260104T020000Z" 0
make_set "$ROOT/daily" "20260105T020000Z" 1
# The pre-fix logic, verbatim in spirit: newest N by mtime, marker ignored.
ls -1dt "$ROOT/daily"/*/ | awk -v k=3 'NR>k {print $0}' \
  | while IFS= read -r d; do rm -rf "$d"; done
old=$(names "$ROOT/daily")
old_v=$(count verified_sets "$ROOT/daily")
# The bug is not that a particular set vanished - it is that the partial one
# took a slot, so the operator ends up with DAILY_KEEP-1 real backups while
# the console still reports DAILY_KEEP.
if [ "$old_v" -lt 3 ]; then
  t_ok "the old mtime rule left only ${old_v}/3 real backups (bug reproduced)"
else
  t_bad "negative test did not reproduce the old bug" "$old"
fi
rm -rf "$ROOT"

# --- 3. partial sets are bounded, not hoarded -------------------------------
new_root
DAILY_KEEP=7; WEEKLY_KEEP=4; UNVERIFIED_KEEP=2
make_set "$ROOT/daily" "20260110T020000Z" 1
for n in 20260101T020000Z 20260102T020000Z 20260103T020000Z 20260104T020000Z; do
  make_set "$ROOT/daily" "$n" 0
done
apply_retention "$ROOT/daily/20260110T020000Z" >/dev/null 2>&1
u=$(count unverified_sets "$ROOT/daily")
[ "$u" = "2" ] && t_ok "unverified sets are bounded at UNVERIFIED_KEEP" \
               || t_bad "unverified sets left: $u, want 2" "$(names "$ROOT/daily")"
if verified_sets "$ROOT/daily" | grep -q 20260110T020000Z; then
  t_ok "the verified set survives a pile of partial ones"
else
  t_bad "the good set was pruned"
fi
rm -rf "$ROOT"

# --- 4. ordering is by name, not by a mutable mtime -------------------------
new_root
DAILY_KEEP=2; WEEKLY_KEEP=4; UNVERIFIED_KEEP=2
for n in 20260101T020000Z 20260202T020000Z 20260303T020000Z; do
  make_set "$ROOT/daily" "$n" 1
done
touch "$ROOT/daily/20260101T020000Z"   # make the OLDEST look newest by mtime
apply_retention "$ROOT/daily/20260303T020000Z" >/dev/null 2>&1
if verified_sets "$ROOT/daily" | grep -q 20260101T020000Z; then
  t_bad "a touched mtime changed which backups were kept" "$(names "$ROOT/daily")"
else
  t_ok "retention orders by set name, not by a mutable mtime"
fi
rm -rf "$ROOT"

# --- 5. the weekly layer obeys WEEKLY_KEEP ----------------------------------
new_root
DAILY_KEEP=7; WEEKLY_KEEP=2; UNVERIFIED_KEEP=2
make_set "$ROOT/daily" "20260110T020000Z" 1
for w in 2026-W01 2026-W02 2026-W03 2026-W04; do make_set "$ROOT/weekly" "$w" 1; done
apply_retention "$ROOT/daily/20260110T020000Z" >/dev/null 2>&1
w=$(count verified_sets "$ROOT/weekly")
# WEEKLY_KEEP, plus possibly this ISO week's snapshot created by the call.
[ "$w" -le 3 ] && t_ok "weekly snapshots are pruned to WEEKLY_KEEP" \
               || t_bad "weekly kept $w" "$(names "$ROOT/weekly")"
rm -rf "$ROOT"

# --- 6. a set with no marker is never the newest "good" one -----------------
# The freshness monitor and a restore both pick the newest verified set. If an
# unverified one could win, the operator would be told the backup is current.
new_root
DAILY_KEEP=7; WEEKLY_KEEP=4; UNVERIFIED_KEEP=2
make_set "$ROOT/daily" "20260101T020000Z" 1
make_set "$ROOT/daily" "20260209T020000Z" 0   # newer, but never verified
newest=$(verified_sets "$ROOT/daily" | head -1)
case "$newest" in
  *20260101T020000Z) t_ok "the newest verified set skips an unverified newer one" ;;
  *) t_bad "an unverified set was returned as the newest verified" "$newest" ;;
esac
rm -rf "$ROOT"

# --- 7. a config that would keep nothing is refused -------------------------
grep -q 'refusing to run' "$SRC" && grep -q 'require_count DAILY_KEEP' "$SRC" \
  && t_ok "a zero or non-numeric retention count is refused before anything runs" \
  || t_bad "retention counts are not validated"

# Run the validator for real, both ways.
require_count() { :; }   # placeholder so the extraction below can replace it
eval "$(awk '/^require_count\(\) \{/,/^\}/' "$SRC")"
CONF=/dev/null
if ( require_count DAILY_KEEP "0" 1 ) >/dev/null 2>&1; then
  t_bad "require_count accepted DAILY_KEEP=0"
else
  t_ok "require_count rejects a zero daily count"
fi
if ( require_count DAILY_KEEP "abc" 1 ) >/dev/null 2>&1; then
  t_bad "require_count accepted a non-numeric count"
else
  t_ok "require_count rejects a non-numeric count"
fi
if ( require_count UNVERIFIED_KEEP "0" 0 ) >/dev/null 2>&1; then
  t_ok "require_count allows UNVERIFIED_KEEP=0 (keeping no partial sets is valid)"
else
  t_bad "require_count rejected a legitimate UNVERIFIED_KEEP=0"
fi

# --- 8. the marker is only written after checksums pass ---------------------
if awk '/SHA256SUMS match/,/backup-ok/' "$SRC" | grep -q 'touch .*\.backup-ok'; then
  t_ok "the verified marker is written only after SHA256SUMS match"
else
  t_bad "the .backup-ok marker is not gated on checksum verification"
fi
if awk '/checksum_rc/,/^fi$/' "$SRC" | grep -q 'mv "\$DEST"'; then
  t_ok "a corrupt set is moved out of daily/ rather than left to be found"
else
  t_bad "a set that failed its checksum stays in daily/"
fi

# --- 9. staging on the mail node is cleared on every exit path -------------
# Staging is a full copy of the store on the production node's OS disk. A
# collector that failed part-way used to leave it there until the next night's
# run reached its own rm -rf - and if the failure was a full disk, that copy is
# what kept it full.
grep -q 'trap cleanup_remote_staging EXIT' "$SRC" \
  && t_ok "remote staging is cleared by an EXIT trap, not only on success" \
  || t_bad "a failed collector leaves a mail-store copy on the production node"
trap_line=$(grep -n 'trap cleanup_remote_staging EXIT' "$SRC" | head -1 | cut -d: -f1)
collect_line=$(grep -n 'REMOTE_SCRIPT_DST --backup' "$SRC" | head -1 | cut -d: -f1)
if [ -n "$trap_line" ] && [ -n "$collect_line" ] && [ "$trap_line" -lt "$collect_line" ]; then
  t_ok "the trap is armed before the collector is invoked"
else
  t_bad "the trap is armed too late to cover a failed collect" "trap=$trap_line collect=$collect_line"
fi
# The success path must not clean twice, and must not be skipped either.
grep -q 'STAGING_CLEANED=1' "$SRC" \
  && t_ok "the normal cleanup marks staging as already cleared" \
  || t_bad "the trap would re-run cleanup after the normal path"

# --- 10. an incomplete set is reported, not silently marked good ----------
if awk '/SHA256SUMS match/,/^say "Retention/' "$SRC" | grep -q 'complete=false'; then
  t_ok "a set with failed mailbox exports is called out after verification"
else
  t_bad "an incomplete set reads exactly like a complete one"
fi

echo
if [ "$t_fail" -eq 0 ]; then echo "ALL OK ($t_pass checks)"; exit 0; fi
echo "FAILED $t_fail test(s) ($t_pass ok)"; exit 1
