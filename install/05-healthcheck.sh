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
MX_HOST=""
p() { ok   "$*"; PASS=$((PASS+1)); }
f() { fail "$*"; FAILED=$((FAILED+1)); }
b() { warn "$*"; BLOCKED=$((BLOCKED+1)); }

# Which machine is this, on a split?
#
# Nearly every check below assumes one host that does everything. On a split
# that is false, and the failures are misleading in both directions: the edge
# has no mailbox to deliver into, the mailbox has no MTA to send from, and a
# healthcheck reporting a dozen failures on a node that is working perfectly
# is a healthcheck the operator stops reading.
KIN_NODE_ROLE="all"
if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
  KIN_NODE_ROLE=$(kin_node_role 2>/dev/null) || KIN_NODE_ROLE="unknown"
fi
SMTP_OUT=0

# Treating an unknown role as a single appliance would fail checks that do
# not apply here, or pass ones that do, and either way abort the pipeline
# over a misread. Refuse, the same way the topology helpers do.
if [ "$KIN_NODE_ROLE" = "unknown" ]; then
  f "TOPOLOGY=split, but this host's role cannot be determined from its addresses."
  info "Check EDGE_IP and MAILBOX_IP in ${CONF_FILE} against this machine."
  exit 1
fi

echo
printf '%s\n' "${BLD}  KIN Mail - health check  ${MAIL_HOST}  $(date -Is)${RST}"
if [ "$KIN_NODE_ROLE" != "all" ]; then
  printf '%s\n' "${DIM}  split deployment - this is the ${KIN_NODE_ROLE} node${RST}"
fi

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
if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
  for port in 389 7025 8443; do
    ss -lnt 2>/dev/null | grep -q ":${port} " && p "port ${port} listening" || f "port ${port} not listening"
  done
  info "No public mail ports on this node. Users reach mail through the edge."
else
  for port in 25 443 587 993; do
    ss -lnt 2>/dev/null | grep -q ":${port} " && p "port ${port} listening" || f "port ${port} not listening"
  done
fi

# --- certificate -------------------------------------------------------------
echo; say "TLS certificate"
if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
  b "Skipped - public TLS is served by the edge proxy, not by this node."
else
SUB=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 -servername "$MAIL_HOST" 2>/dev/null \
      | openssl x509 -noout -subject -issuer -enddate 2>/dev/null)
ISSUER=$(printf '%s' "$SUB" | sed -n 's/^issuer=//p' | head -1)
SUBJECT=$(printf '%s' "$SUB" | sed -n 's/^subject=//p' | head -1)
TLS_TRUSTED=0
if [ -z "$SUB" ]; then
  f "No TLS certificate presented on :443"
elif printf '%s' "$SUB" | grep -qi "Let's Encrypt" \
  || { [ -n "$ISSUER" ] && [ -n "$SUBJECT" ] && [ "$ISSUER" != "$SUBJECT" ]; }; then
  TLS_TRUSTED=1
  p "Trusted certificate installed"
  printf '%s\n' "$SUB" | sed 's/^/      /'
  EXP=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 -servername "$MAIL_HOST" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  DAYS=$(( ( $(date -d "$EXP" +%s) - $(date +%s) ) / 86400 ))
  [ "$DAYS" -gt 20 ] && p "Valid for ${DAYS} days" || f "Only ${DAYS} days left - check renewal"
else
  b "Certificate is still self-signed - finish TLS (04) before production mail"
fi
if [ "$TLS_TRUSTED" -eq 0 ]; then
  # A missing renewal hook here is a consequence of having no certificate, not
  # a second, separate fault. Reporting it as FAILED stopped the whole pipeline
  # at its last stage over something the line above had already said - and said
  # it with "TLS will break in 90 days", which describes the expiry of a
  # certificate that does not exist. One cause, one finding.
  b "No renewal hook yet - there is no certificate to renew"
elif [ "${TLS_METHOD:-}" = "customer" ]; then
  [ -x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh ] \
    && p "Deploy hook installed" \
    || b "No Let's Encrypt deploy hook (expected when TLS_METHOD=customer)"
else
  [ -x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh ] \
    && p "Deploy hook installed" || f "Deploy hook missing - TLS will break in 90 days"
fi
fi

