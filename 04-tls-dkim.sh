#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 04 TLS + DKIM
#
# Issues a Let's Encrypt certificate using the DNS-01 challenge via Cloudflare,
# deploys it into Zimbra, installs a renewal hook and PROVES the hook works.
# Then generates the DKIM key and prints the record to publish.
#
# DNS-01 is used deliberately: this host sits behind NAT with no inbound path,
# so the HTTP-01 challenge cannot succeed.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
LE_DIR="/etc/letsencrypt/live/${MAIL_HOST}"
ZS="/opt/zimbra/ssl/letsencrypt"

[ -d /opt/zimbra ] || { fail "Zimbra belum terpasang. Jalankan 03 dulu."; exit 1; }

# --- 1. certbot + cloudflare plugin ------------------------------------------
say "1. certbot dan plugin Cloudflare"
apt-get -qq update
apt-get -y install certbot python3-certbot-dns-cloudflare >/dev/null 2>&1
certbot plugins 2>/dev/null | grep -qi cloudflare \
  && ok "plugin dns-cloudflare terpasang" \
  || { fail "plugin dns-cloudflare tidak tersedia"; exit 1; }

# --- 2. api token ------------------------------------------------------------
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

# --- 3. issue ----------------------------------------------------------------
say "3. Menerbitkan sertifikat (DNS-01)"
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

# --- 4. deploy hook ----------------------------------------------------------
# Zimbra will not verify a chain that does not reach the root, so ISRG Root X1
# is appended. The hook exists because certbot renewing the file while Zimbra
# keeps serving the old certificate is a silent outage 90 days later.
say "4. Memasang deploy hook"
mkdir -p "$ZS" /etc/letsencrypt/renewal-hooks/deploy
curl -s -m 30 -o "${ZS}/isrgrootx1.pem" https://letsencrypt.org/certs/isrgrootx1.pem
[ -s "${ZS}/isrgrootx1.pem" ] || { fail "Gagal mengunduh ISRG Root X1"; exit 1; }

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
ok "Hook terpasang"

# --- 5. run the hook for real ------------------------------------------------
# certbot's dry run exercises ACME and DNS but skips deploy hooks, so the hook
# is executed once in earnest. An untested hook is the classic silent failure.
say "5. Menjalankan hook (deploy sertifikat + restart Zimbra)"
if /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh >/dev/null 2>&1; then
  ok "Hook berhasil, sertifikat ter-deploy"
else
  fail "Hook gagal - jalankan manual untuk melihat pesannya"; exit 1
fi

# --- 6. verify TLS on the live ports -----------------------------------------
say "6. Verifikasi TLS"
for spec in "443 https" "587 starttls-smtp" "993 imaps"; do
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

say "7. Uji perpanjangan otomatis"
certbot renew --dry-run 2>&1 | grep -qi "simulated renewal" \
  && ok "certbot renew --dry-run berhasil" \
  || warn "dry-run tidak konklusif, periksa /var/log/letsencrypt/"

# --- 8. DKIM -----------------------------------------------------------------
say "8. DKIM"
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
printf '%s\n' "${BLD}  TEMPEL RECORD INI DI CLOUDFLARE${RST}"
echo   "  ----------------------------------------------------------------"
echo   "  Type    : TXT"
echo   "  Name    : ${SEL}._domainkey"
echo   "  TTL     : Auto"
echo   "  Content :"
echo   "$VAL" | fold -w 76 | sed 's/^/    /'
echo   "  ----------------------------------------------------------------"
info "Satu baris, tanpa tanda kutip atau kurung. Cloudflare memecahnya sendiri."
info "Yang ditempel adalah kunci PUBLIK. Private key tetap di LDAP Zimbra."
echo
warn "Setelah record dipublish, jalankan: ./05-healthcheck.sh"
info "Proses yang sudah berjalan menyimpan hasil DNS negatif, jadi 05 akan"
info "me-restart dnsmasq, amavis dan opendkim sebelum memverifikasi DKIM."
echo
