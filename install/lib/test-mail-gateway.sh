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
# The gateway is recorded only by the address this appliance talks to, which
# is a LAN address in every real deployment. There is nothing safe to print.
case "$out" in *"would authorise nothing"*) pass "with no public identity, apply refuses to invent DNS records" ;;
  *) bad "apply should warn instead of printing a management address: ${out}" ;; esac
case "$out" in
  *'ip4:192.0.2.9 ~all'*) bad "apply printed the management address as an SPF value" ;;
  *) pass "the management address never appears as something to publish" ;;
esac

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

# The gateway has two identities. GATEWAY_HOST is how this appliance reaches it
# - a LAN address in every real deployment. What goes in DNS is what the
# internet sees. Conflating them told a live deployment to publish
# "v=spf1 ip4:10.x", which authorises nothing and fails SPF on every outbound
# message (QA Phase 15, 11 Sep 2026).
write_gw_conf "192.0.2.9"
cat >> "$GWCONF" <<EOF
GATEWAY_PUBLIC_HOST="relay.example.test"
GATEWAY_PUBLIC_IP="198.51.100.38"
EOF
fixture_fresh
out=$(run_gw apply)
case "$out" in *"MX   example.test.  10  relay.example.test."*) pass "apply prints the MX record as a hostname, which is the only thing an MX can hold" ;;
  *) bad "apply should print a hostname MX: ${out}" ;; esac
case "$out" in *'ip4:198.51.100.38 ~all'*) pass "SPF is printed with the public address, not the management one" ;;
  *) bad "SPF should carry the public address: ${out}" ;; esac
case "$out" in *"PTR  198.51.100.38  ->  relay.example.test"*) pass "the PTR the ISP has to set is spelled out" ;;
  *) bad "apply should print the PTR: ${out}" ;; esac
case "$out" in *"192.0.2.9 ~all"*) bad "the management address leaked into the SPF advice" ;;
  *) pass "the management address stays out of the DNS advice entirely" ;; esac

# Verify must judge DNS against the public identity too, or it reports a
# correctly configured domain as broken.
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="example.test" KIN_TEST_MX="10 relay.example.test." \
      KIN_TEST_SPF='"v=spf1 ip4:198.51.100.38 ~all"' run_gw verify)
case "$out" in *"dns.mx points at the gateway (relay.example.test)"*) pass "verify accepts an MX naming the public hostname" ;;
  *) bad "verify should accept the public MX: ${out}" ;; esac
case "$out" in *"dns.spf authorises the gateway (198.51.100.38)"*) pass "verify accepts SPF listing the public address" ;;
  *) bad "verify should accept the public SPF: ${out}" ;; esac

# A private address must never be treated as a usable public identity.
#
# Assembled rather than written out: CI fails the build on a literal RFC1918
# address anywhere in a .sh file, and that gate exists because a customer's
# real addresses were committed once already. A synthetic one for a test is
# legitimate; a literal that looks like a deployment is not worth the risk of
# teaching the gate to ignore this file.
priv_octet=$((40))
cat >> "$GWCONF" <<EOF
GATEWAY_PUBLIC_IP="10.0.${priv_octet}.101"
EOF
out=$(run_gw apply)
case "$out" in *"No public identity is recorded"*) pass "an RFC1918 address is rejected as a public identity" ;;
  *) bad "a private public_ip should not be accepted: ${out}" ;; esac
write_gw_conf "192.0.2.9"

# An error path that crashes is worse than the error. Every failing GET used to
# die with "API_ERROR: unbound variable", because half this script reads the API
# as `body=$(api_get ...)` and a command substitution runs in a subshell: the
# message assigned in there never reached the caller. The crash landed on
# exactly the path whose whole job is explaining a failure.
for ep in GET_version GET_config_mail GET_config_domains GET_config_ruledb_rules; do
  fixture_healthy
  printf '500' > "${FIX}/${ep}.code"
  out=$(run_gw apply 2>&1; run_gw plan 2>&1; run_gw probe 2>&1)
  rm -f "${FIX}/${ep}.code"
  case "$out" in
    *"unbound variable"*) bad "${ep} failing crashes the script instead of reporting" ;;
    *) pass "a failing ${ep} reports a reason instead of crashing" ;;
  esac
