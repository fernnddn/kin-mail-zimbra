#!/usr/bin/env bash
# =============================================================================
# Put a Proxmox Mail Gateway in front of this Zimbra appliance, and prove it.
#
#   mail-gateway.sh probe     reachability, auth and certificate. Changes nothing.
#   mail-gateway.sh plan      every change that apply would make. Changes nothing.
#   mail-gateway.sh apply     configures PMG, then Zimbra.
#   mail-gateway.sh verify    end-to-end assertions against both sides.
#   mail-gateway.sh revert    un-does the Zimbra side. Leaves PMG alone.
#   mail-gateway.sh status    machine-readable current state.
#
# WHAT IT DOES NOT DO
#   install PMG - PMG is Debian, this appliance is Ubuntu; it is its own VM
#   change DNS - the MX and SPF records live at a registrar we cannot reach
#   disable Zimbra's own filtering unless explicitly asked
#   guess. Every check either proves something or refuses and says why.
#
# The two settings that silently destroy mail if they are wrong are PMG's
# relaynomx (an MX lookup back at PMG is a loop) and PMG's dkim_sign (Zimbra
# already signed, and two signatures fail DMARC at some receivers). Both are
# forced in apply AND asserted in verify, because writing a setting and
# checking a setting are different promises.
#
# Secrets never live in a file this script owns. privhelperd decrypts them from
# the vault and passes them in the environment for the life of one run:
#   KIN_PMG_TOKEN_SECRET   secret half of a PMG API token
#   KIN_PMG_PASSWORD       password, only for ticket auth on older PMG
#
# Injection points, all for tests. The defaults are the real tools.
#   KIN_PMG_CURL KIN_PMG_OPENSSL KIN_PMG_DIG KIN_PMG_ZMPROV KIN_PMG_PYTHON
#   KIN_MAIL_GATEWAY_CONF KIN_PMG_STATE_DIR KIN_MAIL_CONFIG
#   KIN_PMG_ALLOW_NONROOT    skip the euid check (tests only)
# =============================================================================
set -uo pipefail

CURL="${KIN_PMG_CURL:-curl}"
OPENSSL="${KIN_PMG_OPENSSL:-openssl}"
DIG="${KIN_PMG_DIG:-dig}"
PYTHON="${KIN_PMG_PYTHON:-python3}"
ZMPROV="${KIN_PMG_ZMPROV:-}"

GW_CONF="${KIN_MAIL_GATEWAY_CONF:-/etc/kin-mail/mail-gateway.conf}"
STATE_DIR="${KIN_PMG_STATE_DIR:-/var/lib/kin-mail-console}"
PIN_FILE="${STATE_DIR}/mail-gateway-fingerprint"
APPLIANCE_CONF="${KIN_MAIL_CONFIG:-/etc/kin-mail/config}"

# PMG's own defaults. ext_port faces the internet, int_port is the one the
# internal mail server relays out on. Changing either here changes it on PMG.
PMG_EXT_PORT="${KIN_PMG_EXT_PORT:-25}"
PMG_INT_PORT="${KIN_PMG_INT_PORT:-26}"
PMG_API_PORT_DEFAULT=8006

