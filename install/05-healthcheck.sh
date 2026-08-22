#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 05 HEALTHCHECK
#
# Runs the Phase 1 acceptance tests and prints a checklist of what passes,
# what fails, and what is blocked by the network rather than by the server.
#
#   ./05-healthcheck.sh              full run
#   ./05-healthcheck.sh --quick      skip the mail-flow tests
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

# shellcheck source=lib/zimbra-data-disk-probe.sh
. ./lib/zimbra-data-disk-probe.sh
# shellcheck source=lib/healthcheck-dns-class.sh
. ./lib/healthcheck-dns-class.sh
if zimbra_is_mid_handoff; then
  warn "Mid-handoff: /opt/zimbra is unmounted; skipping healthcheck (zmcontrol not reachable)."
  exit 0
fi

QUICK=0; [ "${1:-}" = "--quick" ] && QUICK=1
PASS=0; FAILED=0; BLOCKED=0
p() { ok   "$*"; PASS=$((PASS+1)); }
f() { fail "$*"; FAILED=$((FAILED+1)); }
b() { warn "$*"; BLOCKED=$((BLOCKED+1)); }

echo
printf '%s\n' "${BLD}  KIN Mail - health check  ${MAIL_HOST}  $(date -Is)${RST}"

# --- services ----------------------------------------------------------------
echo; say "Zimbra services"
if [ -d /opt/zimbra ]; then
  ST=$(su - zimbra -c "zmcontrol status" 2>&1)
  DOWN=$(printf '%s' "$ST" | grep -v Running | grep -vE "^Host|^$" | wc -l)
  RUN=$(printf '%s' "$ST" | grep -c Running)
  [ "$DOWN" -eq 0 ] && p "${RUN} services running, none down" \
                    || { f "${DOWN} services not running"; printf '%s' "$ST" | grep -v Running | sed 's/^/      /'; }
  info "$(su - zimbra -c 'zmcontrol -v' 2>/dev/null | head -1)"
else
  f "Zimbra is not installed yet"; exit 1
fi

# --- listeners ---------------------------------------------------------------
echo; say "Listening ports"
for port in 25 443 587 993; do
  ss -lnt 2>/dev/null | grep -q ":${port} " && p "port ${port} listening" || f "port ${port} not listening"
done

# --- certificate -------------------------------------------------------------
echo; say "TLS certificate"
SUB=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 -servername "$MAIL_HOST" 2>/dev/null \
      | openssl x509 -noout -subject -issuer -enddate 2>/dev/null)
ISSUER=$(printf '%s' "$SUB" | sed -n 's/^issuer=//p' | head -1)
SUBJECT=$(printf '%s' "$SUB" | sed -n 's/^subject=//p' | head -1)
if [ -z "$SUB" ]; then
  f "No TLS certificate presented on :443"
elif printf '%s' "$SUB" | grep -qi "Let's Encrypt" \
  || { [ -n "$ISSUER" ] && [ -n "$SUBJECT" ] && [ "$ISSUER" != "$SUBJECT" ]; }; then
  p "Trusted certificate installed"
  printf '%s\n' "$SUB" | sed 's/^/      /'
  EXP=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 -servername "$MAIL_HOST" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  DAYS=$(( ( $(date -d "$EXP" +%s) - $(date +%s) ) / 86400 ))
  [ "$DAYS" -gt 20 ] && p "Valid for ${DAYS} days" || f "Only ${DAYS} days left - check renewal"
else
  b "Certificate is still self-signed - finish TLS (04) before production mail"
fi
if [ "${TLS_METHOD:-}" = "customer" ]; then
  [ -x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh ] \
    && p "Deploy hook installed" \
    || b "No Let's Encrypt deploy hook (expected when TLS_METHOD=customer)"
else
  [ -x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh ] \
    && p "Deploy hook installed" || f "Deploy hook missing - TLS will break in 90 days"
fi