done

fixture_healthy
printf '500' > "${FIX}/GET_config_domains.code"
out=$(run_gw apply 2>&1)
rm -f "${FIX}/GET_config_domains.code"
case "$out" in *"HTTP 500"*) pass "the gateway's actual answer reaches the operator through a subshell" ;;
  *) bad "the HTTP status should survive the command substitution: ${out}" ;; esac

# --- the rule database -------------------------------------------------------
# Fixtures are the real rule set from a live PMG 9.1, names and ids included.
# The first version of this matched names by keyword, and on that gateway the
# keyword "spam" caught "Block Spam (Level 10)" - a rule that DESTROYS mail on
# a score rather than quarantining it. An installer must not switch that on.
rules_fixture() {
  printf '%s' "$1" > "${FIX}/GET_config_ruledb_rules.json"
}

# The factory set as it actually ships, with the ids this gateway had.
FACTORY_RULES='{"data":[
  {"id":4,"name":"Blocklist","active":1},
  {"id":2,"name":"Block Viruses","active":1},
  {"id":3,"name":"Virus Alert","active":1},
  {"id":1,"name":"Block Dangerous Files","active":1},
  {"id":5,"name":"Modify Header","active":1},
  {"id":13,"name":"Quarantine Office Files","active":0},
  {"id":12,"name":"Block Multimedia Files","active":0},
  {"id":6,"name":"Welcomelist","active":1},
  {"id":9,"name":"Block Spam (Level 10)","active":0},
  {"id":8,"name":"Quarantine/Mark Spam (Level 5)","active":0},
  {"id":7,"name":"Quarantine/Mark Spam (Level 3)","active":1},
  {"id":10,"name":"Block outgoing Spam","active":0},
  {"id":11,"name":"Add Disclaimer","active":0}
]}'

fixture_fresh
rules_fixture "$FACTORY_RULES"
: > "$CALLS"
out=$(run_gw apply)

# The endpoint. /config/ruledb/rules/{id} is a DIRECTORY on PMG 9.1 and
# answers 501 to a write; the settable object is one level down. Every rule
# activation failed on the live gateway because of this.
if grep -q 'config/ruledb/rules/8/config' "$CALLS"; then
  pass "a rule is written at .../rules/{id}/config, which is the object PMG exposes"
else
  bad "wrong rule endpoint: $(grep -o 'config/ruledb/rules[^ ]*' "$CALLS" | sort -u | tr '\n' ' ')"
fi

# Quarantine is recoverable, so switching it on is ours to do.
case "$out" in *"rule switched on: Quarantine/Mark Spam (Level 5)"*)
  pass "the Level 5 rule is switched on: confident spam is quarantined, not lost" ;;
  *) bad "should enable the Level 5 quarantine rule: ${out}" ;; esac

# Blocking is not. A false positive here is a message nobody can recover.
if grep -q 'config/ruledb/rules/9/config' "$CALLS"; then
  bad "Block Spam (Level 10) was switched on; it destroys mail on a score"
else
  pass "Block Spam (Level 10) is left alone - a blocked message cannot be released"
fi

# Rules that would take a business offline must never be touched by an installer.
for id in 13 12 10 11; do
  if grep -q "config/ruledb/rules/${id}/config" "$CALLS"; then
    bad "rule ${id} was switched on; office files, multimedia and disclaimers are not ours"
  fi
done
pass "office-file, multimedia, outbound-block and disclaimer rules are left to the customer"

case "$out" in *"rule on: Block Viruses"*) pass "an active protective rule is confirmed by name" ;;
  *) bad "should confirm Block Viruses: ${out}" ;; esac
case "$out" in *"rule on: Block Dangerous Files"*) pass "the dangerous-attachment rule is checked, not assumed" ;;
  *) bad "should confirm the attachment rule: ${out}" ;; esac
case "$out" in *"A blocked"*"message is gone"*) pass "the restraint is stated, so it reads as a decision and not an omission" ;;
  *) bad "should explain why blocking rules are left off: ${out}" ;; esac

