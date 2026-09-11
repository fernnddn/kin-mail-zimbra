#!/usr/bin/env bash
# Putting a gateway in front of a mail server is a change that either works or
# silently eats mail, and the failures are not loud. A wrong relaynomx builds a
# loop. A missing relay domain rejects every inbound message. A second DKIM
# signature fails DMARC at receivers we never hear from. So the tests that
# matter here are the refusals and the assertions, not the happy path.
#
# Every external tool is stubbed: curl, openssl, zmprov, dig. Nothing here
# reaches a network or a Zimbra. The stubs record their argv, so a test can
# assert on the exact API calls that would have been made - including the
# order, which matters in apply.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

LIB="$(pwd)/mail-gateway.sh"
[ -f "$LIB" ] || { printf 'FAIL mail-gateway.sh not found\n'; exit 1; }

WORK="$(mktemp -d)" || exit 1
trap 'rm -rf "$WORK"' EXIT
BIN="${WORK}/bin"; mkdir -p "$BIN"

# --- stubs -------------------------------------------------------------------

# curl: answers from fixture files chosen by the request path, and appends its
# argv to the call log. Understands the -w '\n%{http_code}' contract the
# library relies on.
cat > "${BIN}/curl" <<'STUB'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$KIN_TEST_CALLS"
method=GET
url=""
prev=""
for a in "$@"; do
  case "$prev" in -X) method="$a" ;; esac
  case "$a" in https://*) url="$a" ;; esac
  prev="$a"
done
path=${url#*/api2/json}
key=$(printf '%s' "${method}${path}" | tr -c 'A-Za-z0-9' '_')
code_file="${KIN_TEST_FIXTURES}/${key}.code"
body_file="${KIN_TEST_FIXTURES}/${key}.json"
code=200
[ -f "$code_file" ] && code=$(cat "$code_file")
# The routing PUT and the policy PUT share a path, so a test that wants to
# fail only the policy half has to be able to say so.
case "${method} ${path}" in
  "PUT /config/mail") case " $* " in *hide_received=*) code="${KIN_TEST_POLICY_PUT_CODE:-$code}" ;; esac ;;
esac

# A gateway that accepts a write and then keeps reporting the old value is a
# real failure mode - and it is the one apply's read-back step exists to
# catch - so the stub models a gateway whose writes actually stick. Set
# KIN_TEST_WRITES_STICK=0 to model the broken one.
if [ "${KIN_TEST_WRITES_STICK:-1}" = "1" ] && [ "${code#2}" != "$code" ]; then
  case "${method} ${path}" in
    "PUT /config/mail")
      # Only the routing PUT establishes the routing values; the policy PUT
      # must not be what makes relaynomx look right.
      case " $* " in
        *relaynomx=1*)
          printf '%s' '{"data":{"relay":"192.0.2.20","relayport":25,"relaynomx":1,"int_port":26,"ext_port":25,"hide_received":0}}' \
            > "${KIN_TEST_FIXTURES}/GET_config_mail.json" ;;
        *hide_received=1*)
          printf '%s' '{"data":{"relay":"192.0.2.20","relayport":25,"relaynomx":1,"int_port":26,"ext_port":25,"hide_received":1}}' \
            > "${KIN_TEST_FIXTURES}/GET_config_mail.json" ;;
      esac ;;
    "PUT /config/admin")
      printf '%s' '{"data":{"dkim_sign":0}}' > "${KIN_TEST_FIXTURES}/GET_config_admin.json" ;;
    "POST /config/domains")
      printf '%s' '{"data":[{"domain":"example.test"}]}' > "${KIN_TEST_FIXTURES}/GET_config_domains.json" ;;
    "POST /config/mynetworks")
      printf '%s' '{"data":[{"cidr":"192.0.2.20/32"}]}' > "${KIN_TEST_FIXTURES}/GET_config_mynetworks.json" ;;
    "POST /config/transport"|"PUT /config/transport/example.test")
      printf '%s' '{"data":[{"domain":"example.test","host":"192.0.2.20","port":25}]}' \
        > "${KIN_TEST_FIXTURES}/GET_config_transport.json" ;;
  esac
fi

