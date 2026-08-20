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
if printf '%s' "$SUB" | grep -qi "Let's Encrypt"; then
  p "Trusted certificate installed"
  printf '%s\n' "$SUB" | sed 's/^/      /'
  EXP=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 -servername "$MAIL_HOST" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  DAYS=$(( ( $(date -d "$EXP" +%s) - $(date +%s) ) / 86400 ))
  [ "$DAYS" -gt 20 ] && p "Valid for ${DAYS} days" || f "Only ${DAYS} days left - check renewal"
else
  f "No trusted certificate yet (still self-signed)"
fi
[ -x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh ] \
  && p "Deploy hook installed" || f "Deploy hook missing - TLS will break in 90 days"

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
  f "Zimbra not healthy after DNS-cache flush restarts"
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
[ -n "$MX" ] && p "MX     : $MX" || f "MX not published yet"

SPF=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" TXT 2>/dev/null | grep -i spf1 | tr -d '"')
[ -n "$SPF" ] && p "SPF    : $SPF" || f "SPF not published yet"

DM=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "_dmarc.${MAIL_DOMAIN}" TXT 2>/dev/null | tr -d '"')
[ -n "$DM" ] && p "DMARC  : $DM" || f "DMARC not published yet"

A=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_HOST" A 2>/dev/null | tr '\n' ' ')
[ -n "$A" ] && p "A      : $A" || b "A record not published yet - need public IP from the network team"

# --- DKIM --------------------------------------------------------------------
echo; say "DKIM"
SEL=$(su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" 2>/dev/null \
      | awk '/DKIM Selector/{getline; while($0==""){getline}; print; exit}')
if [ -n "$SEL" ]; then
  p "Selector: ${SEL}"
  TK=$(ls /opt/zimbra/common/sbin/opendkim-testkey /usr/sbin/opendkim-testkey 2>/dev/null | head -1)
  if [ -n "$TK" ]; then
    # -vvv is required. Without it the tool prints nothing at all on success
    # and signals the result only through its exit status, so grepping the
    # output reports a healthy key as broken.
    TKOUT=$(su - zimbra -c "$TK -d ${MAIL_DOMAIN} -s ${SEL} -vvv" 2>&1)
    if printf '%s' "$TKOUT" | grep -qi "key OK"; then
      p "Private key matches published record"
    else
      f "opendkim-testkey failed - record missing or does not match"
      printf '%s\n' "$TKOUT" | tail -3 | sed 's/^/      /'
    fi
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
    f "Cannot determine sending address"
  elif [ "$SEND_IP" = "$S2" ] && [ "$SEND_IP" = "$S3" ]; then
    p "Consistent: ${SEND_IP}"
  else
    f "NOT consistent: ${SEND_IP} / ${S2} / ${S3}"
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
  if [ "$A_REC" = "$SEND_IP" ]; then
    p "A ${MAIL_HOST} = ${A_REC} = sending address"
  elif [ -z "$A_REC" ]; then
    # Same external dependency as the "A record not published yet" check
    # above - do not fail the server for a record it does not control yet.
    b "A ${MAIL_HOST} not published yet - need public IP from the network team"
  else
    f "A ${MAIL_HOST} = ${A_REC}, but sending from ${SEND_IP}"
  fi

  PTR=$(dig +short +time=5 -x "$SEND_IP" 2>/dev/null | head -1)
  if [ -z "$PTR" ]; then
    # Reverse DNS belongs to the owner of the IP block, so this is an external
    # dependency rather than a server fault. Counting it as a failure would
    # make the summary blame a host that is configured correctly.
    b "PTR ${SEND_IP} not set yet - request from the IP block owner (ISP), not in Cloudflare"
  elif [ "$PTR" = "${MAIL_HOST}." ]; then
    p "PTR ${SEND_IP} -> ${PTR}"
    BACK=$(dig +short +time=5 @"$DNS_UPSTREAM_1" A "${PTR%.}" 2>/dev/null | head -1)
    [ "$BACK" = "$SEND_IP" ] \
      && p "Chain complete and matching" \
      || f "Chain broken: ${PTR%.} points to ${BACK:-empty}, not ${SEND_IP}"
  else
    b "PTR ${SEND_IP} -> ${PTR} (not ${MAIL_HOST}, OK if its A record returns to this IP)"
  fi

  case "$SPF" in
    *"ip4:${SEND_IP}"*) p "SPF explicitly lists the sending address" ;;
    *mx*) [ "$A_REC" = "$SEND_IP" ] \
            && p "SPF 'mx' covers the sending address" \
            || f "SPF 'mx' does NOT cover ${SEND_IP} - add ip4:${SEND_IP}" ;;
    *) b "SPF does not clearly cover ${SEND_IP}" ;;
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
    *)       f "AD_AUTH_ENABLED=yes but zimbraAuthMech=${MECH:-empty} — run 06-hybrid-auth.sh" ;;
  esac
  case "$FALLBACK" in
    TRUE|true|1) p "zimbraAuthFallbackToLocal active" ;;
    *)           f "zimbraAuthFallbackToLocal not TRUE — local accounts on hybrid domain will fail login" ;;
  esac
  if [ -n "${AD_TEST_USER}" ] && [ -n "${AD_TEST_PASS}" ]; then
    if zimbra_user_auth_ok "$AD_TEST_USER" "$AD_TEST_PASS"; then
      p "AD LDAP auth: ${AD_TEST_USER}"
    else
      f "AD LDAP auth failed: ${AD_TEST_USER}"
    fi
  else
    b "AD enabled in config but AD_TEST_USER/PASS empty — cannot test AD path"
  fi
else
  info "AD_AUTH_ENABLED is not yes — AD path not tested (local only, skippable by design)"
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
    for _t1_try in 1 2 3 4 5 6; do
      sleep 6
      MSG=$(find /opt/zimbra/store -name '*.msg' -newermt '-2 minutes' 2>/dev/null | head -1)
      [ -n "$MSG" ] && break
    done
    if [ -n "$MSG" ]; then
      grep -qi '^DKIM-Signature' "$MSG" && p "Message signed with DKIM" || f "No DKIM-Signature header"
      AR=$(grep -i '^Authentication-Results' -A1 "$MSG" | tr '\n' ' ')
      case "$AR" in
        *"dkim=pass"*)    p "Verification: dkim=pass" ;;
        *"dkim=neutral"*) f "dkim=neutral - record not yet visible to verifier" ;;
        *)                b "Verification result unreadable" ;;
      esac
      grep -q "status=sent" /var/log/zimbra.log 2>/dev/null && p "LMTP delivered to mailbox"
    else
      f "Message not found in message store"
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