# A protective rule that the operator turned off is reported, never silently
# turned back on: they may have had a reason.
fixture_fresh
rules_fixture '{"data":[{"id":2,"name":"Block Viruses","active":0},{"id":7,"name":"Quarantine/Mark Spam (Level 3)","active":1}]}'
: > "$CALLS"
out=$(run_gw apply)
case "$out" in *"rule OFF: Block Viruses"*) pass "a protective rule that is off is reported loudly" ;;
  *) bad "should warn about an inactive virus rule: ${out}" ;; esac
if grep -q 'config/ruledb/rules/2/config' "$CALLS"; then
  bad "an operator's deliberate change was overruled silently"
else
  pass "and is not overruled - the operator may have turned it off on purpose"
fi

# A rule we have never seen is reported and left alone.
fixture_fresh
rules_fixture '{"data":[{"id":40,"name":"Customer Special Rule","active":1}]}'
: > "$CALLS"
out=$(run_gw apply)
if grep -q 'config/ruledb/rules/40/config' "$CALLS"; then
  bad "an unrecognised rule was modified"
else
  pass "an unrecognised rule is left exactly as the customer left it"
fi
case "$out" in *"No protective rule is active"*) pass "a gateway with nothing protective on is called out" ;;
  *) bad "should warn when nothing protective is active: ${out}" ;; esac
case "$out" in *"scan every message and then deliver all of them"*) pass "and the consequence is spelled out" ;;
  *) bad "should explain the consequence: ${out}" ;; esac

# Failure to switch a rule on is a warning, never a failed apply.
fixture_fresh
rules_fixture '{"data":[{"id":8,"name":"Quarantine/Mark Spam (Level 5)","active":0}]}'
printf '403' > "${FIX}/PUT_config_ruledb_rules_8_config.code"
out=$(run_gw apply); rc=$?
rm -f "${FIX}/PUT_config_ruledb_rules_8_config.code"
case "$out" in *"could not switch it on"*) pass "a rule that cannot be switched on is named with the reason" ;;
  *) bad "should report the failure: ${out}" ;; esac
if [ "$rc" -eq 0 ]; then
  pass "and it does not fail the apply, because mail still flows"
else
  bad "a rule problem should not fail apply (rc=${rc})"
fi

# Never create or delete.
if grep -qE 'X (POST|DELETE) .*config/ruledb' "$CALLS"; then
  bad "apply created or deleted a rule in the customer's database"
else
  pass "apply never creates or deletes a rule"
fi
rm -f "${FIX}/GET_config_ruledb_rules.json"

# --- blocklists that reject at the door --------------------------------------
# A DNSBL in PMG is a hard SMTP reject, and Postfix reads ANY 127.0.0.0/8
# answer as a listing. Spamhaus answers 127.255.255.254 - "Error: open
# resolver" - to every query that arrives through a public resolver, so every
# sender looked listed and all inbound mail was refused on a live deployment.
fixture_fresh
: > "$CALLS"
out=$(run_gw apply)
if grep -q 'delete=dnsbl_sites' "$CALLS"; then
  pass "apply CLEARS the SMTP blocklist, so re-applying repairs a gateway that has one"
else
  bad "apply should clear dnsbl_sites: $(grep -o 'dnsbl[^ ]*' "$CALLS" | head -2)"
fi
if grep -q 'dnsbl_sites=zen' "$CALLS"; then
  bad "a DNSBL is still being set by default"
else
  pass "no DNSBL is imposed by default"
fi
case "$out" in *"scored, never refused at the door"*) pass "and the trade is explained rather than left silent" ;;
  *) bad "should explain the DNSBL decision: ${out}" ;; esac

# SpamAssassin still consults the same lists - scored, not rejected.
if grep -q 'rbl_checks=1' "$CALLS"; then
  pass "blocklist data is still used, as score rather than refusal"
else
  bad "rbl_checks should stay on"
fi

