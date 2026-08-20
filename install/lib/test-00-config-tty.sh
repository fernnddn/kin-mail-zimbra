#!/usr/bin/env bash
# Wizard must exit immediately when /dev/tty is unusable, not spin a CPU.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

SCRIPT="$(pwd)/../00-config.sh"

run_no_tty() {
  # Drop the controlling terminal (SSH without -t). Timeout catches a hang.
  python3 - "$SCRIPT" <<'PY'
import os, subprocess, sys
script = sys.argv[1]
env = os.environ.copy()
try:
    r = subprocess.run(
        ["bash", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=8,
        start_new_session=True,
    )
except subprocess.TimeoutExpired as exc:
    sys.stdout.buffer.write(exc.stdout or b"")
    sys.stdout.buffer.write(b"\nHUNG: 00-config.sh did not exit within 8s\n")
    sys.exit(99)
sys.stdout.buffer.write(r.stdout)
sys.exit(r.returncode)
PY
}

tmpdir=$(mktemp -d)
export KIN_MAIL_CONFIG="$tmpdir/config"
# File must not exist: this is the fresh-peer path.
rm -f "$KIN_MAIL_CONFIG"

out=$(run_no_tty)
rc=$?
if [ "$rc" -ne 0 ] \
  && printf '%s\n' "$out" | grep -q 'no controlling terminal and no /etc/kin-mail/config present'; then
  pass "missing config + no TTY exits immediately with a clear error"
else
  bad "expected immediate fail, rc=$rc out=[$out]"
fi

# Existing config must still load with no TTY (the non-interactive path).
printf '%s\n' 'MAIL_DOMAIN="example.test"' > "$KIN_MAIL_CONFIG"
out=$(run_no_tty)
rc=$?
if [ "$rc" -eq 0 ] \
  && ! printf '%s\n' "$out" | grep -q 'cannot run interactively'; then
  pass "existing config + no TTY sources the file and does not prompt"
else
  bad "existing config should load silently, rc=$rc out=[$out]"
fi

rm -rf "$tmpdir"

if [ "$fails" -ne 0 ]; then
  printf '%s\n' "FAILED $fails test(s)"
  exit 1
fi
printf '%s\n' "All 00-config TTY tests passed"
exit 0
