#!/usr/bin/env bash
# Per-mailbox storage caps, and the two reading bugs that hid them.
#
# The Storage column in the console was blank for every mailbox since it was
# written, and it read as "no cap set" rather than as a failed call. Two faults
# stacked:
#
#   1. getQuotaUsage takes a SERVER and was handed the mail DOMAIN. Zimbra
#      resolves the argument as a hostname, so it answered "No address
#      associated with hostname <domain>" and returned nothing at all.
#   2. Its output is "<account> <limit> <used>" and the parser read it as
#      used-then-limit. Proven on a live store: setting a 1 GiB cap moved the
#      FIRST number. Fault 1 hid fault 2 by never returning a row to misread.
#
# Both are tested here against the real parser lifted out of the script, not
# against a copy of it.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/../08-create-mailbox.sh"
[ -f "$S" ] || { printf 'FAIL 08-create-mailbox.sh not found\n'; exit 1; }
bash -n "$S" && pass "08-create-mailbox parses" || bad "syntax error"

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# --- the reading path ---------------------------------------------------------
# Lift the embedded parser out and run it on fixtures. This is the code that
# had the columns the wrong way round.
PY="${WORK}/parse.py"
awk '/^  python3 - "\$tmp_raw" "\$tmp_quota" <<.PY.$/{flag=1;next} /^PY$/{flag=0} flag' "$S" > "$PY"
if [ -s "$PY" ]; then
  pass "the listing parser could be lifted out of the script"
else
  bad "could not find the listing parser in the script"
  printf '%s test(s) failed\n' "$((fails + 1))"
  exit 1
fi

cat > "${WORK}/raw" <<'RAW'
# name capped@example.test
displayName: Capped User
zimbraAccountStatus: active
zimbraMailQuota: 5368709120
# name uncapped@example.test
displayName: Uncapped User
zimbraAccountStatus: active
zimbraMailQuota: 0
RAW
# "<account> <limit> <used>" - the order a live store actually prints.
cat > "${WORK}/quota" <<'Q'
capped@example.test 5368709120 1073741824
uncapped@example.test 0 2570
Q

OUT=$(python3 "$PY" "${WORK}/raw" "${WORK}/quota" 2>&1) || OUT="PARSER FAILED: $OUT"
# The parser prints MAILBOX_JSON:<json>; read it back the way the console does.
read_field() {
  printf '%s' "$OUT" | sed -n 's/^MAILBOX_JSON://p' | python3 -c "
import json,sys
rows = json.load(sys.stdin)
row = next(r for r in rows if r['email'] == '$1')
print(row['$2'])
" 2>/dev/null
}

if [ "$(read_field capped@example.test quota_bytes)" = "5368709120" ]; then
  pass "a 5 GiB cap is reported as the cap"
else
  bad "cap misread (got '$(read_field capped@example.test quota_bytes)') - columns swapped?"
fi
if [ "$(read_field capped@example.test used_bytes)" = "1073741824" ]; then
  pass "the bytes in use are reported as the usage"
else
  bad "usage misread (got '$(read_field capped@example.test used_bytes)')"
fi
# Zimbra writes 0 for "no cap". That must survive as 0, not become the usage.
if [ "$(read_field uncapped@example.test quota_bytes)" = "0" ]; then
  pass "no cap stays no cap"
else
  bad "an uncapped mailbox reported a cap of '$(read_field uncapped@example.test quota_bytes)'"
fi
if [ "$(read_field uncapped@example.test used_bytes)" = "2570" ]; then
  pass "an uncapped mailbox still reports its usage"
else
  bad "usage lost on an uncapped mailbox"
fi

# The cap comes from the directory, so it must survive a mail store that
# answered nothing at all - which is exactly what happened for months.
: > "${WORK}/empty"
OUT=$(python3 "$PY" "${WORK}/raw" "${WORK}/empty" 2>&1) || OUT=""
if [ "$(read_field capped@example.test quota_bytes)" = "5368709120" ]; then
  pass "the cap survives an unreachable mail store"
else
  bad "the cap vanishes when the mail store cannot be reached"
fi

# --- the server argument ------------------------------------------------------
if grep -q 'getQuotaUsage "\$MAIL_DOMAIN"' "$S"; then
  bad "still asks getQuotaUsage for a domain, which it treats as a hostname"
else
  pass "no longer hands getQuotaUsage the mail domain"
fi
if grep -q 'zmprov -s "\$host" getQuotaUsage "\$host"' "$S"; then
  pass "targets the mail store by name, and over SOAP"
else
  bad "does not point zmprov at the mail store; an edge refuses this call"