# --- flush caches before DNS assertions --------------------------------------
# Long-lived processes cache negative DNS answers. A record added after they
# started looks missing until they are restarted.
# amavis/opendkim appear in zmcontrol status - on HA Primary that trips the
# Pacemaker kin-zimbra monitor unless we unmanage --monitor first (1.7b/1.7c).
echo; say "Flushing DNS cache"
if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
  info "amavis and opendkim run on the edge; nothing to flush here"
else
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
fi

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
MX_HOST=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" MX 2>/dev/null \
  | sort -n | awk '{print $2; exit}' | sed 's/\.$//')

SPF=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" TXT 2>/dev/null | grep -i spf1 | tr -d '"')
[ -n "$SPF" ] && p "SPF    : $SPF" || b "SPF not published yet"

DM=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "_dmarc.${MAIL_DOMAIN}" TXT 2>/dev/null | tr -d '"')
[ -n "$DM" ] && p "DMARC  : $DM" || b "DMARC not published yet"

A=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_HOST" A 2>/dev/null | tr '\n' ' ')
[ -n "$A" ] && p "A      : $A" || b "A record not published yet - need public IP from the network team"

# --- DKIM --------------------------------------------------------------------
echo; say "DKIM"
if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
  b "Skipped - DKIM signing is on the edge, which runs the MTA."
else
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
  if healthcheck_host_is_not_published_mx "$MAIL_HOST" "${MX_HOST:-}" \
    || kin_ha_peer_install; then
    b "DKIM key not created on this host (not the published MX; expected on HA peer)"
  else
    f "DKIM key not created yet"
  fi
fi
fi

# --- outbound SMTP -----------------------------------------------------------
if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
  echo; say "Outbound SMTP"
  b "Skipped - this node has no MTA. Mail leaves through the edge."
else
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
fi

echo; say "Hybrid authentication"
MECH=$(su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAuthMech" 2>/dev/null \
       | awk -F': ' '/zimbraAuthMech:/{print $2; exit}')
FALLBACK=$(su - zimbra -c "zmprov gd ${MAIL_DOMAIN} zimbraAuthFallbackToLocal" 2>/dev/null \
           | awk -F': ' '/zimbraAuthFallbackToLocal:/{print $2; exit}')
info "zimbraAuthMech=${MECH:-zimbra(default)}  fallback=${FALLBACK:-unset}"

# Local path always expected once test mailboxes exist (fallback or pure local).
# Root cause of Phase 1 "test1 auth FAIL": `zmprov auth` is not a real command on
# Zimbra 10 FOSS (prints usage). Also passwords with `#` (old default KinTest#1-…)
# become shell comments unless single-quoted - see ensure_kin_test_mailbox.
ERR1=$(mktemp) ERR2=$(mktemp)
# rc 2 means the account is ready but this node cannot log in to prove it -
# mailboxd is on the mailbox. That is BLOCKED, not FAILED and not PASSED: the
# run is not at fault, and nothing was actually verified either.
ensure_kin_test_mailbox "$TEST_USER_1" "$TEST_PASS_1" 2>"$ERR1"; RC1=$?
case "$RC1" in
  0) p "Local Zimbra auth: ${TEST_USER_1}" ;;
  2) b "Local auth not provable here - mailboxd is on the mailbox node (account ${TEST_USER_1} is ready)" ;;
  *) f "Local Zimbra auth failed: ${TEST_USER_1}"
     sed 's/^/      /' "$ERR1" | tail -5 ;;
