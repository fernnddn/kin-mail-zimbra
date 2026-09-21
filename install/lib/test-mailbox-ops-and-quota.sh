#!/usr/bin/env bash
# Deleting, renaming, and how large a new mailbox is allowed to get.
#
# THE FAULT THESE EXIST FOR
#
# zmprov on a node without a local mailboxd falls back to LDAP-only mode
# without saying so, and in that mode the destructive operations refuse to run
# unattended: `da` and `ra` print "Continue? [Y]es, [N]o", read end-of-file,
# print "aborted" - and exit 0. Nothing in this product runs on a terminal.
#
# So on a multi deployment the console reported "Deleted" for a mailbox that
# was still receiving mail and still holding a seat, and "Renamed" for an
# address that had not changed. Measured on the live pair, 21 Sep 2026:
#
#   [ OK ] Deleted kin-del-test@...   EXIT=0   -> account still present
#   [ OK ] Renamed to kin-del-baru@... EXIT=0  -> name unchanged
#
# The stub below models exactly that: without -s the operation reports success
# and changes nothing. A test that passes here must therefore be testing the
# real difference, not a simulation of the fix.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/../08-create-mailbox.sh"
H="$(pwd)/../09-hardening.sh"
for f in "$S" "$H"; do
  [ -f "$f" ] || { printf 'FAIL %s not found\n' "$f"; exit 1; }
done
bash -n "$S" && pass "08-create-mailbox parses" || bad "08 syntax error"
bash -n "$H" && pass "09-hardening parses" || bad "09 syntax error"

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
ACCTS="${WORK}/accounts"
CALLS="${WORK}/calls"

say()  { :; }
ok()   { printf 'OK %s\n' "$*"; }
info() { printf 'INFO %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; }
MAIL_DOMAIN="example.test"
require_same_domain() {
  case "$1" in
    *@"$MAIL_DOMAIN") return 0 ;;
    *) fail "Refusing action outside configured domain"; return 2 ;;
  esac
}
kin_topology_is_split() { [ "${IS_SPLIT:-1}" -eq 1 ]; }
kin_mailbox_host() { printf '%s\n' "${MBOX_HOST-store.example.test}"; }

# The stub. -s is what separates "did it" from "said it did".
zimbra_cmd() {
  case "${1:-}" in
    zmhostname) printf '%s\n' "${STUB_HOSTNAME:-single.example.test}"; return 0 ;;
    zmprov) shift ;;
    *) return 0 ;;
  esac
  local soap=0
  if [ "${1:-}" = "-s" ]; then soap=1; shift 2; fi
  if [ "${1:-}" = "-l" ]; then shift; fi
  local op="${1:-}"; shift || true
  printf '%s soap=%s\n' "$op" "$soap" >>"$CALLS"
  case "$op" in
    ga) grep -qxF "${1:-}" "$ACCTS" ;;
    da)
      if [ "$soap" -eq 1 ]; then
        grep -vxF "${1:-}" "$ACCTS" >"${ACCTS}.t" || true; mv "${ACCTS}.t" "$ACCTS"
      else
        printf 'Continue? [Y]es, [N]o: aborted\n'
      fi
      return 0
      ;;
    ra)
      if [ "$soap" -eq 1 ]; then
        grep -vxF "${1:-}" "$ACCTS" >"${ACCTS}.t" || true; mv "${ACCTS}.t" "$ACCTS"
        printf '%s\n' "${2:-}" >>"$ACCTS"
      else
        printf 'Continue? [Y]es, [N]o: aborted\n'
      fi
      return 0
      ;;
  esac
}

eval "$(awk '/^mailbox_server\(\) \{/,/^\}/' "$S")"
eval "$(awk '/^zmprov_on_store\(\) \{/,/^\}/' "$S")"
eval "$(awk '/^account_exists\(\) \{/,/^\}/' "$S")"
eval "$(awk '/^delete_mailbox\(\) \{/,/^\}/' "$S")"
eval "$(awk '/^rename_mailbox\(\) \{/,/^\}/' "$S")"

reset_world() { printf 'jane@example.test\n' >"$ACCTS"; : >"$CALLS"; }

# --- delete -------------------------------------------------------------------
reset_world
OUT=$(delete_mailbox jane@example.test 2>&1; printf 'rc=%s' "$?")
if printf '%s' "$OUT" | grep -q "rc=0" && ! grep -qxF jane@example.test "$ACCTS"; then
  pass "delete actually removes the account"
else
  bad "delete did not remove the account ($OUT)"
fi
if grep -q '^da soap=1$' "$CALLS"; then
  pass "delete is aimed at the mail store, not the LDAP-only path"
else
  bad "delete still runs through the path that aborts and exits 0"
fi
if printf '%s' "$OUT" | grep -q 'DELETE_JSON:'; then
  pass "delete emits DELETE_JSON for the console"
else
  bad "delete emits no DELETE_JSON"
fi

# The heart of it: if the operation silently does nothing, say so.
reset_world
OUT=$(zmprov_on_store() { zimbra_cmd zmprov "$@"; }
  delete_mailbox jane@example.test 2>&1; printf 'rc=%s' "$?")
