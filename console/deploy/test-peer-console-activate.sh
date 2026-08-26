#!/usr/bin/env bash
# Gating tests for peer-console-activate.sh (no live systemd).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

SCRIPT=./peer-console-activate.sh
if [ ! -x "$SCRIPT" ]; then
  bad "peer-console-activate.sh must be executable"
else
  pass "peer-console-activate.sh is executable"
fi

if grep -q 'KIN_PEER_CONSOLE_ACTIVATE' "$SCRIPT" \
  && grep -q 'No users.json yet' "$SCRIPT" \
  && ! grep -q 'token_urlsafe(18)' "$SCRIPT"; then
  pass "activate requires flag and does not mint admin password"
else
  bad "activate must gate on KIN_PEER_CONSOLE_ACTIVATE and skip admin mint"
fi

if grep -q 'subjectAltName' "$SCRIPT" \
  && grep -q 'KIN_PEER_CONSOLE_IP' "$SCRIPT" \
  && grep -q 'KIN_PEER_CONSOLE_NAME' "$SCRIPT"; then
  pass "TLS SAN includes peer name/IP knobs"
else
  bad "activate TLS must accept peer name/IP for SAN"
fi

if grep -q 'api/health' "$SCRIPT" \
  && grep -q 'kin-mail-console.service' "$SCRIPT" \
  && grep -q 'kin-mail-privhelperd.service' "$SCRIPT"; then
  pass "activate enables units and health-checks :9443"
else
  bad "activate must start units and curl health"
fi

# Refuse without the activate flag (even as non-root we hit the flag/root checks).
out="$(KIN_PEER_CONSOLE_ACTIVATE=0 "$SCRIPT" 2>&1 || true)"
if printf '%s' "$out" | grep -Eq 'KIN_PEER_CONSOLE_ACTIVATE=1|Run as root'; then
  pass "activate refuses without flag or non-root"
else
  bad "activate must refuse closed without KIN_PEER_CONSOLE_ACTIVATE=1"
fi

orch="../backend/kin_privhelper/orchestration.py"
if [ -f "$orch" ] \
  && grep -q 'ensure_peer_console_runtime' "$orch" \
  && grep -q 'build_peer_console_archive' "$orch" \
  && grep -q 'peer-console-activate.sh' "$orch"; then
  pass "orchestration wires peer console archive + activate"
else
  bad "orchestration must call ensure_peer_console_runtime / activate script"
fi

if [ "$fails" -eq 0 ]; then
  printf 'ALL OK\n'
  exit 0
fi
printf '%s failure(s)\n' "$fails"
exit 1
