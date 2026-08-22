#!/usr/bin/env bash
# =============================================================================
# KIN Mail - shared library and interactive configuration
#
# Sourced by every numbered script. On first run it asks for the few settings
# that differ per deployment and stores them in /etc/kin-mail/config.
# Later runs read that file and ask nothing.
# Remote/console installs must pre-stage this file; without a usable TTY the
# wizard exits instead of looping.
#
# To reconfigure from scratch:  sudo ./00-config.sh --reset
# =============================================================================

CONF_DIR="${KIN_MAIL_CONF_DIR:-/etc/kin-mail}"
CONF_FILE="${KIN_MAIL_CONFIG:-${CONF_DIR}/config}"
if [ -n "${KIN_MAIL_CONFIG:-}" ]; then
  CONF_DIR=$(dirname "$CONF_FILE")
fi

# 0755 so unprivileged kin-console can traverse to 0644 markers. Do not rely
# on umask. Sensitive files inside (config) stay 0600.
ensure_kin_mail_conf_dir() {
  local dir="${1:-$CONF_DIR}"
  mkdir -p "$dir" || return 1
  chmod 755 "$dir" 2>/dev/null || true
}

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'

say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Run as root:  sudo $0"; exit 1; }
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

# --- Pacemaker / kin-zimbra (shared; used by 04 hook pattern, 05, 07, …) -------
# OCF monitor runs `zmcontrol status` every ~30s. Any zm*ctl / zmcontrol restart
# that leaves a service not Running will mark kin-zimbra FAILED and drop kin-vip
# unless we unmanage with --monitor first (see 1.7b).

pcs_has_kin_zimbra() {
  command -v pcs >/dev/null 2>&1 || return 1
  # pcs 0.11 replaced `resource status <id>`; prefer config, fall back to status.
  pcs resource config kin-zimbra >/dev/null 2>&1 \
    || pcs resource status kin-zimbra >/dev/null 2>&1
}

# Set PCS_KIN_ZIMBRA_UNMANAGED=1 when we successfully unmanaged (for trap/remanage).
PCS_KIN_ZIMBRA_UNMANAGED="${PCS_KIN_ZIMBRA_UNMANAGED:-0}"

kin_zimbra_unmanage() {
  PCS_KIN_ZIMBRA_UNMANAGED=0
  if pcs_has_kin_zimbra; then
    warn "kin-zimbra is Pacemaker-managed — unmanage --monitor before service restart"
    pcs resource unmanage kin-zimbra --monitor
    PCS_KIN_ZIMBRA_UNMANAGED=1
  fi
}

kin_zimbra_remanage() {
  if [ "${PCS_KIN_ZIMBRA_UNMANAGED:-0}" = "1" ]; then
    # manage alone leaves monitors enabled=0 after unmanage --monitor (pcs 0.11).
    pcs resource manage kin-zimbra --monitor 2>/dev/null \
      || pcs resource manage kin-zimbra 2>/dev/null \
      || true
    PCS_KIN_ZIMBRA_UNMANAGED=0
    ok "kin-zimbra re-managed (--monitor)"
  fi
}

