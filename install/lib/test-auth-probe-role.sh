#!/usr/bin/env bash
# Where a login can be proved, and what it means when it cannot.
#
# Creating an account and setting its password are directory writes and work
# from any node. Logging in is a mailboxd operation, and on a split mailboxd
# runs on the mailbox only. The edge is the machine the operator actually logs
# into, so without this distinction every account created on a healthy split is
# reported as failed, the healthcheck fails a node that is fine, and hybrid auth
# stops the pipeline on the line "mailboxd is not usable" - which is true, and
# which is not a fault.
#
# The three return codes are the contract:
#   0  proved
#   1  broken
#   2  ready, but this node cannot prove it
#
# 2 must never collapse into 0. A green check nobody earned is worse than a
# blocked one, because it is the kind of result that gets believed.
set -u
cd "$(dirname "$0")" || exit 1

CONF="$(pwd)/../00-config.sh"
[ -f "$CONF" ] || { echo "missing $CONF" >&2; exit 1; }

pass=0; fails=0
ok()  { pass=$((pass+1)); printf 'ok  %s\n' "$1"; }
bad() { fails=$((fails+1)); printf 'FAIL %s\n' "$1"; }

# Lift only the two functions out of the wizard. Running 00-config.sh would
# start it; this tests the code without that.
FN_PROVABLE=$(awk '/^kin_auth_provable_here\(\) \{/,/^\}/' "$CONF")
FN_ENSURE=$(awk '/^ensure_kin_test_mailbox\(\) \{/,/^\}/' "$CONF")
[ -n "$FN_PROVABLE" ] || { echo "kin_auth_provable_here not found in 00-config.sh" >&2; exit 1; }
[ -n "$FN_ENSURE" ]   || { echo "ensure_kin_test_mailbox not found in 00-config.sh" >&2; exit 1; }
eval "$FN_PROVABLE"
eval "$FN_ENSURE"

# --- stubs -------------------------------------------------------------------
# Calls are recorded in a FILE, not a variable: ensure_kin_test_mailbox runs
# zmprov inside $( ), which is a subshell, and a variable written there does
# not come back. Recording into a variable made this suite report that the
# create never happened when it had.
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
CALLS="${WORK}/calls"
AUTH_RESULT=0      # what zimbra_user_auth_ok returns
CA_RESULT=0        # what zmprov ca / sp return
ACCOUNT_MISSING=0  # zmprov ga exit status: 0 = the account is there

zimbra_cmd() {
  case "$2" in
    ga) printf 'ga\n' >>"$CALLS"; return "$ACCOUNT_MISSING" ;;
    ca) printf 'ca\n' >>"$CALLS"; [ "$CA_RESULT" -eq 0 ] || echo "ca refused" >&2; return "$CA_RESULT" ;;
    sp) printf 'sp\n' >>"$CALLS"; [ "$CA_RESULT" -eq 0 ] || echo "sp refused" >&2; return "$CA_RESULT" ;;
  esac
  return 0
}
zimbra_user_auth_ok() { return "$AUTH_RESULT"; }

reset_stubs() { : > "$CALLS"; AUTH_RESULT=0; CA_RESULT=0; ACCOUNT_MISSING=0; }
called() { grep -qx "$1" "$CALLS"; }
calls_seen() { tr '\n' ' ' < "$CALLS"; }

single_appliance() { unset -f kin_topology_is_split kin_node_role 2>/dev/null || true; }
split_as() {
  kin_topology_is_split() { return 0; }
  kin_node_role() { printf '%s' "$1"; }
  eval "kin_node_role() { printf '%s' '$1'; }"
}

run_ensure() { ensure_kin_test_mailbox "$@" 2>/dev/null; printf '%s' $?; }

# --- a single appliance is unchanged ----------------------------------------
single_appliance
reset_stubs
if kin_auth_provable_here; then ok "single appliance: a login is provable here"
else bad "single appliance: refused to prove a login on the only machine there is"; fi

reset_stubs; AUTH_RESULT=0
[ "$(run_ensure a@example.test pw)" = 0 ] \
  && ok "single appliance: a working account returns 0" \
  || bad "single appliance: a working account did not return 0"