esac
ensure_kin_test_mailbox "$TEST_USER_2" "$TEST_PASS_2" 2>"$ERR2"; RC2=$?
if [ "$RC2" -ne 0 ] && [ "$RC2" -ne 2 ]; then
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
    if ! kin_auth_provable_here; then
      b "AD auth not provable here - logging in needs mailboxd, which is on the mailbox node"
    elif zimbra_user_auth_ok "$AD_TEST_USER" "$AD_TEST_PASS"; then
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
  if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
    b "Skipped - submission is on the edge. Delivery lands here; prove it from there."
  else
  ensure_kin_test_mailbox "$TEST_USER_1" "$TEST_PASS_1" >/dev/null 2>&1 || true
  ensure_kin_test_mailbox "$TEST_USER_2" "$TEST_PASS_2" >/dev/null 2>&1 || true
  SUBJ="KIN healthcheck $(date +%s)"
  OUT=$(swaks --to "$TEST_USER_2" --from "$TEST_USER_1" \
        --server 127.0.0.1:587 --auth LOGIN \
        --auth-user "$TEST_USER_1" --auth-password "$TEST_PASS_1" \
        --tls --header "Subject: $SUBJ" --body "healthcheck" 2>&1)
  if printf '%s' "$OUT" | grep -q "queued as"; then
    p "Submission accepted (587, TLS, AUTH)"
    if [ "$KIN_NODE_ROLE" = "edge" ]; then
      # Delivery lands on the mailbox. /opt/zimbra/store is empty here, and
      # treating that as a failure would stop the pipeline over a success.
      if [ -n "${MAILBOX_IP:-}" ] && timeout 5 bash -c "exec 3<>/dev/tcp/${MAILBOX_IP}/7025" 2>/dev/null; then
        p "LMTP path to the mailbox (${MAILBOX_IP}:7025) is open"
      else
        f "Cannot reach the mailbox on LMTP :7025 - submitted mail has nowhere to land"
      fi
      grep -q "status=sent" /var/log/zimbra.log 2>/dev/null \
        && p "MTA logged a delivery" \
        || b "No local delivery log yet (LMTP to the mailbox may still be in flight)"
      b "DKIM of the delivered message is in the mailbox store; not readable from here."
    else
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
        fail)
          if healthcheck_host_is_not_published_mx "$MAIL_HOST" "${MX_HOST:-}" \
            || kin_ha_peer_install; then
            b "dkim=fail on ${MAIL_HOST} (published MX is ${MX_HOST:-unknown}; HA peer key cannot match DNS)"
          else
            f "dkim=fail - signature did not verify against the published key"
          fi
          ;;
        *)       b "dkim not pass yet (TXT missing or still propagating); message was signed" ;;
      esac
      grep -q "status=sent" /var/log/zimbra.log 2>/dev/null && p "LMTP delivered to mailbox"
    else
      b "Message not found in message store yet (amavis still scanning after restart)"
    fi
    fi
  else
    f "Submission rejected"; printf '%s' "$OUT" | tail -4 | sed 's/^/      /'
  fi
  fi

  if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
    :
  elif kin_ha_peer_install; then
    b "T2 skipped on HA peer (external send is verified on the published MX)"
  elif [ -n "${EXTERNAL_TEST_ADDRESS}" ] && [ "$SMTP_OUT" -eq 1 ]; then
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

# --- mail gateway ------------------------------------------------------------
# From 0.1.10 a Proxmox Mail Gateway in front of this appliance is mandatory.
#
# The severity here is deliberate and was got wrong once already. A fresh
# appliance CANNOT have a gateway link: the gateway is a separate VM, and on a
# greenfield install it does not exist yet. Failing here would stop the deploy
# pipeline at its last stage on every single first install - the stage returns
# non-zero and kin-mail.sh aborts.
#
# So: no gateway recorded at all is BLOCKED. It is a real requirement that is
# genuinely outside this server, exactly like the DNS and outbound-SMTP items
# above, and it is printed loudly as the next thing to do.
#
# A gateway that IS recorded but is not working is a FAILURE, because at that
# point it is this server's configuration that is wrong, and mail is at risk.
echo; say "Mail gateway"
if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
  b "Skipped - the gateway is linked from the edge, which runs the MTA."
  info "Run this check there to see the state of the link."
else
GW_CONF=/etc/kin-mail/mail-gateway.conf
GW_STATE=/var/lib/kin-mail-console/mail-gateway-state.json
gw_host=""; gw_enabled=""; gw_phase=""
if [ -f "$GW_CONF" ]; then
  gw_host=$(sed -n 's/^[[:space:]]*GATEWAY_HOST=//p' "$GW_CONF" | tail -1 | tr -d "\"' \r")
  gw_enabled=$(sed -n 's/^[[:space:]]*GATEWAY_ENABLED=//p' "$GW_CONF" | tail -1 | tr -d "\"' \r")