if [ -f "$body_file" ]; then cat "$body_file"; else printf '{"data":{}}'; fi
# access/ticket is fetched without -w, so only emit the code when asked.
case " $* " in *" %{http_code} "*|*"%{http_code}"*) printf '\n%s' "$code" ;; esac
exit 0
STUB

# openssl: two roles. s_client is a no-op pipe; x509 prints whichever
# fingerprint the test wants the live gateway to be showing.
cat > "${BIN}/openssl" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  s_client) cat >/dev/null; printf 'stub-cert\n'; exit "${KIN_TEST_TLS_EXIT:-0}" ;;
  x509)     cat >/dev/null; printf 'sha256 Fingerprint=%s\n' "${KIN_TEST_FINGERPRINT:-AA:BB}" ;;
esac
STUB

# zmprov: answers from the environment and logs its argv, so a test can assert
# that apply ADDED to mynetworks rather than replacing it.
cat > "${BIN}/zmprov-stub" <<'STUB'
#!/usr/bin/env bash
printf 'zmprov %s\n' "$*" >> "$KIN_TEST_CALLS"
case "$1 $2" in
  "gs "*)
    case "$*" in *zimbraMtaRelayHost*) printf 'zimbraMtaRelayHost: %s\n' "${KIN_TEST_RELAYHOST:-}" ;; esac ;;
esac
case "$1" in
  gacf) printf 'zimbraMtaMyNetworks: %s\n' "${KIN_TEST_ZNETS:-127.0.0.0/8}" ;;
  gad)  printf '%s\n' "${KIN_TEST_ZDOMAINS:-example.test}" ;;
esac
exit 0
STUB

# A stub TCP prober. The real one uses bash's /dev/tcp, which against a
# documentation address blackholes for the full timeout on every call; the
# suite went from two seconds to several minutes the moment probing was added.
cat > "${BIN}/tcp-probe-stub" <<'STUB'
#!/usr/bin/env bash
printf 'tcp %s %s\n' "$1" "$2" >> "$KIN_TEST_CALLS"
case "$2" in
  26) exit "${KIN_TEST_TCP26:-0}" ;;
  25) exit "${KIN_TEST_TCP25:-0}" ;;
esac
exit 0
STUB

cat > "${BIN}/dig" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  *MX*) printf '%s\n' "${KIN_TEST_MX:-10 gw.example.test.}" ;;
  *TXT*) printf '%s\n' "${KIN_TEST_SPF:-\"v=spf1 ip4:192.0.2.9 ~all\"}" ;;
