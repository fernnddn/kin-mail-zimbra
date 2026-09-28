#!/usr/bin/env bash
# =============================================================================
# The dead-man has to survive the one thing a locked-out operator will do.
#
# 10-host-firewall.sh applies ufw and arms a dead-man: if nobody confirms
# access within KIN_UFW_DEADMAN_SEC, ufw is disabled again. That is the whole
# safety story for the highest-risk stage in the product.
#
# It was a userspace loop holding its state in /run, which is tmpfs. The
# reaction to losing access to a machine is to reboot it from the hypervisor -
# and that kills the loop and clears /run, leaving ufw enabled (which is what
# ufw is for) with exactly the rules that locked the operator out, and nothing
# left to undo them. The net was gone at the one moment it existed for.
#
# The asymmetry that makes the fix safe: the boot unit can only DISABLE ufw. Its
# worst possible misbehaviour leaves the machine as it was before stage 10 ran,
# which is what the dead-man does anyway. What it must never become is a
# firewall that silently switches itself off on every reboot - so these tests
# pin both directions: it fires when an apply was never confirmed, and it is
# gone once it was.
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

S="$(pwd)/../10-host-firewall.sh"
[ -f "$S" ] || {
  printf 'FAIL 10-host-firewall.sh not found\n'
  exit 1
}
bash -n "$S" && pass "parses" || bad "syntax error"

# --- arming writes state that outlives tmpfs ---------------------------------
arm=$(awk '/^arm_deadman_across_reboot\(\) \{/,/^\}/' "$S")
if [ -z "$arm" ]; then
  bad "arm_deadman_across_reboot is not defined" \
    "the dead-man lives only in /run, so a reboot leaves ufw enabled" \
    "with the rules that locked the operator out"