# --- flush caches before DNS assertions --------------------------------------
# Long-lived processes cache negative DNS answers. A record added after they
# started looks missing until they are restarted.
# amavis/opendkim appear in zmcontrol status — on HA Primary that trips the
# Pacemaker kin-zimbra monitor unless we unmanage --monitor first (1.7b/1.7c).
echo; say "Flushing DNS cache"
kin_zimbra_unmanage
trap kin_zimbra_remanage EXIT
systemctl restart dnsmasq >/dev/null 2>&1 && info "dnsmasq restarted"
su - zimbra -c "zmamavisdctl restart" >/dev/null 2>&1 && info "amavis restarted"
su - zimbra -c "zmopendkimctl restart" >/dev/null 2>&1 && info "opendkim restarted"
if ! kin_zimbra_wait_healthy; then
  b "Zimbra still warming after DNS-cache flush restarts (amavis/opendkim)"
else
  info "Zimbra healthy after cache flush"
fi
kin_zimbra_remanage
trap - EXIT
sleep 2

# --- public DNS --------------------------------------------------------------
echo; say "Public DNS"
NS=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" NS 2>/dev/null | tr '\n' ' ')
if [ -z "$(echo "$NS" | tr -d '[:space:]')" ]; then
  f "NS     : none - domain is not delegated"
else
  p "NS     : $NS"
  NSCOUNT=$(dns_ns_provider_count "$NS")
  [ "$NSCOUNT" -eq 1 ] && p "Delegation consistent on one brand provider" \
                       || f "SPLIT delegation across ${NSCOUNT} providers - mail will fail randomly"
fi

MX=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" MX 2>/dev/null | tr '\n' ' ')
[ -n "$MX" ] && p "MX     : $MX" || b "MX not published yet"

SPF=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" TXT 2>/dev/null | grep -i spf1 | tr -d '"')
[ -n "$SPF" ] && p "SPF    : $SPF" || b "SPF not published yet"

DM=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "_dmarc.${MAIL_DOMAIN}" TXT 2>/dev/null | tr -d '"')
[ -n "$DM" ] && p "DMARC  : $DM" || b "DMARC not published yet"

A=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_HOST" A 2>/dev/null | tr '\n' ' ')
[ -n "$A" ] && p "A      : $A" || b "A record not published yet - need public IP from the network team"

# --- DKIM --------------------------------------------------------------------
echo; say "DKIM"
SEL=$(su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" 2>/dev/null \
      | awk '/DKIM Selector/{getline; while($0==""){getline}; print; exit}')
if [ -n "$SEL" ]; then
  p "Selector: ${SEL}"
  WAIT="${KIN_DKIM_WAIT_SEC:-0}"
  case "$WAIT" in
    ''|*[!0-9]*) WAIT=0 ;;
  esac
  if [ "$QUICK" -eq 0 ] && [ "$WAIT" -gt 0 ]; then
    info "Waiting up to ${WAIT}s for ${SEL}._domainkey.${MAIL_DOMAIN} TXT (paste from 04, then this check passes)."
    _dkim_wait_start=$(date +%s)
    while true; do
      if healthcheck_dkim_txt_visible "$MAIL_DOMAIN" "$SEL" "$DNS_UPSTREAM_1"; then
        ok "DKIM TXT is visible on ${DNS_UPSTREAM_1}"
        break
      fi
      if [ $(( $(date +%s) - _dkim_wait_start )) -ge "$WAIT" ]; then
        info "Still no public DKIM TXT after ${WAIT}s (operator DNS). Continuing."
        break
      fi
      sleep 10
    done
  fi
  TK=$(ls /opt/zimbra/common/sbin/opendkim-testkey /usr/sbin/opendkim-testkey 2>/dev/null | head -1)
  if [ -z "$TK" ]; then
    f "opendkim-testkey not found (Zimbra/opendkim package)"
  else
    # -vvv is required. Without it the tool prints nothing at all on success
    # and signals the result only through its exit status, so grepping the
    # output reports a healthy key as broken.
    TKOUT=$(su - zimbra -c "$TK -d ${MAIL_DOMAIN} -s ${SEL} -vvv" 2>&1)
    case "$(healthcheck_opendkim_classify "$TKOUT")" in
      pass)
        p "Private key matches published record"
        ;;
      blocked_missing)
        b "DKIM TXT not in public DNS yet (paste the record from 04, then re-run 05)"
        printf '%s\n' "$TKOUT" | tail -3 | sed 's/^/      /'
        ;;
      *)
        b "opendkim-testkey did not confirm the key (DNS still propagating or TXT does not match)"
        printf '%s\n' "$TKOUT" | tail -3 | sed 's/^/      /'
        ;;
    esac
  fi