say()  { printf '%s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
mark() { printf 'KIN_GW_%s\n' "$*"; }
ok()   { printf 'KIN_GW_OK %s\n' "$*"; }
warn() { printf 'KIN_GW_WARN %s\n' "$*"; }
fail() { printf 'KIN_GW_REFUSED reason=%s\n' "$1"; shift; [ $# -gt 0 ] && printf '%s\n' "$*"; return 0; }

problems=0
note_problem() { problems=$((problems + 1)); }

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# Non-secret settings live in a file the console writes. Secrets arrive in the
# environment. Reading a config file with `source` would let a corrupted file
# run commands, so every value is pulled out by name instead.
conf_get() {
  local key="$1" file="$2" line
  [ -f "$file" ] || return 1
  line=$(grep -E "^[[:space:]]*${key}=" "$file" 2>/dev/null | tail -1) || return 1
  [ -n "$line" ] || return 1
  line=${line#*=}
  line=${line#\"}; line=${line%\"}
  line=${line#\'}; line=${line%\'}
  printf '%s' "$line"
}

load_config() {
  GW_ENABLED=$(conf_get GATEWAY_ENABLED "$GW_CONF" || printf '0')
  GW_HOST=$(conf_get GATEWAY_HOST "$GW_CONF" || printf '')
  GW_API_PORT=$(conf_get GATEWAY_API_PORT "$GW_CONF" || printf '%s' "$PMG_API_PORT_DEFAULT")
  GW_AUTH=$(conf_get GATEWAY_AUTH "$GW_CONF" || printf 'token')
  GW_TOKEN_ID=$(conf_get GATEWAY_TOKEN_ID "$GW_CONF" || printf '')
  GW_USER=$(conf_get GATEWAY_USER "$GW_CONF" || printf 'root@pam')
  GW_CACERT=$(conf_get GATEWAY_CACERT "$GW_CONF" || printf '')
  GW_NATIVE_FILTERING=$(conf_get GATEWAY_NATIVE_FILTERING "$GW_CONF" || printf 'keep')

  [ -n "$GW_API_PORT" ] || GW_API_PORT=$PMG_API_PORT_DEFAULT

  MAIL_DOMAIN=$(conf_get MAIL_DOMAIN "$APPLIANCE_CONF" || printf '')
  MAIL_HOST=$(conf_get MAIL_HOST "$APPLIANCE_CONF" || printf '')
  SERVER_IP=$(conf_get SERVER_IP "$APPLIANCE_CONF" || printf '')
}

# A hostname or an IPv4 address, and nothing that could carry a shell
# metacharacter into curl's --url or a Postfix setting.
valid_host() {
  case "$1" in
    ''|*[!A-Za-z0-9.:_-]*) return 1 ;;
  esac
  case "$1" in .*|-*) return 1 ;; esac
  return 0
}

valid_port() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

valid_ipv4() {
  local o1 o2 o3 o4 rest
  IFS=. read -r o1 o2 o3 o4 rest <<EOF
$1
EOF
  [ -n "${o4:-}" ] && [ -z "${rest:-}" ] || return 1
  for o in "$o1" "$o2" "$o3" "$o4"; do
    case "$o" in ''|*[!0-9]*) return 1 ;; esac
    [ "$o" -le 255 ] || return 1
  done
  return 0
}

require_config() {
  local bad=0
  if [ -z "${GW_HOST:-}" ]; then
    fail no-gateway-host "No gateway address is configured. Console > Mail Gateway > Connect."
    bad=1
  elif ! valid_host "$GW_HOST"; then
    fail bad-gateway-host "Gateway address '${GW_HOST}' is not a hostname or IP address."
    bad=1
  fi
  if ! valid_port "${GW_API_PORT:-}"; then
    fail bad-api-port "Gateway API port '${GW_API_PORT:-}' is not a port number."
    bad=1
  fi
  if [ -z "${MAIL_DOMAIN:-}" ]; then
    fail no-mail-domain "This appliance has no MAIL_DOMAIN in ${APPLIANCE_CONF}; deploy it first."
    bad=1
  fi
  if [ -z "${SERVER_IP:-}" ]; then
    fail no-server-ip "This appliance has no SERVER_IP in ${APPLIANCE_CONF}; deploy it first."
    bad=1
  elif ! valid_ipv4 "$SERVER_IP"; then
    # This value becomes the gateway's relay target and a /32 in its trusted
    # networks. Anything that is not a plain address there is either a typo
    # that breaks delivery or an entry that widens who may relay.
    fail bad-server-ip "SERVER_IP in ${APPLIANCE_CONF} is '${SERVER_IP}', which is not an IPv4 address."
    bad=1
  fi
  case "${GW_AUTH:-token}" in
    token)
      [ -n "${GW_TOKEN_ID:-}" ] || { fail no-token-id "GATEWAY_AUTH=token but no GATEWAY_TOKEN_ID is set."; bad=1; }
      [ -n "${KIN_PMG_TOKEN_SECRET:-}" ] || { fail no-token-secret "No API token secret was supplied. The console holds it; re-enter it under Connect."; bad=1; }
      ;;
    ticket)
      [ -n "${KIN_PMG_PASSWORD:-}" ] || { fail no-password "GATEWAY_AUTH=ticket but no password was supplied."; bad=1; }
      ;;
    *) fail bad-auth-mode "GATEWAY_AUTH must be token or ticket, not '${GW_AUTH}'."; bad=1 ;;
  esac
  # The gateway must not be this machine. That configuration is a mail loop
  # with extra steps, and it is an easy typo to make.
  if [ -n "${GW_HOST:-}" ] && [ "${GW_HOST}" = "${SERVER_IP:-}" ]; then
    fail gateway-is-self "The gateway address is this appliance's own IP. PMG has to be a separate machine."
    bad=1
  fi
  if [ -n "${GW_HOST:-}" ] && [ -n "${MAIL_HOST:-}" ] && [ "${GW_HOST}" = "${MAIL_HOST}" ]; then
    fail gateway-is-self "The gateway address is this appliance's own hostname. PMG has to be a separate machine."
    bad=1
  fi
  return $bad
}