esac
exit 0
STUB
chmod +x "${BIN}"/*

# --- environment -------------------------------------------------------------
FIX="${WORK}/fixtures"; mkdir -p "$FIX"
CALLS="${WORK}/calls"
STATE="${WORK}/state"; mkdir -p "$STATE"

GWCONF="${WORK}/mail-gateway.conf"
APPCONF="${WORK}/appliance.conf"

write_appliance_conf() {
  cat > "$APPCONF" <<EOF
MAIL_DOMAIN="example.test"
MAIL_HOST="mail.example.test"
SERVER_IP="${1:-192.0.2.20}"
EOF
}

write_gw_conf() {
  cat > "$GWCONF" <<EOF
GATEWAY_ENABLED=1
GATEWAY_HOST="${1:-192.0.2.9}"
GATEWAY_API_PORT=8006
GATEWAY_AUTH=token
GATEWAY_TOKEN_ID="root@pam!kinmail"
EOF
}

# Fixtures for a gateway that is already correctly configured.
fixture_healthy() {
  printf '%s' '{"data":{"relay":"192.0.2.20","relayport":25,"relaynomx":1,"int_port":26,"ext_port":25,"hide_received":1}}' > "${FIX}/GET_config_mail.json"
  printf '%s' '{"data":[{"domain":"example.test"}]}' > "${FIX}/GET_config_domains.json"
  printf '%s' '{"data":[{"cidr":"192.0.2.20/32"}]}' > "${FIX}/GET_config_mynetworks.json"
  printf '%s' '{"data":[{"domain":"example.test","host":"192.0.2.20","port":25}]}' > "${FIX}/GET_config_transport.json"
  printf '%s' '{"data":{"dkim_sign":0}}' > "${FIX}/GET_config_admin.json"
  printf '%s' '{"data":{"release":"8.1"}}' > "${FIX}/GET_version.json"
}

# Fixtures for a gateway straight out of the ISO: nothing configured.
fixture_fresh() {
  printf '%s' '{"data":{"relay":"","relayport":25,"relaynomx":0,"int_port":26,"ext_port":25,"hide_received":0}}' > "${FIX}/GET_config_mail.json"
  printf '%s' '{"data":[]}' > "${FIX}/GET_config_domains.json"
  printf '%s' '{"data":[]}' > "${FIX}/GET_config_mynetworks.json"
  printf '%s' '{"data":[]}' > "${FIX}/GET_config_transport.json"
  printf '%s' '{"data":{"dkim_sign":0}}' > "${FIX}/GET_config_admin.json"
  printf '%s' '{"data":{"release":"8.1"}}' > "${FIX}/GET_version.json"
}

run_gw() {
  : > "$CALLS"
  env \
    PATH="${BIN}:${PATH}" \
    KIN_TEST_CALLS="$CALLS" \
    KIN_TEST_FIXTURES="$FIX" \
    KIN_PMG_ALLOW_NONROOT=1 \
    KIN_MAIL_GATEWAY_CONF="$GWCONF" \
    KIN_MAIL_CONFIG="$APPCONF" \
    KIN_PMG_STATE_DIR="$STATE" \
    KIN_PMG_ZMPROV="${BIN}/zmprov-stub" \
    KIN_PMG_TCP_PROBE="${BIN}/tcp-probe-stub" \
    KIN_PMG_TOKEN_SECRET="${KIN_TEST_SECRET-s3cr3t}" \
    KIN_TEST_FINGERPRINT="${KIN_TEST_FINGERPRINT:-AA:BB:CC}" \
    bash "$LIB" "$@" 2>&1
}

# =============================================================================
# Configuration refusals - the cheap failures, caught before anything connects
# =============================================================================
write_appliance_conf
: > "$GWCONF"
out=$(run_gw probe)
case "$out" in *"reason=no-gateway-host"*) pass "no gateway configured is refused, not guessed" ;;
  *) bad "unconfigured gateway should be refused: ${out}" ;; esac

write_gw_conf "192.0.2.9"
printf 'GATEWAY_HOST="192.0.2.9; rm -rf /"\n' >> "$GWCONF"
out=$(run_gw probe)
case "$out" in *"reason=bad-gateway-host"*) pass "a gateway address carrying shell metacharacters is refused" ;;
  *) bad "metacharacters in the host should be refused: ${out}" ;; esac

# The typo that would be a mail loop: pointing the appliance at itself.
write_gw_conf "192.0.2.20"
out=$(run_gw probe)
case "$out" in *"reason=gateway-is-self"*) pass "a gateway address equal to this appliance is refused (that is a loop)" ;;
  *) bad "self-as-gateway should be refused: ${out}" ;; esac

write_gw_conf "192.0.2.9"
out=$(KIN_TEST_SECRET="" run_gw probe)
case "$out" in *"reason=no-token-secret"*) pass "a missing API token secret is named, not reported as a connection failure" ;;
  *) bad "missing secret should be refused: ${out}" ;; esac

write_appliance_conf "not-an-ip"
out=$(run_gw probe)
case "$out" in *"reason=bad-server-ip"*) pass "a SERVER_IP that is not an address is refused before it reaches the gateway" ;;
  *) bad "bad SERVER_IP should be refused: ${out}" ;; esac
write_appliance_conf

# =============================================================================
# Certificate handling - the part curl -k would have thrown away
# =============================================================================
fixture_healthy
rm -f "${STATE}/mail-gateway-fingerprint"
out=$(run_gw probe)
case "$out" in *"reason=certificate-not-trusted"*) pass "an unconfirmed gateway certificate stops everything" ;;
  *) bad "unpinned certificate should refuse: ${out}" ;; esac
case "$out" in *"AA:BB:CC"*) pass "the refusal prints the fingerprint the operator has to confirm" ;;
  *) bad "refusal should show the fingerprint: ${out}" ;; esac

out=$(KIN_PMG_TRUST_ON_FIRST_USE=1 run_gw probe)
case "$out" in *"KIN_GW_OK Reached the gateway"*) pass "confirming the certificate lets the connection proceed" ;;
  *) bad "trust-on-first-use should connect: ${out}" ;; esac
if grep -q "AA:BB:CC" "${STATE}/mail-gateway-fingerprint" 2>/dev/null; then
  pass "the confirmed fingerprint is recorded for next time"
else
  bad "fingerprint was not stored"
fi

# The one that matters: the gateway's certificate changing under us.
out=$(KIN_TEST_FINGERPRINT="DD:EE:FF" run_gw probe)
case "$out" in *"reason=certificate-changed"*) pass "a changed gateway certificate refuses instead of reconnecting" ;;
  *) bad "changed fingerprint should refuse: ${out}" ;; esac

# =============================================================================
# probe
# =============================================================================
out=$(run_gw probe)
case "$out" in *"KIN_GW_VERSION 8.1"*) pass "probe reports the gateway's version" ;;
  *) bad "probe should report version: ${out}" ;; esac
if grep -q 'Authorization: PMGAPIToken=root@pam!kinmail=s3cr3t' "$CALLS"; then
  pass "the API token is sent in the header PMG expects"
else
  bad "token header wrong: $(grep curl "$CALLS" | head -1)"
fi

printf '403' > "${FIX}/GET_config_mail.code"
out=$(run_gw probe)
case "$out" in *"reason=insufficient-permission"*) pass "a read-only or revoked token is caught by probe, not half-way through apply" ;;
  *) bad "403 should be caught at probe: ${out}" ;; esac
rm -f "${FIX}/GET_config_mail.code"

# =============================================================================
# plan - must report accurately and must not write
# =============================================================================
fixture_fresh
out=$(run_gw plan)
case "$out" in *"KIN_GW_CHANGE pmg.relaynomx from=0 to=1"*) pass "plan spots that MX lookup is on, which would loop" ;;
  *) bad "plan should flag relaynomx: ${out}" ;; esac
case "$out" in *"KIN_GW_CHANGE pmg.relay-domain"*) pass "plan spots the missing relay domain" ;;
  *) bad "plan should flag the relay domain: ${out}" ;; esac
if grep -qE 'curl .*-X (PUT|POST|DELETE)' "$CALLS"; then
  bad "plan wrote to the gateway: $(grep -E '\-X (PUT|POST|DELETE)' "$CALLS" | head -1)"
else
  pass "plan changes nothing on the gateway"
fi
if grep -qE 'zmprov (ms|mcf|md) ' "$CALLS"; then
  bad "plan wrote to Zimbra: $(grep -E 'zmprov (ms|mcf|md) ' "$CALLS" | head -1)"
else
  pass "plan changes nothing on the appliance"
fi

fixture_healthy
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" run_gw plan)
case "$out" in *"KIN_GW_PLAN changes=0"*) pass "plan on an already-configured link reports nothing to do" ;;
  *) bad "idempotent plan should report zero changes: ${out}" ;; esac

# =============================================================================
# apply
# =============================================================================
fixture_fresh
out=$(run_gw apply)

# Order is not cosmetic. If the MX has already moved and the gateway does not
# yet accept the domain, every inbound message is rejected for the length of
# the gap. The relay domain therefore goes first.
dom_line=$(grep -n 'X POST .*config/domains' "$CALLS" | head -1 | cut -d: -f1)
mail_line=$(grep -n 'X PUT .*config/mail' "$CALLS" | head -1 | cut -d: -f1)
if [ -n "$dom_line" ] && [ -n "$mail_line" ] && [ "$dom_line" -lt "$mail_line" ]; then
  pass "apply adds the relay domain before touching anything else"
else
  bad "relay domain was not written first (domain=${dom_line:-none} mail=${mail_line:-none})"
fi

if grep -q 'relaynomx=1' "$CALLS"; then
  pass "apply forces relaynomx=1 so the gateway does not follow the MX back to itself"
else
  bad "relaynomx=1 was not sent"
fi
if grep -q 'use_mx=0' "$CALLS"; then
  pass "the transport entry delivers to the host directly, not via its MX"
else
  bad "transport use_mx=0 was not sent"
fi
if grep -q 'dkim_sign=0' "$CALLS"; then
  pass "apply turns the gateway's own DKIM signing off (Zimbra already signed)"
else
  bad "dkim_sign=0 was not sent"
fi
if grep -q 'hide_received=1' "$CALLS"; then
  pass "apply stops the gateway leaking the mail node's address in headers"
else
  bad "hide_received=1 was not sent"
fi
if grep -q 'cidr=192.0.2.20/32' "$CALLS"; then
  pass "apply lets this appliance relay out through the gateway"
else
  bad "mynetworks entry was not sent"
fi

# Replacing zimbraMtaMyNetworks wholesale would silently drop whatever the
# operator already relayed for. It has to be an addition.
if grep -q 'zmprov mcf +zimbraMtaMyNetworks 192.0.2.9/32' "$CALLS"; then
  pass "the gateway is ADDED to zimbraMtaMyNetworks, not substituted for it"
else
  bad "mynetworks should be appended with +: $(grep zimbraMtaMyNetworks "$CALLS" | head -1)"
fi
if grep -q 'zmprov ms mail.example.test zimbraMtaRelayHost 192.0.2.9:26' "$CALLS"; then
  pass "outbound mail is pointed at the gateway's internal port 26, not 25"
else
  bad "relay host wrong: $(grep RelayHost "$CALLS" | head -1)"
fi
case "$out" in *"v=spf1 ip4:192.0.2.9"*) pass "apply prints the exact SPF record the operator still has to set" ;;
  *) bad "apply should print the DNS changes: ${out}" ;; esac

# Re-running a successful apply must be safe: operators re-run things.
fixture_healthy
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" run_gw apply)
case "$out" in *"KIN_GW_OK Gateway link applied."*) pass "applying twice is safe and still ends successfully" ;;
  *) bad "second apply should succeed: ${out}" ;; esac
if grep -q 'zmprov mcf +zimbraMtaMyNetworks' "$CALLS"; then
  bad "second apply added the gateway to mynetworks again"
else
  pass "a second apply does not re-add what is already there"
fi

# A failure part-way through must not be reported as success.
printf '500' > "${FIX}/PUT_config_mail.code"
fixture_fresh
out=$(run_gw apply); rc=$?
rm -f "${FIX}/PUT_config_mail.code"
if [ "$rc" -ne 0 ] && case "$out" in *"reason=api-failed"*) true ;; *) false ;; esac; then
  pass "a gateway that refuses a write stops apply with a non-zero exit"
else
  bad "failed apply should exit non-zero with a reason (rc=${rc}): ${out}"
fi

# =============================================================================
# The failures found in audit, which must not come back
# =============================================================================

# A gateway that accepts a write and keeps reporting the old value looks like a
# success and is not one. Before the read-back step, apply said "applied" and
# the compliance banner went green on a relay that was never set.
fixture_fresh
out=$(KIN_TEST_WRITES_STICK=0 run_gw apply); rc=$?
if [ "$rc" -ne 0 ] && case "$out" in *"reason=settings-did-not-stick"*) true ;; *) false ;; esac; then
  pass "a gateway whose writes do not stick is caught before apply claims success"
else
  bad "settings that do not stick should fail apply (rc=${rc}): ${out}"
fi
case "$out" in *"Do not move the MX record yet"*) pass "the operator is told not to move the MX on a half-applied link" ;;
  *) bad "should warn against moving DNS: ${out}" ;; esac
if [ -f "${STATE}/mail-gateway-state.json" ] && grep -q '"phase":"applied"' "${STATE}/mail-gateway-state.json" 2>/dev/null; then
  bad "a failed apply recorded itself as applied"
else
  pass "a failed apply does not record itself as applied"
fi

# Routing and policy are written separately so one unrecognised policy field
# cannot take the relay down with it. PMG rejects a whole request when any
# parameter in it is invalid.
fixture_fresh
: > "$CALLS"
out=$(run_gw apply)
routing_puts=$(grep -c 'X PUT .*config/mail' "$CALLS" || true)
if [ "${routing_puts:-0}" -ge 2 ]; then
  pass "mail routing and mail policy are two separate writes"
else
  bad "routing and policy should not share one request (saw ${routing_puts} PUTs)"
fi
routing_line=$(grep -n 'X PUT .*config/mail' "$CALLS" | head -1 | cut -d: -f1)
if grep -n 'X PUT .*config/mail' "$CALLS" | head -1 | grep -q 'relaynomx=1'; then
  pass "routing is written first, so a policy problem cannot take it down"
else
  bad "the first PUT to /config/mail should carry the routing settings (line ${routing_line})"
fi

# A spam or TLS setting PMG does not recognise must not stop mail flowing.
fixture_fresh
out=$(KIN_TEST_POLICY_PUT_CODE=400 run_gw apply); rc=$?
if [ "$rc" -eq 0 ]; then
  pass "a rejected policy setting does not fail the whole apply"
else
  bad "policy failure should not fail apply (rc=${rc}): ${out}"
fi
case "$out" in *"Mail routing is configured and mail will flow"*)
  pass "the operator is told exactly what did and did not get set" ;;
  *) bad "a policy failure should say what still works: ${out}" ;; esac

# Recipient verification must be soft. 550 refuses permanently: a Zimbra that
# is merely restarting would make the sender give up on real mail for good.
fixture_fresh
out=$(run_gw apply)
if grep -q 'verifyreceivers=450' "$CALLS"; then
  pass "recipient verification defers rather than refusing mail for good"
else
  bad "verifyreceivers should be 450, not 550: $(grep -o 'verifyreceivers=[0-9]*' "$CALLS" | head -1)"
fi

# rejectunknown refuses senders with no reverse DNS. Plenty of small but real
# senders have none, and PMG's own default is off. An installer must not
# quietly impose that policy on a customer's mail.
if grep -q 'rejectunknown=1' "$CALLS"; then
  bad "apply should not turn on rejectunknown; that is the customer's policy"
else
  pass "apply does not impose reverse-DNS rejection on the customer"
fi

# Deleting a transport entry and then failing to re-create it leaves the domain
# with no route at all - worse than the wrong route we started from.
fixture_healthy
: > "$CALLS"
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" run_gw apply)
if grep -q 'X DELETE .*config/transport' "$CALLS"; then
  bad "an existing transport entry was deleted rather than updated in place"
else
  pass "an existing transport entry is updated in place, never deleted first"
fi

# The commonest real failure is neither side's configuration: it is a firewall.
fixture_healthy
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="example.test" KIN_TEST_TCP26=1 run_gw verify)
case "$out" in *"cannot open 192.0.2.9:26"*) pass "verify notices when outbound mail has no path to the gateway" ;;
  *) bad "verify should probe the relay port: ${out}" ;; esac
case "$out" in *"KIN_GW_VERIFY problems=0"*) pass "an unreachable port is a warning, not a config failure" ;;
  *) bad "reachability should not fail verify: ${out}" ;; esac

# Reverting narrows nothing back. If the firewall is left as it was, the
# appliance refuses inbound mail from everyone except a gateway it no longer
# trusts, and the operator has no way to know from here.
out=$(run_gw revert)
case "$out" in *"10-host-firewall.sh apply"*) pass "revert says the firewall must be re-run or inbound mail stops" ;;
  *) bad "revert should mention the firewall: ${out}" ;; esac

# =============================================================================
# verify - every assertion here is a way mail stops
# =============================================================================
fixture_healthy
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="example.test" KIN_TEST_MX="10 192.0.2.9." run_gw verify)
case "$out" in *"KIN_GW_VERIFY problems=0"*) pass "verify passes a correctly configured link" ;;
  *) bad "healthy link should verify clean: ${out}" ;; esac

printf '%s' '{"data":{"relay":"192.0.2.20","relayport":25,"relaynomx":0,"int_port":26,"ext_port":25}}' > "${FIX}/GET_config_mail.json"
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" run_gw verify)
case "$out" in *"KIN_GW_PROBLEM pmg.relaynomx expected=1 actual=0"*) pass "verify catches the MX-lookup loop even though apply set it" ;;
  *) bad "verify should catch relaynomx drift: ${out}" ;; esac
case "$out" in *"That is a loop."*) pass "verify explains what the drift would do, not just that it differs" ;;
  *) bad "verify should explain relaynomx: ${out}" ;; esac
fixture_healthy

printf '%s' '{"data":[]}' > "${FIX}/GET_config_domains.json"
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" run_gw verify)
case "$out" in *"KIN_GW_PROBLEM pmg.relay-domain"*) pass "verify catches a gateway that would answer 'relay access denied'" ;;
  *) bad "verify should catch a missing relay domain: ${out}" ;; esac
fixture_healthy

printf '%s' '{"data":{"dkim_sign":1}}' > "${FIX}/GET_config_admin.json"
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" run_gw verify)
case "$out" in *"KIN_GW_PROBLEM pmg.dkim_sign"*) pass "verify catches a gateway signing DKIM a second time" ;;
  *) bad "verify should catch double DKIM signing: ${out}" ;; esac
fixture_healthy

# The loop nothing else notices: the domain is not local on Zimbra, so Zimbra
# relays it straight back to its relay host, which is the gateway.
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="other.test" run_gw verify)
case "$out" in *"KIN_GW_PROBLEM zimbra.local-domain"*) pass "verify catches a domain Zimbra would bounce back to the gateway" ;;
  *) bad "verify should catch a non-local domain: ${out}" ;; esac

out=$(KIN_TEST_RELAYHOST="" run_gw verify)
case "$out" in *"KIN_GW_PROBLEM zimbra.relayhost"*) pass "verify catches outbound mail still bypassing the gateway" ;;
  *) bad "verify should catch a missing relay host: ${out}" ;; esac

# DNS lives at a registrar; it is a warning, never a hard failure, because an
# operator can legitimately be mid-migration.
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="example.test" KIN_TEST_MX="10 mail.example.test." run_gw verify)
case "$out" in *"KIN_GW_WARN"*"not the gateway"*) pass "verify warns when the MX still bypasses the gateway" ;;
  *) bad "verify should warn about the MX: ${out}" ;; esac
case "$out" in *"KIN_GW_VERIFY problems=0"*) pass "an un-moved MX is a warning, not a failure - migrations take time" ;;
  *) bad "MX warning should not fail verify: ${out}" ;; esac

out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="example.test" KIN_TEST_MX="10 192.0.2.9." \
      KIN_TEST_SPF='"v=spf1 ip4:192.0.2.20 ~all"' run_gw verify)
case "$out" in *"Add:"*"ip4:192.0.2.9"*) pass "verify says exactly what to add to SPF, not just that it is wrong" ;;
  *) bad "verify should print the SPF fix: ${out}" ;; esac

# =============================================================================
# revert and status
# =============================================================================
out=$(run_gw revert)
if grep -q "zmprov ms mail.example.test zimbraMtaRelayHost" "$CALLS"; then
  pass "revert clears the relay host so the appliance sends directly again"
else
  bad "revert should clear zimbraMtaRelayHost: ${out}"
fi
if grep -qE 'curl .*-X (PUT|POST|DELETE)' "$CALLS"; then
  bad "revert reconfigured the gateway; it should leave it alone"
else
  pass "revert leaves the gateway configured, so re-applying is instant"
fi
case "$out" in *"Move the MX record back"*) pass "revert says the one thing it cannot do itself" ;;
  *) bad "revert should mention the MX: ${out}" ;; esac

out=$(run_gw status)
case "$out" in *'"configured":true'*) pass "status reports the link as configured" ;;
  *) bad "status should report configured: ${out}" ;; esac
case "$out" in *'"phase":"reverted"'*) pass "status remembers that the last action was a revert" ;;
  *) bad "status should carry the phase: ${out}" ;; esac
case "$out" in *"s3cr3t"*) bad "status leaked the API token secret" ;;
  *) pass "status never carries the API token secret" ;; esac

out=$(run_gw frobnicate)
case "$out" in *"reason=unknown-action"*) pass "an unknown action is refused rather than silently doing nothing" ;;
  *) bad "unknown action should be refused: ${out}" ;; esac

# =============================================================================
printf '\n'
if [ "$fails" -eq 0 ]; then
  printf 'all mail-gateway tests passed\n'
  exit 0
fi
printf '%d mail-gateway test(s) failed\n' "$fails"
exit 1