reset_stubs; AUTH_RESULT=1
[ "$(run_ensure a@example.test pw)" = 1 ] \
  && ok "single appliance: a failing login still returns 1" \
  || bad "single appliance: a failing login no longer reports failure"

# --- the mailbox half of a split is the machine that CAN prove it ------------
split_as mailbox
reset_stubs
if kin_auth_provable_here; then ok "split mailbox: a login is provable here"
else bad "split mailbox: refused on the node that actually runs mailboxd"; fi

reset_stubs; AUTH_RESULT=1
[ "$(run_ensure a@example.test pw)" = 1 ] \
  && ok "split mailbox: a real auth failure is still a failure" \
  || bad "split mailbox: an auth failure was excused"

# --- the edge cannot, and says so with 2, not 0 and not 1 --------------------
split_as edge
reset_stubs
if kin_auth_provable_here; then bad "split edge: claimed a login is provable without mailboxd"
else ok "split edge: a login is not provable here"; fi

reset_stubs; AUTH_RESULT=1     # would fail if it were attempted at all
[ "$(run_ensure a@example.test pw)" = 2 ] \
  && ok "split edge: reports 2 (ready, not provable), not 0 and not 1" \
  || bad "split edge: did not report 2 for an account it cannot log in to"

# The account still has to be really created - that half is a directory write
# and works from either node. Skipping it would leave the healthcheck's mail
# flow test with no mailbox to send to.
reset_stubs; ACCOUNT_MISSING=1; AUTH_RESULT=1
run_ensure a@example.test pw >/dev/null
if called ca; then ok "split edge: a missing account is still created from here"
else bad "split edge: skipped the create as well as the probe ($(calls_seen))"; fi

reset_stubs; ACCOUNT_MISSING=0; AUTH_RESULT=1
run_ensure a@example.test pw >/dev/null
if called sp; then ok "split edge: an existing account still has its password aligned"
else bad "split edge: skipped the password alignment ($(calls_seen))"; fi

# A directory write that genuinely fails is still a failure, wherever we are.
reset_stubs; ACCOUNT_MISSING=1; CA_RESULT=1
[ "$(run_ensure a@example.test pw)" = 1 ] \
  && ok "split edge: a refused create is still 1, not 2" \
  || bad "split edge: a refused create was reported as merely unprovable"

# --- the callers must not treat 2 as a failure -------------------------------
# Structural, because each of these is a whole stage. The point is that none of
# them exits non-zero on a 2.
STAGES="$(pwd)/.."
if grep -q 'LOCAL_PREP_RC' "${STAGES}/06-hybrid-auth.sh" \
   && grep -q 'kin_auth_provable_here' "${STAGES}/06-hybrid-auth.sh"; then
  ok "06-hybrid-auth knows a split edge cannot prove a login"
else
  bad "06-hybrid-auth still exits 1 on a node with no mailboxd"
fi
if grep -q 'RC1' "${STAGES}/05-healthcheck.sh" \
   && grep -qE 'b "Local auth not provable here' "${STAGES}/05-healthcheck.sh"; then
  ok "05-healthcheck reports it as blocked, not failed and not passed"
else
  bad "05-healthcheck does not have a blocked verdict for an unprovable login"
fi
if grep -q 'auth_probe_skipped' "${STAGES}/08-create-mailbox.sh"; then
  ok "08-create-mailbox does not report a created account as failed"
else
  bad "08-create-mailbox still fails every account created on a split edge"
fi
# ...and 08 must return success there, or the console shows a red box for an
# account that exists and has already spent a seat.
if awk '/kin_auth_provable_here/,/^  fi$/' "${STAGES}/08-create-mailbox.sh" | grep -q 'return 0'; then
  ok "08-create-mailbox returns success for an account it could not probe"
else
  bad "08-create-mailbox still returns non-zero for a perfectly good account"
fi

echo
if [ "$fails" -eq 0 ]; then printf 'ALL OK (%s checks)\n' "$pass"; exit 0; fi
printf 'FAILED %s test(s) (%s ok)\n' "$fails" "$pass"; exit 1