# -----------------------------------------------------------------------------
# Certificate pinning
# -----------------------------------------------------------------------------
# A fresh PMG serves a self-signed certificate on :8006. The tempting answer is
# curl -k, which means every later connection trusts whatever answers on that
# address - including whatever replaces it. Instead: record the fingerprint the
# first time, with the operator's consent, and verify it every time after.
# Handing over a real CA bundle switches this off in favour of ordinary
# verification, which is better still.
cert_fingerprint() {
  local out
  out=$(printf '' | "$OPENSSL" s_client -connect "${GW_HOST}:${GW_API_PORT}" \
          -servername "${GW_HOST}" 2>/dev/null |
        "$OPENSSL" x509 -noout -fingerprint -sha256 2>/dev/null) || return 1
  out=${out#*=}
  [ -n "$out" ] || return 1
  printf '%s' "$out" | tr -d ' ' | tr 'a-f' 'A-F'
}

pinned_fingerprint() {
  [ -f "$PIN_FILE" ] || return 1
  local val
  val=$(grep -E "^${GW_HOST}[[:space:]]" "$PIN_FILE" 2>/dev/null | tail -1) || return 1
  [ -n "$val" ] || return 1
  printf '%s' "${val#* }" | tr -d ' \t'
}

store_fingerprint() {
  local fp="$1" tmp
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  tmp="${PIN_FILE}.tmp"
  { [ -f "$PIN_FILE" ] && grep -vE "^${GW_HOST}[[:space:]]" "$PIN_FILE" 2>/dev/null; :; } > "$tmp"
  printf '%s %s\n' "$GW_HOST" "$fp" >> "$tmp"
  mv -f "$tmp" "$PIN_FILE"
  chmod 0644 "$PIN_FILE" 2>/dev/null || true
}

# Sets CURL_TLS_ARGS. Returns non-zero when the gateway's identity cannot be
# established, which must stop everything - a mis-trusted gateway is a machine
# we are about to hand every credential and every message to.
resolve_tls() {
  CURL_TLS_ARGS=()
  if [ -n "${GW_CACERT:-}" ]; then
    if [ ! -f "$GW_CACERT" ]; then
      fail cacert-missing "GATEWAY_CACERT points at ${GW_CACERT}, which does not exist."
      return 1
    fi
    CURL_TLS_ARGS=(--cacert "$GW_CACERT")
    mark TLS mode=cacert
    return 0
  fi

  local live pinned
  live=$(cert_fingerprint) || {
    fail unreachable "Nothing answered TLS on ${GW_HOST}:${GW_API_PORT}. Is the PMG VM up, and is :${GW_API_PORT} reachable from here?"
    return 1
  }
  pinned=$(pinned_fingerprint || printf '')

  if [ -z "$pinned" ]; then
    if [ "${KIN_PMG_TRUST_ON_FIRST_USE:-0}" = "1" ]; then
      store_fingerprint "$live"
      mark TLS mode=pinned-now fingerprint="$live"
      CURL_TLS_ARGS=(--insecure)
      return 0
    fi
    mark TLS mode=unpinned fingerprint="$live"
    fail certificate-not-trusted \
      "The gateway's certificate has not been confirmed yet. Fingerprint: ${live}. Confirm it in the console, or set GATEWAY_CACERT to a CA bundle."
    return 1
  fi

  if [ "$pinned" != "$live" ]; then
    mark TLS mode=mismatch expected="$pinned" got="$live"
    fail certificate-changed \
      "The gateway's certificate changed. Expected ${pinned}, got ${live}. If you replaced it on purpose, re-confirm it in the console; otherwise stop and find out why."
    return 1
  fi

  # Pinned and matching. --insecure here means "do not consult the CA store",
  # not "do not check" - the check already happened, above, against a
  # fingerprint this operator confirmed.
  CURL_TLS_ARGS=(--insecure)
  mark TLS mode=pinned fingerprint="$live"
  return 0
}

# -----------------------------------------------------------------------------
# PMG REST API
# -----------------------------------------------------------------------------
API_BASE=""
CURL_TLS_ARGS=()
AUTH_HEADER=""
CSRF_HEADER=""

api_login() {
  API_BASE="https://${GW_HOST}:${GW_API_PORT}/api2/json"
  if [ "${GW_AUTH:-token}" = "token" ]; then
    AUTH_HEADER="Authorization: PMGAPIToken=${GW_TOKEN_ID}=${KIN_PMG_TOKEN_SECRET}"
    CSRF_HEADER=""
    mark AUTH mode=token id="${GW_TOKEN_ID}"
    return 0
  fi

  local body ticket csrf
  body=$("$CURL" -sS --max-time 20 "${CURL_TLS_ARGS[@]}" \
    -d "username=${GW_USER}" --data-urlencode "password=${KIN_PMG_PASSWORD}" \
    "${API_BASE}/access/ticket" 2>/dev/null) || {
      fail auth-failed "Could not reach ${API_BASE}/access/ticket."
      return 1
    }
  ticket=$(json_field "$body" data ticket) || ticket=""
  csrf=$(json_field "$body" data CSRFPreventionToken) || csrf=""
  if [ -z "$ticket" ]; then
    fail auth-failed "${GW_USER} was refused by the gateway. Check the password, and that the realm is right."
    return 1
  fi
  AUTH_HEADER="Cookie: PMGAuthCookie=${ticket}"
  CSRF_HEADER="CSRFPreventionToken: ${csrf}"
  mark AUTH mode=ticket user="${GW_USER}"
  return 0
}

# json_field <json> <key> [key...] - walk a path, print the leaf, empty if absent.
json_field() {
  local doc="$1"; shift
  printf '%s' "$doc" | "$PYTHON" -c '
import json, sys
keys = sys.argv[1:]
try:
    cur = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for k in keys:
    if isinstance(cur, dict) and k in cur:
        cur = cur[k]
    else:
        sys.exit(1)
if isinstance(cur, (dict, list)):
    print(json.dumps(cur, separators=(",", ":")))
elif isinstance(cur, bool):
    print("1" if cur else "0")
elif cur is None:
    sys.exit(1)
else:
    print(cur)
' "$@" 2>/dev/null
}

# api_call <METHOD> <path> [field=value ...]
# Prints the response body. Returns non-zero on transport or HTTP failure and
# says which, because "it did not work" is not a diagnosis.
api_call() {
  local method="$1" path="$2"; shift 2
  local args=(-sS --max-time 30 -o /dev/stdout -w '\n%{http_code}' -X "$method")
  args+=("${CURL_TLS_ARGS[@]}" -H "$AUTH_HEADER")
  [ -n "$CSRF_HEADER" ] && args+=(-H "$CSRF_HEADER")
  local kv
  for kv in "$@"; do
    args+=(--data-urlencode "$kv")
  done
  local raw code body
  raw=$("$CURL" "${args[@]}" "${API_BASE}${path}" 2>/dev/null) || {
    API_ERROR="could not reach ${path}"
    return 1
  }
  code=$(printf '%s' "$raw" | tail -1)
  body=$(printf '%s' "$raw" | sed '$d')
  case "$code" in
    2*) API_BODY="$body"; printf '%s' "$body"; return 0 ;;
    401|403)
      API_ERROR="the gateway refused the credential on ${path} (HTTP ${code}); the token may be revoked or lack permission"
      return 1 ;;
    501|404)
      API_ERROR="the gateway has no ${path} (HTTP ${code}); this PMG version may be older than we support"
      return 1 ;;
    *)
      API_ERROR="the gateway answered HTTP ${code} on ${path}"
      return 1 ;;
  esac
}