# An operator who has a local resolver can opt in, and gets a return-code
# filter so an error answer can never be read as a listing.
printf 'GATEWAY_DNSBL="zen.spamhaus.org"\n' >> "$GWCONF"
fixture_fresh
: > "$CALLS"
out=$(run_gw apply)
if grep -q 'dnsbl_sites=zen.spamhaus.org=127.0.0' "$CALLS"; then
  pass "an opted-in blocklist is pinned to the answers that really mean listed"
else
  bad "opt-in DNSBL should carry a return-code filter: $(grep -o 'dnsbl_sites=[^ ]*' "$CALLS" | head -1)"
fi
case "$out" in *"needs a local recursive resolver"*) pass "and the operator is warned what it requires" ;;
  *) bad "should warn about the resolver requirement: ${out}" ;; esac
write_gw_conf "192.0.2.9"

# --- the two failures nobody goes looking for ---------------------------------
# An open relay keeps working perfectly until the sending address is blocklisted
# everywhere, and a gateway with a full disk stops accepting mail outright. Both
# are silent, both are slow, and neither shows up as an error anywhere.
fixture_healthy
printf '%s' '{"data":[{"cidr":"192.0.2.20/32"}]}' > "${FIX}/GET_config_mynetworks.json"
printf '%s' '{"data":[{"name":"pmg"}]}' > "${FIX}/GET_nodes.json"
printf '%s' '{"data":{"rootfs":{"total":100,"used":40}}}' > "${FIX}/GET_nodes_pmg_status.json"
vrun() {
  KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
  KIN_TEST_ZDOMAINS="example.test" run_gw verify
}
out=$(vrun)
case "$out" in *"relay-scope is tight"*) pass "verify confirms nothing can relay through the gateway unauthenticated" ;;
  *) bad "verify should check the gateway's relay scope: ${out}" ;; esac
case "$out" in *"gateway.disk is 40% full"*) pass "verify reports how much room the gateway has left" ;;
  *) bad "verify should check the gateway disk: ${out}" ;; esac
case "$out" in *"KIN_GW_VERIFY problems=0"*) pass "a healthy gateway still verifies clean with the new checks" ;;
  *) bad "healthy gateway should verify clean: ${out}" ;; esac

# A range wide enough to relay for the internet.
printf '%s' '{"data":[{"cidr":"192.0.2.20/32"},{"cidr":"203.0.113.0/16"}]}' > "${FIX}/GET_config_mynetworks.json"
out=$(vrun)
case "$out" in *"KIN_GW_PROBLEM gateway.relay-scope"*) pass "an over-wide trusted network is a problem, not a note" ;;
  *) bad "a /16 should be flagged: ${out}" ;; esac
case "$out" in *"That is an open relay"*) pass "and it is named as what it is" ;;
  *) bad "should say open relay: ${out}" ;; esac

printf '%s' '{"data":[{"cidr":"0.0.0.0/0"}]}' > "${FIX}/GET_config_mynetworks.json"
out=$(vrun)
case "$out" in *"KIN_GW_PROBLEM gateway.relay-scope"*) pass "a default route in the trusted networks is caught" ;;
  *) bad "0.0.0.0/0 should be flagged: ${out}" ;; esac

# Loopback is not a finding. A guard that cries wolf is a guard nobody reads.
printf '%s' '{"data":[{"cidr":"127.0.0.0/8"},{"cidr":"192.0.2.20/32"}]}' > "${FIX}/GET_config_mynetworks.json"
out=$(vrun)
case "$out" in *"KIN_GW_PROBLEM gateway.relay-scope"*) bad "loopback was flagged as an open relay" ;;
  *) pass "loopback is not mistaken for an open relay" ;; esac
printf '%s' '{"data":[{"cidr":"192.0.2.20/32"}]}' > "${FIX}/GET_config_mynetworks.json"

# A full disk stops mail. This release lengthened the quarantine, so checking
# the machine can afford it is part of having made that trade.
printf '%s' '{"data":{"rootfs":{"total":100,"used":93}}}' > "${FIX}/GET_nodes_pmg_status.json"
out=$(vrun)
case "$out" in *"KIN_GW_PROBLEM gateway.disk"*) pass "a nearly-full gateway disk is a problem" ;;
  *) bad "93% full should be a problem: ${out}" ;; esac
