#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 04 TLS + DKIM
#
# Issues or installs a TLS certificate according to TLS_METHOD from config:
#   cloudflare — Let's Encrypt DNS-01 via Cloudflare API (auto-renewable)
#   manual     — Let's Encrypt DNS-01 interactive (operator creates TXT)
#   customer   — skip certbot; document zmcertmgr install of customer files
#
# Then generates the DKIM key and prints the record to publish.
#
# DNS-01 (cloudflare/manual) is used when the host sits behind NAT with no
# inbound HTTP path for HTTP-01.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
LE_DIR="/etc/letsencrypt/live/${MAIL_HOST}"
ZS="/opt/zimbra/ssl/letsencrypt"

[ -d /opt/zimbra ] || { fail "Zimbra is not installed yet. Run 03 first."; exit 1; }

: "${TLS_METHOD:=cloudflare}"
case "$TLS_METHOD" in
  cloudflare|manual|customer) ;;
  *) fail "Unknown TLS_METHOD: ${TLS_METHOD} (cloudflare|manual|customer)"; exit 1 ;;
esac

say "TLS method: ${TLS_METHOD}"

install_isrg_root() {
  mkdir -p "$ZS" /etc/letsencrypt/renewal-hooks/deploy
  curl -s -m 30 -o "${ZS}/isrgrootx1.pem" https://letsencrypt.org/certs/isrgrootx1.pem
  [ -s "${ZS}/isrgrootx1.pem" ] || { fail "Failed to download ISRG Root X1"; exit 1; }
}

