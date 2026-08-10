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
echo; say "Layanan Zimbra"
if [ -d /opt/zimbra ]; then
  ST=$(su - zimbra -c "zmcontrol status" 2>&1)
  DOWN=$(printf '%s' "$ST" | grep -v Running | grep -vE "^Host|^$" | wc -l)
  RUN=$(printf '%s' "$ST" | grep -c Running)
  [ "$DOWN" -eq 0 ] && p "${RUN} service berjalan, tidak ada yang mati" \
                    || { f "${DOWN} service tidak berjalan"; printf '%s' "$ST" | grep -v Running | sed 's/^/      /'; }
  info "$(su - zimbra -c 'zmcontrol -v' 2>/dev/null | head -1)"
else
  f "Zimbra belum terpasang"; exit 1
fi

# --- listeners ---------------------------------------------------------------
echo; say "Port yang mendengarkan"
for port in 25 443 587 993; do
  ss -lnt 2>/dev/null | grep -q ":${port} " && p "port ${port} listening" || f "port ${port} tidak listening"
done

# --- certificate -------------------------------------------------------------
echo; say "Sertifikat TLS"
SUB=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 -servername "$MAIL_HOST" 2>/dev/null \
      | openssl x509 -noout -subject -issuer -enddate 2>/dev/null)
if printf '%s' "$SUB" | grep -qi "Let's Encrypt"; then
  p "Sertifikat tepercaya terpasang"
  printf '%s\n' "$SUB" | sed 's/^/      /'
  EXP=$(echo | timeout 15 openssl s_client -connect 127.0.0.1:443 -servername "$MAIL_HOST" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  DAYS=$(( ( $(date -d "$EXP" +%s) - $(date +%s) ) / 86400 ))
  [ "$DAYS" -gt 20 ] && p "Masa berlaku ${DAYS} hari" || f "Tinggal ${DAYS} hari - periksa perpanjangan"
else
  f "Belum ada sertifikat tepercaya (masih self-signed)"
fi
[ -x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh ] \
  && p "Deploy hook terpasang" || f "Deploy hook tidak ada - TLS akan mati dalam 90 hari"

# --- flush caches before DNS assertions --------------------------------------
# Long-lived processes cache negative DNS answers. A record added after they
# started looks missing until they are restarted.
echo; say "Membersihkan cache DNS"
systemctl restart dnsmasq >/dev/null 2>&1 && info "dnsmasq direstart"
su - zimbra -c "zmamavisdctl restart" >/dev/null 2>&1 && info "amavis direstart"
su - zimbra -c "zmopendkimctl restart" >/dev/null 2>&1 && info "opendkim direstart"
sleep 8

# --- public DNS --------------------------------------------------------------
echo; say "DNS publik"
NS=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" NS 2>/dev/null | sed 's/^[^.]*\.//' | sort -u)
NSCOUNT=$(printf '%s' "$NS" | grep -c .)
[ "$NSCOUNT" -eq 1 ] && p "Delegasi konsisten pada satu provider" \
                     || f "Delegasi TERPECAH di ${NSCOUNT} provider - mail akan gagal acak"

MX=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" MX 2>/dev/null | tr '\n' ' ')
[ -n "$MX" ] && p "MX     : $MX" || f "MX belum dipublish"

SPF=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" TXT 2>/dev/null | grep -i spf1 | tr -d '"')
[ -n "$SPF" ] && p "SPF    : $SPF" || f "SPF belum dipublish"

DM=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "_dmarc.${MAIL_DOMAIN}" TXT 2>/dev/null | tr -d '"')
[ -n "$DM" ] && p "DMARC  : $DM" || f "DMARC belum dipublish"

A=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_HOST" A 2>/dev/null | tr '\n' ' ')
[ -n "$A" ] && p "A      : $A" || b "A record belum dipublish - butuh IP publik dari tim network"

