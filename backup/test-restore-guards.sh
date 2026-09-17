#!/usr/bin/env bash
# Tests for the restore script's refusals and provenance notices.
#
# This script overwrites a Zimbra installation. Everything here is about what
# it refuses to do and what it tells the operator before it starts.
set -u

SRC="$(cd "$(dirname "$0")" && pwd)/kin-mail-restore.sh"
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

t_pass=0
t_fail=0
t_ok()  { t_pass=$((t_pass + 1)); echo "ok  $1"; }
t_bad() { t_fail=$((t_fail + 1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; return 0; }

LIB=$(mktemp "${TMPDIR:-/tmp}/kin-restore.XXXXXX")
{
  echo 'say(){ :; }; ok(){ echo "OK: $*"; }; warn(){ echo "WARN: $*"; }; fail(){ echo "FAIL: $*"; }'
  awk '/^assert_set\(\) \{/,/^\}/' "$SRC"
} > "$LIB"
# shellcheck disable=SC1090
. "$LIB"
rm -f "$LIB"
declare -F assert_set >/dev/null 2>&1 || { echo "FAILED  could not extract assert_set" >&2; exit 1; }
t_ok "assert_set was extracted and is callable"

mkset() {  # mkset <verified 1|0> <complete true|false|none>
  D=$(mktemp -d "${TMPDIR:-/tmp}/kin-set.XXXXXX")
  S="$D/20260101T020000Z"
  mkdir -p "$S"
  printf 'kind=kin-mail-backup\ntaken_on=mail-a.example.test\ncreated_utc=2026-01-01T02:00:00Z\n' > "$S/MANIFEST.txt"
  case "$2" in
    true)  printf 'mailboxes_expected=3\nmailboxes_exported=3\ncomplete=true\n'  >> "$S/MANIFEST.txt" ;;
    false) printf 'mailboxes_expected=3\nmailboxes_exported=1\ncomplete=false\n' >> "$S/MANIFEST.txt" ;;
  esac
  : > "$S/SHA256SUMS"
  [ "$1" = "1" ] && touch "$S/.backup-ok"
  return 0
}
run_assert() { ( SET="$1"; assert_set ) 2>&1; }

# --- refusals ---------------------------------------------------------------
out=$( ( SET=""; assert_set ) 2>&1 ); rc=$?
[ "$rc" -ne 0 ] && t_ok "refuses an empty --set" || t_bad "accepted an empty --set"
out=$( ( SET="/nonexistent/kin/set"; assert_set ) 2>&1 ); rc=$?
[ "$rc" -ne 0 ] && t_ok "refuses a --set that does not exist" || t_bad "accepted a missing directory"

D=$(mktemp -d "${TMPDIR:-/tmp}/kin-set.XXXXXX"); mkdir -p "$D/notaset"
out=$( ( SET="$D/notaset"; assert_set ) 2>&1 ); rc=$?
[ "$rc" -ne 0 ] && t_ok "refuses a directory that is not a backup set" \
                || t_bad "accepted a directory with no MANIFEST/SHA256SUMS"
case "$out" in *"Not a KIN Mail backup set"*) t_ok "says why it is not a backup set" ;;
               *) t_bad "unhelpful refusal" "$out" ;; esac
rm -rf "$D"

# --- an unverified set is called out before the restore starts --------------
mkset 0 true
out=$(run_assert "$S")
case "$out" in
  *"no .backup-ok marker"*) t_ok "warns that the set was never verified" ;;
  *) t_bad "restoring an unverified set says nothing" "$out" ;;
esac
case "$out" in
  *"interrupted run"*) t_ok "explains where an unverified set comes from" ;;
  *) t_bad "does not explain what an unverified set means" ;;
esac
rm -rf "$D"

# A verified set must NOT produce that warning, or the warning means nothing.
mkset 1 true
out=$(run_assert "$S")
case "$out" in
  *"no .backup-ok marker"*) t_bad "warned about a properly verified set" "$out" ;;
  *) t_ok "a verified set produces no verification warning" ;;
esac
case "$out" in
  *INCOMPLETE*) t_bad "called a complete set incomplete" "$out" ;;
  *) t_ok "a complete set is not called incomplete" ;;
esac
rm -rf "$D"

# --- an incomplete set is named, and correctly described --------------------
mkset 1 false
out=$(run_assert "$S")
case "$out" in
  *"INCOMPLETE: 1 of 3"*) t_ok "reports how many mailbox exports are missing" ;;
  *) t_bad "did not report the shortfall" "$out" ;;
esac
case "$out" in
  *"this restore will still work"*) t_ok "says the restore is still valid" ;;
  *) t_bad "leaves the operator thinking the set is unusable" "$out" ;;
esac
rm -rf "$D"

# --- provenance is always stated --------------------------------------------
mkset 1 true
out=$(run_assert "$S")
case "$out" in
  *"mail-a.example.test"*) t_ok "names the node the set was taken from" ;;
  *) t_bad "does not say where the set came from" "$out" ;;
esac
case "$out" in
  *"2026-01-01T02:00:00Z"*) t_ok "names when the set was taken" ;;
  *) t_bad "does not say when the set was taken" "$out" ;;
esac
rm -rf "$D"

# --- the refusals that keep this off a live cluster -------------------------
grep -q 'systemctl is-active --quiet pacemaker' "$SRC" \
  && t_ok "refuses to run where Pacemaker is active" \
  || t_bad "no Pacemaker guard"
grep -q 'refusing restore onto live replica' "$SRC" \
  && t_ok "refuses when /opt/zimbra is on DRBD" \
  || t_bad "no DRBD guard"
grep -q 'restore onto the mailbox, which holds the mail' "$SRC" \
  && t_ok "refuses to restore onto the edge of a split" \
  || t_bad "no split-edge restore guard"
grep -q 'i-understand-this-overwrites-zimbra' "$SRC" \
  && t_ok "requires an explicit overwrite acknowledgement" \
  || t_bad "no explicit confirmation flag"

# Ordering: every guard must precede the first destructive step.
scratch=$(grep -n '^assert_scratch$' "$SRC" | head -1 | cut -d: -f1)
setl=$(grep -n '^assert_set$' "$SRC" | head -1 | cut -d: -f1)
csum=$(grep -n '^verify_checksums$' "$SRC" | head -1 | cut -d: -f1)
first_destructive=$(grep -n '^    restore_ldap$\|^    restore_mysql$\|^    restore_store$' "$SRC" | head -1 | cut -d: -f1)
if [ -n "$scratch" ] && [ -n "$setl" ] && [ -n "$csum" ] && [ -n "$first_destructive" ] \
   && [ "$scratch" -lt "$first_destructive" ] && [ "$setl" -lt "$first_destructive" ] \
   && [ "$csum" -lt "$first_destructive" ]; then
  t_ok "scratch, set and checksum guards all run before anything is written"
else
  t_bad "a guard runs after the first destructive step" \
        "scratch=$scratch set=$setl checksum=$csum destructive=$first_destructive"
fi
if [ -n "$setl" ] && [ -n "$csum" ] && [ "$setl" -lt "$csum" ]; then
  t_ok "the set is identified before its checksums are read"
else
  t_bad "checksums are verified before the set is validated"
fi

echo
if [ "$t_fail" -eq 0 ]; then echo "ALL OK ($t_pass checks)"; exit 0; fi
echo "FAILED $t_fail test(s) ($t_pass ok)"; exit 1
