#!/usr/bin/env bash
# =============================================================================
# KIN Mail - shared library and interactive configuration
#
# Sourced by every numbered script. On first run it asks for the few settings
# that differ per deployment and stores them in /etc/kin-mail/config.
# Later runs read that file and ask nothing.
#
# To reconfigure from scratch:  sudo ./00-config.sh --reset
# =============================================================================

CONF_DIR="/etc/kin-mail"
CONF_FILE="${CONF_DIR}/config"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'

say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Jalankan sebagai root:  sudo $0"; exit 1; }
}

# An SMTP port is only usable if it returns a 220 banner. A bare TCP connect
# is NOT proof: inline security devices complete the handshake and then drop
# the session, which reads as "open" but delivers nothing.
smtp_banner() {
  timeout "${3:-12}" bash -c "exec 3<>/dev/tcp/${1}/${2}; head -c 100 <&3" 2>/dev/null
}

# ask VARNAME "Pertanyaan" "default"        -> normal input
# ask_secret VARNAME "Pertanyaan"           -> hidden input, no default
ask() {
  local __var="$1" __prompt="$2" __default="${3:-}" __reply=""
  if [ -n "$__default" ]; then
    printf '  %s %s[%s]%s: ' "$__prompt" "$DIM" "$__default" "$RST"
  else
    printf '  %s: ' "$__prompt"
  fi
  read -r __reply </dev/tty
  [ -z "$__reply" ] && __reply="$__default"
  printf -v "$__var" '%s' "$__reply"
}

ask_secret() {
  local __var="$1" __prompt="$2" __reply=""
  printf '  %s: ' "$__prompt"
  read -rs __reply </dev/tty; echo
  printf -v "$__var" '%s' "$__reply"
}

ask_yn() {
  local __prompt="$1" __default="${2:-y}" __reply=""
  printf '  %s %s[%s/%s]%s: ' "$__prompt" "$DIM" \
    "$([ "$__default" = y ] && echo Y || echo y)" \
    "$([ "$__default" = y ] && echo n || echo N)" "$RST"
  read -r __reply </dev/tty
  [ -z "$__reply" ] && __reply="$__default"
  case "$__reply" in [Yy]*) return 0;; *) return 1;; esac
}

# --- detect sensible defaults from the running system ------------------------
detect_defaults() {
  DEF_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
  DEF_IFACE=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $2}' | head -1)
  DEF_HOST=$(hostname -f 2>/dev/null || hostname)
  case "$DEF_HOST" in
    *.*.*) DEF_DOMAIN="${DEF_HOST#*.}" ;;
    *)     DEF_DOMAIN="" ;;
  esac
  DEF_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "Asia/Jakarta")
  [ "$DEF_TZ" = "Etc/UTC" ] && DEF_TZ="Asia/Jakarta"
}