api_get()  { api_call GET "$@"; }
api_put()  { api_call PUT "$@"; }
api_post() { api_call POST "$@"; }
api_del()  { api_call DELETE "$@"; }

# -----------------------------------------------------------------------------
# Zimbra side
# -----------------------------------------------------------------------------
# Zimbra's tools only work as the zimbra user. Tests inject a stub instead of
# su, so this is the one place that knows the difference.
zm() {
  if [ -n "$ZMPROV" ]; then
    "$ZMPROV" "$@"
    return $?
  fi
  local cmd="$*"
  su - zimbra -c "zmprov ${cmd}" 2>/dev/null
}

zimbra_available() {
  if [ -n "$ZMPROV" ]; then return 0; fi
  [ -x /opt/zimbra/bin/zmprov ] || [ -d /opt/zimbra ]
}

zimbra_relay_host() {
  zm gs "${MAIL_HOST}" zimbraMtaRelayHost 2>/dev/null |
    sed -n 's/^zimbraMtaRelayHost: //p' | head -1 | tr -d ' \r'
}

zimbra_mynetworks() {
  zm gacf zimbraMtaMyNetworks 2>/dev/null |
    sed -n 's/^zimbraMtaMyNetworks: //p' | tr '\n' ' '
}

zimbra_local_domains() {
  zm gad 2>/dev/null | tr -d ' \r'
}

# -----------------------------------------------------------------------------
# Reachability
# -----------------------------------------------------------------------------
# Every setting can be perfect and mail can still not move, because something
# between the two machines drops the packets. That is the commonest real
# failure when a gateway is introduced, and it is invisible in the API: PMG
# will happily accept a relay target it cannot reach, and Zimbra will happily
# queue for a relay host it cannot reach.
#
# bash's /dev/tcp rather than nc, which is not installed on a Zimbra node.
tcp_probe() {
  local host="$1" port="$2" secs="${3:-5}"
  if [ -n "${KIN_PMG_TCP_PROBE:-}" ]; then
    "$KIN_PMG_TCP_PROBE" "$host" "$port"
    return $?
  fi
  timeout "$secs" bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null
}

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
desired_relay() { printf '%s:%s' "$GW_HOST" "$PMG_INT_PORT"; }

cmd_probe() {
  load_config
  require_config || return 2
  resolve_tls || return 2
  api_login || return 2

  local body version
  body=$(api_get /version) || { fail api-failed "$API_ERROR"; return 2; }
  version=$(json_field "$body" data release) || version=$(json_field "$body" data version) || version="unknown"
  mark VERSION "$version"
  ok "Reached the gateway at ${GW_HOST}:${GW_API_PORT} and the credential works. PMG ${version}."

  # A credential that can read but not write is the most annoying way to
  # discover a permissions problem: half-way through apply. Find out now.
  if ! api_get /config/mail >/dev/null; then
    fail insufficient-permission "The credential cannot read /config/mail: ${API_ERROR}"
    return 2
  fi
  ok "The credential can read the mail-proxy configuration."
  return 0
}

# Prints one line per intended change. Reads both sides; writes nothing.
cmd_plan() {
  load_config
  require_config || return 2
  resolve_tls || return 2
  api_login || return 2

  local body changes=0
  body=$(api_get /config/mail) || { fail api-failed "$API_ERROR"; return 2; }

  plan_line() {
    local what="$1" now="$2" want="$3"
    if [ "$now" = "$want" ]; then
      info "unchanged  ${what} = ${want}"
    else
      printf 'KIN_GW_CHANGE %s from=%s to=%s\n' "$what" "${now:-<unset>}" "$want"
      changes=$((changes + 1))
    fi
  }

  say "On the gateway (${GW_HOST}):"
  plan_line pmg.relay        "$(json_field "$body" data relay || printf '')"        "$SERVER_IP"
  plan_line pmg.relayport    "$(json_field "$body" data relayport || printf '')"    "25"
  plan_line pmg.relaynomx    "$(json_field "$body" data relaynomx || printf '0')"   "1"
  plan_line pmg.int_port     "$(json_field "$body" data int_port || printf '')"     "$PMG_INT_PORT"
  plan_line pmg.ext_port     "$(json_field "$body" data ext_port || printf '')"     "$PMG_EXT_PORT"
  plan_line pmg.hide_received "$(json_field "$body" data hide_received || printf '0')" "1"

  local domains
  domains=$(api_get /config/domains) || domains=""
  if printf '%s' "$domains" | grep -q "\"${MAIL_DOMAIN}\""; then
    info "unchanged  pmg.relay-domain ${MAIL_DOMAIN} already listed"
  else
    printf 'KIN_GW_CHANGE %s from=%s to=%s\n' pmg.relay-domain "<absent>" "$MAIL_DOMAIN"
    changes=$((changes + 1))
  fi

  local admin
  admin=$(api_get /config/admin) || admin=""
  plan_line pmg.dkim_sign "$(json_field "$admin" data dkim_sign || printf '0')" "0"

  say "On this appliance:"
  if zimbra_available; then
    plan_line zimbra.relayhost "$(zimbra_relay_host)" "$(desired_relay)"
    local nets
    nets=$(zimbra_mynetworks)
    if printf ' %s ' "$nets" | grep -q " ${SERVER_IP}/32 \| ${GW_HOST}/32 "; then
      info "unchanged  zimbra.mynetworks already trusts the gateway"
    else
      printf 'KIN_GW_CHANGE %s from=%s to=%s\n' zimbra.mynetworks "$nets" "${nets} ${GW_HOST}/32"
      changes=$((changes + 1))
    fi
  else
    warn "Zimbra is not installed here, so the appliance side cannot be planned."
  fi

  mark PLAN changes="$changes"
  if [ "$changes" -eq 0 ]; then
    ok "Nothing to change. The gateway link is already configured as intended."
  else
    say ""
    say "Apply would make ${changes} change(s). Mail keeps flowing while it does;"
    say "nothing here restarts Zimbra or interrupts a delivery in progress."
  fi
  return 0
}