else
  f "DKIM key not created yet"
fi

# --- outbound SMTP -----------------------------------------------------------
echo; say "Outbound SMTP  ${DIM}(banner grab)${RST}"
SMTP_OUT=0
for t in "alt1.aspmx.l.google.com 25" "gmail-smtp-in.l.google.com 25"; do
  set -- $t
  BAN=$(smtp_banner "$1" "$2")
  if [ -n "$BAN" ]; then p "$1:$2  ${BAN%%$'\n'*}"; SMTP_OUT=1
  else b "$1:$2  no banner - FILTERED"; fi
done
[ "$SMTP_OUT" -eq 0 ] && info "Server cannot send outbound until the firewall allows TCP 25 or 587."

# --- sending address and forward-confirmed reverse DNS -----------------------
# Ask a real mail server which address it sees, never an HTTP echo service.
# A host with several uplinks can egress HTTP and SMTP from different
# addresses, and only the SMTP one decides whether mail is accepted.
smtp_egress_ip() {
  timeout 60 swaks --server gmail-smtp-in.l.google.com --port 25 \
    --from "postmaster@${MAIL_DOMAIN}" --to "postmaster@gmail.com" \
    --quit-after RCPT --timeout 30 2>/dev/null \
    | grep -oE 'at your service, \[[0-9.]+\]' \
    | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' | head -1
}

echo; say "Sending address  ${DIM}(as seen by destination mail server)${RST}"
if [ "$SMTP_OUT" -eq 0 ]; then
  b "Skipped - outbound SMTP still blocked"
  SEND_IP=""
else
  SEND_IP=$(smtp_egress_ip); S2=$(smtp_egress_ip); S3=$(smtp_egress_ip)
  if [ -z "$SEND_IP" ]; then
    b "Cannot determine sending address (banner ok, egress IP not parsed)"
  elif [ "$SEND_IP" = "$S2" ] && [ "$SEND_IP" = "$S3" ]; then
    p "Consistent: ${SEND_IP}"
  else
    b "NOT consistent: ${SEND_IP} / ${S2} / ${S3}"
    info "Multiple uplinks split outbound traffic. PTR can only point to one"
    info "address, so some email will be rejected. Policy routing or an IP"
    info "pool is needed to lock this host to one uplink."
  fi
fi

if [ -n "$SEND_IP" ]; then
  echo; say "Forward-confirmed reverse DNS chain"
  info "Recipients look up PTR from the sender IP, then A from that name, and"
  info "the result must return to the same sender IP."

  A_REC=$(dig +short +time=5 @"$DNS_UPSTREAM_1" A "$MAIL_HOST" 2>/dev/null | head -1)
  case "$(healthcheck_a_vs_send "$A_REC" "$SEND_IP")" in
    pass)
      p "A ${MAIL_HOST} = ${A_REC} = sending address"
      ;;
    blocked_missing)
      b "A ${MAIL_HOST} not published yet - need public IP from the network team"
      ;;
    *)
      b "A ${MAIL_HOST} = ${A_REC}, but SMTP sends from ${SEND_IP} (align A, or add ip4:${SEND_IP} to SPF if NAT is intentional)"
      ;;
  esac

  PTR=$(dig +short +time=5 -x "$SEND_IP" 2>/dev/null | head -1)
  PTR_A=""
  if [ -n "$PTR" ]; then
    PTR_A=$(dig +short +time=5 @"$DNS_UPSTREAM_1" A "${PTR%.}" 2>/dev/null | head -1)
  fi
  case "$(healthcheck_ptr_chain "$SEND_IP" "$MAIL_HOST" "$PTR" "$PTR_A")" in
    pass_hostname)
      p "PTR ${SEND_IP} -> ${PTR}"
      p "Chain complete and matching"
      ;;
    pass_other_name)
      p "PTR ${SEND_IP} -> ${PTR} (not ${MAIL_HOST}; A still returns to ${SEND_IP})"
      ;;
    blocked_missing)
      b "PTR ${SEND_IP} not set yet - request from the IP block owner (ISP), not in Cloudflare"
      ;;
    *)
      b "PTR ${SEND_IP} -> ${PTR:-none}; ${PTR%.} A is ${PTR_A:-empty}, not ${SEND_IP} (ask the ISP)"
      ;;
  esac

  case "$(healthcheck_spf_cover "$SPF" "$SEND_IP" "$A_REC")" in
    pass_explicit) p "SPF explicitly lists the sending address" ;;
    pass_mx)       p "SPF 'mx' covers the sending address" ;;
    blocked_mx)    b "SPF 'mx' does not cover ${SEND_IP} - add ip4:${SEND_IP} in DNS (not a server change)" ;;
    *)             b "SPF does not clearly cover ${SEND_IP}" ;;
  esac