if printf '%s' "$OUT" | grep -q "rc=1" && printf '%s' "$OUT" | grep -q "still lists"; then
  pass "a delete that changed nothing is reported as a failure"
else
  bad "a delete that changed nothing was reported as success ($OUT)"
fi

reset_world
OUT=$(delete_mailbox ghost@example.test 2>&1; printf 'rc=%s' "$?")
printf '%s' "$OUT" | grep -q "rc=1" && pass "delete refuses an account that does not exist" \
  || bad "delete accepted a missing account"
OUT=$(delete_mailbox jane@elsewhere.test 2>&1; printf 'rc=%s' "$?")
printf '%s' "$OUT" | grep -q "rc=2" && pass "delete refuses another domain" \
  || bad "delete accepted another domain"

# --- rename -------------------------------------------------------------------
reset_world
OUT=$(rename_mailbox jane@example.test janet 2>&1; printf 'rc=%s' "$?")
if printf '%s' "$OUT" | grep -q "rc=0" \
  && grep -qxF janet@example.test "$ACCTS" && ! grep -qxF jane@example.test "$ACCTS"; then
  pass "rename actually moves the address"
else
  bad "rename did not move the address ($OUT)"
fi
if grep -q '^ra soap=1$' "$CALLS"; then
  pass "rename is aimed at the mail store"
else
  bad "rename still runs through the path that aborts and exits 0"
fi

reset_world
OUT=$(zmprov_on_store() { zimbra_cmd zmprov "$@"; }
  rename_mailbox jane@example.test janet 2>&1; printf 'rc=%s' "$?")
if printf '%s' "$OUT" | grep -q "rc=1"; then
  pass "a rename that changed nothing is reported as a failure"
else
  bad "a rename that changed nothing was reported as success ($OUT)"
fi

# Half-done is worse than not done: the console would print the new address
# while the old one still delivers.
reset_world
OUT=$(zmprov_on_store() { zimbra_cmd zmprov -s h "$@"; printf 'jane@example.test\n' >>"$ACCTS"; }
  rename_mailbox jane@example.test janet 2>&1; printf 'rc=%s' "$?")
if printf '%s' "$OUT" | grep -q "rc=1" && printf '%s' "$OUT" | grep -q "still lists the old address"; then
  pass "a rename that leaves the old address behind is a failure"
else
  bad "the old address surviving a rename went unreported ($OUT)"
fi

reset_world
printf 'janet@example.test\n' >>"$ACCTS"
OUT=$(rename_mailbox jane@example.test janet 2>&1; printf 'rc=%s' "$?")
printf '%s' "$OUT" | grep -q "rc=1" && pass "rename refuses an address already in use" \
  || bad "rename overwrote an existing address"

# --- which host is asked ------------------------------------------------------
IS_SPLIT=1 MBOX_HOST=store.example.test
[ "$(mailbox_server)" = "store.example.test" ] \
  && pass "on a multi deployment the mail store is asked" \
  || bad "wrong host on a multi deployment"
[ "$(IS_SPLIT=0 mailbox_server)" = "single.example.test" ] \
  && pass "on a single appliance the local host is asked" \
  || bad "wrong host on a single appliance"
# An unknown store must not become `-s ""`.
reset_world
OUT=$(IS_SPLIT=1 MBOX_HOST="" delete_mailbox jane@example.test 2>&1 || true)
if grep -q '^da soap=0$' "$CALLS"; then
  pass "an unknown store falls back rather than passing an empty -s"
else
  bad "an empty store name was passed to -s"
fi

# --- reading the size back ----------------------------------------------------
# Two faults stacked here. getQuotaUsage takes a SERVER and was handed the mail
# DOMAIN, so it resolved the domain as a hostname and returned nothing at all -
# which rendered as "no cap set" rather than as a failed call. Under that, the
# columns were read backwards. The first hid the second by never returning a row.
PY="${WORK}/parse.py"
awk '/^  python3 - "\$tmp_raw" "\$tmp_quota" <<.PY.$/{flag=1;next} /^PY$/{flag=0} flag' "$S" >"$PY"
[ -s "$PY" ] && pass "the listing parser could be lifted out" || bad "listing parser not found"

cat >"${WORK}/raw" <<'RAW'
# name capped@example.test
displayName: Capped
zimbraAccountStatus: active
zimbraMailQuota: 10737418240
RAW
printf 'capped@example.test 10737418240 2570\n' >"${WORK}/q"

field() {
  python3 "$PY" "${WORK}/raw" "${1}" 2>/dev/null | sed -n 's/^MAILBOX_JSON://p' \
    | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['$2'])" 2>/dev/null
}
[ "$(field "${WORK}/q" quota_bytes)" = "10737418240" ] \
  && pass "a 10 GB cap reads back as the cap" || bad "cap misread - columns swapped?"
[ "$(field "${WORK}/q" used_bytes)" = "2570" ] \
  && pass "bytes in use read back as usage" || bad "usage misread"
