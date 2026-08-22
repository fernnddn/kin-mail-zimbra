#!/usr/bin/env bash
# Unit tests for healthcheck-dns-class.sh (no live DNS).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

export KIN_HEALTHCHECK_DNS_SOURCE_ONLY=1
# shellcheck source=healthcheck-dns-class.sh
. ./healthcheck-dns-class.sh

if healthcheck_host_is_not_published_mx "mail2.example.test" "mail.example.test"; then
  pass "non-MX host is not the published MX"
else
  bad "mail2 vs mail MX"
fi

if healthcheck_host_is_not_published_mx "mail.example.test" "mail.example.test"; then
  bad "primary MAIL_HOST should match published MX"
else
  pass "primary MAIL_HOST matches published MX"
fi

if healthcheck_host_is_not_published_mx "mail.example.test" ""; then
  bad "empty MX must not classify as non-MX (keep primary FAIL path)"
else
  pass "empty MX does not trip non-MX heuristic"
fi

if grep -q 'b "DKIM key not created on this host' ../05-healthcheck.sh \
  && grep -q 'HA peer key cannot match DNS' ../05-healthcheck.sh \
  && grep -q 'f "DKIM key not created yet"' ../05-healthcheck.sh \
  && grep -q 'not creating a DKIM key' ../04-tls-dkim.sh; then
  pass "HA peer DKIM is skipped / blocked, primary still fail-closes missing key"
else
  bad "HA peer DKIM landmine still fail-closes 05 after peer zmsetup"
fi

if grep -q 'T2 skipped on HA peer' ../05-healthcheck.sh \
  && grep -q 'tasks_from: snippets.yml' ../../ansible/playbooks/mail-zpush.yml \
  && grep -q 'Disable the Zimbra boot unit' \
    ../../ansible/roles/pacemaker_agents/tasks/systemd.yml; then
  pass "HA peer T2 skip, snippets play, and Zimbra boot disable are wired"
else
  bad "HA peer T2 / snippets play / Zimbra boot disable missing"
fi

act=$(healthcheck_opendkim_classify "opendkim-testkey: key OK")
if [ "$act" = "pass" ]; then
  pass "opendkim: key OK is pass"
else
  bad "opendkim key OK: [$act]"
fi

act=$(healthcheck_opendkim_classify "$(cat <<'EOF'
opendkim-testkey: checking key 'SEL._domainkey.example.test'
opendkim-testkey: 'SEL._domainkey.example.test' record not found
EOF
)")
if [ "$act" = "blocked_missing" ]; then
  pass "opendkim: record not found is blocked_missing"
else
  bad "opendkim missing: [$act]"
fi

act=$(healthcheck_opendkim_classify "opendkim-testkey: bad key data")
if [ "$act" = "blocked_other" ]; then
  pass "opendkim: other tool output is blocked_other"
else
  bad "opendkim other: [$act]"
fi

act=$(healthcheck_auth_results_dkim "Authentication-Results: mail; dkim=pass header.d=example.test")
if [ "$act" = "pass" ]; then
  pass "auth-results: dkim=pass"
else
  bad "auth pass: [$act]"
fi

act=$(healthcheck_auth_results_dkim "dkim=neutral (no key)")
if [ "$act" = "blocked" ]; then
  pass "auth-results: dkim=neutral is blocked (DNS not visible)"
else
  bad "auth neutral: [$act]"
fi

act=$(healthcheck_auth_results_dkim "dkim=fail (bad signature)")
if [ "$act" = "fail" ]; then
  pass "auth-results: dkim=fail stays server-side fail"
else
  bad "auth fail: [$act]"
fi

act=$(healthcheck_a_vs_send "" "119.82.245.34")
if [ "$act" = "blocked_missing" ]; then
  pass "A vs send: missing A is blocked"
else
  bad "A missing: [$act]"
fi

act=$(healthcheck_a_vs_send "119.82.245.34" "119.82.245.34")
if [ "$act" = "pass" ]; then
  pass "A vs send: match is pass"
else
  bad "A match: [$act]"
fi

act=$(healthcheck_a_vs_send "119.82.245.37" "119.82.245.34")
if [ "$act" = "blocked_mismatch" ]; then
  pass "A vs send: NAT mismatch is blocked not fail"
else
  bad "A mismatch: [$act]"
fi

act=$(healthcheck_spf_cover "v=spf1 mx ~all" "119.82.245.34" "119.82.245.34")
if [ "$act" = "pass_mx" ]; then
  pass "SPF mx covers when A equals send IP"
else
  bad "SPF mx cover: [$act]"
fi

act=$(healthcheck_spf_cover "v=spf1 mx ~all" "119.82.245.34" "119.82.245.37")
if [ "$act" = "blocked_mx" ]; then
  pass "SPF mx does not cover NAT send IP: blocked (add ip4)"
else
  bad "SPF mx nat: [$act]"
fi

act=$(healthcheck_spf_cover "v=spf1 ip4:119.82.245.34 ~all" "119.82.245.34" "119.82.245.37")
if [ "$act" = "pass_explicit" ]; then
  pass "SPF ip4: covers send IP even if A differs"
else
  bad "SPF ip4: [$act]"
fi

act=$(healthcheck_ptr_chain "119.82.245.34" "mail.example.test" "mail.example.test." "119.82.245.34")
if [ "$act" = "pass_hostname" ]; then
  pass "PTR chain: hostname + matching A"
else
  bad "PTR hostname: [$act]"
fi

