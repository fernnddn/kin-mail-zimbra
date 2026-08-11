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
#
# Read a LINE, never a fixed byte count. `head -c N` blocks until N bytes
# arrive, and a real banner is shorter than that, so it hangs until the
# timeout and reports a working port as blocked.
smtp_banner() {
  local host="$1" port="$2" tmo="${3:-15}"
  if command -v swaks >/dev/null 2>&1; then
    timeout $((tmo + 10)) swaks --server "$host" --port "$port" \
      --quit-after CONNECT --timeout "$tmo" 2>/dev/null \
      | grep -m1 -oE '^<-[[:space:]]+220 .*' | sed 's/^<-[[:space:]]*//'
  else
    timeout "$tmo" bash -c "exec 3<>/dev/tcp/${host}/${port}; head -1 <&3" 2>/dev/null
  fi
}

# Escape for inclusion inside single quotes in a shell -c string.
shell_single_quote() {
  printf '%s' "${1:-}" | sed "s/'/'\\\\''/g"
}

# Run a command as the zimbra user with each argument safely single-quoted
# (passwords may contain #, spaces, etc. — unquoted values become shell comments).
zimbra_cmd() {
  local args=() a q
  for a in "$@"; do
    q=$(shell_single_quote "$a")
    args+=("'$q'")
  done
  # shellcheck disable=SC2029
  su - zimbra -c "${args[*]}"
}

# Verify an account password the way clients do. `zmprov auth` is NOT a command
# on Zimbra 10 FOSS (it prints usage and can exit 0) — use zmmailbox instead.
zimbra_user_auth_ok() {
  local user="$1" pass="$2"
  zimbra_cmd zmmailbox -m "$user" -p "$pass" gaf >/dev/null 2>&1
}

# Create test mailbox if missing; align password; verify auth. Prints zmprov
# errors on stderr. Return 0 only when the account authenticates.
ensure_kin_test_mailbox() {
  local email="$1" pass="$2" out
  if ! zimbra_cmd zmprov ga "$email" >/dev/null 2>&1; then
    if ! out=$(zimbra_cmd zmprov ca "$email" "$pass" displayName 'KIN test' 2>&1); then
      printf '%s\n' "$out" >&2
      return 1
    fi
  else
    # Prior runs may have created the account under a different password (or
    # with a mangled one). Always align to the config password before testing.
    if ! out=$(zimbra_cmd zmprov sp "$email" "$pass" 2>&1); then
      printf '%s\n' "$out" >&2
      return 1
    fi
  fi
  if zimbra_user_auth_ok "$email" "$pass"; then
    return 0
  fi
  printf 'auth failed for %s after create/reset\n' "$email" >&2
  return 1
}

# Brand label for an NS hostname — used to detect split delegation.
# Providers like Rumahweb intentionally publish ns*.brand.com/.net/.org/.biz;
# comparing the full parent would false-FAIL. For common two-part public
# suffixes (co.id, co.uk, …) the brand is the label before that suffix so two
# unrelated *.co.id operators are not collapsed into one "brand".
dns_ns_brand() {
  local host="${1%.}"
  local IFS=.
  # shellcheck disable=SC2206
  local parts=(${host})
  local n=${#parts[@]}
  local i

  for ((i = 0; i < n; i++)); do
    parts[$i]=$(printf '%s' "${parts[$i]}" | tr '[:upper:]' '[:lower:]')
  done

  if [ "$n" -lt 2 ]; then
    printf '%s\n' "$host"
    return 0
  fi

  local suffix2="${parts[n-2]}.${parts[n-1]}"
  case "$suffix2" in
    co.id|or.id|go.id|ac.id|sch.id|my.id|web.id|biz.id|co.uk|org.uk|ac.uk|gov.uk|ltd.uk|plc.uk|me.uk|com.au|net.au|org.au|edu.au|gov.au|asn.au|id.au|co.jp|or.jp|ne.jp|ac.jp|go.jp|com.br|net.br|org.br|co.nz|org.nz|net.nz|com.sg|org.sg|net.sg|co.za|org.za|net.za|web.za|com.mx|org.mx|com.my|org.my|net.my|com.hk|org.hk|net.hk|com.tw|org.tw|co.in|org.in|firm.in|gen.in|ind.in|com.tr|org.tr|com.ar|org.ar|co.kr|or.kr|com.ph|net.ph|org.ph|com.vn|net.vn|co.th|or.th|ac.th|go.th)
      if [ "$n" -ge 3 ]; then
        printf '%s\n' "${parts[n-3]}"
        return 0
      fi
      ;;
  esac

  printf '%s\n' "${parts[n-2]}"
}