write_deploy_hook() {
  # Zimbra will not verify a chain that does not reach the root, so ISRG Root X1
  # is appended. The hook exists because certbot renewing the file while Zimbra
  # keeps serving the old certificate is a silent outage 90 days later.
  #
  # zmcertmgr deploycrt + zmcontrol restart stop services briefly. On HA Primary,
  # Pacemaker must unmanage kin-zimbra first (same idea as 09-hardening /
  # 11-admin-path-lockdown) or the monitor races into FAILED and drops the VIP.
  cat > /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh <<'HOOK'
#!/bin/bash
# KIN Mail - redeploy the renewed certificate into Zimbra.
# Pacemaker-aware: unmanage kin-zimbra around deploy/restart when HA-managed.
set -eu

LE="/etc/letsencrypt/live/MAIL_HOST_PLACEHOLDER"
ZS="/opt/zimbra/ssl/letsencrypt"

[ "${RENEWED_LINEAGE:-$LE}" = "$LE" ] || exit 0

pcs_has_kin_zimbra() {
  command -v pcs >/dev/null 2>&1 || return 1
  # pcs 0.11 replaced `resource status <id>`; prefer config, fall back to status.
  pcs resource config kin-zimbra >/dev/null 2>&1 \
    || pcs resource status kin-zimbra >/dev/null 2>&1
}

PCS_UNMANAGED=0
remanage_kin_zimbra() {
  if [ "$PCS_UNMANAGED" = "1" ]; then
    # manage alone leaves monitors enabled=0 after unmanage --monitor
    pcs resource manage kin-zimbra --monitor 2>/dev/null \
      || pcs resource manage kin-zimbra 2>/dev/null \
      || true
    PCS_UNMANAGED=0
    logger -t kin-mail "kin-zimbra re-managed (--monitor) after cert deploy"
  fi
}
trap remanage_kin_zimbra EXIT

cp "$LE/privkey.pem" "$LE/cert.pem" "$LE/chain.pem" "$LE/fullchain.pem" "$ZS/"
cat "$ZS/isrgrootx1.pem" >> "$ZS/chain.pem"
chown -R zimbra:zimbra "$ZS"
chmod 640 "$ZS"/*

su - zimbra -c "/opt/zimbra/bin/zmcertmgr verifycrt comm $ZS/privkey.pem $ZS/cert.pem $ZS/chain.pem"

cp "$ZS/privkey.pem" /opt/zimbra/ssl/zimbra/commercial/commercial.key
chown zimbra:zimbra /opt/zimbra/ssl/zimbra/commercial/commercial.key
chmod 640 /opt/zimbra/ssl/zimbra/commercial/commercial.key

if pcs_has_kin_zimbra; then
  logger -t kin-mail "kin-zimbra is Pacemaker-managed — unmanage --monitor before cert deploy/restart"
  # Without --monitor, the 30s OCF probe still fires during zmcontrol restart,
  # marks FAILED, and Pacemaker stops later group members (kin-vip). Proxy-only
  # restarts in 09/11 are short enough to often dodge that window; full restart is not.
  pcs resource unmanage kin-zimbra --monitor
  PCS_UNMANAGED=1
fi

su - zimbra -c "/opt/zimbra/bin/zmcertmgr deploycrt comm $ZS/cert.pem $ZS/chain.pem"
su - zimbra -c "zmcontrol restart"

# Wait for mailboxd/proxy to come back (restart can take minutes).
ok_https=0
i=0
while [ "$i" -lt 90 ]; do
  code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 https://127.0.0.1/ || true)
  if [ "$code" = "200" ]; then
    ok_https=1
    break
  fi
  i=$((i + 1))
  sleep 2
done
[ "$ok_https" = "1" ] || {
  echo "kin-mail deploy hook: https://127.0.0.1/ not 200 after restart" >&2
  exit 1
}

# All zmcontrol services must report Running (exclude blank/Host lines).
status_out=$(su - zimbra -c "zmcontrol status" 2>/dev/null || true)
if printf '%s\n' "$status_out" | grep -vE '^Host|^$' | grep -qv Running; then
  echo "kin-mail deploy hook: zmcontrol status not all Running:" >&2
  printf '%s\n' "$status_out" >&2
  exit 1
fi

remanage_kin_zimbra
trap - EXIT

logger -t kin-mail "Zimbra certificate redeployed after Let's Encrypt renewal"
HOOK
  # Bake MAIL_HOST into the installed hook (quoted heredoc above keeps runtime logic intact).
  sed -i "s|MAIL_HOST_PLACEHOLDER|${MAIL_HOST}|g" \
    /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh
  ok "Deploy hook installed (Pacemaker-aware)"
}

run_deploy_hook() {
  say "Deploy certificate to Zimbra (hook)"
  if /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh; then
    ok "Hook succeeded, certificate deployed"
  else
    fail "Hook failed - run manually to see its message"
    exit 1
  fi
}

verify_tls_ports() {
  say "Verify TLS on live ports"
  local spec S
  for spec in "443 https" "587 starttls-smtp" "993 imaps"; do
    # shellcheck disable=SC2086
    set -- $spec
    if [ "$2" = "starttls-smtp" ]; then
      S=$(echo | timeout 20 openssl s_client -connect 127.0.0.1:$1 -starttls smtp 2>/dev/null \
          | openssl x509 -noout -subject 2>/dev/null)
    else
      S=$(echo | timeout 20 openssl s_client -connect 127.0.0.1:$1 -servername "$MAIL_HOST" 2>/dev/null \
          | openssl x509 -noout -subject 2>/dev/null)
    fi
    [ -n "$S" ] && ok "port $1 : $S" || fail "port $1 : handshake failed"
  done
}

# --- Cloudflare DNS-01 -------------------------------------------------------
tls_cloudflare() {
  say "1. certbot and Cloudflare plugin"
  apt-get -qq update
  apt-get -y install certbot python3-certbot-dns-cloudflare >/dev/null 2>&1
  certbot plugins 2>/dev/null | grep -qi cloudflare \
    && ok "dns-cloudflare plugin installed" \
    || { fail "dns-cloudflare plugin not available"; exit 1; }

  say "2. Cloudflare API token"
  if [ -s "$CF_CREDS" ] && ! grep -q "PASTE" "$CF_CREDS"; then
    ok "Credentials already at ${CF_CREDS}"
  else
    info "Create at Cloudflare: My Profile -> API Tokens -> Create Token"
    info "Template 'Edit zone DNS', restrict to zone ${MAIL_DOMAIN} only."
    ask_secret CF_TOKEN "Paste Cloudflare API token"
    [ -z "$CF_TOKEN" ] && { fail "Token is empty."; exit 1; }
    mkdir -p /etc/letsencrypt
    printf 'dns_cloudflare_api_token = %s\n' "$CF_TOKEN" > "$CF_CREDS"
    chmod 600 "$CF_CREDS"; chown root:root "$CF_CREDS"
    ok "Saved with mode 600"
  fi

  say "3. Issuing certificate (DNS-01 Cloudflare)"
  if [ -f "${LE_DIR}/cert.pem" ]; then
    ok "Certificate already exists, issuance skipped"
  else
    certbot certonly \
      --dns-cloudflare \
      --dns-cloudflare-credentials "$CF_CREDS" \
      --dns-cloudflare-propagation-seconds "$CF_PROPAGATION" \
      -d "$MAIL_HOST" \
      --preferred-chain "ISRG Root X1" \
      --agree-tos --no-eff-email -m "$LE_EMAIL" \
      --non-interactive 2>&1 | tail -8
    [ -f "${LE_DIR}/cert.pem" ] || { fail "Issuance failed. See /var/log/letsencrypt/"; exit 1; }
  fi
  openssl x509 -in "${LE_DIR}/cert.pem" -noout -subject -dates | sed 's/^/    /'

  say "4. Installing deploy hook"
  install_isrg_root
  write_deploy_hook
  run_deploy_hook
  verify_tls_ports

  say "5. Test automatic renewal"
  certbot renew --dry-run 2>&1 | grep -qi "simulated renewal" \
    && ok "certbot renew --dry-run succeeded" \
    || warn "dry-run inconclusive, check /var/log/letsencrypt/"
}

# --- Manual DNS-01 (any provider) --------------------------------------------
tls_manual() {
  warn "TLS_METHOD=manual — renewal is NOT automatic."
  warn "certbot --manual requires a human at the terminal for each issue/renew,"
  warn "unless a provider-specific --manual-auth-hook is added later."
  info "Do not rely on cron 'certbot renew' for this mode."

  say "1. certbot"
  apt-get -qq update
  apt-get -y install certbot >/dev/null 2>&1
  command -v certbot >/dev/null || { fail "certbot is not installed"; exit 1; }
  ok "certbot ready"

  say "2. Issuing certificate (DNS-01 manual)"
  if [ -f "${LE_DIR}/cert.pem" ]; then
    ok "Certificate already exists, issuance skipped"
  else
    # Interactive TTY: classic certbot prompts.
    # Non-TTY / KIN_MAIL_DNS_POLL=1: print TXT + poll public DNS until the
    # operator creates the record (needed for remote/bootstrap deploys).
    if [ -t 0 ] && [ "${KIN_MAIL_DNS_POLL:-0}" != "1" ]; then
      info "Interactive mode: create TXT in the DNS panel when prompted, then press Enter."
      certbot certonly \
        --manual \
        --preferred-challenges dns \
        -d "$MAIL_HOST" \
        --preferred-chain "ISRG Root X1" \
        --agree-tos --no-eff-email -m "$LE_EMAIL" \
        --manual-public-ip-logging-ok
    else
      info "Poll mode: challenge written to /tmp/kin-mail-acme-challenge.txt"
      info "Create TXT in the DNS panel for zone ${MAIL_DOMAIN}, then wait for propagation."
      AUTH_HOOK=$(mktemp /tmp/kin-mail-acme-auth.XXXXXX)
      CLEAN_HOOK=$(mktemp /tmp/kin-mail-acme-clean.XXXXXX)
      chmod 700 "$AUTH_HOOK" "$CLEAN_HOOK"
      cat > "$AUTH_HOOK" <<'HOOK'
#!/bin/bash
set -u
printf '%s\n' "=== KIN Mail ACME DNS-01 challenge ===" | tee /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Host/Name : _acme-challenge.${CERTBOT_DOMAIN}" | tee -a /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Type      : TXT" | tee -a /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Value     : ${CERTBOT_VALIDATION}" | tee -a /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Waiting up to 20 minutes for public DNS (1.1.1.1)…" | tee -a /tmp/kin-mail-acme-challenge.txt
for _i in $(seq 1 120); do
  if dig +short TXT "_acme-challenge.${CERTBOT_DOMAIN}" @1.1.1.1 2>/dev/null | grep -F "\"${CERTBOT_VALIDATION}\"" >/dev/null; then
    echo "TXT visible on 1.1.1.1" | tee -a /tmp/kin-mail-acme-challenge.txt
    exit 0
  fi
  sleep 10
done
echo "TIMEOUT waiting for TXT _acme-challenge.${CERTBOT_DOMAIN}" | tee -a /tmp/kin-mail-acme-challenge.txt
exit 1
HOOK
      cat > "$CLEAN_HOOK" <<'HOOK'
#!/bin/bash
exit 0
HOOK
      certbot certonly \
        --manual \
        --preferred-challenges dns \
        --manual-auth-hook "$AUTH_HOOK" \
        --manual-cleanup-hook "$CLEAN_HOOK" \
        -d "$MAIL_HOST" \
        --preferred-chain "ISRG Root X1" \
        --agree-tos --no-eff-email -m "$LE_EMAIL" \
        --manual-public-ip-logging-ok \
        --non-interactive
      rm -f "$AUTH_HOOK" "$CLEAN_HOOK"
    fi
    [ -f "${LE_DIR}/cert.pem" ] || { fail "Issuance failed. See /var/log/letsencrypt/ and /tmp/kin-mail-acme-challenge.txt"; exit 1; }
  fi
  openssl x509 -in "${LE_DIR}/cert.pem" -noout -subject -dates | sed 's/^/    /'

  say "3. Installing deploy hook (for current deploy; not auto-renew)"
  install_isrg_root
  write_deploy_hook
  run_deploy_hook
  verify_tls_ports

  say "4. Automatic renewal"
  warn "Skipped / not relied upon for TLS_METHOD=manual."
  info "Before expiry: re-run 04 (or certbot certonly --manual …) with an operator present."
}

# --- Customer-provided certificate -------------------------------------------
tls_customer() {
  say "1. Customer certificate — certbot skipped"
  warn "No automated issuance. Operator installs files from the customer."
  echo
  printf '%s\n' "${BLD}  MANUAL STEPS (zmcertmgr)${RST}"
  echo   "  ----------------------------------------------------------------"
  info "1. Save files from the customer, for example:"
  info "     /root/customer-tls/${MAIL_HOST}.crt   (certificate + intermediate if needed)"
  info "     /root/customer-tls/${MAIL_HOST}.key   (private key)"
  info "     /root/customer-tls/ca-chain.pem       (CA chain to root, if separate)"
  info "2. Combine chain if needed, then verify:"
  info "     mkdir -p ${ZS} && cp … ${ZS}/"
  info "     su - zimbra -c '/opt/zimbra/bin/zmcertmgr verifycrt comm ${ZS}/privkey.pem ${ZS}/cert.pem ${ZS}/chain.pem'"
  info "3. Deploy:"
  info "     cp ${ZS}/privkey.pem /opt/zimbra/ssl/zimbra/commercial/commercial.key"
  info "     chown zimbra:zimbra /opt/zimbra/ssl/zimbra/commercial/commercial.key"
  info "     chmod 640 /opt/zimbra/ssl/zimbra/commercial/commercial.key"
  info "     su - zimbra -c '/opt/zimbra/bin/zmcertmgr deploycrt comm ${ZS}/cert.pem ${ZS}/chain.pem'"
  info "     su - zimbra -c 'zmcontrol restart'"
  echo   "  ----------------------------------------------------------------"
  echo

  if [ -f /opt/zimbra/ssl/zimbra/commercial/commercial.crt ] || \
     [ -f /opt/zimbra/ssl/zimbra/server/server.crt ]; then
    ok "Certificate found in Zimbra tree — continuing with port verification (if already deployed)"
    verify_tls_ports || true
  else
    warn "No deployed commercial cert detected — complete the steps above before production."
  fi
}

case "$TLS_METHOD" in
  cloudflare) tls_cloudflare ;;
  manual)     tls_manual ;;
  customer)   tls_customer ;;
esac

# --- DKIM (independent of TLS method) ----------------------------------------
say "DKIM"
if su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" >/dev/null 2>&1; then
  ok "DKIM key already exists"
else
  su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -a -d ${MAIL_DOMAIN}" >/dev/null 2>&1
  ok "DKIM key created"
fi

Q=$(su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" 2>/dev/null)
SEL=$(printf '%s' "$Q" | awk '/DKIM Selector/{getline; while($0==""){getline}; print; exit}')
VAL=$(printf '%s' "$Q" | awk '/DKIM Public signature/,0' | grep -oE '"[^"]*"' | tr -d '"' | tr -d '\n')

echo
printf '%s\n' "${BLD}  PASTE DKIM RECORD IN DNS PANEL (zone ${MAIL_DOMAIN})${RST}"
echo   "  ----------------------------------------------------------------"
echo   "  Type    : TXT"
echo   "  Name    : ${SEL}._domainkey"
echo   "  TTL     : Auto / 300"
echo   "  Content :"
echo   "$VAL" | fold -w 76 | sed 's/^/    /'
echo   "  ----------------------------------------------------------------"
info "One line, without quotes or parentheses. The DNS provider splits it as needed."
info "Paste the PUBLIC key. The private key stays in Zimbra LDAP."
echo
warn "After the record is published, run: ./05-healthcheck.sh (from the install/ folder)"
info "Running processes cache negative DNS results, so 05 will"
info "restart dnsmasq, amavis, and opendkim before verifying DKIM."
echo