else
  # The path is assigned OUTSIDE the function, so read it from the assignment
  # rather than from the body - which only ever mentions the variable, and made
  # this check pass on a marker moved back to tmpfs.
  marker_path=$(sed -n 's/^DEADMAN_MARKER=//p' "$S" | head -1)
  case "$marker_path" in
    "" ) bad "DEADMAN_MARKER is not defined" ;;
    /run/* | /tmp/* | /var/run/*)
      bad "the reboot marker is at ${marker_path}, which is tmpfs and is the bug" \
        "a reboot clears it, so the boot unit finds nothing and ufw stays enabled" \
        "with the rules that locked the operator out"
      ;;
    /etc/* | /var/lib/*) pass "the marker is on real disk (${marker_path})" ;;
    *) bad "the marker is at ${marker_path}; is that persistent across a reboot?" ;;
  esac
  printf '%s' "$arm" | grep -q 'systemctl enable' &&
    pass "a boot unit is enabled" || bad "nothing runs at boot to act on the marker"
  # A timestamp is what lets the unit ignore a marker from months ago.
  printf '%s' "$arm" | grep -q 'armed=' &&
    pass "the marker records when it was armed" || bad "the marker has no timestamp"
  # Failing to enable the unit must not abort an apply that is otherwise fine.
  printf '%s' "$arm" | grep -qE 'warn ' &&
    pass "a unit that cannot be enabled is reported, not fatal" ||
    bad "the failure path is silent or fatal"
fi

# --- the unit can only ever take the firewall DOWN ---------------------------
unit=$(sed -n '/cat >"\$DEADMAN_BOOT_UNIT"/,/^UNIT$/p' "$S")
if [ -z "$unit" ]; then
  bad "the boot unit is not written by this stage"
else
  printf '%s' "$unit" | grep -q 'ufw --force disable' &&
    pass "the unit disables ufw" || bad "the unit does not disable ufw"
  if printf '%s' "$unit" | grep -qE 'ufw (--force )?(enable|allow|deny|limit)'; then
    bad "the boot unit can change firewall rules" \
      "its whole safety argument is that the worst it can do is leave the" \
      "machine as it was before stage 10 ran"
  else
    pass "the unit cannot add a rule or enable ufw"
  fi
  # Ordering: disabling after ufw.service has started is a window where the
  # locking rules are live.
  printf '%s' "$unit" | grep -q 'Before=.*ufw.service' &&
    pass "it runs before ufw.service" || bad "it runs after ufw is already up"
  printf '%s' "$unit" | grep -q "ConditionPathExists" &&
    pass "it does not even start without the marker" ||
    bad "the unit runs on every boot regardless of the marker"
  printf '%s' "$unit" | grep -q 'logger -t kin-ufw' &&
    pass "it says why, in the journal" || bad "it acts silently"
  # The dangerous direction: an old marker must not disable a firewall that has
  # been working for months.
  printf '%s' "$unit" | grep -q 'gt .\\\$win' &&
    pass "a stale marker is ignored rather than acted on" ||
    bad "an old marker would disable a working firewall on an unrelated reboot"
  printf '%s' "$unit" | grep -q 'rm -f .\\\$m' &&
    pass "the marker is removed once acted on, so it fires once" ||
    bad "the marker survives, so every boot disables ufw"
fi

# --- confirming access removes BOTH halves -----------------------------------
dis=$(awk '/^disarm_deadman_across_reboot\(\) \{/,/^\}/' "$S")
if [ -z "$dis" ]; then
  bad "nothing removes the on-disk marker" \
    "a confirmed firewall would be disabled on the next reboot"
else
  printf '%s' "$dis" | grep -q 'rm -f "\$DEADMAN_MARKER"' &&
    pass "cancel removes the marker" || bad "cancel leaves the marker in place"
  printf '%s' "$dis" | grep -q 'systemctl disable' &&
    pass "cancel disables the boot unit" || bad "the boot unit stays enabled"
fi

# cancel_deadman is the operator-facing command; it must call the disarm.
cancel=$(awk '/^cancel_deadman\(\) \{/,/^\}/' "$S")
printf '%s' "$cancel" | grep -q 'disarm_deadman_across_reboot' &&
  pass "cancel-deadman disarms the reboot half too" ||
  bad "cancel-deadman only kills the in-memory loop"

# And arming must happen on every apply path, not just one of them.
n=$(grep -c 'start_deadman' "$S")
[ "${n:-0}" -ge 3 ] && pass "every apply path arms the dead-man" ||
  bad "an apply path exists that arms nothing (found ${n} references)"
start=$(awk '/^start_deadman\(\) \{/,/^\}/' "$S")
printf '%s' "$start" | grep -q 'arm_deadman_across_reboot' &&
  pass "arming the loop also arms the reboot half" ||
  bad "the two halves can be armed independently, which is how one gets forgotten"

# --- the ufw enable must come after arming, never before ---------------------
# A crash between enabling ufw and arming the dead-man is an unprotected lockout.
if awk '/start_deadman/{armed=NR} /ufw --force enable/{ if (!armed || armed > NR) { print "BAD:" NR; } }' "$S" | grep -q BAD; then
  bad "ufw is enabled before the dead-man is armed on at least one path"
else
  pass "ufw is enabled only after the dead-man is armed"
fi

# --- the unit's command is EXECUTED, not just read -----------------------------
#
# Everything above reads the source. A systemd ExecStart is a shell command
# inside a heredoc inside a shell script, with two levels of escaping, and a
# unit whose command does not parse fails at boot with nothing on screen - which
# is indistinguishable from a dead-man that simply never fired.
#
# So the real command is extracted the way the shell will expand it, and run
# against a stubbed ufw in a sandbox, through every state a marker can be in.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
printf '#!/bin/sh\necho "UFW: $*" >> "$OUT"\n' >"$work/bin/ufw"
printf '#!/bin/sh\necho "LOG: $*" >> "$OUT"\n' >"$work/bin/logger"
chmod +x "$work/bin/ufw" "$work/bin/logger"

python3 - "$S" "$work/exec.sh" <<'PY'
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()
i = src.index('cat >"$DEADMAN_BOOT_UNIT"')
j = src.index("\nUNIT\n", i)
h = src[i:j]
tag = "ExecStart=/bin/bash -c '"
body = h[h.index(tag) + len(tag):]
body = body[: body.rindex("'")]
# The heredoc is unquoted, so the shell turns \$ into $ and expands ${VAR}.
body = body.replace("\\$", "$").replace("\\\n", "\n")
body = body.replace("${DEADMAN_MARKER}", "MARKERPATH").replace("${DEADMAN_BOOT_WINDOW}", "86400")
pathlib.Path(sys.argv[2]).write_text(body + "\n")
PY

if [ ! -s "$work/exec.sh" ]; then
  bad "the boot unit's ExecStart could not be extracted"
elif ! bash -n "$work/exec.sh"; then
  bad "the boot unit's command does not parse" \
    "systemd would fail it at boot with nothing on screen, which looks" \
    "exactly like a dead-man that never fired"
else
  pass "the boot unit's command parses"

  OUT="$work/out.log"
  export OUT
  _run() { # $1 marker body (or ABSENT)
    : >"$OUT"
    if [ "$1" = ABSENT ]; then rm -f "$work/marker"; else printf '%s' "$1" >"$work/marker"; fi
    sed "s|MARKERPATH|$work/marker|" "$work/exec.sh" >"$work/run.sh"
    PATH="$work/bin:$PATH" bash "$work/run.sh" >/dev/null 2>&1
  }
  _disabled() { grep -q 'UFW: --force disable' "$OUT"; }

  now=$(date +%s)
  _run "armed=${now}
window=86400
"
  if _disabled; then
    pass "an apply that was never confirmed disables ufw on the next boot"
  else
    bad "a reboot after an unconfirmed apply leaves the locking rules in place"
  fi
  [ -e "$work/marker" ] &&
    bad "the marker survives, so every later boot would disable ufw too" ||
    pass "it fires once and removes its own marker"

  _run "armed=$((now - 200000))
window=86400
"
  if _disabled; then
    bad "a marker from days ago disabled a firewall that has been working" \
      "this is the one direction that must never happen: a customer's" \
      "appliance quietly rebooting into no firewall"
  else
    pass "a stale marker is ignored"
  fi

  _run "this is not a marker
"
  _disabled && bad "an unreadable marker disabled ufw" ||
    pass "an unreadable marker is ignored"

  _run ABSENT
  _disabled && bad "ufw was disabled with no marker present" ||
    pass "no marker means no action"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All firewall dead-man reboot tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