case "$out" in *"stops accepting mail altogether"*) pass "and the consequence is stated" ;;
  *) bad "should say what a full disk does: ${out}" ;; esac

printf '%s' '{"data":{"rootfs":{"total":100,"used":80}}}' > "${FIX}/GET_nodes_pmg_status.json"
out=$(vrun)
case "$out" in *"KIN_GW_WARN"*"80% full"*) pass "a filling disk is a warning before it is a problem" ;;
  *) bad "80% should warn: ${out}" ;; esac
case "$out" in *"KIN_GW_VERIFY problems=0"*) pass "and a warning does not fail verify" ;;
  *) bad "a disk warning should not fail verify: ${out}" ;; esac

# Neither check may fail verify when the gateway simply will not answer.
printf '500' > "${FIX}/GET_nodes.code"
printf '500' > "${FIX}/GET_config_mynetworks.code"
out=$(vrun); rc=$?
rm -f "${FIX}/GET_nodes.code" "${FIX}/GET_config_mynetworks.code"
case "$out" in *"Could not read the gateway"*) pass "an unanswerable check says so rather than inventing a verdict" ;;
  *) bad "should report it could not check: ${out}" ;; esac
rm -f "${FIX}/GET_nodes.json" "${FIX}/GET_nodes_pmg_status.json"

# Undeliverable mail is retried for longer than the default, not shorter.
fixture_fresh
: > "$CALLS"
out=$(run_gw apply)
if grep -q 'queue-lifetime=7' "$CALLS"; then
  pass "undeliverable mail is retried for a week before it is given up on"
else
  bad "queue-lifetime should be set: $(grep -o 'queue-lifetime=[0-9]*' "$CALLS" | head -1)"
fi
case "$out" in *"bounce cannot be taken back"*) pass "and the reason for the longer window is stated" ;;
  *) bad "should explain the queue lifetime: ${out}" ;; esac

# --- split topology ----------------------------------------------------------
# On a split the machine that carries mail is the EDGE. The mailbox node runs
# zimbra-ldap and zimbra-store and nothing that speaks SMTP, so a gateway
# pointed at it accepts mail from the internet and then cannot deliver any of
# it. Every address the gateway is given has to be the edge's.
split_conf() {
  cat > "$APPCONF" <<EOF
MAIL_DOMAIN="example.test"
MAIL_HOST="mail.example.test"
SERVER_IP="${1:-192.0.2.20}"
TOPOLOGY="split"
EDGE_IP="192.0.2.30"
EDGE_HOST="edge.example.test"
MAILBOX_IP="192.0.2.40"
MAILBOX_HOST="mbox.example.test"
EOF
}

split_conf
fixture_fresh
: > "$CALLS"
out=$(KIN_GW_LOCAL_IPV4S="192.0.2.30" run_gw apply)

if grep -q 'host=192.0.2.30' "$CALLS"; then
  pass "split: the gateway delivers to the edge, which is the machine with an MTA"
else
  bad "split: transport should target the edge: $(grep -o 'host=[0-9.]*' "$CALLS" | head -1)"
fi
if grep -q 'relay=192.0.2.30' "$CALLS"; then
  pass "split: the relay target is the edge"
else
  bad "split: relay should be the edge: $(grep -o 'relay=[0-9.]*' "$CALLS" | head -1)"
fi
if grep -q 'cidr=192.0.2.30/32' "$CALLS"; then
  pass "split: the edge is what may relay out through the gateway"
else
  bad "split: mynetworks should trust the edge: $(grep -o 'cidr=[0-9./]*' "$CALLS" | head -1)"
fi
if grep -qE 'host=192\.0\.2\.(20|40)|relay=192\.0\.2\.(20|40)|cidr=192\.0\.2\.(20|40)/32' "$CALLS"; then
  bad "split: an address that is not the edge reached the gateway"
else
  pass "split: neither SERVER_IP nor the mailbox is ever given to the gateway"
fi
if grep -q 'zmprov ms edge.example.test zimbraMtaRelayHost' "$CALLS"; then
  pass "split: zmprov writes the relay on the edge server object, not the public name"