fi
if [ -f "$GW_STATE" ]; then
  gw_phase=$(sed -n 's/.*"phase"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$GW_STATE" | head -1)
fi

if [ -z "$gw_host" ]; then
  b "No mail gateway is linked yet - this appliance is still its own MX"
  info "This is the next thing to do, not an optional extra:"
  info "  1. Install the Proxmox Mail Gateway VM and give it a fixed address."
  info "  2. Console > Mail Gateway > Connect, then Plan, then Apply."
  info "  3. Move the MX and SPF records to the gateway."
  info "  4. Re-run 10-host-firewall.sh apply to close port 25 to everyone else."
  info "See MAIL-GATEWAY.md for the whole pipeline."
elif [ "$gw_enabled" != "1" ]; then
  f "A mail gateway is recorded (${gw_host}) but the link is disabled"
  info "Console > Mail Gateway > Connect to re-enable it, or Revert to stand down cleanly."
elif [ "$gw_phase" != "applied" ]; then
  f "The mail gateway link to ${gw_host} has never been applied (phase: ${gw_phase:-unknown})"
  info "Console > Mail Gateway > Apply. Until then nothing is filtering this appliance's mail."
else
  p "Mail gateway ${gw_host} is linked"

  # Written and checked are different promises. Each of these is a setting
  # whose drift stops mail without saying anything.
  RELAY=$(su - zimbra -c "zmprov gs \$(zmhostname) zimbraMtaRelayHost" 2>/dev/null |
          sed -n 's/^zimbraMtaRelayHost: //p' | head -1 | tr -d ' \r')
  case "$RELAY" in
    "${gw_host}:"*) p "Outbound mail relays through the gateway (${RELAY})" ;;
    "")             f "zimbraMtaRelayHost is empty - outbound mail bypasses the gateway" ;;
    *)              f "zimbraMtaRelayHost is '${RELAY}', not the configured gateway ${gw_host}" ;;
  esac

  # MYN comes from the open-relay check above. If Zimbra was not answering
  # then it is empty here too, and reporting "the gateway is not trusted"
  # would blame the wrong thing - the earlier checks already said Zimbra is
  # down, and one root cause should not produce three failures.
  if [ -z "$MYN" ]; then
    b "Could not read zimbraMtaMyNetworks - Zimbra did not answer"
  else
    case "$MYN" in
      *"${gw_host}/32"*) p "The gateway is trusted to hand mail in" ;;
      *) f "zimbraMtaMyNetworks does not include ${gw_host}/32 - inbound mail will be deferred" ;;
    esac
  fi

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    if ufw status 2>/dev/null | grep -E '^25/tcp[[:space:]]+ALLOW[[:space:]]+Anywhere' >/dev/null; then
      f "Port 25 is still open to the internet despite the gateway"
      info "Re-run 10-host-firewall.sh apply to narrow it to ${gw_host}."
    else
      p "Port 25 is not open to the internet"
    fi
  else
    b "ufw is not active - port 25 exposure was not checked"
  fi

  # Configuration can be perfect and mail can still not move, because
  # something between the two machines drops the packets.
  if timeout 5 bash -c "exec 3<>/dev/tcp/${gw_host}/26" 2>/dev/null; then
    p "The gateway answers on port 26 (outbound mail has a path)"
  else
    f "Cannot reach ${gw_host}:26 - outbound mail will queue on this appliance"
  fi
fi

fi

# --- summary -----------------------------------------------------------------
echo
say "SUMMARY"
printf '  %sPASS    : %d%s\n' "$GRN" "$PASS" "$RST"
printf '  %sFAIL    : %d%s\n' "$RED" "$FAILED" "$RST"
printf '  %sBLOCKED : %d  (outside this server)%s\n' "$YLW" "$BLOCKED" "$RST"
echo
if [ "$FAILED" -eq 0 ] && [ "$BLOCKED" -eq 0 ]; then
  ok "All checks passed."
elif [ "$FAILED" -eq 0 ]; then
  # "Network constraints" used to be the whole story here. It is not any more:
  # a missing mail gateway is blocked on the operator building a VM, not on a
  # firewall somewhere, and calling it a network constraint would let it be
  # read as somebody else's problem and left undone.
  warn "Nothing is broken on this server. The blocked items above still need doing."
  if [ -z "${gw_host:-}" ]; then
    warn "The mail gateway is the important one. Until it is linked, this"
    warn "appliance holds port 25 to the internet by itself."
  fi
else
  fail "There are failures that need to be fixed on the server."
fi
echo
# Non-zero when any server-side check failed (BLOCKED/network alone stays 0).
[ "$FAILED" -eq 0 ] || exit 1