# After intentional restarts: wait until https responds and zmcontrol is healthy.
kin_zimbra_wait_healthy() {
  local i=0 code status_out
  while [ "$i" -lt 90 ]; do
    code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 https://127.0.0.1/ || true)
    if [ "$code" = "200" ]; then
      status_out=$(su - zimbra -c "zmcontrol status" 2>/dev/null || true)
      if ! printf '%s\n' "$status_out" | grep -vE '^Host|^$' | grep -v 'Connect:' | grep -qv Running; then
        return 0
      fi
    fi
    i=$((i + 1))
    sleep 2
  done
  fail "Zimbra not healthy after restart (https/zmcontrol)"
  return 1
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

# ask VARNAME "Question" "default"        -> normal input
# ask_secret VARNAME "Question"           -> hidden input, no default
# /dev/tty can exist and be permission-readable while a non-TTY SSH session
# still gets immediate EOF from read (that pinned a CPU on a fresh HA peer).
# Opening it as stdin is the real usability check; a failed read is fatal.
kin_tty_usable() {
  { : </dev/tty; } 2>/dev/null
}

kin_require_tty() {
  local why="${1:-interactive prompt}"
  if ! kin_tty_usable; then
    fail "cannot read /dev/tty (no controlling terminal); refusing to prompt"
    info "Refused ${why}."
    exit 1
  fi
}

ask() {
  local __var="$1" __prompt="$2" __default="${3:-}" __reply=""
  kin_require_tty "prompt ${__var}"
  if [ -n "$__default" ]; then
    printf '  %s %s[%s]%s: ' "$__prompt" "$DIM" "$__default" "$RST"
  else
    printf '  %s: ' "$__prompt"
  fi
  if ! read -r __reply </dev/tty; then
    fail "read from /dev/tty failed for ${__var}; cannot run interactively here"
    exit 1
  fi
  [ -z "$__reply" ] && __reply="$__default"
  printf -v "$__var" '%s' "$__reply"
}

ask_secret() {
  local __var="$1" __prompt="$2" __reply=""
  kin_require_tty "prompt ${__var}"
  printf '  %s: ' "$__prompt"
  if ! read -rs __reply </dev/tty; then
    echo
    fail "read from /dev/tty failed for ${__var}; cannot run interactively here"
    exit 1
  fi
  echo
  printf -v "$__var" '%s' "$__reply"
}

ask_yn() {
  local __prompt="$1" __default="${2:-y}" __reply=""
  kin_require_tty "yes/no prompt"
  printf '  %s %s[%s/%s]%s: ' "$__prompt" "$DIM" \
    "$([ "$__default" = y ] && echo Y || echo y)" \
    "$([ "$__default" = y ] && echo n || echo N)" "$RST"
  if ! read -r __reply </dev/tty; then
    fail "read from /dev/tty failed; cannot run interactively here"
    exit 1
  fi
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
  kin_require_tty "initial configuration wizard"
  detect_defaults
  echo
  printf '%s\n' "${BLD}  KIN Mail - initial configuration${RST}"
  printf '%s\n' "${DIM}  Press Enter to accept the value in brackets.${RST}"
  echo

  say "Mail server identity"
  ask MAIL_DOMAIN "Email domain (part after @)" "${DEF_DOMAIN}"
  _domain_tries=0
  while [ -z "$MAIL_DOMAIN" ]; do
    _domain_tries=$((_domain_tries + 1))
    if [ "$_domain_tries" -ge 5 ]; then
      fail "MAIL_DOMAIN is required; giving up (no usable answer)"
      exit 1
    fi
    warn "Domain is required. Example: example.co.id"
    sleep 1
    ask MAIL_DOMAIN "Email domain" ""
  done
  ask MAIL_HOST   "Mail server hostname (FQDN)" "mail.${MAIL_DOMAIN}"
  ask SERVER_IP   "This server's LAN IP address"    "${DEF_IP}"
  ask NET_IFACE   "Network interface name"     "${DEF_IFACE}"
  ask TIMEZONE    "System timezone"             "${DEF_TZ}"

  # Zimbra's installer timezone list historically omitted Asia/Jakarta; Asia/Bangkok
  # is the same UTC+7 with no DST. Current timezones.ics may list Jakarta — 03
  # prefers the live zmsetup number for TIMEZONE, then this fallback.
  case "$TIMEZONE" in
    Asia/Jakarta|Asia/Pontianak) ZIMBRA_TZ_NAME="Asia/Bangkok" ;;
    *)                           ZIMBRA_TZ_NAME="$TIMEZONE" ;;
  esac

  echo; say "Resolver"
  info "This server will run local dnsmasq: the mail domain is answered locally,"
  info "everything else is forwarded to public resolvers (split-horizon)."
  ask DNS_UPSTREAM_1 "Primary upstream DNS"   "1.1.1.1"
  ask DNS_UPSTREAM_2 "Backup upstream DNS" "8.8.8.8"
  INTERNAL_ZONE=""; INTERNAL_DNS=""
  if ask_yn "Is there a customer internal zone that needs special forwarding (AD, etc.)?" n; then
    ask INTERNAL_ZONE "Internal zone name (example: corp.local)" ""
    ask INTERNAL_DNS  "DNS server for that zone"               ""
  fi

  echo; say "Build Zimbra FOSS"
  info "Source: github.com/maldua/zimbra-foss (BTACTIC, GPL-2.0)"
  info "Community build from official Zimbra source. Not an official Zimbra binary,"
  info "and may lag up to 2 months behind embargoed security fixes."
  ZCS_VERSION=""; ZCS_FILE=""
  if ask_yn "Automatically detect the latest release from GitHub?" y; then
    detect_latest_zcs
  fi
  if [ -z "$ZCS_FILE" ]; then
    ask ZCS_VERSION "Version tag"  "10.1.18.p1"
    . /etc/os-release 2>/dev/null || true
    case "${VERSION_ID:-22.04}" in
      24.04) _plat_hint="UBUNTU24_64" ;;
      *)     _plat_hint="UBUNTU22_64" ;;
    esac
    ask ZCS_FILE    "File name"  "zcs-10.1.18_GA_4200001.${_plat_hint}.20260801175919.tgz"
  fi
  if [ -z "${ZCS_BASE:-}" ] && [ -n "${ZCS_VERSION:-}" ]; then
    . /etc/os-release 2>/dev/null || true
    case "${VERSION_ID:-22.04}" in
      24.04) ZCS_BASE="https://github.com/maldua/zimbra-foss/releases/download/zimbra-foss-build-ubuntu-24.04/${ZCS_VERSION}" ;;
      *)     ZCS_BASE="https://github.com/maldua/zimbra-foss/releases/download/zimbra-foss-build-ubuntu-22.04/${ZCS_VERSION}" ;;
    esac
  fi

  echo; say "Credentials"
  _pass_tries=0
  while :; do
    ask_secret ADMIN_PASS "Zimbra admin password (min 8 characters)"
    [ ${#ADMIN_PASS} -ge 8 ] && break
    _pass_tries=$((_pass_tries + 1))
    if [ "$_pass_tries" -ge 5 ]; then
      fail "ADMIN_PASS is required (min 8 characters); giving up"
      exit 1
    fi
    warn "Too short."
    sleep 1
  done
  ask LE_EMAIL "Email for Let's Encrypt notifications" "admin@${MAIL_DOMAIN}"

  echo; say "TLS issuance method"
  info "1) Cloudflare DNS-01  — automatic (requires Cloudflare API token)"
  info "2) Manual DNS-01      — any provider; operator creates TXT once in DNS panel"
  info "3) Customer-provided  — skip certbot; install customer cert/key via zmcertmgr"
  TLS_METHOD=""
  _tls_tries=0
  while [ -z "$TLS_METHOD" ]; do
    _tls_tries=$((_tls_tries + 1))
    if [ "$_tls_tries" -gt 8 ]; then
      fail "TLS_METHOD was not chosen; cannot run interactively here"
      exit 1
    fi
    printf '  Choice [1/2/3]: '
    if ! read -r __tls </dev/tty; then
      fail "no controlling terminal and no /etc/kin-mail/config present, cannot run interactively here"
      exit 1
    fi
    case "$__tls" in
      1) TLS_METHOD=cloudflare ;;
      2) TLS_METHOD=manual ;;
      3) TLS_METHOD=customer ;;
      *)
        warn "Choose 1, 2, or 3."
        sleep 1
        ;;
    esac
  done
  ok "TLS_METHOD=${TLS_METHOD}"
  if [ "$TLS_METHOD" = "manual" ]; then
    warn "Manual mode does NOT auto-renew certificates via cron."
    info "Each issue/renew requires an operator at the terminal (unless an auth-hook is added later)."
  fi

  # Hybrid auth is optional. Without AD values the domain stays on local
  # Zimbra auth only — single-node install must not hard-fail.
  echo; say "Hybrid authentication (Active Directory)"
  info "Domain can bind to customer AD, with fallback to local Zimbra password"
  info "(zimbraAuthFallbackToLocal). Skip if AD is not ready yet."
  AD_AUTH_ENABLED=no
  AD_LDAP_URL=""; AD_SEARCH_BASE=""; AD_SEARCH_FILTER=""
  AD_SEARCH_BIND_DN=""; AD_SEARCH_BIND_PASSWORD=""; AD_BIND_DN_TEMPLATE=""
  AD_TEST_USER=""; AD_TEST_PASS=""
  if ask_yn "Configure Active Directory bind now?" n; then
    ask AD_LDAP_URL "LDAP/AD URL (example: ldap://dc.corp.local:389)" ""
    ask AD_SEARCH_BASE "Search base (example: DC=corp,DC=local)" ""
    ask AD_SEARCH_FILTER "Search filter" "(sAMAccountName=%u)"
    ask AD_SEARCH_BIND_DN "Bind DN for search (service account)" ""
    ask_secret AD_SEARCH_BIND_PASSWORD "Search bind DN password"
    ask AD_BIND_DN_TEMPLATE "Optional bind DN template (example: %u@corp.local; empty=use search)" ""
    ask AD_TEST_USER "AD test account (full email on mail domain, must exist in AD)" ""
    ask_secret AD_TEST_PASS "AD test account password"
    if [ -n "$AD_LDAP_URL" ] && [ -n "$AD_SEARCH_BASE" ] && \
       [ -n "$AD_SEARCH_BIND_DN" ] && [ -n "$AD_SEARCH_BIND_PASSWORD" ] && \
       [ -n "$AD_TEST_USER" ] && [ -n "$AD_TEST_PASS" ]; then
      AD_AUTH_ENABLED=yes
      ok "Hybrid AD will be enabled by 06-hybrid-auth.sh"
    else
      warn "Incomplete AD data - hybrid auth skipped (local auth only)."
      AD_AUTH_ENABLED=no
      AD_SEARCH_BIND_PASSWORD=""; AD_TEST_PASS=""
    fi
  else
    info "Skipped - Zimbra local auth only."
  fi

  echo; say "Testing"
  ask EXTERNAL_TEST_ADDRESS "External email address for send test (leave blank if not ready)" ""

  echo; say "Licensing / mailbox seats"
  info "CONTRACTED_SEATS limits NEW mailbox creation only (quota gate)."
  info "Password reset and other modify operations are never gated by this value."
  info "Use PLACEHOLDER_UNSET until the operator confirms the real contracted count."
  info "Do not invent a production number here."
  ask CONTRACTED_SEATS "Contracted mailbox seats" "PLACEHOLDER_UNSET"
  case "$CONTRACTED_SEATS" in
    ''|PLACEHOLDER_UNSET)
      CONTRACTED_SEATS=PLACEHOLDER_UNSET
      ;;
    *[!0-9]*)
      warn "Not a non-negative integer — storing PLACEHOLDER_UNSET"
      CONTRACTED_SEATS=PLACEHOLDER_UNSET
      ;;
  esac

  echo; say "Host firewall — admin sources"
  info "Space-separated IPs/CIDRs allowed for SSH:22 and Zimbra Admin:7071"
  info "(cluster LAN /24 from SERVER_IP is always added by 10-host-firewall.sh)."
  info "Example: 203.0.113.10 198.51.100.0/24"
  info "Required before stage 10 (ufw). Leave empty only if you will set KIN_ADMIN_IPS later."
  ask KIN_ADMIN_IPS "Admin source IPs/CIDRs" "${KIN_ADMIN_IPS:-}"
  # Trim and reject obviously broken tokens; empty is allowed for deferred firewall.
  KIN_ADMIN_IPS=$(printf '%s' "$KIN_ADMIN_IPS" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')

  echo; say "Z-Push ActiveSync (mobile)"
  info "Not every deployment needs ActiveSync. Full install skips 07 when disabled."
  if ask_yn "Install Z-Push (mobile ActiveSync) during full install?" y; then
    ZPUSH_ENABLED=yes
  else
    ZPUSH_ENABLED=no
  fi

  ensure_kin_mail_conf_dir "$CONF_DIR"
  cat > "$CONF_FILE" <<EOF
# KIN Mail - created $(date -Is)
# Change with: sudo ./00-config.sh --reset
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
# PLACEHOLDER until operator confirms the real contracted seat count.
# Quota gate blocks NEW mailbox creates only when at/over this limit (or unset).
CONTRACTED_SEATS="${CONTRACTED_SEATS}"
# Admin sources for ufw SSH/7071 (stage 10). Cluster LAN is added automatically.
KIN_ADMIN_IPS="${KIN_ADMIN_IPS}"
# yes = run 07-zpush.sh in full install; no = skip
ZPUSH_ENABLED="${ZPUSH_ENABLED}"
EOF
  chmod 600 "$CONF_FILE"

  echo
  ok "Saved to ${CONF_FILE} (mode 600)"
  info "Contains admin password — do not share."
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
    ok "Latest release for Ubuntu ${VERSION_ID:-?} (${plat}): ${ZCS_VERSION}"
    info "$ZCS_FILE"
  else
    warn "Automatic detection failed (${plat}); will ask manually."
  fi
}