# --- DKIM --------------------------------------------------------------------
echo; say "DKIM"
SEL=$(su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" 2>/dev/null \
      | awk '/DKIM Selector/{getline; while($0==""){getline}; print; exit}')
if [ -n "$SEL" ]; then
  p "Selector: ${SEL}"
  TK=$(ls /opt/zimbra/common/sbin/opendkim-testkey /usr/sbin/opendkim-testkey 2>/dev/null | head -1)
  if [ -n "$TK" ]; then
    if su - zimbra -c "$TK -d ${MAIL_DOMAIN} -s ${SEL}" 2>&1 | grep -qi "key OK"; then
      p "Kunci privat cocok dengan record yang dipublish"
    else
      f "opendkim-testkey gagal - record belum ada atau tidak cocok"
    fi
  fi
else
  f "Kunci DKIM belum dibuat"
fi

# --- outbound SMTP -----------------------------------------------------------
echo; say "SMTP keluar  ${DIM}(banner grab)${RST}"
SMTP_OUT=0
for t in "alt1.aspmx.l.google.com 25" "gmail-smtp-in.l.google.com 25"; do
  set -- $t
  BAN=$(smtp_banner "$1" "$2")
  if [ -n "$BAN" ]; then p "$1:$2  ${BAN%%$'\n'*}"; SMTP_OUT=1
  else b "$1:$2  tidak ada banner - DIFILTER"; fi
done
[ "$SMTP_OUT" -eq 0 ] && info "Server tidak bisa mengirim keluar sampai firewall mengizinkan TCP 25 atau 587."

# --- egress stability --------------------------------------------------------
echo; say "Alamat egress"
E1=$(curl -s -m 12 https://ifconfig.me 2>/dev/null)
E2=$(curl -s -m 12 https://api.ipify.org 2>/dev/null)
if [ -n "$E1" ] && [ "$E1" = "$E2" ]; then
  p "Stabil: $E1"
  PTR=$(dig +short +time=5 -x "$E1" 2>/dev/null | tr '\n' ' ')
  [ "$PTR" = "${MAIL_HOST}." ] && p "PTR cocok" || b "PTR '${PTR:-tidak ada}' - seharusnya ${MAIL_HOST}"
elif [ -n "$E1" ]; then
  f "TIDAK stabil: ${E1} vs ${E2} - PTR dan SPF tidak akan pernah konsisten"
else
  f "Tidak bisa menentukan alamat egress"
fi

# --- mail flow ---------------------------------------------------------------
if [ $QUICK -eq 0 ]; then
  echo; say "T1 - alur mail internal"
  for u in "$TEST_USER_1:$TEST_PASS_1" "$TEST_USER_2:$TEST_PASS_2"; do
    su - zimbra -c "zmprov ga ${u%%:*}" >/dev/null 2>&1 || \
      su - zimbra -c "zmprov ca ${u%%:*} '${u##*:}' displayName 'KIN test'" >/dev/null 2>&1
  done
  SUBJ="KIN healthcheck $(date +%s)"
  OUT=$(swaks --to "$TEST_USER_2" --from "$TEST_USER_1" \
        --server 127.0.0.1:587 --auth LOGIN \
        --auth-user "$TEST_USER_1" --auth-password "$TEST_PASS_1" \
        --tls --header "Subject: $SUBJ" --body "healthcheck" 2>&1)
  if printf '%s' "$OUT" | grep -q "queued as"; then
    p "Submission diterima (587, TLS, AUTH)"
    sleep 12
    MSG=$(find /opt/zimbra/store -name '*.msg' -newermt '-2 minutes' 2>/dev/null | head -1)
    if [ -n "$MSG" ]; then
      grep -qi '^DKIM-Signature' "$MSG" && p "Pesan ditandatangani DKIM" || f "Tidak ada header DKIM-Signature"
      AR=$(grep -i '^Authentication-Results' -A1 "$MSG" | tr '\n' ' ')
      case "$AR" in
        *"dkim=pass"*)    p "Verifikasi: dkim=pass" ;;
        *"dkim=neutral"*) f "dkim=neutral - record belum terlihat oleh verifier" ;;
        *)                b "Hasil verifikasi tidak terbaca" ;;
      esac
      grep -q "status=sent" /var/log/zimbra.log 2>/dev/null && p "LMTP mengirim ke mailbox"
    else
      f "Pesan tidak ditemukan di message store"
    fi
  else
    f "Submission ditolak"; printf '%s' "$OUT" | tail -4 | sed 's/^/      /'
  fi

  if [ -n "${EXTERNAL_TEST_ADDRESS}" ] && [ "$SMTP_OUT" -eq 1 ]; then
    echo; say "T2 - kirim ke alamat luar"
    swaks --to "$EXTERNAL_TEST_ADDRESS" --from "$TEST_USER_1" \
          --server 127.0.0.1:587 --auth LOGIN \
          --auth-user "$TEST_USER_1" --auth-password "$TEST_PASS_1" \
          --tls --header "Subject: KIN external test" --body "external test" 2>&1 \
      | grep -q "queued as" && p "Diterima untuk pengiriman ke ${EXTERNAL_TEST_ADDRESS}" \
                            || f "Ditolak"
    info "Periksa header di sisi penerima: SPF, DKIM dan DMARC harus pass."
  elif [ -n "${EXTERNAL_TEST_ADDRESS}" ]; then
    b "T2 dilewati - SMTP keluar masih diblokir"
  fi
fi

# --- open relay --------------------------------------------------------------
echo; say "Keamanan dasar"
MYN=$(su - zimbra -c "zmprov gacf zimbraMtaMyNetworks" 2>/dev/null)
case "$MYN" in
  *"0.0.0.0/0"*) f "OPEN RELAY - zimbraMtaMyNetworks berisi 0.0.0.0/0" ;;
  *)             p "Bukan open relay" ;;
esac

# --- summary -----------------------------------------------------------------
echo
say "RINGKASAN"
printf '  %sLULUS   : %d%s\n' "$GRN" "$PASS" "$RST"
printf '  %sGAGAL   : %d%s\n' "$RED" "$FAILED" "$RST"
printf '  %sBLOKIR  : %d  (di jaringan, bukan di server)%s\n' "$YLW" "$BLOCKED" "$RST"
echo
if [ "$FAILED" -eq 0 ] && [ "$BLOCKED" -eq 0 ]; then
  ok "Semua pemeriksaan lulus."
elif [ "$FAILED" -eq 0 ]; then
  warn "Server sehat. Yang tersisa hanya kendala jaringan di atas."
else
  fail "Ada kegagalan yang perlu diperbaiki di server."
fi
echo