# stdin/arg: whitespace-separated NS hostnames (trailing dots OK).
# Prints unique brand count on stdout; unique brands on stderr if KIN_DNS_NS_DEBUG=1.
dns_ns_provider_count() {
  local ns_list="${1:-}" host brand
  local brands=""
  for host in $ns_list; do
    [ -z "$host" ] && continue
    brand=$(dns_ns_brand "$host")
    brands="${brands}${brand}"$'\n'
  done
  if [ "${KIN_DNS_NS_DEBUG:-0}" = 1 ]; then
    printf '%s' "$brands" | sed '/^$/d' | sort -u >&2
  fi
  printf '%s' "$brands" | sed '/^$/d' | sort -u | grep -c .
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
    . /etc/os-release 2>/dev/null || true
    case "${VERSION_ID:-22.04}" in
      24.04) _plat_hint="UBUNTU24_64" ;;
      *)     _plat_hint="UBUNTU22_64" ;;
    esac
    ask ZCS_FILE    "Nama file"  "zcs-10.1.18_GA_4200001.${_plat_hint}.tgz"
  fi
  if [ -z "${ZCS_BASE:-}" ] && [ -n "${ZCS_VERSION:-}" ]; then
    . /etc/os-release 2>/dev/null || true
    case "${VERSION_ID:-22.04}" in
      24.04) ZCS_BASE="https://github.com/maldua/zimbra-foss/releases/download/zimbra-foss-build-ubuntu-24.04/${ZCS_VERSION}" ;;
      *)     ZCS_BASE="https://github.com/maldua/zimbra-foss/releases/download/zimbra-foss-build-ubuntu-22.04/${ZCS_VERSION}" ;;
    esac
  fi

  echo; say "Kredensial"
  while :; do
    ask_secret ADMIN_PASS "Password admin Zimbra (min 8 karakter)"
    [ ${#ADMIN_PASS} -ge 8 ] && break
    warn "Terlalu pendek."
  done
  ask LE_EMAIL "Email untuk notifikasi Let's Encrypt" "admin@${MAIL_DOMAIN}"

  echo; say "Metode penerbitan TLS"
  info "1) Cloudflare DNS-01  — otomatis (butuh API token Cloudflare)"
  info "2) DNS-01 manual      — provider apa pun; operator buat TXT sekali di panel DNS"
  info "3) Customer-provided  — skip certbot; install cert/key customer via zmcertmgr"
  TLS_METHOD=""
  while [ -z "$TLS_METHOD" ]; do
    printf '  Pilihan [1/2/3]: '
    read -r __tls </dev/tty
    case "$__tls" in
      1) TLS_METHOD=cloudflare ;;
      2) TLS_METHOD=manual ;;
      3) TLS_METHOD=customer ;;
      *) warn "Pilih 1, 2, atau 3." ;;
    esac
  done
  ok "TLS_METHOD=${TLS_METHOD}"
  if [ "$TLS_METHOD" = "manual" ]; then
    warn "Mode manual TIDAK memperpanjang sertifikat otomatis lewat cron."
    info "Setiap issue/renew butuh operator hadir di terminal (kecuali nanti ada auth-hook)."
  fi

  # Hybrid auth is optional. Without AD values the domain stays on local
  # Zimbra auth only — single-node install must not hard-fail.
  echo; say "Hybrid authentication (Active Directory)"
  info "Domain bisa bind ke AD customer, dengan fallback ke password lokal Zimbra"
  info "(zimbraAuthFallbackToLocal). Lewati bila AD belum siap."
  AD_AUTH_ENABLED=no
  AD_LDAP_URL=""; AD_SEARCH_BASE=""; AD_SEARCH_FILTER=""
  AD_SEARCH_BIND_DN=""; AD_SEARCH_BIND_PASSWORD=""; AD_BIND_DN_TEMPLATE=""
  AD_TEST_USER=""; AD_TEST_PASS=""
  if ask_yn "Konfigurasi bind ke Active Directory sekarang?" n; then
    ask AD_LDAP_URL "LDAP/AD URL (contoh: ldap://dc.corp.local:389)" ""
    ask AD_SEARCH_BASE "Search base (contoh: DC=corp,DC=local)" ""
    ask AD_SEARCH_FILTER "Search filter" "(sAMAccountName=%u)"
    ask AD_SEARCH_BIND_DN "Bind DN untuk search (service account)" ""
    ask_secret AD_SEARCH_BIND_PASSWORD "Password bind DN search"
    ask AD_BIND_DN_TEMPLATE "Bind DN template opsional (contoh: %u@corp.local; kosong=pakai search)" ""
    ask AD_TEST_USER "Akun uji AD (email penuh di domain mail, harus ada di AD)" ""
    ask_secret AD_TEST_PASS "Password akun uji AD"
    if [ -n "$AD_LDAP_URL" ] && [ -n "$AD_SEARCH_BASE" ] && \
       [ -n "$AD_SEARCH_BIND_DN" ] && [ -n "$AD_SEARCH_BIND_PASSWORD" ] && \
       [ -n "$AD_TEST_USER" ] && [ -n "$AD_TEST_PASS" ]; then
      AD_AUTH_ENABLED=yes
      ok "Hybrid AD akan diaktifkan oleh 06-hybrid-auth.sh"
    else
      warn "Data AD tidak lengkap - hybrid auth di-skip (auth lokal saja)."
      AD_AUTH_ENABLED=no
      AD_SEARCH_BIND_PASSWORD=""; AD_TEST_PASS=""
    fi
  else
    info "Dilewati - auth lokal Zimbra saja."
  fi

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
ZCS_BASE="${ZCS_BASE}"
ZCS_SRC="/opt/zcs-src"
ADMIN_PASS='${ADMIN_PASS}'
LE_EMAIL="${LE_EMAIL}"
TLS_METHOD="${TLS_METHOD}"
CF_CREDS="/etc/letsencrypt/cloudflare.ini"
CF_PROPAGATION=40
EXTERNAL_TEST_ADDRESS="${EXTERNAL_TEST_ADDRESS}"
TEST_USER_1="test1@${MAIL_DOMAIN}"
TEST_PASS_1="KinTest1-\$(hostname -s)"
TEST_USER_2="test2@${MAIL_DOMAIN}"
TEST_PASS_2="KinTest2-\$(hostname -s)"
AD_AUTH_ENABLED="${AD_AUTH_ENABLED}"
AD_LDAP_URL="${AD_LDAP_URL}"
AD_SEARCH_BASE="${AD_SEARCH_BASE}"
AD_SEARCH_FILTER='${AD_SEARCH_FILTER}'
AD_SEARCH_BIND_DN="${AD_SEARCH_BIND_DN}"
AD_SEARCH_BIND_PASSWORD='${AD_SEARCH_BIND_PASSWORD}'
AD_BIND_DN_TEMPLATE="${AD_BIND_DN_TEMPLATE}"
AD_TEST_USER="${AD_TEST_USER}"
AD_TEST_PASS='${AD_TEST_PASS}'
EOF
  chmod 600 "$CONF_FILE"

  echo
  ok "Tersimpan di ${CONF_FILE} (mode 600)"
  info "Berisi password admin, jadi jangan dibagikan."
  echo
}