cmd_apply() {
  load_config
  require_config || return 2
  resolve_tls || return 2
  api_login || return 2

  # Order matters. The relay domain goes first: if the MX has already been
  # moved and PMG does not yet accept mail for the domain, every inbound
  # message is rejected with "relay access denied" for as long as the gap
  # lasts. Everything else can be late; this cannot.
  say "1. Teaching the gateway which domain it accepts mail for"
  local domains
  domains=$(api_get /config/domains) || { fail api-failed "$API_ERROR"; return 2; }
  if printf '%s' "$domains" | grep -q "\"${MAIL_DOMAIN}\""; then
    info "${MAIL_DOMAIN} already listed"
  else
    api_post /config/domains "domain=${MAIL_DOMAIN}" >/dev/null || {
      fail api-failed "Could not add the relay domain: ${API_ERROR}"; return 2; }
    ok "relay domain ${MAIL_DOMAIN}"
  fi

  say "2. Telling the gateway where filtered mail goes"
  # The transport entry is what actually routes the domain at this appliance.
  # use_mx=0 for the same reason as relaynomx: the MX for this domain now
  # points back at the gateway.
  #
  # This used to delete the entry and then create it. If the create failed -
  # a wrong field name, a gateway that rebooted mid-apply - the domain was
  # left with NO route at all, which is worse than the wrong route we started
  # with. Update in place instead, and only fall back to delete-then-create
  # when the update is genuinely not supported, re-checking afterwards that
  # something is actually there.
  local transports
  transports=$(api_get /config/transport) || transports=""
  if printf '%s' "$transports" | grep -q "\"${MAIL_DOMAIN}\""; then
    if api_put "/config/transport/${MAIL_DOMAIN}" \
         "host=${SERVER_IP}" "port=25" "protocol=smtp" "use_mx=0" >/dev/null; then
      ok "transport ${MAIL_DOMAIN} -> ${SERVER_IP}:25 (updated)"
    else
      warn "The gateway would not update the transport entry (${API_ERROR}); replacing it."
      api_del "/config/transport/${MAIL_DOMAIN}" >/dev/null 2>&1 || true
      if ! api_post /config/transport \
             "domain=${MAIL_DOMAIN}" "host=${SERVER_IP}" "port=25" \
             "protocol=smtp" "use_mx=0" >/dev/null; then
        fail api-failed \
          "Could not write the transport entry: ${API_ERROR}. The old entry was removed to replace it, so ${MAIL_DOMAIN} may now have NO route on the gateway. Fix this before mail arrives: Mail Proxy > Transports on the gateway."
        return 2
      fi
      ok "transport ${MAIL_DOMAIN} -> ${SERVER_IP}:25 (replaced)"
    fi
  else
    api_post /config/transport \
      "domain=${MAIL_DOMAIN}" "host=${SERVER_IP}" "port=25" \
      "protocol=smtp" "use_mx=0" >/dev/null || {
        fail api-failed "Could not write the transport entry: ${API_ERROR}"; return 2; }
    ok "transport ${MAIL_DOMAIN} -> ${SERVER_IP}:25"
  fi

  say "3. Letting this appliance relay out through the gateway"
  local nets
  nets=$(api_get /config/mynetworks) || nets=""
  if printf '%s' "$nets" | grep -q "\"${SERVER_IP}/32\""; then
    info "${SERVER_IP}/32 already trusted"
  else
    api_post /config/mynetworks "cidr=${SERVER_IP}/32" >/dev/null || {
      fail api-failed "Could not add ${SERVER_IP}/32 to the gateway's trusted networks: ${API_ERROR}"; return 2; }
    ok "trusted network ${SERVER_IP}/32"
  fi

  say "4. Mail routing"
  # Two writes, not one, and the split is deliberate.
  #
  # These five decide whether mail moves at all. They go alone so that one
  # unrecognised field in the policy set below cannot take the routing down
  # with it - PMG rejects a whole request when any parameter is invalid, and
  # a single combined PUT meant a typo in "hide_received" would silently
  # leave the relay unset.
  api_put /config/mail \
    "relay=${SERVER_IP}" \
    "relayport=25" \
    "relaynomx=1" \
    "int_port=${PMG_INT_PORT}" \
    "ext_port=${PMG_EXT_PORT}" >/dev/null || {
      fail api-failed "Could not write the mail routing settings: ${API_ERROR}"; return 2; }
  ok "relay ${SERVER_IP}:25, no MX lookup, internal port ${PMG_INT_PORT}"

  say "5. Mail policy"
  # Softer than the first draft of this, on purpose.
  #
  # verifyreceivers is 450, not 550. Both ask the mail server whether the
  # recipient exists; 550 refuses permanently and the sender never tries
  # again. If Zimbra is restarting, or LDAP is briefly unhappy, 550 throws
  # away real mail for good. 450 tells the sender to come back, which costs
  # a delay and loses nothing.
  #
  # rejectunknown is NOT set. It refuses senders whose client address has no
  # reverse DNS, and plenty of small but legitimate senders do not have it.
  # PMG's own default is off, and turning it on is a policy the customer
  # should choose in the PMG UI, not one an installer imposes on their mail.
  local policy_failed=0
  api_put /config/mail \
    "verifyreceivers=450" \
    "greylist=1" \
    "spf=1" \
    "tls=1" \
    "hide_received=1" \
    "before_queue_filtering=0" \
    "banner=ESMTP KIN Mail Gateway" >/dev/null || policy_failed=1

  if [ "$policy_failed" -eq 1 ]; then
    # Mail still flows without these. Say so honestly rather than failing the
    # whole apply and leaving the operator thinking nothing worked.
    warn "The gateway refused the policy settings: ${API_ERROR}"
    info "Mail routing is configured and mail will flow. Set spam policy, TLS and"
    info "the banner by hand under Mail Proxy > Options on the gateway."
  else
    ok "recipient verification (soft), greylisting, SPF, outbound TLS, no header leak"
  fi

  say "6. Making sure the gateway does not sign DKIM a second time"
  api_put /config/admin "dkim_sign=0" >/dev/null || {
    fail api-failed "Could not turn off DKIM signing on the gateway: ${API_ERROR}"; return 2; }
  ok "gateway DKIM signing off (Zimbra signs; two signatures fail DMARC at some receivers)"

  if ! zimbra_available; then
    warn "Zimbra is not installed here. The gateway is configured; the appliance side is not."
    return 1
  fi

  say "7. Pointing this appliance's outbound mail at the gateway"
  local want cur
  want=$(desired_relay)
  cur=$(zimbra_relay_host)
  if [ "$cur" = "$want" ]; then
    info "zimbraMtaRelayHost already ${want}"
  else
    zm ms "${MAIL_HOST}" zimbraMtaRelayHost "${want}" >/dev/null 2>&1 || {
      fail zimbra-failed "zmprov would not set zimbraMtaRelayHost to ${want}."; return 2; }
    ok "zimbraMtaRelayHost ${want}"
  fi

  say "8. Trusting the gateway to hand mail in"
  local nets_now
  nets_now=$(zimbra_mynetworks)
  if printf ' %s ' "$nets_now" | grep -q " ${GW_HOST}/32 "; then
    info "zimbraMtaMyNetworks already trusts ${GW_HOST}"
  else
    # Added, never replaced. Whatever the operator already relays for stays,
    # and the open-relay guard in stage 09 still judges the result.
    zm mcf "+zimbraMtaMyNetworks" "${GW_HOST}/32" >/dev/null 2>&1 || {
      fail zimbra-failed "zmprov would not add ${GW_HOST}/32 to zimbraMtaMyNetworks."; return 2; }
    ok "zimbraMtaMyNetworks += ${GW_HOST}/32"
  fi

  say "9. Reloading Postfix so the new relay takes effect"
  # zmmtactl reload is a configuration reload, not a restart: connections in
  # flight are not dropped and the queue is untouched.
  if [ -n "$ZMPROV" ]; then
    info "(stubbed in tests)"
  else
    su - zimbra -c 'zmmtactl reload' >/dev/null 2>&1 ||
      warn "Postfix did not reload cleanly; run 'su - zimbra -c \"zmmtactl reload\"' and check."
  fi

  say "10. Reading back the settings that matter"
  # Writing a setting and the setting being set are different things, and the
  # two below are the ones that silently destroy mail. Checking them now costs
  # one API call and means "applied" is a claim we have evidence for.
  local check_mail check_admin drift=0
  check_mail=$(api_get /config/mail) || check_mail=""
  if [ "$(json_field "$check_mail" data relaynomx || printf '0')" != "1" ]; then
    warn "The gateway did not keep relaynomx=1. Left as it is, mail for this domain will loop."
    drift=1
  fi
  if [ "$(json_field "$check_mail" data relay || printf '')" != "$SERVER_IP" ]; then
    warn "The gateway did not keep relay=${SERVER_IP}. Filtered mail has nowhere to go."
    drift=1
  fi
  check_admin=$(api_get /config/admin) || check_admin=""
  if [ "$(json_field "$check_admin" data dkim_sign || printf '0')" != "0" ]; then
    warn "The gateway is still signing DKIM. Two signatures fail DMARC at some receivers."
    drift=1
  fi
  if [ "$drift" -eq 1 ]; then
    fail settings-did-not-stick \
      "The gateway accepted the writes but is not reporting them back. Do not move the MX record yet. Check Mail Proxy > Relaying on the gateway by hand."
    return 2
  fi
  ok "the gateway reports back what we asked it to be"

  say "11. Can the two machines actually reach each other"
  if tcp_probe "$GW_HOST" "$PMG_INT_PORT"; then
    ok "this appliance can reach ${GW_HOST}:${PMG_INT_PORT} (outbound mail has a path)"
  else
    warn "This appliance cannot open ${GW_HOST}:${PMG_INT_PORT}. Outbound mail will queue here."
    info "Check the gateway is running and that nothing between the two blocks port ${PMG_INT_PORT}."
  fi

  record_state applied
  say ""
  ok "Gateway link applied."
  say ""
  say "DNS still has to change, and only you can do that:"
  say "  MX   ${MAIL_DOMAIN}.  10  <gateway hostname>"
  say "  SPF  ${MAIL_DOMAIN}.  TXT \"v=spf1 ip4:${GW_HOST} ~all\""
  say "  PTR  ${GW_HOST} -> the gateway's hostname"
  say ""
  say "Then run Verify. Until the MX moves, inbound mail still arrives directly"
  say "and the gateway only handles what leaves."
  return 0
}

