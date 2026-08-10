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

[ -d /opt/zimbra ] || { fail "Zimbra belum terpasang. Jalankan 03 dulu."; exit 1; }

: "${TLS_METHOD:=cloudflare}"
case "$TLS_METHOD" in
  cloudflare|manual|customer) ;;
  *) fail "TLS_METHOD tidak dikenal: ${TLS_METHOD} (cloudflare|manual|customer)"; exit 1 ;;
esac

say "Metode TLS: ${TLS_METHOD}"

install_isrg_root() {
  mkdir -p "$ZS" /etc/letsencrypt/renewal-hooks/deploy
  curl -s -m 30 -o "${ZS}/isrgrootx1.pem" https://letsencrypt.org/certs/isrgrootx1.pem
  [ -s "${ZS}/isrgrootx1.pem" ] || { fail "Gagal mengunduh ISRG Root X1"; exit 1; }
}

write_deploy_hook() {
  # Zimbra will not verify a chain that does not reach the root, so ISRG Root X1
  # is appended. The hook exists because certbot renewing the file while Zimbra
  # keeps serving the old certificate is a silent outage 90 days later.
  cat > /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh <<HOOK
#!/bin/bash
# KIN Mail - redeploy the renewed certificate into Zimbra.
set -e
LE="${LE_DIR}"
ZS="${ZS}"

[ "\${RENEWED_LINEAGE:-\$LE}" = "\$LE" ] || exit 0

cp "\$LE/privkey.pem" "\$LE/cert.pem" "\$LE/chain.pem" "\$LE/fullchain.pem" "\$ZS/"
cat "\$ZS/isrgrootx1.pem" >> "\$ZS/chain.pem"
chown -R zimbra:zimbra "\$ZS"; chmod 640 "\$ZS"/*

su - zimbra -c "/opt/zimbra/bin/zmcertmgr verifycrt comm \$ZS/privkey.pem \$ZS/cert.pem \$ZS/chain.pem"

cp "\$ZS/privkey.pem" /opt/zimbra/ssl/zimbra/commercial/commercial.key
chown zimbra:zimbra /opt/zimbra/ssl/zimbra/commercial/commercial.key
chmod 640 /opt/zimbra/ssl/zimbra/commercial/commercial.key

su - zimbra -c "/opt/zimbra/bin/zmcertmgr deploycrt comm \$ZS/cert.pem \$ZS/chain.pem"
su - zimbra -c "zmcontrol restart"

logger -t kin-mail "Zimbra certificate redeployed after Let's Encrypt renewal"
HOOK
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh
  ok "Deploy hook terpasang"
}

run_deploy_hook() {
  say "Deploy sertifikat ke Zimbra (hook)"
  if /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh; then
    ok "Hook berhasil, sertifikat ter-deploy"
  else
    fail "Hook gagal - jalankan manual untuk melihat pesannya"
    exit 1
  fi
}

verify_tls_ports() {
  say "Verifikasi TLS di port live"
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
    [ -n "$S" ] && ok "port $1 : $S" || fail "port $1 : handshake gagal"
  done
}

# --- Cloudflare DNS-01 -------------------------------------------------------
tls_cloudflare() {
  say "1. certbot dan plugin Cloudflare"
  apt-get -qq update
  apt-get -y install certbot python3-certbot-dns-cloudflare >/dev/null 2>&1
  certbot plugins 2>/dev/null | grep -qi cloudflare \
    && ok "plugin dns-cloudflare terpasang" \
    || { fail "plugin dns-cloudflare tidak tersedia"; exit 1; }

  say "2. Token API Cloudflare"
  if [ -s "$CF_CREDS" ] && ! grep -q "PASTE" "$CF_CREDS"; then
    ok "Kredensial sudah ada di ${CF_CREDS}"
  else
    info "Buat di Cloudflare: My Profile -> API Tokens -> Create Token"
    info "Template 'Edit zone DNS', batasi ke zona ${MAIL_DOMAIN} saja."
    ask_secret CF_TOKEN "Tempel API token Cloudflare"
    [ -z "$CF_TOKEN" ] && { fail "Token kosong."; exit 1; }
    mkdir -p /etc/letsencrypt
    printf 'dns_cloudflare_api_token = %s\n' "$CF_TOKEN" > "$CF_CREDS"
    chmod 600 "$CF_CREDS"; chown root:root "$CF_CREDS"
    ok "Tersimpan mode 600"
  fi

  say "3. Menerbitkan sertifikat (DNS-01 Cloudflare)"
  if [ -f "${LE_DIR}/cert.pem" ]; then
    ok "Sertifikat sudah ada, penerbitan dilewati"
  else
    certbot certonly \
      --dns-cloudflare \
      --dns-cloudflare-credentials "$CF_CREDS" \
      --dns-cloudflare-propagation-seconds "$CF_PROPAGATION" \
      -d "$MAIL_HOST" \
      --preferred-chain "ISRG Root X1" \
      --agree-tos --no-eff-email -m "$LE_EMAIL" \
      --non-interactive 2>&1 | tail -8
    [ -f "${LE_DIR}/cert.pem" ] || { fail "Penerbitan gagal. Lihat /var/log/letsencrypt/"; exit 1; }
  fi
  openssl x509 -in "${LE_DIR}/cert.pem" -noout -subject -dates | sed 's/^/    /'

  say "4. Memasang deploy hook"
  install_isrg_root
  write_deploy_hook
  run_deploy_hook
  verify_tls_ports

  say "5. Uji perpanjangan otomatis"
  certbot renew --dry-run 2>&1 | grep -qi "simulated renewal" \
    && ok "certbot renew --dry-run berhasil" \
    || warn "dry-run tidak konklusif, periksa /var/log/letsencrypt/"
}

# --- Manual DNS-01 (any provider) --------------------------------------------
tls_manual() {
  warn "TLS_METHOD=manual — renewal TIDAK otomatis."
  warn "certbot --manual butuh manusia di terminal tiap issue/renew,"
  warn "kecuali nanti ditambahkan --manual-auth-hook khusus provider."
  info "Jangan andalkan cron 'certbot renew' untuk mode ini."

  say "1. certbot"
  apt-get -qq update
  apt-get -y install certbot >/dev/null 2>&1
  command -v certbot >/dev/null || { fail "certbot tidak terpasang"; exit 1; }
  ok "certbot siap"

  say "2. Menerbitkan sertifikat (DNS-01 manual, interaktif)"
  info "Saat diminta, buat TXT record di panel DNS (Rumahweb / provider zona),"
  info "tunggu propagasi, lalu tekan Enter di terminal ini."
  if [ -f "${LE_DIR}/cert.pem" ]; then
    ok "Sertifikat sudah ada, penerbitan dilewati"
  else
    # Interactive: operator must be present to create the ACME TXT record.
    certbot certonly \
      --manual \
      --preferred-challenges dns \
      -d "$MAIL_HOST" \
      --preferred-chain "ISRG Root X1" \
      --agree-tos --no-eff-email -m "$LE_EMAIL" \
      --manual-public-ip-logging-ok
    [ -f "${LE_DIR}/cert.pem" ] || { fail "Penerbitan gagal. Lihat /var/log/letsencrypt/"; exit 1; }
  fi
  openssl x509 -in "${LE_DIR}/cert.pem" -noout -subject -dates | sed 's/^/    /'

  say "3. Memasang deploy hook (untuk deploy sekarang; bukan auto-renew)"
  install_isrg_root
  write_deploy_hook
  run_deploy_hook
  verify_tls_ports

  say "4. Perpanjangan otomatis"
  warn "Dilewati / tidak diandalkan untuk TLS_METHOD=manual."
  info "Sebelum expired: jalankan ulang 04 (atau certbot certonly --manual …) dengan operator hadir."
}

# --- Customer-provided certificate -------------------------------------------
tls_customer() {
  say "1. Sertifikat customer — certbot dilewati"
  warn "Tidak ada otomasi penerbitan. Operator memasang file dari customer."
  echo
  printf '%s\n' "${BLD}  LANGKAH MANUAL (zmcertmgr)${RST}"
  echo   "  ----------------------------------------------------------------"
  info "1. Simpan file dari customer, contoh:"
  info "     /root/customer-tls/${MAIL_HOST}.crt   (sertifikat + intermediate bila perlu)"
  info "     /root/customer-tls/${MAIL_HOST}.key   (private key)"
  info "     /root/customer-tls/ca-chain.pem       (rantai CA sampai root, bila terpisah)"
  info "2. Gabungkan chain bila perlu, lalu verifikasi:"
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
    ok "Ada sertifikat di tree Zimbra — lanjut verifikasi port (bila sudah di-deploy)"
    verify_tls_ports || true
  else
    warn "Belum terdeteksi cert commercial ter-deploy — selesaikan langkah di atas sebelum production."
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
  ok "Kunci DKIM sudah ada"
else
  su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -a -d ${MAIL_DOMAIN}" >/dev/null 2>&1
  ok "Kunci DKIM dibuat"
fi

Q=$(su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" 2>/dev/null)
SEL=$(printf '%s' "$Q" | awk '/DKIM Selector/{getline; while($0==""){getline}; print; exit}')
VAL=$(printf '%s' "$Q" | awk '/DKIM Public signature/,0' | grep -oE '"[^"]*"' | tr -d '"' | tr -d '\n')

echo
printf '%s\n' "${BLD}  TEMPEL RECORD DKIM DI PANEL DNS (zona ${MAIL_DOMAIN})${RST}"
echo   "  ----------------------------------------------------------------"
echo   "  Type    : TXT"
echo   "  Name    : ${SEL}._domainkey"
echo   "  TTL     : Auto / 300"
echo   "  Content :"
echo   "$VAL" | fold -w 76 | sed 's/^/    /'
echo   "  ----------------------------------------------------------------"
info "Satu baris, tanpa tanda kutip atau kurung. Provider DNS memecahnya sendiri bila perlu."
info "Yang ditempel adalah kunci PUBLIK. Private key tetap di LDAP Zimbra."
echo
warn "Setelah record dipublish, jalankan: ./05-healthcheck.sh"
info "Proses yang sudah berjalan menyimpan hasil DNS negatif, jadi 05 akan"
info "me-restart dnsmasq, amavis dan opendkim sebelum memverifikasi DKIM."
echo
