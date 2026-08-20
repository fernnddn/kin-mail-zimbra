#!/usr/bin/env bash
# follow_file_while must print tee'd challenge lines before the writer exits
# (certbot holds --manual-auth-hook stdout until that hook returns).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

export KIN_TLS_SOURCE_ONLY=1
export KIN_FOLLOW_POLL_S=0.1
# shellcheck source=../04-tls-dkim.sh
. "$(pwd)/../04-tls-dkim.sh"

tmpdir=$(mktemp -d)
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

# Child exit status must reach the caller (certbot failure must still fail 04).
f="$tmpdir/challenge.txt"
follow_file_while "$f" bash -c 'exit 7'
rc=$?
if [ "$rc" -eq 7 ]; then
  pass "follow_file_while returns the command exit status"
else
  bad "expected rc 7, got $rc"
fi

# Lines written by a still-running command must show up on stdout before that
# command exits. This is the certbot-hook case.
python3 - "$tmpdir" <<'PY'
import os
import subprocess
import sys
import time

tmpdir = sys.argv[1]
challenge = os.path.join(tmpdir, "live.txt")
script = os.path.join(os.getcwd(), "..", "04-tls-dkim.sh")
env = os.environ.copy()
env["KIN_TLS_SOURCE_ONLY"] = "1"
env["KIN_FOLLOW_POLL_S"] = "0.1"
proc = subprocess.Popen(
    [
        "bash",
        "-c",
        'set -u; . "$1"; follow_file_while "$2" bash -c '
        '\'printf "%s\\n" "=== KIN Mail ACME DNS-01 challenge ===" > "$0"; '
        'printf "%s\\n" "Value     : live-token-abc" >> "$0"; '
        "sleep 4; exit 0' \"$2\"",
        "bash",
        script,
        challenge,
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=env,
)
assert proc.stdout is not None
try:
    os.set_blocking(proc.stdout.fileno(), False)
except (OSError, AttributeError):
    pass
deadline = time.time() + 2.5
buf = b""
saw = False
while time.time() < deadline:
    if proc.poll() is not None:
        try:
            rest = proc.stdout.read() or b""
        except BlockingIOError:
            rest = b""
        buf += rest
        sys.stdout.buffer.write(buf)
        sys.stderr.write("FAIL writer exited before challenge appeared\n")
        sys.exit(2)
    try:
        chunk = proc.stdout.read() or b""
    except BlockingIOError:
        chunk = b""
    if chunk:
        buf += chunk
        if b"Value     : live-token-abc" in buf:
            saw = True
            break
    time.sleep(0.05)

if not saw:
    try:
        proc.kill()
    except OSError:
        pass
    sys.stdout.buffer.write(buf)
    sys.stderr.write("FAIL challenge did not appear on stdout within 2.5s\n")
    sys.exit(3)

if proc.poll() is not None:
    sys.stdout.buffer.write(buf)
    sys.stderr.write("FAIL writer already exited when challenge appeared\n")
    sys.exit(4)

rc = proc.wait(timeout=8)
if rc != 0:
    sys.stderr.write(f"FAIL writer rc={rc}\n")
    sys.exit(5)
sys.exit(0)
PY
live_rc=$?
if [ "$live_rc" -eq 0 ]; then
  pass "challenge lines appear on stdout while the writer is still running"
else
  bad "live follow failed (python exit $live_rc)"
fi

# In-place truncate (hook tee without -a) must reprint from offset 0.
python3 - "$tmpdir" <<'PY'
import os
import subprocess
import sys

tmpdir = sys.argv[1]
challenge = os.path.join(tmpdir, "trunc.txt")
script = os.path.join(os.getcwd(), "..", "04-tls-dkim.sh")
env = os.environ.copy()
env["KIN_TLS_SOURCE_ONLY"] = "1"
env["KIN_FOLLOW_POLL_S"] = "0.1"
proc = subprocess.Popen(
    [
        "bash",
        "-c",
        'set -u; . "$1"; follow_file_while "$2" bash -c '
        '\'printf "%s\\n" "first-token" > "$0"; '
        "sleep 0.4; "
        'printf "%s\\n" "second-token" > "$0"; '
        "sleep 0.4; exit 0' \"$2\"",
        "bash",
        script,
        challenge,
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=env,
)
out, _ = proc.communicate(timeout=8)
if proc.returncode != 0:
    sys.stderr.write(f"FAIL truncate-follow rc={proc.returncode}\n")
    sys.exit(1)
if b"first-token" not in out or b"second-token" not in out:
    sys.stdout.buffer.write(out)
    sys.stderr.write("FAIL expected both tokens after truncate\n")
    sys.exit(2)
sys.exit(0)
PY
trunc_rc=$?
if [ "$trunc_rc" -eq 0 ]; then
  pass "truncate then rewrite is printed (tee overwrite)"
else
  bad "truncate follow failed (python exit $trunc_rc)"
fi

# Append (tee -a) must not replay the whole file.
python3 - "$tmpdir" <<'PY'
import os
import subprocess
import sys

tmpdir = sys.argv[1]
challenge = os.path.join(tmpdir, "append.txt")
script = os.path.join(os.getcwd(), "..", "04-tls-dkim.sh")
env = os.environ.copy()
env["KIN_TLS_SOURCE_ONLY"] = "1"
env["KIN_FOLLOW_POLL_S"] = "0.1"
proc = subprocess.Popen(
    [
        "bash",
        "-c",
        'set -u; . "$1"; follow_file_while "$2" bash -c '
        '\'printf "%s\\n" "Host/Name : _acme-challenge.example.test" > "$0"; '
        "sleep 0.4; "
        'printf "%s\\n" "Value     : appended-token" >> "$0"; '
        "sleep 0.4; exit 0' \"$2\"",
        "bash",
        script,
        challenge,
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=env,
)
out, _ = proc.communicate(timeout=8)
if proc.returncode != 0:
    sys.stderr.write(f"FAIL append-follow rc={proc.returncode}\n")
    sys.exit(1)
if out.count(b"Host/Name : _acme-challenge.example.test") != 1:
    sys.stdout.buffer.write(out)
    sys.stderr.write("FAIL Host/Name should appear once on append\n")
    sys.exit(2)
if b"Value     : appended-token" not in out:
    sys.stdout.buffer.write(out)
    sys.stderr.write("FAIL appended value missing\n")
    sys.exit(3)
sys.exit(0)
PY
append_rc=$?
if [ "$append_rc" -eq 0 ]; then
  pass "append does not replay earlier lines (tee -a)"
else
  bad "append follow failed (python exit $append_rc)"
fi

if [ "$fails" -ne 0 ]; then
  printf '%s\n' "FAILED $fails test(s)"
  exit 1
fi
printf '%s\n' "All ACME follow_file_while tests passed"
exit 0