# --- interactive wizard ------------------------------------------------------
run_wizard() {
  detect_defaults
  echo
  printf '%s\n' "${BLD}  KIN Mail - konfigurasi awal${RST}"
  printf '%s\n' "${DIM}  Tekan Enter untuk memakai nilai dalam kurung.${RST}"
  echo

  say "Identitas mail server"
  ask MAIL_DOMAIN "Domain email (bagian setelah @)" "${DEF_DOMAIN}"
  while [ -z "$MAIL_DOMAIN" ]; do
    warn "Domain wajib diisi. Contoh: example.co.id"
    ask MAIL_DOMAIN "Domain email" ""
  done
  ask MAIL_HOST   "Hostname server mail (FQDN)" "mail.${MAIL_DOMAIN}"
  ask SERVER_IP   "Alamat IP LAN server ini"    "${DEF_IP}"
  ask NET_IFACE   "Nama interface jaringan"     "${DEF_IFACE}"
  ask TIMEZONE    "Timezone sistem"             "${DEF_TZ}"

  # Zimbra's timezone list has no Asia/Jakarta entry. Asia/Bangkok is the same
  # UTC+7 with no DST, so it is the correct equivalent for WIB.
  case "$TIMEZONE" in
    Asia/Jakarta|Asia/Pontianak) ZIMBRA_TZ_NAME="Asia/Bangkok" ;;
    *)                           ZIMBRA_TZ_NAME="$TIMEZONE" ;;
  esac

  echo; say "Resolver"
  info "Server ini akan menjalankan dnsmasq lokal: domain mail dijawab sendiri,"
  info "sisanya diteruskan ke resolver publik (split-horizon)."
  ask DNS_UPSTREAM_1 "DNS upstream utama"   "1.1.1.1"
  ask DNS_UPSTREAM_2 "DNS upstream cadangan" "8.8.8.8"
  INTERNAL_ZONE=""; INTERNAL_DNS=""
  if ask_yn "Ada zona internal customer yang perlu diteruskan khusus (AD dsb)?" n; then
    ask INTERNAL_ZONE "Nama zona internal (contoh: corp.local)" ""
    ask INTERNAL_DNS  "DNS server untuk zona itu"               ""
  fi

  echo; say "Build Zimbra FOSS"
  info "Sumber: github.com/maldua/zimbra-foss (BTACTIC, GPL-2.0)"
  info "Build komunitas dari source resmi Zimbra. Bukan binary resmi Zimbra,"
  info "dan bisa tertinggal s/d 2 bulan dari security fix yang di-embargo."
  ZCS_VERSION=""; ZCS_FILE=""
  if ask_yn "Deteksi otomatis rilis terbaru dari GitHub?" y; then
    detect_latest_zcs
  fi
  if [ -z "$ZCS_FILE" ]; then
    ask ZCS_VERSION "Tag versi"  "10.1.18.p1"
    ask ZCS_FILE    "Nama file"  "zcs-10.1.18_GA_4200001.UBUNTU22_64.20260801175925.tgz"
  fi

  echo; say "Kredensial"
  while :; do
    ask_secret ADMIN_PASS "Password admin Zimbra (min 8 karakter)"
    [ ${#ADMIN_PASS} -ge 8 ] && break
    warn "Terlalu pendek."
  done
  ask LE_EMAIL "Email untuk notifikasi Let's Encrypt" "admin@${MAIL_DOMAIN}"

  echo; say "Pengujian"
  ask EXTERNAL_TEST_ADDRESS "Alamat email luar untuk tes kirim (kosongkan bila belum)" ""

  mkdir -p "$CONF_DIR"
  cat > "$CONF_FILE" <<EOF
# KIN Mail - dibuat $(date -Is)
# Ubah dengan: sudo ./00-config.sh --reset
MAIL_DOMAIN="${MAIL_DOMAIN}"
MAIL_HOST="${MAIL_HOST}"
SERVER_IP="${SERVER_IP}"
NET_IFACE="${NET_IFACE}"
TIMEZONE="${TIMEZONE}"
ZIMBRA_TZ_NAME="${ZIMBRA_TZ_NAME}"
DNS_UPSTREAM_1="${DNS_UPSTREAM_1}"
DNS_UPSTREAM_2="${DNS_UPSTREAM_2}"
INTERNAL_ZONE="${INTERNAL_ZONE}"
INTERNAL_DNS="${INTERNAL_DNS}"
ZCS_VERSION="${ZCS_VERSION}"
ZCS_FILE="${ZCS_FILE}"
ZCS_BASE="https://github.com/maldua/zimbra-foss/releases/download/zimbra-foss-build-ubuntu-22.04/${ZCS_VERSION}"
ZCS_SRC="/opt/zcs-src"
ADMIN_PASS='${ADMIN_PASS}'
LE_EMAIL="${LE_EMAIL}"
CF_CREDS="/etc/letsencrypt/cloudflare.ini"
CF_PROPAGATION=40
EXTERNAL_TEST_ADDRESS="${EXTERNAL_TEST_ADDRESS}"
TEST_USER_1="test1@${MAIL_DOMAIN}"
TEST_PASS_1="KinTest#1-\$(hostname -s)"
TEST_USER_2="test2@${MAIL_DOMAIN}"
TEST_PASS_2="KinTest#2-\$(hostname -s)"
EOF
  chmod 600 "$CONF_FILE"

  echo
  ok "Tersimpan di ${CONF_FILE} (mode 600)"
  info "Berisi password admin, jadi jangan dibagikan."
  echo
}

detect_latest_zcs() {
  command -v curl >/dev/null || return 1
  local url
  url=$(curl -s -m 25 "https://api.github.com/repos/maldua/zimbra-foss/releases?per_page=60" 2>/dev/null \
        | grep -oE '"browser_download_url": *"[^"]*UBUNTU22_64[^"]*\.tgz"' \
        | sed -E 's/.*: *"//; s/"$//' | sort -V | tail -1)
  if [ -n "$url" ]; then
    ZCS_FILE="${url##*/}"
    ZCS_VERSION=$(printf '%s' "$url" | awk -F/ '{print $(NF-1)}')
    ok "Rilis terbaru: ${ZCS_VERSION}"
    info "$ZCS_FILE"
  else
    warn "Gagal mendeteksi otomatis, akan ditanyakan manual."
  fi
}

# --- entry point -------------------------------------------------------------
if [ "${1:-}" = "--reset" ]; then
  need_root
  [ -f "$CONF_FILE" ] && mv "$CONF_FILE" "${CONF_FILE}.bak.$(date +%s)"
  run_wizard
  exit 0
fi

if [ -f "$CONF_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONF_FILE"
else
  need_root
  run_wizard
  # shellcheck disable=SC1090
  . "$CONF_FILE"
fi