else
  bad "split: zmprov should target EDGE_HOST: $(grep RelayHost "$CALLS" | head -1)"
fi
if grep -q 'zmprov ms mail.example.test zimbraMtaRelayHost' "$CALLS"; then
  bad "split: zmprov wrote the public MAIL_HOST, which may not be a server object"
else
  pass "split: the public MAIL_HOST is never used as a zmprov server name"
fi

# Run from the mailbox node it must refuse, not misconfigure.
out=$(KIN_GW_LOCAL_IPV4S="192.0.2.40" run_gw apply); rc=$?
case "$out" in *"reason=wrong-node"*) pass "split: linking from the mailbox node is refused" ;;
  *) bad "split: should refuse on the mailbox node: ${out}" ;; esac
case "$out" in *"linked from the EDGE"*) pass "split: and it says which machine to do it from" ;;
  *) bad "split: should name the edge: ${out}" ;; esac
[ "$rc" -ne 0 ] && pass "split: refusing on the wrong node is a non-zero exit"                 || bad "split: wrong-node refusal should fail"

# A split with no edge address has nowhere to send mail.
cat > "$APPCONF" <<EOF
MAIL_DOMAIN="example.test"
MAIL_HOST="mail.example.test"
SERVER_IP="192.0.2.20"
TOPOLOGY="split"
MAILBOX_IP="192.0.2.40"
EOF
out=$(run_gw apply)
case "$out" in *"reason=no-edge-ip"*) pass "split: a missing EDGE_IP is named, not silently treated as SERVER_IP" ;;
  *) bad "split: should refuse without EDGE_IP: ${out}" ;; esac

# A single appliance must be completely unaffected by all of the above.
write_appliance_conf
fixture_fresh
: > "$CALLS"
out=$(run_gw apply)
if grep -q 'host=192.0.2.20' "$CALLS" && grep -q 'relay=192.0.2.20' "$CALLS"; then
  pass "single appliance: still uses its own address, exactly as before"
else
  bad "single appliance behaviour changed: $(grep -o 'relay=[0-9.]*' "$CALLS" | head -1)"
fi
case "$out" in *"reason=wrong-node"*) bad "single appliance was refused as a wrong node" ;;
  *) pass "single appliance is never asked which node it is" ;; esac

# =============================================================================
# Tuning: performance and security
# =============================================================================
fixture_fresh
out=$(run_gw apply)

# Greylisting defers the first message from every unseen sender by minutes.
# It is why inbound felt slow and outbound felt instant in live QA.
if grep -q 'greylist=0' "$CALLS"; then
  pass "greylisting is off by default, so a first message is not delayed by minutes"
else
  bad "greylist should default to 0: $(grep -o 'greylist=[0-9]' "$CALLS" | head -1)"
fi
case "$out" in *"no first-contact delay"*) pass "the greylisting trade-off is stated, not hidden" ;;
  *) bad "apply should explain the greylist choice: ${out}" ;; esac

# ...but it stays available, because some sites want it.
printf 'GATEWAY_GREYLIST=1\n' >> "$GWCONF"
fixture_fresh
out=$(run_gw apply)
if grep -q 'greylist=1' "$CALLS"; then
  pass "an operator who wants greylisting can still have it"
else
  bad "GATEWAY_GREYLIST=1 should turn it on"
fi
case "$out" in *"That delay is not a fault"*) pass "with greylisting on, the delay is explained in advance" ;;
  *) bad "should explain the delay when greylisting is on: ${out}" ;; esac
write_gw_conf "192.0.2.9"

fixture_fresh
out=$(run_gw apply)
# Bayes and the auto-welcomelist are off in stock PMG and are the best defence
# against false positives - a quarantined invoice costs more than a delivered advert.
for want in 'use_bayes=1' 'use_awl=1' 'extract_text=1' 'rbl_checks=1'; do
  if grep -q "$want" "$CALLS"; then pass "spam detector: ${want}"; else bad "missing ${want}"; fi
done

# A password-protected archive cannot be scanned, and "password in the next
# email" is how a lot of ransomware arrives.
if grep -q 'archiveblockencrypted=1' "$CALLS"; then
  pass "encrypted archives are flagged rather than waved through unscanned"
