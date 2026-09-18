#!/usr/bin/env bash
# What "trusted certificate" means, proved against real TLS handshakes.
#
# The old test was "is the issuer different from the subject". Zimbra issues its
# own certificate from its own private CA, so on every appliance that has not
# finished stage 04 the two differ, and the answer came back "trusted". Two
# callers believed it:
#
#   05-healthcheck printed "Trusted certificate installed" and "Valid for 1824
#   days" in green on a deployment with no public certificate at all.
#
#   09-hardening switched on HSTS. On an untrusted certificate that is not a
#   weak header - RFC 6797 requires the browser to refuse the connection and
#   forbids offering "proceed anyway". Every user who opened webmail once was
#   locked out of it, per browser, until someone cleared the entry by hand.
#
# So this suite builds the exact shape Zimbra ships - a leaf signed by a private
# CA - serves it, and requires the answer to be "not trusted". Then it makes the
# same CA trusted and requires the answer to flip. Real openssl, real handshake,
# no network.
set -u
cd "$(dirname "$0")" || exit 1

LIB="$(pwd)/mail-surface-hardening.sh"
[ -f "$LIB" ] || { echo "missing $LIB" >&2; exit 1; }
# shellcheck disable=SC1090
. "$LIB"

pass=0; fails=0
ok()  { pass=$((pass+1)); printf 'ok  %s\n' "$1"; }
bad() { fails=$((fails+1)); printf 'FAIL %s\n' "$1"; }

# --- nothing listening: must fail closed -------------------------------------
# Failing open here would turn a probe that cannot connect into a green light
# for HSTS.
if KIN_TLS_PROBE_ADDR=127.0.0.1:1 cert_is_trusted; then
  bad "reported trusted with nothing listening"
else
  ok "fails closed when it cannot reach the port"
fi

command -v openssl >/dev/null 2>&1 || {
  printf 'ok  openssl missing; handshake tests skipped\n'
  printf 'ALL OK (%s checks)\n' "$((pass + 1))"
  exit 0
}

WORK=$(mktemp -d) || exit 1
SRV_PID=""
cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

# A private CA and a leaf it signed: the shape Zimbra builds for itself, where
# the CA is the mailbox node and the leaf is the edge.
build_chain() {
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$WORK/ca.key" -out "$WORK/ca.pem" \
    -subj "/O=CA/OU=Zimbra Collaboration Server/CN=store.example.test" \
    >/dev/null 2>&1 || return 1
  openssl req -newkey rsa:2048 -nodes \
    -keyout "$WORK/srv.key" -out "$WORK/srv.csr" \
    -subj "/OU=Zimbra Collaboration Server/CN=mail.example.test" \
    >/dev/null 2>&1 || return 1
  openssl x509 -req -in "$WORK/srv.csr" -days 3650 \
    -CA "$WORK/ca.pem" -CAkey "$WORK/ca.key" -CAcreateserial \
    -out "$WORK/srv.pem" >/dev/null 2>&1 || return 1
}

pick_port() {
  python3 - <<'PY' 2>/dev/null || echo 18443
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

if ! build_chain; then
  printf 'ok  openssl cannot build a test chain here; handshake tests skipped\n'
  printf 'ALL OK (%s checks)\n' "$((pass + 1))"
  exit 0
fi

PORT=$(pick_port)
openssl s_server -quiet -accept "$PORT" -naccept 20 \
  -cert "$WORK/srv.pem" -key "$WORK/srv.key" -CAfile "$WORK/ca.pem" \
  >/dev/null 2>&1 &
SRV_PID=$!
# Wait for the listener rather than sleeping a fixed amount.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null && break
  sleep 0.3
done

if ! kill -0 "$SRV_PID" 2>/dev/null; then
  printf 'ok  could not start a local TLS server; handshake tests skipped\n'
  printf 'ALL OK (%s checks)\n' "$((pass + 1))"
  exit 0
fi

export MAIL_HOST=mail.example.test

# --- the regression itself ---------------------------------------------------
# Subject CN=mail.example.test, issuer CN=store.example.test. They differ, so
# the old predicate said trusted. No client on earth accepts this chain.
if KIN_TLS_PROBE_ADDR="127.0.0.1:${PORT}" cert_is_trusted; then
  bad "a private-CA certificate is still reported as trusted (the HSTS trap)"
else
  ok "a certificate signed by a private CA is not trusted"
fi

reason=$(KIN_TLS_PROBE_ADDR="127.0.0.1:${PORT}" cert_trust_reason)
if [ -n "$reason" ] && [ "${reason%% *}" != "0" ]; then
  ok "the reason names openssl's verify code (${reason})"
else
  bad "no usable reason for an untrusted chain (got '${reason}')"
fi

# --- and it must still say yes to a chain that really does verify ------------
# Same certificate, same server; the only change is that its CA is now in the
# trust store this probe consults. A predicate that can only say no is as
# useless as one that can only say yes.
if SSL_CERT_FILE="$WORK/ca.pem" KIN_TLS_PROBE_ADDR="127.0.0.1:${PORT}" cert_is_trusted; then
  ok "the same certificate is trusted once its CA is in the trust store"
else
  bad "a chain that verifies against the trust store was still called untrusted"
fi

reason=$(SSL_CERT_FILE="$WORK/ca.pem" KIN_TLS_PROBE_ADDR="127.0.0.1:${PORT}" cert_trust_reason)
case "$reason" in
  0*) ok "a verifying chain reports code 0 (${reason})" ;;
  *)  bad "a verifying chain did not report 0 (got '${reason}')" ;;
esac

# --- the callers -------------------------------------------------------------
# HSTS is the one that locks people out, so the gate has to be this predicate
# and the stage has to be able to take the header back off again.
STAGE09="$(cd .. && pwd)/09-hardening.sh"
STAGE05="$(cd .. && pwd)/05-healthcheck.sh"
if grep -q 'if cert_is_trusted; then' "$STAGE09" \
   && grep -A1 'if cert_is_trusted; then' "$STAGE09" | grep -q 'Strict-Transport-Security'; then
  ok "HSTS is still gated on the trust answer"
else
  bad "HSTS is no longer gated on cert_is_trusted"
fi
if grep -q 'remove_response_header "Strict-Transport-Security"' "$STAGE09"; then
  ok "re-running the stage takes HSTS back off an untrusted server"
else
  bad "a deployment that already set HSTS wrongly can never undo it"
fi
if grep -q 'elif cert_is_trusted; then' "$STAGE05" \
   && ! grep -q 'ISSUER" != "\$SUBJECT"' "$STAGE05"; then
  ok "the healthcheck asks the same question, not its own version of it"
else
  bad "05-healthcheck still decides trust by comparing issuer and subject"
fi

echo
if [ "$fails" -eq 0 ]; then printf 'ALL OK (%s checks)\n' "$pass"; exit 0; fi
printf 'FAILED %s test(s) (%s ok)\n' "$fails" "$pass"; exit 1