cmd_verify() {
  load_config
  require_config || return 2
  resolve_tls || return 2
  api_login || return 2

  problems=0
  local body admin

  say "Gateway side"
  body=$(api_get /config/mail) || { fail api-failed "$API_ERROR"; return 2; }

  assert_eq() {
    local what="$1" got="$2" want="$3" why="$4"
    if [ "$got" = "$want" ]; then
      ok "${what} = ${want}"
    else
      note_problem
      printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' "$what" "$want" "${got:-<unset>}"
      info "$why"
    fi
  }

  assert_eq pmg.relay "$(json_field "$body" data relay || printf '')" "$SERVER_IP" \
    "Filtered mail has nowhere to go. Inbound mail will sit in the gateway's queue."
  assert_eq pmg.relaynomx "$(json_field "$body" data relaynomx || printf '0')" "1" \
    "With MX lookup on, the gateway looks up this domain's MX - which now points at the gateway. That is a loop."
  assert_eq pmg.int_port "$(json_field "$body" data int_port || printf '')" "$PMG_INT_PORT" \
    "This appliance relays out on this port. If it moves, outbound mail stops."

  local domains
  domains=$(api_get /config/domains) || domains=""
  if printf '%s' "$domains" | grep -q "\"${MAIL_DOMAIN}\""; then
    ok "pmg.relay-domain ${MAIL_DOMAIN} accepted"
  else
    note_problem
    printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' pmg.relay-domain "$MAIL_DOMAIN" "<absent>"
    info "The gateway will answer 'relay access denied' to every message for this domain."
  fi

  local nets
  nets=$(api_get /config/mynetworks) || nets=""
  if printf '%s' "$nets" | grep -q "\"${SERVER_IP}/32\""; then
    ok "pmg.mynetworks trusts ${SERVER_IP}"
  else
    note_problem
    printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' pmg.mynetworks "${SERVER_IP}/32" "<absent>"
    info "Outbound mail from this appliance will be refused at port ${PMG_INT_PORT}."
  fi

  admin=$(api_get /config/admin) || admin=""
  assert_eq pmg.dkim_sign "$(json_field "$admin" data dkim_sign || printf '0')" "0" \
    "Zimbra already signed. A second signature makes some receivers fail DMARC."

  say "Appliance side"
  if zimbra_available; then
    assert_eq zimbra.relayhost "$(zimbra_relay_host)" "$(desired_relay)" \
      "Outbound mail is not going through the gateway at all."

    local zn
    zn=$(zimbra_mynetworks)
    if printf ' %s ' "$zn" | grep -q " ${GW_HOST}/32 "; then
      ok "zimbra.mynetworks trusts ${GW_HOST}"
    else
      note_problem
      printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' zimbra.mynetworks "${GW_HOST}/32" "$zn"
      info "The gateway cannot hand mail in; every inbound message will be deferred."
    fi

    # The loop that nothing else catches: if this domain is not local on
    # Zimbra, Zimbra relays it straight back out to its relay host, which is
    # the gateway, which sends it here again.
    if zimbra_local_domains | grep -qx "$MAIL_DOMAIN"; then
      ok "zimbra.local-domain ${MAIL_DOMAIN} is delivered locally"
    else
      note_problem
      printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' zimbra.local-domain "$MAIL_DOMAIN" "<not a local domain>"
      info "Mail for this domain would be relayed back to the gateway. That is a loop."
    fi
  else
    warn "Zimbra is not installed here; the appliance side could not be checked."
  fi

  say "Reachability"
  # A warning, not a problem: this runs from the appliance, and an operator
  # checking from elsewhere, or mid-maintenance on the gateway, should not see
  # a red verify for a link that is correctly configured.
  if tcp_probe "$GW_HOST" "$PMG_INT_PORT"; then
    ok "this appliance can reach ${GW_HOST}:${PMG_INT_PORT}"
  else
    warn "This appliance cannot open ${GW_HOST}:${PMG_INT_PORT}. Outbound mail will queue here."
  fi
  if tcp_probe "$GW_HOST" "$PMG_EXT_PORT"; then
    ok "the gateway is listening on ${PMG_EXT_PORT} for inbound mail"
  else
    warn "Nothing answers on ${GW_HOST}:${PMG_EXT_PORT}. Inbound mail has nowhere to arrive."
  fi

  say "DNS"
  check_dns

  mark VERIFY problems="$problems"
  if [ "$problems" -eq 0 ]; then
    ok "Everything the gateway link depends on is in place."
    return 0
  fi
  say ""
  say "${problems} problem(s) above. Each one is a way mail stops; none of them"
  say "are cosmetic. Fix them and run Verify again."
  return 1
}