detect_latest_zcs() {
  command -v curl >/dev/null || return 1
  local url plat tag_prefix
  # Pick the Maldua artefact that matches this host's Ubuntu release.
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
  fi
  case "${VERSION_ID:-22.04}" in
    24.04)
      plat=UBUNTU24_64
      tag_prefix=zimbra-foss-build-ubuntu-24.04
      ;;
    *)
      plat=UBUNTU22_64
      tag_prefix=zimbra-foss-build-ubuntu-22.04
      ;;
  esac
  url=$(curl -s -m 25 "https://api.github.com/repos/maldua/zimbra-foss/releases?per_page=60" 2>/dev/null \
        | grep -oE "\"browser_download_url\": *\"[^\"]*${plat}[^\"]*\\.tgz\"" \
        | sed -E 's/.*: *"//; s/"$//' | sort -V | tail -1)
  if [ -n "$url" ]; then
    ZCS_FILE="${url##*/}"
    ZCS_VERSION=$(printf '%s' "$url" | awk -F/ '{print $(NF-1)}')
    ZCS_BASE="https://github.com/maldua/zimbra-foss/releases/download/${tag_prefix}/${ZCS_VERSION}"
    ok "Rilis terbaru untuk Ubuntu ${VERSION_ID:-?} (${plat}): ${ZCS_VERSION}"
    info "$ZCS_FILE"
  else
    warn "Gagal mendeteksi otomatis (${plat}), akan ditanyakan manual."
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

# Defaults for configs written before hybrid-auth / TLS_METHOD fields existed.
: "${AD_AUTH_ENABLED:=no}"
: "${AD_LDAP_URL:=}"
: "${AD_SEARCH_BASE:=}"
: "${AD_SEARCH_FILTER:=(sAMAccountName=%u)}"
: "${AD_SEARCH_BIND_DN:=}"
: "${AD_SEARCH_BIND_PASSWORD:=}"
: "${AD_BIND_DN_TEMPLATE:=}"
: "${AD_TEST_USER:=}"
: "${AD_TEST_PASS:=}"
: "${TLS_METHOD:=cloudflare}"