# --- entry point -------------------------------------------------------------
if [ "${1:-}" = "--reset" ]; then
  kin_require_tty "00-config.sh --reset"
  need_root
  [ -f "$CONF_FILE" ] && mv "$CONF_FILE" "${CONF_FILE}.bak.$(date +%s)"
  run_wizard
  exit 0
fi

if [ -f "$CONF_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONF_FILE"
  # Repair a 0750 leftover (LUKS keyfile used to chmod the parent) or a umask
  # without other-execute. Harmless when the dir is already 0755.
  if [ "$(id -u)" -eq 0 ]; then
    chmod 755 "$CONF_DIR" 2>/dev/null || true
  fi
else
  if ! kin_tty_usable; then
    fail "no controlling terminal and no /etc/kin-mail/config present, cannot run interactively here"
    exit 1
  fi
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
# PLACEHOLDER_UNSET until the operator confirms the real contracted seat count.
# Empty legacy configs behave the same as PLACEHOLDER_UNSET (gate denies creates).
: "${CONTRACTED_SEATS:=PLACEHOLDER_UNSET}"
if [ -z "$CONTRACTED_SEATS" ]; then
  CONTRACTED_SEATS=PLACEHOLDER_UNSET
fi
: "${KIN_ADMIN_IPS:=}"
: "${ZPUSH_ENABLED:=yes}"
case "${ZPUSH_ENABLED}" in
  yes|no) ;;
  *) ZPUSH_ENABLED=yes ;;
esac