# DNS lives at a registrar. We cannot fix it, so we say precisely what is wrong
# and precisely what to set - a warning, not a failure, because an operator may
# legitimately be mid-migration with the MX not yet moved.
check_dns() {
  command -v "$DIG" >/dev/null 2>&1 || { warn "dig is not installed; DNS was not checked."; return 0; }

  local mx spf
  mx=$("$DIG" +short +time=5 MX "$MAIL_DOMAIN" 2>/dev/null | awk '{print $2}' | sed 's/\.$//' | tr '\n' ' ')
  if [ -z "$mx" ]; then
    warn "No MX record for ${MAIL_DOMAIN} could be resolved from here."
  elif printf ' %s ' "$mx" | grep -q " ${GW_HOST} "; then
    ok "dns.mx points at the gateway"
  else
    warn "dns.mx for ${MAIL_DOMAIN} is '${mx}', not the gateway. Inbound mail still bypasses it."
    info "Set:  ${MAIL_DOMAIN}.  MX  10  <gateway hostname>"
  fi

  spf=$("$DIG" +short +time=5 TXT "$MAIL_DOMAIN" 2>/dev/null | tr -d '"' | grep -i 'v=spf1' | head -1)
  if [ -z "$spf" ]; then
    warn "No SPF record for ${MAIL_DOMAIN}. Outbound mail from the gateway will be treated as suspicious."
    info "Set:  ${MAIL_DOMAIN}.  TXT  \"v=spf1 ip4:${GW_HOST} ~all\""
  elif printf '%s' "$spf" | grep -q "$GW_HOST"; then
    ok "dns.spf authorises the gateway"
  else
    warn "SPF for ${MAIL_DOMAIN} does not mention the gateway (${GW_HOST}). Outbound mail now leaves from there."
    info "Current: ${spf}"
    info "Add:     ip4:${GW_HOST}"
  fi
  return 0
}