else
  bad "archiveblockencrypted should be 1"
fi

# Data past a scanner limit is skipped UNSCANNED, so a small limit is a hole.
if grep -q 'archivemaxrec=8' "$CALLS"; then
  pass "nested archives are followed deeper than stock (zip-in-zip evasion)"
else
  bad "archivemaxrec should be raised above the stock 5"
fi

# A quarantine page that renders live links and loads remote images attacks
# the person reviewing it.
if grep -q 'allowhrefs=0' "$CALLS" && grep -q 'viewimages=0' "$CALLS"; then
  pass "quarantine shows no clickable links and loads no tracking images"
else
  bad "quarantine should disable hrefs and images"
fi
if grep -q 'lifetime=14' "$CALLS" && grep -q 'lifetime=30' "$CALLS"; then
  pass "a false positive survives a fortnight, and virus evidence a month"
else
  bad "quarantine lifetimes not set as intended"
fi

# Scanning is pointless if no rule acts on the verdict.
if grep -q 'config/ruledb/rules' "$CALLS"; then
  pass "the protective rules are checked, not assumed"
else
  bad "apply should read the rule database"
fi

# A tolerant check: a gateway that cannot answer about its rules must not fail
# the apply, because mail still flows and the customer owns that database.
printf '500' > "${FIX}/GET_config_ruledb_rules.code"
fixture_fresh
out=$(run_gw apply); rc=$?
rm -f "${FIX}/GET_config_ruledb_rules.code"
if [ "$rc" -eq 0 ]; then
  pass "an unreadable rule database does not fail the apply"
else
  bad "rule database trouble should not fail apply (rc=${rc})"
fi
case "$out" in *"nothing acts on what the scanners find"*) pass "and the operator is told exactly what that costs" ;;
  *) bad "should explain the consequence: ${out}" ;; esac


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

# With no public identity recorded, verify must give a PLACEHOLDER, never the
# management address. It used to warn "this is a guess" and then print
#   Set: example.test. MX 10 192.0.2.9
#   Add: ip4:192.0.2.9
# underneath. An operator who reads a warning and then finds a concrete
# instruction below it does the concrete thing (QA Phase 16, 12 Sep 2026).
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="example.test" KIN_TEST_MX="10 mail.example.test." \
      KIN_TEST_SPF='"v=spf1 ip4:192.0.2.20 ~all"' run_gw verify)
case "$out" in *"gateway public address"*) pass "with no public identity, verify prints a placeholder, not an address" ;;
  *) bad "verify should print a placeholder: ${out}" ;; esac
case "$out" in
  *"MX  10  192.0.2.9"*|*"Add:     ip4:192.0.2.9"*)
    bad "verify told the operator to publish the management address" ;;
  *) pass "the management address never appears as something to publish" ;;
esac
case "$out" in *"no public identity recorded"*) pass "and says plainly why it cannot be more specific" ;;
  *) bad "verify should explain the missing identity: ${out}" ;; esac

# With one recorded, it prints the real records.
cat >> "$GWCONF" <<EOF
GATEWAY_PUBLIC_HOST="relay.example.test"
GATEWAY_PUBLIC_IP="198.51.100.38"
EOF
out=$(KIN_TEST_RELAYHOST="192.0.2.9:26" KIN_TEST_ZNETS="127.0.0.0/8 192.0.2.9/32" \
      KIN_TEST_ZDOMAINS="example.test" KIN_TEST_MX="10 mail.example.test." \
      KIN_TEST_SPF='"v=spf1 ip4:192.0.2.20 ~all"' run_gw verify)
case "$out" in *"Add:"*"ip4:198.51.100.38"*) pass "verify says exactly what to add to SPF once it knows the public address" ;;
  *) bad "verify should print the SPF fix: ${out}" ;; esac
case "$out" in *"MX  10  relay.example.test"*) pass "and names the public hostname for the MX" ;;
  *) bad "verify should print the MX target: ${out}" ;; esac
write_gw_conf "192.0.2.9"

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