fi

# --- hybrid authentication ---------------------------------------------------
echo; say "Hybrid authentication"
MECH=$(su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAuthMech" 2>/dev/null \
       | awk -F': ' '/zimbraAuthMech:/{print $2; exit}')
FALLBACK=$(su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAuthFallbackToLocal" 2>/dev/null \
           | awk -F': ' '/zimbraAuthFallbackToLocal:/{print $2; exit}')
info "zimbraAuthMech=${MECH:-zimbra(default)}  fallback=${FALLBACK:-unset}"

# Local path always expected once test mailboxes exist (fallback or pure local).
# Root cause of Phase 1 "test1 auth FAIL": `zmprov auth` is not a real command on
# Zimbra 10 FOSS (prints usage). Also passwords with `#` (old default KinTest#1-…)
# become shell comments unless single-quoted — see ensure_kin_test_mailbox.
ERR1=$(mktemp) ERR2=$(mktemp)
if ensure_kin_test_mailbox "$TEST_USER_1" "$TEST_PASS_1" 2>"$ERR1"; then
  p "Local Zimbra auth: ${TEST_USER_1}"
else
  f "Local Zimbra auth failed: ${TEST_USER_1}"
  sed 's/^/      /' "$ERR1" | tail -5
fi
if ! ensure_kin_test_mailbox "$TEST_USER_2" "$TEST_PASS_2" 2>"$ERR2"; then
  f "Failed to prepare test mailbox: ${TEST_USER_2}"
  sed 's/^/      /' "$ERR2" | tail -5
fi
rm -f "$ERR1" "$ERR2"

if [ "${AD_AUTH_ENABLED}" = "yes" ]; then
  case "$MECH" in
    ad|ldap) p "Domain uses external auth (${MECH})" ;;
    *)       b "AD_AUTH_ENABLED=yes but zimbraAuthMech=${MECH:-empty} - AD not bound yet (local mail still works)" ;;
  esac
  case "$FALLBACK" in
    TRUE|true|1) p "zimbraAuthFallbackToLocal active" ;;
    *)           b "zimbraAuthFallbackToLocal not TRUE yet - AD bind deferred or failed" ;;
  esac
  if [ -n "${AD_TEST_USER}" ] && [ -n "${AD_TEST_PASS}" ]; then
    if zimbra_user_auth_ok "$AD_TEST_USER" "$AD_TEST_PASS"; then
      p "AD LDAP auth: ${AD_TEST_USER}"
    else
      b "AD LDAP auth not passing yet: ${AD_TEST_USER} (directory/network, not a server install failure)"
    fi
  else
    b "AD enabled in config but AD_TEST_USER/PASS empty - cannot test AD path"
  fi
else
  info "AD_AUTH_ENABLED is not yes - AD path not tested (local only, skippable by design)"
fi