cmd_revert() {
  load_config
  if [ -z "${MAIL_HOST:-}" ]; then
    fail no-mail-host "This appliance has no MAIL_HOST; nothing to revert."
    return 2
  fi
  if ! zimbra_available; then
    fail no-zimbra "Zimbra is not installed here; nothing to revert."
    return 2
  fi

  say "Removing the relay host so this appliance sends directly again"
  zm ms "${MAIL_HOST}" zimbraMtaRelayHost '' >/dev/null 2>&1 ||
    warn "Could not clear zimbraMtaRelayHost; check it by hand."
  if [ -n "${GW_HOST:-}" ]; then
    zm mcf "-zimbraMtaMyNetworks" "${GW_HOST}/32" >/dev/null 2>&1 || true
  fi
  [ -n "$ZMPROV" ] || su - zimbra -c 'zmmtactl reload' >/dev/null 2>&1 || true

  record_state reverted
  ok "Reverted the appliance side."
  say ""
  say "The gateway is left configured; re-applying is then instant."
  say ""
  say "TWO THINGS STILL HAVE TO HAPPEN, and neither is something this can do:"
  say ""
  say "  1. Move the MX record back to ${MAIL_HOST}, or inbound mail still"
  say "     goes to a gateway this appliance no longer trusts."
  say "  2. Re-run 10-host-firewall.sh apply. While a gateway was linked, port"
  say "     25 was narrowed to accept mail from it only. Until that is re-run,"
  say "     this appliance will refuse inbound mail from the internet."
  say ""
  say "Leaving either half-done stops mail. Do them together."
  return 0
}

record_state() {
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  local f="${STATE_DIR}/mail-gateway-state.json"
  "$PYTHON" - "$f" "$1" "${GW_HOST:-}" "${MAIL_DOMAIN:-}" <<'PY' 2>/dev/null || true
import json, sys, time, os
path, phase, host, domain = sys.argv[1:5]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({
        "phase": phase,
        "gateway_host": host,
        "domain": domain,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, fh)
os.replace(tmp, path)
PY
}

cmd_status() {
  load_config
  "$PYTHON" - "${GW_ENABLED:-0}" "${GW_HOST:-}" "${GW_AUTH:-token}" \
    "${STATE_DIR}/mail-gateway-state.json" "${MAIL_DOMAIN:-}" "${PIN_FILE}" <<'PY'
import json, os, sys
enabled, host, auth, statefile, domain, pinfile = sys.argv[1:7]
state = {}
try:
    with open(statefile, encoding="utf-8") as fh:
        state = json.load(fh)
except Exception:
    state = {}
pinned = False
try:
    with open(pinfile, encoding="utf-8") as fh:
        pinned = any(line.split(" ")[0] == host for line in fh if line.strip())
except Exception:
    pinned = False
print("KIN_GW_STATUS " + json.dumps({
    "enabled": enabled == "1",
    "gateway_host": host,
    "auth_mode": auth,
    "domain": domain,
    "certificate_pinned": pinned,
    "phase": state.get("phase", "never-applied"),
    "applied_at": state.get("at", ""),
    "configured": bool(host) and enabled == "1",
}, separators=(",", ":")))
PY
  return 0
}

# -----------------------------------------------------------------------------
usage() {
  sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  local action="${1:-}"
  case "$action" in
    probe|plan|apply|verify|revert)
      if [ "${KIN_PMG_ALLOW_NONROOT:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
        fail not-root "This has to run as root: it reads the credential vault and talks to Zimbra."
        return 2
      fi
      ;;
  esac
  case "$action" in
    probe)  cmd_probe ;;
    plan)   cmd_plan ;;
    apply)  cmd_apply ;;
    verify) cmd_verify ;;
    revert) cmd_revert ;;
    status) cmd_status ;;
    ''|-h|--help|help) usage; return 0 ;;
    *) fail unknown-action "No such action '${action}'. Try: probe, plan, apply, verify, revert, status."; return 2 ;;
  esac
}

main "$@"