: >"${WORK}/empty"
[ "$(field "${WORK}/empty" quota_bytes)" = "10737418240" ] \
  && pass "the cap survives a mail store that did not answer" \
  || bad "the cap vanishes when the store is unreachable"

if grep -q 'getQuotaUsage "\$MAIL_DOMAIN"' "$S"; then
  bad "still asks getQuotaUsage for a domain, which it treats as a hostname"
else
  pass "no longer hands getQuotaUsage the mail domain"
fi

# --- the default size a new mailbox gets --------------------------------------
QUOTA_MARKER="${WORK}/marker"
# Read by the lifted configure_default_quota, which shellcheck cannot see.
# shellcheck disable=SC2034
KIN_NODE_ROLE=all
quota_stub() {
  zimbra_cmd() {
    case "${1:-}" in
      zmhostname) printf 'store.example.test\n'; return 0 ;;
      zmprov) shift ;;
      *) return 0 ;;
    esac
    [ "${1:-}" = "-s" ] && shift 2
    [ "${1:-}" = "-l" ] && shift
    case "${1:-}" in
      mc) printf 'mc %s\n' "${4:-}" >>"$CALLS"; printf '%s' "${4:-}" >"${WORK}/cosvalue"; return "${MC_RESULT:-0}" ;;
      gc) printf 'zimbraMailQuota: %s\n' "$(cat "${WORK}/cosvalue" 2>/dev/null || echo 0)"; return 0 ;;
      getQuotaUsage) printf '%s\n' "${USAGE:-}"; return 0 ;;
    esac
    return 0
  }
}
eval "$(awk '/^configure_default_quota\(\) \{/,/^\}/' "$H")"
# A Zimbra tree that is only a directory, so these run everywhere CI does.
ZIMBRA_ROOT="${WORK}/zimbra"
mkdir -p "$ZIMBRA_ROOT"

if true; then
  quota_stub; : >"$CALLS"; rm -f "$QUOTA_MARKER" "${WORK}/cosvalue"
  DEFAULT_QUOTA_GB=10 configure_default_quota >/dev/null 2>&1
  if grep -q '^mc 10737418240$' "$CALLS"; then
    pass "10 GB is written as bytes to the default COS"
  else
    bad "the default size was not written (calls: $(tr '\n' ';' <"$CALLS"))"
  fi
  [ -f "$QUOTA_MARKER" ] && pass "applying it is recorded" || bad "nothing recorded that it was applied"

  : >"$CALLS"
  DEFAULT_QUOTA_GB=20 configure_default_quota >/dev/null 2>&1
  if grep -q '^mc ' "$CALLS"; then
    bad "a later run overwrote a size the operator may have chosen"
  else
    pass "a later run leaves the chosen size alone"
  fi

  quota_stub; : >"$CALLS"; rm -f "$QUOTA_MARKER" "${WORK}/cosvalue"
  KIN_NODE_ROLE=edge DEFAULT_QUOTA_GB=10 configure_default_quota >/dev/null 2>&1
  if grep -q '^mc ' "$CALLS"; then
    bad "the edge also wrote the directory-wide default"
  else
    pass "the edge leaves the directory-wide default to the mailbox"
  fi
  KIN_NODE_ROLE=all

  # Never stop mail that is already flowing.
  quota_stub; : >"$CALLS"; rm -f "$QUOTA_MARKER" "${WORK}/cosvalue"
  OUT=$(USAGE="big@example.test 0 99999999999" DEFAULT_QUOTA_GB=10 configure_default_quota 2>&1)
  if grep -q '^mc ' "$CALLS"; then
    bad "capped a deployment whose mailboxes are already larger"
  else
    printf '%s' "$OUT" | grep -q "already larger" \
      && pass "refuses to cap mailboxes that are already bigger, and names them" \
      || bad "skipped silently instead of explaining"
  fi

  for badv in "abc" "" "12345" "1.5"; do
    quota_stub; : >"$CALLS"; rm -f "$QUOTA_MARKER" "${WORK}/cosvalue"
    DEFAULT_QUOTA_GB="$badv" configure_default_quota >/dev/null 2>&1
    if grep -q '^mc ' "$CALLS"; then
      bad "accepted an implausible default of '${badv}'"
    else
      pass "refuses a default of '${badv}'"
    fi
  done

  # 0 is Zimbra's spelling for "no cap" and must still be applied.
  quota_stub; : >"$CALLS"; rm -f "$QUOTA_MARKER" "${WORK}/cosvalue"
  DEFAULT_QUOTA_GB=0 configure_default_quota >/dev/null 2>&1
  grep -q '^mc 0$' "$CALLS" && pass "0 is applied as no cap" || bad "0 was not applied"

  # A write that did not take must not be recorded as done.
  quota_stub; : >"$CALLS"; rm -f "$QUOTA_MARKER" "${WORK}/cosvalue"
  MC_RESULT=1 DEFAULT_QUOTA_GB=10 configure_default_quota >/dev/null 2>&1
  [ -f "$QUOTA_MARKER" ] && bad "recorded a write that failed" || pass "a failed write is not recorded"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All mailbox-ops and default-size tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