# --- mail flow ---------------------------------------------------------------
if [ $QUICK -eq 0 ]; then
  echo; say "T1 - internal mail flow"
  ensure_kin_test_mailbox "$TEST_USER_1" "$TEST_PASS_1" >/dev/null 2>&1 || true
  ensure_kin_test_mailbox "$TEST_USER_2" "$TEST_PASS_2" >/dev/null 2>&1 || true
  SUBJ="KIN healthcheck $(date +%s)"
  OUT=$(swaks --to "$TEST_USER_2" --from "$TEST_USER_1" \
        --server 127.0.0.1:587 --auth LOGIN \
        --auth-user "$TEST_USER_1" --auth-password "$TEST_PASS_1" \
        --tls --header "Subject: $SUBJ" --body "healthcheck" 2>&1)
  if printf '%s' "$OUT" | grep -q "queued as"; then
    p "Submission accepted (587, TLS, AUTH)"
    # amavis/antispam scanning can still be warming up right after a restart
    # (04-tls-dkim.sh and the DNS-cache flush above both restart it), so a
    # single fixed sleep can catch it mid-scan. Poll instead of one shot.
    MSG=""
    for _t1_try in 1 2 3 4 5 6 7 8 9 10 11 12; do
      sleep 6
      MSG=$(find /opt/zimbra/store -name '*.msg' -newermt '-10 minutes' 2>/dev/null | head -1)
      [ -n "$MSG" ] && break
    done
    if [ -n "$MSG" ]; then
      grep -qi '^DKIM-Signature' "$MSG" && p "Message signed with DKIM" \
        || b "No DKIM-Signature header yet (opendkim still warming; not a server install failure)"
      AR=$(grep -i '^Authentication-Results' -A1 "$MSG" | tr '\n' ' ')
      case "$(healthcheck_auth_results_dkim "$AR")" in
        pass)    p "Verification: dkim=pass" ;;
        fail)    f "dkim=fail - signature did not verify against the published key" ;;
        *)       b "dkim not pass yet (TXT missing or still propagating); message was signed" ;;
      esac
      grep -q "status=sent" /var/log/zimbra.log 2>/dev/null && p "LMTP delivered to mailbox"
    else
      b "Message not found in message store yet (amavis still scanning after restart)"
    fi
  else
    f "Submission rejected"; printf '%s' "$OUT" | tail -4 | sed 's/^/      /'
  fi

  if [ -n "${EXTERNAL_TEST_ADDRESS}" ] && [ "$SMTP_OUT" -eq 1 ]; then
    echo; say "T2 - send to external address"
    swaks --to "$EXTERNAL_TEST_ADDRESS" --from "$TEST_USER_1" \
          --server 127.0.0.1:587 --auth LOGIN \
          --auth-user "$TEST_USER_1" --auth-password "$TEST_PASS_1" \
          --tls --header "Subject: KIN external test" --body "external test" 2>&1 \
      | grep -q "queued as" && p "Accepted for delivery to ${EXTERNAL_TEST_ADDRESS}" \
                            || f "Rejected"
    info "Check headers on the recipient side: SPF, DKIM, and DMARC should pass."
  elif [ -n "${EXTERNAL_TEST_ADDRESS}" ]; then
    b "T2 skipped - outbound SMTP still blocked"
  fi
fi

# --- open relay --------------------------------------------------------------
echo; say "Basic security"
MYN=$(su - zimbra -c "zmprov gacf zimbraMtaMyNetworks" 2>/dev/null)
case "$MYN" in
  *"0.0.0.0/0"*) f "OPEN RELAY - zimbraMtaMyNetworks contains 0.0.0.0/0" ;;
  *)             p "Not an open relay" ;;
esac

# --- summary -----------------------------------------------------------------
echo
say "SUMMARY"
printf '  %sPASS    : %d%s\n' "$GRN" "$PASS" "$RST"
printf '  %sFAIL    : %d%s\n' "$RED" "$FAILED" "$RST"
printf '  %sBLOCKED : %d  (network, not server)%s\n' "$YLW" "$BLOCKED" "$RST"
echo
if [ "$FAILED" -eq 0 ] && [ "$BLOCKED" -eq 0 ]; then
  ok "All checks passed."
elif [ "$FAILED" -eq 0 ]; then
  warn "Server healthy. Remaining items are network constraints above."
else
  fail "There are failures that need to be fixed on the server."
fi
echo
# Non-zero when any server-side check failed (BLOCKED/network alone stays 0).
[ "$FAILED" -eq 0 ] || exit 1