fi

# --- setting a cap ------------------------------------------------------------
FN=$(awk '/^set_quota\(\) \{/,/^\}/' "$S")
[ -n "$FN" ] || { bad "set_quota not found"; printf '%s test(s) failed\n' "$fails"; exit 1; }

CALLS="${WORK}/calls"
say()  { :; }
ok()   { printf 'OK %s\n' "$*"; }
info() { printf 'INFO %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; }
require_same_domain() { [ "${SAME_DOMAIN:-0}" -eq 0 ]; return $?; }
ACCOUNT_MISSING=0
MA_RESULT=0
zimbra_cmd() {
  case "$3" in
    ga) return "$ACCOUNT_MISSING" ;;
    ma) printf 'ma %s %s\n' "$4" "$6" >>"$CALLS"; return "$MA_RESULT" ;;
  esac
  return 0
}
quota_usage_lines() { printf '%s\n' "${USAGE_LINE:-}"; }
eval "$FN"

run_set_quota() { : > "$CALLS"; set_quota "$@" 2>&1; }

# A cap is not a create: the seat gate must never be consulted.
kin_quota_gate_allow_new_mailbox() { printf 'GATE CALLED\n'; return 0; }
OUT=$(SAME_DOMAIN=0 USAGE_LINE="a@b.test 0 10" run_set_quota a@b.test 5368709120)
if printf '%s' "$OUT" | grep -q "GATE CALLED"; then
  bad "a storage cap consulted the seat gate"
else
  pass "a storage cap never consults the seat gate"
fi
if grep -q '^ma a@b.test 5368709120$' "$CALLS"; then
  pass "writes the cap to zimbraMailQuota"
else
  bad "did not write the cap (calls: $(tr '\n' ';' < "$CALLS"))"
fi
if printf '%s' "$OUT" | grep -q 'QUOTA_JSON:'; then
  pass "emits QUOTA_JSON for the console to read"
else
  bad "emits no QUOTA_JSON"
fi

# Setting a cap under what the mailbox already holds stops mail arriving. That
# must be said at the moment it is done, not discovered days later.
OUT=$(SAME_DOMAIN=0 USAGE_LINE="a@b.test 0 9999999999" run_set_quota a@b.test 1048576)
if printf '%s' "$OUT" | grep -q "WARN.*over the new cap"; then
  pass "warns when the cap is below what the mailbox already holds"
else
  bad "silently caps a mailbox below its current size"
fi
OUT=$(SAME_DOMAIN=0 USAGE_LINE="a@b.test 0 10" run_set_quota a@b.test 1048576)
if printf '%s' "$OUT" | grep -q "WARN.*over the new cap"; then
  bad "warns about a cap that is not actually exceeded"
else
  pass "stays quiet when the mailbox fits"
fi

# 0 is the only way to remove a cap and must not be mistaken for missing input.
OUT=$(SAME_DOMAIN=0 USAGE_LINE="a@b.test 0 10" run_set_quota a@b.test 0)
if grep -q '^ma a@b.test 0$' "$CALLS" && printf '%s' "$OUT" | grep -q "no storage cap"; then
  pass "0 removes the cap"
else
  bad "0 did not remove the cap"
fi

# The last one is all digits and passes a naive digit test, but is longer than
# a machine integer: `[ -gt ]` then fails with "integer expression expected"
# and evaluates FALSE, which let it through the ceiling check to zmprov.
for bad_value in "" "-1" "abc" "1.5" "1e9" "9007199254740993" \
  "123456789012345678901234567890123456789"; do
  OUT=$(SAME_DOMAIN=0 run_set_quota a@b.test "$bad_value"; printf 'rc=%s' "$?")
  if printf '%s' "$OUT" | grep -q "rc=2" && [ ! -s "$CALLS" ]; then
    pass "refuses a cap of '${bad_value}' without calling zmprov"
  else
    bad "accepted a cap of '${bad_value}'"
  fi
done

OUT=$(SAME_DOMAIN=0 ACCOUNT_MISSING=1 run_set_quota ghost@b.test 1048576; printf 'rc=%s' "$?")
if printf '%s' "$OUT" | grep -q "rc=1" && [ ! -s "$CALLS" ]; then
  pass "refuses an account that does not exist"
else
  bad "set a cap on an account that does not exist"
fi

OUT=$(SAME_DOMAIN=1 run_set_quota a@elsewhere.test 1048576; printf 'rc=%s' "$?")
if [ ! -s "$CALLS" ]; then
  pass "refuses an address outside the configured domain"
else
  bad "set a cap outside the configured domain"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All mailbox storage cap tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
