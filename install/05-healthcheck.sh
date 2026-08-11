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
NS=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" NS 2>/dev/null | tr '\n' ' ')
if [ -z "$(echo "$NS" | tr -d '[:space:]')" ]; then
  f "NS     : none - domain is not delegated"
else
  p "NS     : $NS"
  NSCOUNT=$(dns_ns_provider_count "$NS")
  [ "$NSCOUNT" -eq 1 ] && p "Delegasi konsisten pada satu provider brand" \
                       || f "Delegasi TERPECAH di ${NSCOUNT} provider - mail akan gagal acak"
fi

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
    # -vvv is required. Without it the tool prints nothing at all on success
    # and signals the result only through its exit status, so grepping the
    # output reports a healthy key as broken.
    TKOUT=$(su - zimbra -c "$TK -d ${MAIL_DOMAIN} -s ${SEL} -vvv" 2>&1)
    if printf '%s' "$TKOUT" | grep -qi "key OK"; then
      p "Kunci privat cocok dengan record yang dipublish"
    else
      f "opendkim-testkey gagal - record belum ada atau tidak cocok"
      printf '%s\n' "$TKOUT" | tail -3 | sed 's/^/      /'
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

echo; say "Alamat pengirim  ${DIM}(dilihat oleh mail server tujuan)${RST}"
if [ "$SMTP_OUT" -eq 0 ]; then
  b "Dilewati - SMTP keluar masih diblokir"
  SEND_IP=""
else
  SEND_IP=$(smtp_egress_ip); S2=$(smtp_egress_ip); S3=$(smtp_egress_ip)
  if [ -z "$SEND_IP" ]; then
    f "Tidak bisa menentukan alamat pengirim"
  elif [ "$SEND_IP" = "$S2" ] && [ "$SEND_IP" = "$S3" ]; then
    p "Konsisten: ${SEND_IP}"
  else
    f "TIDAK konsisten: ${SEND_IP} / ${S2} / ${S3}"
    info "Beberapa uplink membagi trafik keluar. PTR hanya bisa menunjuk satu"
    info "alamat, jadi sebagian email akan ditolak. Perlu policy route atau IP"
    info "pool yang mengunci host ini ke satu uplink."
  fi
fi

if [ -n "$SEND_IP" ]; then
  echo; say "Rantai forward-confirmed reverse DNS"
  info "Penerima mengambil PTR dari IP pengirim, lalu A dari nama itu, dan"
  info "hasilnya harus kembali ke IP pengirim yang sama."

  A_REC=$(dig +short +time=5 @"$DNS_UPSTREAM_1" A "$MAIL_HOST" 2>/dev/null | head -1)
  [ "$A_REC" = "$SEND_IP" ] \
    && p "A ${MAIL_HOST} = ${A_REC} = alamat pengirim" \
    || f "A ${MAIL_HOST} = ${A_REC:-kosong}, tetapi kirim dari ${SEND_IP}"

  PTR=$(dig +short +time=5 -x "$SEND_IP" 2>/dev/null | head -1)
  if [ -z "$PTR" ]; then
    # Reverse DNS belongs to the owner of the IP block, so this is an external
    # dependency rather than a server fault. Counting it as a failure would
    # make the summary blame a host that is configured correctly.
    b "PTR ${SEND_IP} belum ada - minta ke pemilik blok IP (ISP), bukan di Cloudflare"
  elif [ "$PTR" = "${MAIL_HOST}." ]; then
    p "PTR ${SEND_IP} -> ${PTR}"
    BACK=$(dig +short +time=5 @"$DNS_UPSTREAM_1" A "${PTR%.}" 2>/dev/null | head -1)
    [ "$BACK" = "$SEND_IP" ] \
      && p "Rantai lengkap dan cocok" \
      || f "Rantai putus: ${PTR%.} mengarah ke ${BACK:-kosong}, bukan ${SEND_IP}"
  else
    b "PTR ${SEND_IP} -> ${PTR} (bukan ${MAIL_HOST}, boleh asal A-nya kembali ke IP ini)"
  fi

  case "$SPF" in
    *"ip4:${SEND_IP}"*) p "SPF menyebut alamat pengirim secara eksplisit" ;;
    *mx*) [ "$A_REC" = "$SEND_IP" ] \
            && p "SPF 'mx' mencakup alamat pengirim" \
            || f "SPF 'mx' TIDAK mencakup ${SEND_IP} - tambahkan ip4:${SEND_IP}" ;;
    *) b "SPF tidak jelas mencakup ${SEND_IP}" ;;
  esac
fi

# --- hybrid authentication ---------------------------------------------------
echo; say "Autentikasi hybrid"
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
  p "Auth lokal Zimbra: ${TEST_USER_1}"
else
  f "Auth lokal Zimbra gagal: ${TEST_USER_1}"
  sed 's/^/      /' "$ERR1" | tail -5
fi
if ! ensure_kin_test_mailbox "$TEST_USER_2" "$TEST_PASS_2" 2>"$ERR2"; then
  f "Gagal menyiapkan mailbox uji: ${TEST_USER_2}"
  sed 's/^/      /' "$ERR2" | tail -5
fi
rm -f "$ERR1" "$ERR2"

if [ "${AD_AUTH_ENABLED}" = "yes" ]; then
  case "$MECH" in
    ad|ldap) p "Domain memakai auth eksternal (${MECH})" ;;
    *)       f "AD_AUTH_ENABLED=yes tetapi zimbraAuthMech=${MECH:-kosong} — jalankan 06-hybrid-auth.sh" ;;
  esac
  case "$FALLBACK" in
    TRUE|true|1) p "zimbraAuthFallbackToLocal aktif" ;;
    *)           f "zimbraAuthFallbackToLocal tidak TRUE — akun lokal di domain hybrid akan gagal login" ;;
  esac
  if [ -n "${AD_TEST_USER}" ] && [ -n "${AD_TEST_PASS}" ]; then
    if zimbra_user_auth_ok "$AD_TEST_USER" "$AD_TEST_PASS"; then
      p "Auth AD LDAP: ${AD_TEST_USER}"
    else
      f "Auth AD LDAP gagal: ${AD_TEST_USER}"
    fi
  else
    b "AD aktif di config tetapi AD_TEST_USER/PASS kosong — tidak bisa uji jalur AD"
  fi
else
  info "AD_AUTH_ENABLED bukan yes — jalur AD tidak diuji (lokal saja, sesuai desain skippable)"
fi

# --- mail flow ---------------------------------------------------------------
if [ $QUICK -eq 0 ]; then
  echo; say "T1 - alur mail internal"
  ensure_kin_test_mailbox "$TEST_USER_1" "$TEST_PASS_1" >/dev/null 2>&1 || true
  ensure_kin_test_mailbox "$TEST_USER_2" "$TEST_PASS_2" >/dev/null 2>&1 || true
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
