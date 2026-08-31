#!/usr/bin/env bash
# Tests for the apt lock handling.
#
# What this protects: a cloud image starts apt-daily and unattended-upgrades
# within seconds of first boot, and they hold /var/lib/dpkg/lock-frontend for
# minutes. An installer that treats that as a failure stops a good deployment
# thirty seconds in, on a host where the same commands work by hand two minutes
# later. That is what happened on the first Phase 9 attempt.
set -u

LIB="$(cd "$(dirname "$0")" && pwd)/apt-lock.sh"
[ -f "$LIB" ] || { echo "missing $LIB" >&2; exit 1; }

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
ACTIONS="$TMP/actions"; : >"$ACTIONS"
export ACTIONS

cat >"$TMP/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >>"$ACTIONS"
if [ "$1" = "is-active" ]; then
  # --quiet <unit>
  for u in $ACTIVE_UNITS; do [ "$u" = "$3" ] && exit 0; done
  exit 3
fi
exit 0
STUB
cat >"$TMP/bin/fuser" <<'STUB'
#!/usr/bin/env bash
# Holder disappears after HOLD_FOR probes, simulating a lock that clears.
n=0
[ -f "$TMP/probes" ] && n=$(cat "$TMP/probes")
n=$((n+1)); echo "$n" >"$TMP/probes"
[ "$n" -le "${HOLD_FOR:-0}" ] && { echo "${FAKE_PID:-6811}"; exit 0; }
exit 1
STUB
cat >"$TMP/bin/dpkg" <<'STUB'
#!/usr/bin/env bash
echo "dpkg $*" >>"$ACTIONS"
[ "$1" = "--audit" ] && { printf '%s' "${DPKG_AUDIT:-}"; exit 0; }
exit 0
STUB
cat >"$TMP/bin/apt-get" <<'STUB'
#!/usr/bin/env bash
echo "apt-get $*" >>"$ACTIONS"
exit 0
STUB
cat >"$TMP/bin/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$TMP"/bin/*
export PATH="$TMP/bin:$PATH" TMP
export KIN_DPKG_LOCK_FILE="$TMP/lock-frontend"
# shellcheck disable=SC1090
. "$LIB"

# --- kin_apt always passes a lock timeout ----------------------------------
: >"$ACTIONS"
kin_apt -y install dnsmasq >/dev/null 2>&1
if grep -q 'DPkg::Lock::Timeout=' "$ACTIONS"; then
  ok "kin_apt tells apt to wait for the lock instead of failing"
else
  bad "kin_apt does not set DPkg::Lock::Timeout" "$(cat "$ACTIONS")"
fi
grep -q 'install dnsmasq' "$ACTIONS" && ok "kin_apt passes the caller's arguments through" || bad "arguments lost"

# --- waiting for a lock that clears -----------------------------------------
: >"$ACTIONS"; rm -f "$TMP/probes"
HOLD_FOR=3 FAKE_PID=6811 apt_wait_for_lock 60 >"$TMP/out" 2>&1
rc=$?
[ "$rc" -eq 0 ] && ok "a lock that clears is waited out, not failed" || bad "wait returned $rc"
grep -q "Waiting for the package lock" "$TMP/out" && ok "it says what it is waiting for" || bad "silent wait"

# --- a lock that never clears reports who holds it --------------------------
: >"$ACTIONS"; rm -f "$TMP/probes"
HOLD_FOR=100000 FAKE_PID=6811 apt_wait_for_lock 6 >"$TMP/out" 2>&1
[ $? -ne 0 ] && ok "a lock that never clears times out rather than hanging forever" || bad "no timeout"
grep -q "6811" "$TMP/out" && ok "the timeout names the process holding the lock" || bad "holder not named" "$(cat "$TMP/out")"

# --- pausing and restoring the background updaters --------------------------
: >"$ACTIONS"
ACTIVE_UNITS="unattended-upgrades.service apt-daily.timer" apt_pause_background_upgrades >/dev/null
grep -q "systemctl stop .*unattended-upgrades.service" "$ACTIONS" && ok "unattended-upgrades is stopped for the install" || bad "not stopped" "$(cat "$ACTIONS")"
grep -q "apt-daily.timer" "$ACTIONS" && ok "the timer is stopped too, or it just starts again" || bad "timer left running"
# Nothing is killed: a SIGKILL mid-transaction breaks the package database.
grep -qE 'kill|pkill' "$ACTIONS" && bad "it kills the holder instead of stopping the service" || ok "it stops services, it does not kill dpkg"

: >"$ACTIONS"
apt_resume_background_upgrades >/dev/null
grep -q "systemctl start .*unattended-upgrades.service" "$ACTIONS" && ok "what was paused is started again" || bad "not restored" "$(cat "$ACTIONS")"

# --- only what was actually running gets restored ---------------------------
: >"$ACTIONS"
ACTIVE_UNITS="" apt_pause_background_upgrades >/dev/null
apt_resume_background_upgrades >/dev/null
[ -s "$ACTIONS" ] && grep -q "systemctl start" "$ACTIONS" && bad "starts units that were not running" || ok "units that were already stopped are left stopped"

# --- an interrupted transaction is finished, not worked around --------------
: >"$ACTIONS"
DPKG_AUDIT="" apt_repair_dpkg >/dev/null
grep -q "dpkg --configure -a" "$ACTIONS" && bad "runs configure when nothing is broken" || ok "a clean dpkg is left alone"
: >"$ACTIONS"
DPKG_AUDIT="half-configured foo" apt_repair_dpkg >/dev/null
grep -q "dpkg --configure -a" "$ACTIONS" && ok "an interrupted transaction is finished before continuing" || bad "broken dpkg not repaired"

# --- the stage that failed must actually use all of it ----------------------
STAGE="$(cd "$(dirname "$0")/.." && pwd)/02-prepare-os.sh"
grep -q 'apt_prepare' "$STAGE" && ok "02-prepare-os clears the way before its first apt call" || bad "02 does not call apt_prepare"
# `grep -q | grep -v` is a trap: -q prints nothing, so the second grep always
# fails and the check always passes. Count real lines instead.
bare=$(grep -nE '(^|[;&|(]  *|! )apt-get ' "$STAGE" | grep -cvE 'warn |info |^ *#' || true)
if [ "${bare:-0}" -eq 0 ]; then
  ok "every apt call in 02-prepare-os goes through kin_apt"
else
  bad "02-prepare-os still calls apt-get directly (${bare} line(s))" \
    "$(grep -nE '(^|[;&|(]  *|! )apt-get ' "$STAGE" | grep -vE 'warn |info |^ *#')"
fi
grep -q 'kin_stage3_cleanup' "$STAGE" && ok "02 restores the background updaters however it exits" || bad "no cleanup trap"
grep -q 'apt_ensure_tmpdir' "$STAGE" && ok "02 recreates /tmp after the base-image upgrade" || bad "02 does not restore /tmp after upgrade"
grep -q 'apt_ensure_tmpdir' "$LIB" && ok "apt-lock defines apt_ensure_tmpdir" || bad "missing apt_ensure_tmpdir"

# --- /tmp missing after systemd upgrade must not kill apt -------------------
FAKE_TMP="$TMP/gone-tmp"
rm -rf "$FAKE_TMP"
KIN_APT_TMPDIR="$FAKE_TMP" KIN_APT_VARTMP="$TMP/gone-vartmp" apt_ensure_tmpdir
if [ -d "$FAKE_TMP" ]; then
  ok "apt_ensure_tmpdir creates a missing /tmp"
else
  bad "apt_ensure_tmpdir left tmpdir missing"
fi
[ "$TMPDIR" = "$FAKE_TMP" ] && ok "apt_ensure_tmpdir exports TMPDIR for apt/mkstemp" || bad "TMPDIR not set ($TMPDIR)"

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
