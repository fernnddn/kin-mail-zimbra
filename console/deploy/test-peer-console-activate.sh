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
  && grep -q 'Empty users.json placeholder' "$SCRIPT" \
  && grep -q 'version.:1' "$SCRIPT" \
  && ! grep -q 'token_urlsafe(18)' "$SCRIPT"; then
  pass "activate requires flag and does not mint admin password"
else
  bad "activate must gate on KIN_PEER_CONSOLE_ACTIVATE and skip admin mint"
fi

if grep -q 'SERVER_IP' "$SCRIPT" \
  && grep -q '_derive_slash24' "$SCRIPT" \
  && ! grep -q 'elif \[ -n "${MAIL_HOST_IP' "$SCRIPT"; then
  pass "UFW LAN derives from SERVER_IP / VIP (not MAIL_HOST_IP)"
else
  bad "activate UFW must use SERVER_IP, not MAIL_HOST_IP"
fi

if grep -q 'CONSOLE_BIND=0.0.0.0' "$SCRIPT"; then
  pass "activate forces CONSOLE_BIND=0.0.0.0"
else
  bad "activate must force CONSOLE_BIND for remote reachability"
fi

if grep -q 'venv/bin/python3' "$SCRIPT" \
  && grep -q 'PEER_PY' "$SCRIPT"; then
  pass "activate accepts python or python3 venv"
else
  bad "activate must accept venv/bin/python3"
fi

if grep -q 'regenerating cert' "$SCRIPT" \
  && grep -q 'subjectAltName' "$SCRIPT"; then
  pass "activate re-mints TLS when peer IP missing from SAN"
else
  bad "activate must self-heal TLS SAN for peer IP"
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
