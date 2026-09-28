#!/usr/bin/env bash
# =============================================================================
# The console must be able to REACH the things it needs, as the user it runs as.
#
# WHAT THIS EXISTS FOR
#
# 28 September 2026, production deployment. The console answered every
# privileged action with
#
#   cannot connect to privhelper: [Errno 13] Permission denied
#
# Deploy, Install monitoring, the mail gateway page - all of it. The cause was
# an installer helper running chmod 0700 on /run/kin-mail, the privhelper's
# socket directory, which is root:kin-console 0750 precisely so the console can
# traverse it.
#
# Every check missed it, and they missed it the same way: they were asked as
# root, and root ignores permissions. bootstrap.sh confirmed the socket existed
# and printed its mode - 775, unchanged, correct - while the directory holding
# it had become unreachable. 1578 unit tests passed. CI was green on seven
# jobs. The appliance was broken.
#
# The only check that sees this is one asked as the service account. These
# tests exercise that check against real files and real modes.
# =============================================================================
set -u
cd "$(dirname "$0")" || exit 1

fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad() {
  printf 'FAIL %s\n' "$1"
  fails=$((fails + 1))
  shift
  for l in "$@"; do printf '       %s\n' "$l"; done
}

B="$(pwd)/../bootstrap.sh"
[ -f "$B" ] || {
  printf 'FAIL bootstrap.sh not found\n'
  exit 1
}
bash -n "$B" && pass "bootstrap.sh parses" || bad "syntax error"

# --- the socket probe connects, rather than looking ---------------------------
probe=$(sed -n "/^sock_probe='/,/'\$/p" "$B")
if [ -z "$probe" ]; then
  bad "the socket probe is gone" \
    "without it, a socket that exists and cannot be reached reads as healthy"
else
  printf '%s' "$probe" | grep -q 's.connect(' &&
    pass "it connects to the socket, not just stats it" ||
    bad "the probe does not actually connect"
  printf '%s' "$probe" | grep -q 'settimeout' &&
    pass "and cannot hang the install" || bad "the probe has no timeout"
fi
# Asked AS the service account. Asked as root it proves nothing.
if grep -q 'su -s /bin/bash -c "python3 -c .\${sock_probe}' "$B"; then
  pass "the probe runs as the console's own account"
else
  bad "the socket probe does not run as \$SVC_USER" \
    "root ignores permissions, so this is the whole point of the check"
fi

# --- it must be a hard failure ------------------------------------------------
if sed -n '/CANNOT connect to the privhelper socket/,/^fi$/p' "$B" | grep -q 'exit 1'; then
  pass "an unreachable helper stops the install"
else
  bad "an unreachable helper is only a warning" \
    "the console would start and fail at whatever was pressed first"
fi
if sed -n '/CANNOT connect to the privhelper socket/,/^fi$/p' "$B" | grep -q '/run/kin-mail'; then
  pass "the message names the directory, which is where the fault always is"
else
  bad "the failure message does not point at the directory"
fi

# --- probe_path actually discriminates ----------------------------------------
# Extracted and run against real files, because a permissions check that has
# never been shown to fail is not a check.
fn=$(sed -n '/^probe_path() {/,/^}/p' "$B")
if [ -z "$fn" ]; then
  bad "probe_path is not defined"
else
  work=$(mktemp -d)
  trap 'chmod -R u+rwX "$work" 2>/dev/null; rm -rf "$work"' EXIT

  # Stand in for su: run the test as this user instead of the service account.
  # The logic under test is the same; only the identity differs.
  # shellcheck disable=SC2034
  SVC_USER="$(id -un)"
  # shellcheck disable=SC2317
  su() { # su -s /bin/bash -c "<cmd>" <user>
    shift 3
    local cmd="$1"
    bash -c "$cmd"
  }
  ok() { LAST=ok; }
  fail() { LAST=fail; }
  info() { :; }
  # shellcheck source=/dev/null
  eval "$fn"

  reachable="$work/reachable"
  mkdir -p "$reachable"
  printf 'x' >"$reachable/file"

  LAST=""
  probe_path -r "$reachable/file" "why"
  [ "$LAST" = ok ] && pass "a readable file passes" || bad "a readable file was reported unreachable"

  LAST=""
  probe_path -r "$work/absent-file" "why"
  [ "$LAST" = "" ] && pass "a file that does not exist yet is not a failure" ||
    bad "an absent path was treated as a permission fault"

  # The real shape, simulated the way production actually sees it.
  #
  # bootstrap.sh runs as root, so its `[ -e ]` succeeds - root traverses any
  # directory - and the su'd `test -r` is the half that fails. That asymmetry
  # IS the bug: the path is plainly there, and the account that has to use it
  # cannot reach it.
  #
  # chmod'ing the directory here would hide it instead of showing it, because
  # this test does not run as root: the outer `[ -e ]` would fail too and the
  # path would look merely absent. So the service account is denied directly.
  DENY="$reachable/file"
  # shellcheck disable=SC2317
  su() {
    shift 3
    local cmd="$1"
    case "$cmd" in
      *"$DENY"*) return 1 ;;
      *) bash -c "$cmd" ;;
    esac
  }
  LAST=""
  probe_path -r "$reachable/file" "why"
  [ "$LAST" = fail ] &&
    pass "a path root can see and the service account cannot is caught" ||
    bad "a file the service account cannot reach read as healthy" \
      "this is exactly the failure that shipped"
  DENY="__nothing__"

  LAST=""
  probe_path -w "$reachable" "why"
  [ "$LAST" = ok ] && pass "a writable directory passes" || bad "a writable directory failed"

  chmod 0500 "$reachable"
  LAST=""
  probe_path -w "$reachable" "why"
  if [ "$LAST" = fail ]; then
    pass "a read-only state directory is caught"
  elif [ "$(id -u)" = 0 ]; then
    pass "skipped: running as root"
  else
    bad "a read-only state directory read as writable"
  fi
  chmod 0755 "$reachable"
fi

# --- the paths that matter are all covered ------------------------------------
for path in 'DATA_ROOT}"' 'users.json' 'session.secret' 'tls/cert.pem' 'frontend/dist' '/run/kin-mail'; do
  # Anchored past leading whitespace only: an unanchored match also finds the
  # line after somebody comments it out, which is the most likely way one of
  # these stops running.
  if grep -qE "^[[:space:]]*probe_path .*${path}" "$B"; then
    pass "checked: ${path}"
  else
    bad "not checked: ${path}"
  fi
done

if [ "$fails" -eq 0 ]; then
  printf 'All console-reachability tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