act=$(healthcheck_ptr_chain "119.82.245.34" "mail.example.test" "ip-245-34.isp.example." "")
if [ "$act" = "blocked_chain" ]; then
  pass "PTR chain: other name with no A is blocked"
else
  bad "PTR no A: [$act]"
fi

act=$(healthcheck_ptr_chain "119.82.245.34" "mail.example.test" "ip-245-34.isp.example." "119.82.245.34")
if [ "$act" = "pass_other_name" ]; then
  pass "PTR chain: ISP name whose A returns to send IP"
else
  bad "PTR other name: [$act]"
fi

act=$(healthcheck_ptr_chain "119.82.245.34" "mail.example.test" "" "")
if [ "$act" = "blocked_missing" ]; then
  pass "PTR chain: missing PTR is blocked"
else
  bad "PTR missing: [$act]"
fi

if grep -q 'healthcheck-dns-class.sh' ../05-healthcheck.sh \
  && grep -q 'healthcheck_opendkim_classify' ../05-healthcheck.sh \
  && grep -q 'healthcheck_a_vs_send' ../05-healthcheck.sh \
  && grep -q 'healthcheck_spf_cover' ../05-healthcheck.sh \
  && grep -q '|| b "MX not published yet"' ../05-healthcheck.sh \
  && ! grep -q '|| f "MX not published yet"' ../05-healthcheck.sh \
  && grep -q 'KIN_DKIM_WAIT_SEC' ../kin-mail.sh; then
  pass "05-healthcheck.sh and kin-mail.sh wire the classifier and DKIM wait"
else
  bad "05 or kin-mail.sh missing classifier / DKIM wait wiring"
fi

if grep -q 'b "Certificate is still self-signed' ../05-healthcheck.sh \
  && ! grep -q 'f "No trusted certificate yet' ../05-healthcheck.sh \
  && grep -q 'TLS_METHOD:-}" = "customer"' ../05-healthcheck.sh; then
  pass "05-healthcheck.sh treats self-signed / customer hook as blocked not fail"
else
  bad "05-healthcheck.sh still fail-closes TLS for commercial or customer certs"
fi

if grep -q 'Dead-man stays armed' ../kin-mail.sh \
  && ! grep -q 'elapsed without cancel' ../kin-mail.sh; then
  pass "kin-mail.sh console dead-man does not fail the pipeline"
else
  bad "kin-mail.sh still fails Deploy when dead-man is not cancelled"
fi

if grep -q 'b "Cannot determine sending address' ../05-healthcheck.sh \
  && grep -q 'b "NOT consistent:' ../05-healthcheck.sh \
  && ! grep -q 'f "Cannot determine sending address' ../05-healthcheck.sh; then
  pass "05-healthcheck.sh treats SMTP egress IP gaps as blocked not fail"
else
  bad "05-healthcheck.sh still fail-closes on sending-address probes"
fi

if grep -q 'b "Message not found in message store yet' ../05-healthcheck.sh \
  && grep -q 'b "No DKIM-Signature header yet' ../05-healthcheck.sh \
  && ! grep -q 'f "Message not found in message store"' ../05-healthcheck.sh; then
  pass "05-healthcheck.sh treats amavis warmup / missing DKIM header as blocked"
else
  bad "05-healthcheck.sh still fail-closes T1 warmup"
fi

if grep -q '07-zpush.sh failed — mail install continues' ../kin-mail.sh \
  && grep -q '11-admin-path-lockdown.sh failed - mail install continues' ../kin-mail.sh \
  && grep -q '09-hardening.sh failed - mail install continues' ../kin-mail.sh \
  && ! grep -q 'Pipeline stopped at 11-admin-path-lockdown.sh' ../kin-mail.sh \
  && ! grep -q 'Pipeline stopped at 09-hardening.sh' ../kin-mail.sh; then
  pass "kin-mail.sh does not stop Deploy on Z-Push, hardening, or admin-path lockdown"
else
  bad "kin-mail.sh still fail-closes optional post-Zimbra stages"
fi

if grep -q 'stopping it (Zimbra ships its own)' ../01-preflight.sh \
  && ! grep -q 'is running - Zimbra ships its own and will conflict' ../01-preflight.sh; then
  pass "01-preflight.sh stops stock postfix/nginx instead of aborting"
else
  bad "01-preflight.sh still fail-closes on stock conflicting services"
fi

if grep -q 'AD path not verified. Local mail still works' ../06-hybrid-auth.sh; then
  pass "06-hybrid-auth.sh does not abort Deploy when AD is unreachable"
else
  bad "06-hybrid-auth.sh still fail-closes on AD bind"
fi

if grep -q 'b "AD LDAP auth not passing yet' ../05-healthcheck.sh \
  && ! grep -q 'f "AD LDAP auth failed' ../05-healthcheck.sh; then
  pass "05-healthcheck.sh treats AD bind gaps as blocked not fail"
else
  bad "05-healthcheck.sh still fail-closes on AD LDAP"
fi

if grep -q 'b "Zimbra still warming after DNS-cache flush restarts' ../05-healthcheck.sh \
  && ! grep -q 'f "Zimbra not healthy after DNS-cache flush restarts"' ../05-healthcheck.sh; then
  pass "05-healthcheck.sh treats post-flush warmup as blocked not fail"
else
  bad "05-healthcheck.sh still fail-closes after amavis/opendkim restart"
fi

if [ "$fails" -ne 0 ]; then
  printf 'FAILED %s checks\n' "$fails"
  exit 1
fi
printf 'All healthcheck-dns-class tests passed\n'
exit 0
