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

# --- the tuning profile ------------------------------------------------------
# Every value here differs from stock PMG, and every one is a trade. The
# reasoning lives next to the setting that uses it; these are the numbers.
#
# Sizes are deliberately generous. In a scanner, a limit is not caution: data
# past the limit is skipped UNSCANNED and delivered. The cost of a bigger limit
# is CPU; the cost of a smaller one is a hole.
PMG_MAX_MESSAGE_BYTES="${KIN_PMG_MAX_MESSAGE_BYTES:-52428800}"      # 50 MiB accepted
PMG_MAX_SPAM_SCAN_BYTES="${KIN_PMG_MAX_SPAM_SCAN_BYTES:-524288}"    # 512 KiB scored (stock 256 KiB)
PMG_MAX_SCAN_SIZE="${KIN_PMG_MAX_SCAN_SIZE:-157286400}"             # 150 MB total per file
PMG_ARCHIVE_MAX_SIZE="${KIN_PMG_ARCHIVE_MAX_SIZE:-52428800}"        # 50 MB per archived file
PMG_ARCHIVE_MAX_RECURSION="${KIN_PMG_ARCHIVE_MAX_RECURSION:-8}"     # zip-in-zip depth (stock 5)
PMG_ARCHIVE_MAX_FILES="${KIN_PMG_ARCHIVE_MAX_FILES:-5000}"          # files per archive (stock 1000)
# Encrypted archives and the like become heuristic hits; this is the spam score
# they carry. 5 puts them over a default quarantine threshold without being an
# automatic block.
PMG_CLAMAV_HEURISTIC_SCORE="${KIN_PMG_CLAMAV_HEURISTIC_SCORE:-5}"
# Long enough that a false positive survives somebody's holiday.
PMG_SPAMQUAR_DAYS="${KIN_PMG_SPAMQUAR_DAYS:-14}"
# Virus quarantine is evidence, and evidence is cheap to keep.
PMG_VIRUSQUAR_DAYS="${KIN_PMG_VIRUSQUAR_DAYS:-30}"
# No DNSBL by default, and this is the most expensive lesson in this file.
#
# A DNSBL in PMG's mail settings is not "one more signal". It is a HARD SMTP
# REJECT in postscreen, before any scoring happens, and Postfix treats ANY
# answer in 127.0.0.0/8 as a listing.
#
# Spamhaus refuses queries that arrive through a public resolver, and it
# refuses them by ANSWERING - 127.255.255.254, "Error: open resolver". So on
# an appliance whose gateway resolves through Google or Cloudflare DNS, every
# sender on earth looks listed and every inbound message is rejected:
#
#   NOQUEUE: reject: RCPT from [...]: 550 5.7.1 Service unavailable;
#   client [...] blocked using zen.spamhaus.org
#
# That is what happened on a live deployment (QA Phase 16, 12 Sep 2026), and
# it looked like the sender's fault. Verified from the workstation: the same
# 127.255.255.254 came back for 8.8.8.8, which is obviously not a spam source.
#
# SpamAssassin's own blocklist checks (rbl_checks, set below) consult the same
# data and only add SCORE. A wrong answer there costs a few points; a wrong
# answer here costs the message. So: scoring yes, hard reject no.
#
# An operator with a local recursive resolver can opt in, and when they do the
# site is written with a return-code filter so an error answer can never be
# read as a listing.
PMG_DNSBL_SITES="${KIN_PMG_DNSBL_SITES:-}"
# Only these answers mean "listed". Spamhaus's error range (127.255.255.x)
# is deliberately outside it.
PMG_DNSBL_RETURN_FILTER="${KIN_PMG_DNSBL_RETURN_FILTER:-127.0.0.[2..11]}"

# How long the gateway keeps trying a message it cannot deliver yet.
#
# The default is Postfix's 5 days. That is a long time for the appliance behind
# it to be down and nobody to have noticed, and it is also the window in which
# a temporary problem quietly becomes a permanent one. 7 days is deliberately
# LONGER, not shorter: a gateway that gives up during a weekend outage bounces
# mail that would have been delivered on Monday, and a bounce cannot be undone.
PMG_QUEUE_LIFETIME_DAYS="${KIN_PMG_QUEUE_LIFETIME_DAYS:-7}"
# How long before the sender is told their message is still in the queue. Four
# hours is long enough not to alarm anyone about a brief blip, short enough that
# a real outage is visible to the people affected rather than only to us.
PMG_DELAY_WARNING_HOURS="${KIN_PMG_DELAY_WARNING_HOURS:-4}"

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
  # Username and password by default. An API token is the tidier credential,
  # but PMG's token permission model surprised an operator into a half-working
  # link, and a root@pam login is the one thing every PMG install definitely
  # has. The token path stays supported for anyone who prefers it.
  GW_AUTH=$(conf_get GATEWAY_AUTH "$GW_CONF" || printf 'ticket')
  GW_TOKEN_ID=$(conf_get GATEWAY_TOKEN_ID "$GW_CONF" || printf '')
  GW_USER=$(conf_get GATEWAY_USER "$GW_CONF" || printf 'root@pam')
  GW_CACERT=$(conf_get GATEWAY_CACERT "$GW_CONF" || printf '')
  GW_NATIVE_FILTERING=$(conf_get GATEWAY_NATIVE_FILTERING "$GW_CONF" || printf 'keep')

  # The gateway has two identities and conflating them publishes a private
  # address in SPF.
  #
  # GATEWAY_HOST is where this appliance TALKS to it: usually a LAN address,
  # because the two machines sit on the same internal network and the console
  # reaches the API over it. GATEWAY_PUBLIC_HOST and GATEWAY_PUBLIC_IP are
  # what the INTERNET sees, and they are the only values that belong in DNS.
  #
  # The first version of this used GATEWAY_HOST for both, so a deployment
  # with the gateway on 10.x was told to publish "v=spf1 ip4:10.x" - which
  # authorises nothing, and would have made every outbound message fail SPF
  # (live QA Phase 15, 11 Sep 2026).
  GW_PUBLIC_HOST=$(conf_get GATEWAY_PUBLIC_HOST "$GW_CONF" || printf '')
  GW_PUBLIC_IP=$(conf_get GATEWAY_PUBLIC_IP "$GW_CONF" || printf '')

  # Greylisting defers the first message from any unseen sender triplet, and
  # PMG's delay is hardcoded at a few minutes. It is genuinely good at cheap
  # spam - and it is why inbound mail felt slow while outbound felt instant.
  # Off by default: SpamAssassin, ClamAV, SPF and the DNSBLs all still run,
  # and a mail system that takes five minutes to deliver the first message
  # from a new correspondent is a mail system people complain about.
  GW_GREYLIST=$(conf_get GATEWAY_GREYLIST "$GW_CONF" || printf '0')
  case "$GW_GREYLIST" in 0|1) ;; *) GW_GREYLIST=0 ;; esac

  # Opt-in only, and see the note beside PMG_DNSBL_SITES for why. A blocklist
  # here rejects at SMTP; getting it wrong rejects everything.
  GW_DNSBL=$(conf_get GATEWAY_DNSBL "$GW_CONF" || printf '')

  [ -n "$GW_API_PORT" ] || GW_API_PORT=$PMG_API_PORT_DEFAULT

  MAIL_DOMAIN=$(conf_get MAIL_DOMAIN "$APPLIANCE_CONF" || printf '')
  MAIL_HOST=$(conf_get MAIL_HOST "$APPLIANCE_CONF" || printf '')
  SERVER_IP=$(conf_get SERVER_IP "$APPLIANCE_CONF" || printf '')

  # Which machine actually carries mail for the gateway.
  #
  # Everything below wants one address: the host the gateway hands filtered
  # mail to, and the host that relays outbound mail back through it. On a
  # single appliance that is the appliance, and SERVER_IP is right.
  #
  # On a split it is the EDGE. The mailbox node runs no MTA at all - it has
  # zimbra-ldap and zimbra-store and nothing that speaks SMTP - so a gateway
  # pointed at it accepts mail from the internet and then cannot deliver any
  # of it. SERVER_IP on a split names whichever machine the config was written
  # on, which is not a useful answer to this question.
  #
  # Derived here, once, so no caller has to remember which topology it is in.
  TOPOLOGY=$(conf_get TOPOLOGY "$APPLIANCE_CONF" || printf '1vm')
  EDGE_IP=$(conf_get EDGE_IP "$APPLIANCE_CONF" || printf '')
  MAILBOX_IP=$(conf_get MAILBOX_IP "$APPLIANCE_CONF" || printf '')
  EDGE_HOST=$(conf_get EDGE_HOST "$APPLIANCE_CONF" || printf '')
  if [ "$TOPOLOGY" = "split" ]; then
    MTA_IP="$EDGE_IP"
    MTA_HOST="${EDGE_HOST:-$MAIL_HOST}"
  else
    MTA_IP="$SERVER_IP"
    MTA_HOST="$MAIL_HOST"
  fi
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

# Does this machine hold this address?
#
# Deliberately local rather than borrowed from lib/topology.sh: this helper is
# run by privhelperd with a fixed argv and does not source 00-config.sh, so it
# has no access to the stage helpers. KIN_GW_LOCAL_IPV4S overrides it for tests.
host_has_ipv4() {
  local want="${1:-}" addr list
  [ -n "$want" ] || return 1
  if [ -n "${KIN_GW_LOCAL_IPV4S:-}" ]; then
    list="$KIN_GW_LOCAL_IPV4S"
  else
    list=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  fi
  for addr in $list; do
    [ "$addr" = "$want" ] && return 0
  done
  return 1
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
  # On a split, the machine that carries mail is the edge, and everything below
  # is aimed at it rather than at whichever host the config was written on.
  if [ "${TOPOLOGY:-1vm}" = "split" ]; then
    if [ -z "${EDGE_IP:-}" ]; then
      fail no-edge-ip "TOPOLOGY=split but EDGE_IP is not set in ${APPLIANCE_CONF}."
      info "The gateway hands mail to the edge; without its address there is nowhere to send it."
      bad=1
    elif ! valid_ipv4 "$EDGE_IP"; then
      fail bad-edge-ip "EDGE_IP in ${APPLIANCE_CONF} is '${EDGE_IP}', which is not an IPv4 address."
      bad=1
    fi
    # Refuse on the mailbox node, rather than configuring a gateway to deliver
    # to a machine with no MTA.
    #
    # The mailbox runs zimbra-ldap and zimbra-store; nothing there speaks SMTP.
    # A link applied from here would set zimbraMtaRelayHost on a host with no
    # Postfix, and point the gateway's transport at a port nothing is listening
    # on - so the gateway would accept mail from the internet and then be
    # unable to deliver a single message.
    if [ -n "${MAILBOX_IP:-}" ] && host_has_ipv4 "$MAILBOX_IP" \
       && ! host_has_ipv4 "${EDGE_IP:-}"; then
      fail wrong-node \
        "This is the mailbox node of a split. The mail gateway is linked from the EDGE, which is the machine that runs the MTA."
      info "Open the console on ${EDGE_HOST:-the edge} (${EDGE_IP:-unknown}) and link it there."
      bad=1
    fi
  fi

  # The gateway must not be this machine. That configuration is a mail loop
  # with extra steps, and it is an easy typo to make.
  if [ -n "${GW_HOST:-}" ] && [ "${GW_HOST}" = "${MTA_IP:-}" ]; then
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
# Why a file and not a variable.
#
# Half this script reads the API as `body=$(api_get /config/mail)`, and command
# substitution runs in a SUBSHELL. An error message assigned to a variable in
# there is gone by the time the caller looks at it, so every failing GET died
# with "API_ERROR: unbound variable" instead of saying what the gateway
# answered - a crash on exactly the path that exists to explain a failure.
# A file outlives the subshell.
API_ERROR_FILE="${TMPDIR:-/tmp}/kin-gw-api-error.$$"
API_ERROR=""
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

# Record and read the last API error. Written to a file so it survives the
# subshell a command substitution creates.
api_set_error() {
  API_ERROR="$1"
  printf '%s\n' "$1" > "$API_ERROR_FILE" 2>/dev/null || true
}

api_error() {
  if [ -s "$API_ERROR_FILE" ]; then
    head -1 "$API_ERROR_FILE"
    return 0
  fi
  printf '%s' "${API_ERROR:-no reason given}"
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
    api_set_error "could not reach ${path}"
    return 1
  }
  code=$(printf '%s' "$raw" | tail -1)
  body=$(printf '%s' "$raw" | sed '$d')
  case "$code" in
    2*) API_BODY="$body"; printf '%s' "$body"; return 0 ;;
    401|403)
      api_set_error "the gateway refused the credential on ${path} (HTTP ${code}); the token may be revoked or lack permission"
      return 1 ;;
    501|404)
      api_set_error "the gateway has no ${path} (HTTP ${code}); this PMG version may be older than we support"
      return 1 ;;
    *)
      api_set_error "the gateway answered HTTP ${code} on ${path}"
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
# The two failures nobody goes looking for
# -----------------------------------------------------------------------------

# 1. The gateway quietly becoming an open relay.
#
# Anything in the gateway's trusted networks may send through it WITHOUT
# authenticating. We add exactly one /32. But an operator debugging a delivery
# problem at 2am adds "the office range", or a /16, or 0.0.0.0/0, and the
# gateway is then a spam cannon with the company's name on it. Nothing warns
# them: mail keeps flowing, and the first symptom is the sending address on
# every blocklist a week later, which takes months to undo.
#
# Zimbra's own networks are already guarded this way in stage 09. The gateway
# was not, and the gateway is the machine actually facing the internet.
check_relay_scope() {
  local nets bad
  nets=$(api_get /config/mynetworks) || {
    warn "Could not read the gateway's trusted networks."
    return 0
  }
  bad=$(printf '%s' "$nets" | "$PYTHON" -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or []
except Exception:
    sys.exit(0)
wide = []
for row in data:
    cidr = str((row or {}).get("cidr") or "") if isinstance(row, dict) else str(row)
    if not cidr:
        continue
    if cidr in ("0.0.0.0/0", "::/0"):
        wide.append(cidr)
        continue
    if "/" not in cidr:
        continue
    net, _, prefix = cidr.partition("/")
    if not prefix.isdigit():
        continue
    bits = int(prefix)
    if ":" in net:
        if bits < 64:
            wide.append(cidr)
    elif bits < 24 and not net.startswith("127."):
        wide.append(cidr)
print(" ".join(wide))
' 2>/dev/null) || bad=""

  bad=$(printf '%s' "$bad" | tr -d '\n')
  if [ -z "$bad" ]; then
    ok "gateway.relay-scope is tight; nothing can relay through it unauthenticated except this appliance"
    return 0
  fi
  note_problem
  printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' gateway.relay-scope "host or LAN sized" "$bad"
  info "Anything in that range can send mail through this gateway without a password."
  info "That is an open relay. It keeps working perfectly until the sending address"
  info "is blocklisted everywhere, and undoing that takes months."
  info "Narrow it under Mail Proxy > Networks on the gateway."
  return 0
}

# 2. The gateway running out of disk.
#
# A PMG whose filesystem fills stops accepting mail outright, and the queue and
# the quarantine are what fill it. This release deliberately LENGTHENED the
# quarantine - spam 14 days, viruses 30 - which is the right call for recovering
# a false positive and is also more disk than the defaults asked for. Having
# made that trade, it would be careless not to check the machine can afford it.
check_gateway_room() {
  local nodes node body pct
  nodes=$(api_get /nodes) || { warn "Could not ask the gateway about its disk."; return 0; }
  node=$(printf '%s' "$nodes" | "$PYTHON" -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or []
except Exception:
    sys.exit(0)
for row in data:
    if isinstance(row, dict) and row.get("name"):
        print(row["name"]); break
' 2>/dev/null) || node=""
  [ -n "$node" ] || { warn "Could not work out the gateway node name."; return 0; }

  body=$(api_get "/nodes/${node}/status") || {
    warn "Could not read disk usage from the gateway."
    return 0
  }
  pct=$(printf '%s' "$body" | "$PYTHON" -c '
import json, sys
try:
    d = json.load(sys.stdin).get("data") or {}
except Exception:
    sys.exit(0)
root = d.get("rootfs") or {}
total = float(root.get("total") or 0)
used = float(root.get("used") or 0)
if total <= 0:
    sys.exit(0)
print(int(round(used * 100 / total)))
' 2>/dev/null) || pct=""
  case "$pct" in ''|*[!0-9]*) warn "The gateway did not report its disk usage."; return 0 ;; esac

  if [ "$pct" -ge 90 ]; then
    note_problem
    printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' gateway.disk "below 90%" "${pct}%"
    info "A gateway with a full disk stops accepting mail altogether."
    info "The quarantine is usually what fills it: this release keeps spam for"
    info "${PMG_SPAMQUAR_DAYS} days and viruses for ${PMG_VIRUSQUAR_DAYS}. Give the VM more disk, or shorten"
    info "those under Configuration > Spam Quarantine on the gateway."
  elif [ "$pct" -ge 75 ]; then
    warn "The gateway's disk is ${pct}% full. At 100% it stops accepting mail."
    info "Quarantine retention is ${PMG_SPAMQUAR_DAYS} days for spam and ${PMG_VIRUSQUAR_DAYS} for viruses."
  else
    ok "gateway.disk is ${pct}% full, with room for the quarantine this release keeps"
  fi
  return 0
}

# -----------------------------------------------------------------------------
# Blocklists that reject at the door
# -----------------------------------------------------------------------------
# Separate from the rest of the policy because this one setting can refuse all
# mail, and because a gateway already carrying a bad value has to be repaired,
# not just left alone. Re-applying is how an operator fixes things.
apply_dnsbl() {
  local want="${GW_DNSBL:-$PMG_DNSBL_SITES}"

  if [ -z "$want" ]; then
    # Clear it, every time. An appliance configured by an earlier version of
    # this script is carrying zen.spamhaus.org and rejecting its own inbound
    # mail; pressing Apply again has to be the fix.
    if api_put /config/mail "delete=dnsbl_sites" >/dev/null; then
      ok "no SMTP-level blocklist - senders are scored, never refused at the door"
      info "SpamAssassin still consults the same blocklists; a wrong answer there"
      info "costs a few points instead of the whole message."
    else
      warn "Could not clear the SMTP blocklist setting: $(api_error)"
      info "Check Mail Proxy > Options > DNSBL Sites on the gateway is empty."
    fi
    return 0
  fi

  # The operator asked for one. Pin the answers that count as a listing, so an
  # error reply - Spamhaus answers 127.255.255.254 through a public resolver -
  # can never be read as "this sender is spam".
  local site="$want"
  case "$want" in
    *=*) ;;  # the operator supplied their own filter; respect it
    *) site="${want}=${PMG_DNSBL_RETURN_FILTER}" ;;
  esac
  if api_put /config/mail "dnsbl_sites=${site}" "dnsbl_threshold=1" >/dev/null; then
    ok "SMTP blocklist ${site}"
    warn "This REJECTS mail at the door. It needs a local recursive resolver on"
    info "the gateway: Spamhaus answers every query with an error code when it"
    info "is asked through a public resolver, and every sender then looks listed."
  else
    warn "Could not set the SMTP blocklist: $(api_error)"
  fi
  return 0
}

# -----------------------------------------------------------------------------
# The rule database
# -----------------------------------------------------------------------------
# PMG ships a factory rule set, and the rules are what actually act on a
# verdict: scoring a message as a virus does nothing unless a rule says to
# quarantine it. A gateway whose protective rules are switched off filters
# beautifully and delivers everything anyway.
#
# Deliberately tolerant. The rule database is the one part of PMG a customer
# legitimately customises, and an installer that rewrites it would throw away
# their work. This looks, activates the factory rules that are merely switched
# off, and otherwise reports. It never creates, edits or deletes a rule.
# What PMG's factory rules do, and which of them we are willing to switch on.
#
# The first version of this matched rule names by keyword - anything containing
# "spam", "virus" or "dangerous". On a real gateway (PMG 9.1) that set included
# "Block Spam (Level 10)", which does not quarantine a message, it destroys it.
# An installer has no business turning on a rule that deletes a customer's mail
# on a score, and the operator had asked for exactly the opposite: no false
# positives.
#
# So this is an explicit list with a decision and a reason for each, and
# anything not named here is reported and left alone. A rule we have not
# thought about is a rule we do not touch.
#
# Format: name<TAB>verdict<TAB>why
#   enable   safe to switch on: the worst case is a message waiting in
#            quarantine, where somebody can look at it and release it
#   expect   should already be on; warn loudly if it is not
#   leave    deliberately not ours to decide - reported, never changed
RULE_POLICY=$(printf '%s\n' \
  "Block Viruses|expect|infected mail must not reach a mailbox" \
  "Virus Alert|expect|tells the sender and the admin that it was blocked" \
  "Block Dangerous Files|expect|executable attachments" \
  "Blocklist|expect|the operator's own blocklist" \
  "Welcomelist|expect|the operator's own welcomelist" \
  "Quarantine/Mark Spam (Level 3)|expect|marks likely spam without moving it" \
  "Quarantine/Mark Spam (Level 5)|enable|quarantines confident spam, and it can be released" \
  "Block Spam (Level 10)|leave|BLOCKS rather than quarantines - a false positive here is a message nobody can recover, and anything scoring 10 is already quarantined by the Level 5 rule" \
  "Block outgoing Spam|leave|protects this estate's sending reputation, and blocks your own users' mail when it misfires - the customer's call, not an installer's" \
  "Quarantine Office Files|leave|quarantines every Word and Excel attachment; on a business mail server that is an outage" \
  "Block Multimedia Files|leave|blocks audio and video attachments outright" \
  "Modify Header|leave|cosmetic header handling" \
  "Add Disclaimer|leave|a legal footer is a company decision" \
)

rule_verdict() {
  printf '%s\n' "$RULE_POLICY" | awk -F'|' -v n="$1" '$1==n {print $2; found=1} END {if(!found) print "unknown"}'
}

rule_reason() {
  printf '%s\n' "$RULE_POLICY" | awk -F'|' -v n="$1" '$1==n {print $3}'
}

check_rules() {
  local body
  body=$(api_get /config/ruledb/rules) || {
    warn "Could not read the gateway's rule database: $(api_error)"
    info "Check Configuration > Mail Filter by hand: virus, spam and dangerous"
    info "attachment rules must be active or nothing acts on what the scanners find."
    return 0
  }

  local summary
  summary=$(printf '%s' "$body" | "$PYTHON" -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or []
except Exception:
    sys.exit(1)
if not isinstance(data, list):
    sys.exit(1)
for rule in data:
    if not isinstance(rule, dict):
        continue
    rid = rule.get("id")
    name = str(rule.get("name") or "")
    active = 1 if str(rule.get("active", "0")) in ("1", "True", "true") else 0
    if rid is None or not name:
        continue
    # The shell reads these back as tab-separated fields, one rule per line.
    # A rule NAME is operator-supplied text: a tab or a newline in one would
    # silently split a rule into two, and the second half would be looked up
    # as an unknown rule carrying the wrong id. No apostrophes in here: this
    # whole block is a single-quoted shell string.
    name = name.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
    if not name:
        continue
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        continue
    print("%s\t%s\t%s" % (rid, active, name))
' 2>/dev/null) || summary=""

  if [ -z "$summary" ]; then
    warn "The gateway's rule database could not be read in a form we understand."
    info "Check Configuration > Mail Filter by hand."
    return 0
  fi

  local rid active name verdict reason protective=0 enabled=0
  while IFS="$(printf '\t')" read -r rid active name; do
    [ -n "${name:-}" ] || continue
    verdict=$(rule_verdict "$name")
    reason=$(rule_reason "$name")
    case "$verdict" in
      expect)
        protective=$((protective + 1))
        if [ "$active" = "1" ]; then
          ok "rule on: ${name}"
        else
          # Not switched on automatically even though we expect it: an
          # operator may have turned it off on purpose, and this is not the
          # place to overrule that silently.
          warn "rule OFF: ${name} - ${reason}"
          info "Nothing acts on what the scanners find for this case. Turn it on"
          info "under Configuration > Mail Filter if that was not deliberate."
        fi
        ;;
      enable)
        if [ "$active" = "1" ]; then
          ok "rule on: ${name}"
          protective=$((protective + 1))
        elif api_put "/config/ruledb/rules/${rid}/config" "active=1" >/dev/null; then
          ok "rule switched on: ${name} - ${reason}"
          enabled=$((enabled + 1))
          protective=$((protective + 1))
        else
          warn "rule OFF: ${name} - could not switch it on ($(api_error))"
          info "Turn it on under Configuration > Mail Filter."
        fi
        ;;
      leave)
        [ "$active" = "1" ] && info "rule on (yours): ${name}"
        ;;
      *)
        # A rule we have never seen. Say so and change nothing.
        [ "$active" = "1" ] && info "rule on (yours): ${name}" || info "rule off (yours): ${name}"
        ;;
    esac
  done <<EOF
$(printf '%s\n' "$summary")
EOF

  if [ "$protective" -eq 0 ]; then
    warn "No protective rule is active on this gateway."
    info "It will scan every message and then deliver all of them."
    info "Restore the factory rules under Configuration > Mail Filter."
  fi

  # Said explicitly, because it is a deliberate restraint and not an oversight.
  info "Rules that block rather than quarantine are left to you. A blocked"
  info "message is gone; a quarantined one can be released."
  return 0
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

# What the world should see.
#
# These do NOT fall back to the management address. They used to, and the
# warning that said "no public identity is recorded, this is a guess" was then
# followed by advice naming the LAN address anyway:
#
#   Set: example.com. MX 10 <the gateway's LAN address>
#   Add: ip4:<the gateway's LAN address>
#
# An operator who reads a warning and then finds a concrete instruction below
# it does the concrete thing. Published, that MX points at an address the
# internet cannot route to and that SPF authorises nobody (QA Phase 16,
# 12 Sep 2026). Empty is the honest answer; the callers print a placeholder.
public_name() { printf '%s' "${GW_PUBLIC_HOST:-}"; }
public_addr() { printf '%s' "${GW_PUBLIC_IP:-}"; }

# An address nobody on the internet can route to. Publishing one of these in
# SPF or pointing an MX at it is not a warning, it is a broken mail domain.
is_private_addr() {
  case "$1" in
    10.*|127.*|192.168.*|169.254.*) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    fc*:*|fd*:*|fe80:*|::1) return 0 ;;
  esac
  return 1
}

# An MX record cannot hold an address - only a name - so the DNS advice has
# to name something, and it must be a name that resolves publicly.
public_identity_usable() {
  local name addr
  name=$(public_name)
  addr=$(public_addr)
  [ -n "$name" ] || return 1
  [ -n "$addr" ] || return 1
  # A name, not an address: an MX record cannot hold one.
  case "$name" in *[a-zA-Z]*) ;; *) return 1 ;; esac
  # Routable, or SPF authorises nobody.
  is_private_addr "$addr" && return 1
  return 0
}

cmd_probe() {
  load_config
  require_config || return 2
  resolve_tls || return 2
  api_login || return 2

  local body version
  body=$(api_get /version) || { fail api-failed "$(api_error)"; return 2; }
  version=$(json_field "$body" data release) || version=$(json_field "$body" data version) || version="unknown"
  mark VERSION "$version"
  ok "Reached the gateway at ${GW_HOST}:${GW_API_PORT} and the credential works. PMG ${version}."

  # A credential that can read but not write is the most annoying way to
  # discover a permissions problem: half-way through apply. Find out now.
  if ! api_get /config/mail >/dev/null; then
    fail insufficient-permission "The credential cannot read /config/mail: $(api_error)"
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
  body=$(api_get /config/mail) || { fail api-failed "$(api_error)"; return 2; }

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
  plan_line pmg.relay        "$(json_field "$body" data relay || printf '')"        "$MTA_IP"
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
    if printf ' %s ' "$nets" | grep -q " ${MTA_IP}/32 \| ${GW_HOST}/32 "; then
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
  domains=$(api_get /config/domains) || { fail api-failed "$(api_error)"; return 2; }
  if printf '%s' "$domains" | grep -q "\"${MAIL_DOMAIN}\""; then
    info "${MAIL_DOMAIN} already listed"
  else
    api_post /config/domains "domain=${MAIL_DOMAIN}" >/dev/null || {
      fail api-failed "Could not add the relay domain: $(api_error)"; return 2; }
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
         "host=${MTA_IP}" "port=25" "protocol=smtp" "use_mx=0" >/dev/null; then
      ok "transport ${MAIL_DOMAIN} -> ${MTA_IP}:25 (updated)"
    else
      warn "The gateway would not update the transport entry ($(api_error)); replacing it."
      api_del "/config/transport/${MAIL_DOMAIN}" >/dev/null 2>&1 || true
      if ! api_post /config/transport \
             "domain=${MAIL_DOMAIN}" "host=${MTA_IP}" "port=25" \
             "protocol=smtp" "use_mx=0" >/dev/null; then
        fail api-failed \
          "Could not write the transport entry: $(api_error). The old entry was removed to replace it, so ${MAIL_DOMAIN} may now have NO route on the gateway. Fix this before mail arrives: Mail Proxy > Transports on the gateway."
        return 2
      fi
      ok "transport ${MAIL_DOMAIN} -> ${MTA_IP}:25 (replaced)"
    fi
  else
    api_post /config/transport \
      "domain=${MAIL_DOMAIN}" "host=${MTA_IP}" "port=25" \
      "protocol=smtp" "use_mx=0" >/dev/null || {
        fail api-failed "Could not write the transport entry: $(api_error)"; return 2; }
    ok "transport ${MAIL_DOMAIN} -> ${MTA_IP}:25"
  fi

  say "3. Letting this appliance relay out through the gateway"
  local nets
  nets=$(api_get /config/mynetworks) || nets=""
  if printf '%s' "$nets" | grep -q "\"${MTA_IP}/32\""; then
    info "${MTA_IP}/32 already trusted"
  else
    api_post /config/mynetworks "cidr=${MTA_IP}/32" >/dev/null || {
      fail api-failed "Could not add ${MTA_IP}/32 to the gateway's trusted networks: $(api_error)"; return 2; }
    ok "trusted network ${MTA_IP}/32"
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
    "relay=${MTA_IP}" \
    "relayport=25" \
    "relaynomx=1" \
    "int_port=${PMG_INT_PORT}" \
    "ext_port=${PMG_EXT_PORT}" >/dev/null || {
      fail api-failed "Could not write the mail routing settings: $(api_error)"; return 2; }
  ok "relay ${MTA_IP}:25, no MX lookup, internal port ${PMG_INT_PORT}"

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
  #
  # greylist is off unless the operator asked for it. It defers the first
  # message from every unseen sender for a few minutes - PMG's delay is
  # hardcoded - and that is exactly what made inbound mail feel slow while
  # outbound felt instant in live QA. Everything else below still runs.
  local policy_failed=0
  api_put /config/mail \
    "verifyreceivers=450" \
    "queue-lifetime=${PMG_QUEUE_LIFETIME_DAYS}" \
    "dwarning=${PMG_DELAY_WARNING_HOURS}" \
    "greylist=${GW_GREYLIST}" \
    "spf=1" \
    "tls=1" \
    "tlslog=1" \
    "hide_received=1" \
    "before_queue_filtering=0" \
    "maxsize=${PMG_MAX_MESSAGE_BYTES}" \
    "banner=ESMTP KIN Mail Gateway" >/dev/null || policy_failed=1

  # Written separately so that clearing it can be reported on its own, and so
  # a gateway that already carries a bad value is REPAIRED by re-applying
  # rather than merely left alone.
  apply_dnsbl

  if [ "$policy_failed" -eq 1 ]; then
    # Mail still flows without these. Say so honestly rather than failing the
    # whole apply and leaving the operator thinking nothing worked.
    warn "The gateway refused the policy settings: $(api_error)"
    info "Mail routing is configured and mail will flow. Set spam policy, TLS and"
    info "the banner by hand under Mail Proxy > Options on the gateway."
  else
    if [ "$GW_GREYLIST" = "1" ]; then
      ok "greylisting ON - cheap spam is refused, and a first message from a new"
      info "correspondent is delayed a few minutes. That delay is not a fault."
    else
      ok "greylisting OFF - no first-contact delay; spam is judged on content instead"
    fi
    ok "soft recipient verification, SPF, outbound TLS, no header leak"
    ok "undeliverable mail is retried for ${PMG_QUEUE_LIFETIME_DAYS} days before it is given up on"
    info "Longer than the default on purpose: a gateway that gives up during a"
    info "weekend outage bounces mail that would have arrived on Monday, and a"
    info "bounce cannot be taken back."
  fi

  say "6. Spam detection"
  # Bayes and the auto-welcomelist are both OFF in stock PMG and both are the
  # difference between a filter that guesses and one that learns. They are the
  # single best defence against false positives, which is the failure mode that
  # costs a business real money - a quarantined invoice is worse than a
  # delivered advert.
  #
  # extract_text reads attachments (PDF, Office) so phishing that hides its
  # payload in a document is scored on what it actually says.
  if api_put /config/spam \
       "use_bayes=1" \
       "use_awl=1" \
       "use_razor=1" \
       "rbl_checks=1" \
       "extract_text=1" \
       "maxspamsize=${PMG_MAX_SPAM_SCAN_BYTES}" \
       "clamav_heuristic_score=${PMG_CLAMAV_HEURISTIC_SCORE}" >/dev/null; then
    ok "Bayesian learning, auto-welcomelist, Razor, blocklists, attachment text extraction"
    info "Bayes needs traffic before it helps. Expect it to improve over the first weeks."
  else
    warn "The gateway refused the spam detector settings: $(api_error)"
    info "Set them by hand under Configuration > Spam Detector."
  fi

  say "7. Virus and dangerous-attachment detection"
  # archiveblockencrypted is the important one. A password-protected archive
  # cannot be scanned, and "send the password in the next email" is how a
  # large share of ransomware arrives. Flagging them is not the same as
  # deleting them: they score as heuristic hits and land in quarantine, where
  # the operator can look and release.
  #
  # The archive limits are raised rather than lowered on purpose: anything
  # over a limit is skipped UNSCANNED, so a small limit is not caution, it is
  # a hole. Nesting depth catches zip-inside-zip, the oldest evasion there is.
  if api_put /config/clamav \
       "archiveblockencrypted=1" \
       "archivemaxsize=${PMG_ARCHIVE_MAX_SIZE}" \
       "archivemaxrec=${PMG_ARCHIVE_MAX_RECURSION}" \
       "archivemaxfiles=${PMG_ARCHIVE_MAX_FILES}" \
       "maxscansize=${PMG_MAX_SCAN_SIZE}" \
       "scriptedupdates=1" >/dev/null; then
    ok "encrypted archives flagged, deeper nesting scanned, larger files still scanned"
  else
    warn "The gateway refused the virus detector settings: $(api_error)"
    info "Set them by hand under Configuration > Virus Detector."
  fi

  say "8. Quarantine"
  # Longer than stock, and for different reasons at each end. Spam quarantine
  # is where a false positive waits to be noticed, and a week is not long
  # enough to cover somebody's holiday. Virus quarantine is evidence.
  #
  # Links are not clickable and remote images do not load. A quarantine
  # review page that renders a phishing link as a link, and fetches its
  # tracking pixel, is a page that attacks the person reviewing it.
  local quar_failed=0
  api_put /config/spamquar \
    "lifetime=${PMG_SPAMQUAR_DAYS}" \
    "allowhrefs=0" \
    "viewimages=0" \
    "reportstyle=verbose" >/dev/null || quar_failed=1
  api_put /config/virusquar \
    "lifetime=${PMG_VIRUSQUAR_DAYS}" \
    "allowhrefs=0" \
    "viewimages=0" >/dev/null || quar_failed=1
  if [ "$quar_failed" -eq 0 ]; then
    ok "spam kept ${PMG_SPAMQUAR_DAYS} days, viruses ${PMG_VIRUSQUAR_DAYS} days, no live links or tracking images"
  else
    warn "The gateway refused some quarantine settings: $(api_error)"
  fi

  say "9. Protective rules"
  check_rules

  say "10. Making sure the gateway does not sign DKIM a second time"
  api_put /config/admin "dkim_sign=0" >/dev/null || {
    fail api-failed "Could not turn off DKIM signing on the gateway: $(api_error)"; return 2; }
  ok "gateway DKIM signing off (Zimbra signs; two signatures fail DMARC at some receivers)"

  if ! zimbra_available; then
    warn "Zimbra is not installed here. The gateway is configured; the appliance side is not."
    return 1
  fi

  say "11. Pointing this appliance's outbound mail at the gateway"
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

  say "12. Trusting the gateway to hand mail in"
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

  say "13. Reloading Postfix so the new relay takes effect"
  # zmmtactl reload is a configuration reload, not a restart: connections in
  # flight are not dropped and the queue is untouched.
  if [ -n "$ZMPROV" ]; then
    info "(stubbed in tests)"
  else
    su - zimbra -c 'zmmtactl reload' >/dev/null 2>&1 ||
      warn "Postfix did not reload cleanly; run 'su - zimbra -c \"zmmtactl reload\"' and check."
  fi

  say "14. Reading back the settings that matter"
  # Writing a setting and the setting being set are different things, and the
  # two below are the ones that silently destroy mail. Checking them now costs
  # one API call and means "applied" is a claim we have evidence for.
  local check_mail check_admin drift=0
  check_mail=$(api_get /config/mail) || check_mail=""
  if [ "$(json_field "$check_mail" data relaynomx || printf '0')" != "1" ]; then
    warn "The gateway did not keep relaynomx=1. Left as it is, mail for this domain will loop."
    drift=1
  fi
  if [ "$(json_field "$check_mail" data relay || printf '')" != "$MTA_IP" ]; then
    warn "The gateway did not keep relay=${MTA_IP}. Filtered mail has nowhere to go."
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

  say "15. Can the two machines actually reach each other"
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
  local pub_name pub_addr
  pub_name=$(public_name)
  pub_addr=$(public_addr)
  say "DNS still has to change, and only you can do that:"
  if public_identity_usable; then
    say "  A    ${pub_name}.  ->  ${pub_addr}"
    say "  MX   ${MAIL_DOMAIN}.  10  ${pub_name}."
    say "  SPF  ${MAIL_DOMAIN}.  TXT \"v=spf1 ip4:${pub_addr} ~all\""
    say "  PTR  ${pub_addr}  ->  ${pub_name}   (ask the ISP)"
  else
    warn "No public identity is recorded for this gateway, so exact records cannot be printed."
    say "  MX   ${MAIL_DOMAIN}.  10  <the gateway's PUBLIC hostname>"
    say "  SPF  ${MAIL_DOMAIN}.  TXT \"v=spf1 ip4:<the gateway's PUBLIC address> ~all\""
    say "  PTR  <public address>  ->  <public hostname>   (ask the ISP)"
    say ""
    say "  ${GW_HOST} is how THIS APPLIANCE reaches the gateway. Publishing it"
    say "  would authorise nothing and every outbound message would fail SPF."
    say "  Record the public hostname and address under Connect and re-run this."
  fi
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
  body=$(api_get /config/mail) || { fail api-failed "$(api_error)"; return 2; }

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

  assert_eq pmg.relay "$(json_field "$body" data relay || printf '')" "$MTA_IP" \
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
  if printf '%s' "$nets" | grep -q "\"${MTA_IP}/32\""; then
    ok "pmg.mynetworks trusts ${MTA_IP}"
  else
    note_problem
    printf 'KIN_GW_PROBLEM %s expected=%s actual=%s\n' pmg.mynetworks "${MTA_IP}/32" "<absent>"
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

  say "Standing risks"
  check_relay_scope
  check_gateway_room

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

  local pub_name pub_addr have=1
  pub_name=$(public_name)
  pub_addr=$(public_addr)
  public_identity_usable || have=0

  if [ "$have" -eq 0 ]; then
    warn "This gateway has no public identity recorded, so no DNS records can be given."
    info "Record its public hostname and address under Console > Proxmox Mail Gateway."
    info "An MX record can only name a host, never an address, and SPF must list the"
    info "address the internet sees. ${GW_HOST} is how this appliance reaches the"
    info "gateway on the local network; publishing it would authorise nobody."
    # Placeholders, so nothing below can be copied into a zone file by mistake.
    pub_name="<gateway public hostname>"
    pub_addr="<gateway public address>"
  fi

  local mx spf
  mx=$("$DIG" +short +time=5 MX "$MAIL_DOMAIN" 2>/dev/null | awk '{print $2}' | sed 's/\.$//' | tr '\n' ' ')
  if [ -z "$mx" ]; then
    warn "No MX record for ${MAIL_DOMAIN} could be resolved from here."
    info "Set:  ${MAIL_DOMAIN}.  MX  10  ${pub_name}"
  elif [ "$have" -eq 1 ] && printf ' %s ' "$mx" | grep -q " ${pub_name} "; then
    ok "dns.mx points at the gateway (${pub_name})"
  else
    warn "dns.mx for ${MAIL_DOMAIN} is '${mx}', not the gateway. Inbound mail still bypasses it."
    info "Set:  ${MAIL_DOMAIN}.  MX  10  ${pub_name}"
  fi

  spf=$("$DIG" +short +time=5 TXT "$MAIL_DOMAIN" 2>/dev/null | tr -d '"' | grep -i 'v=spf1' | head -1)
  if [ -z "$spf" ]; then
    warn "No SPF record for ${MAIL_DOMAIN}. Outbound mail from the gateway will be treated as suspicious."
    info "Set:  ${MAIL_DOMAIN}.  TXT  \"v=spf1 ip4:${pub_addr} ~all\""
  elif [ "$have" -eq 1 ] && printf '%s' "$spf" | grep -q "$pub_addr"; then
    ok "dns.spf authorises the gateway (${pub_addr})"
  else
    warn "SPF for ${MAIL_DOMAIN} does not list the gateway's public address."
    info "Current: ${spf}"
    info "Add:     ip4:${pub_addr}"
  fi

  if [ "$have" -eq 1 ]; then
    local a
    a=$("$DIG" +short +time=5 A "$pub_name" 2>/dev/null | head -1)
    if [ -z "$a" ]; then
      warn "${pub_name} has no A record. An MX pointing at it cannot be used."
      info "Set:  ${pub_name}.  A  ${pub_addr}"
    elif [ "$a" != "$pub_addr" ]; then
      warn "${pub_name} resolves to ${a}, but its public address is recorded as ${pub_addr}."
    else
      ok "dns.a ${pub_name} resolves to ${a}"
    fi
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

cleanup() { rm -f "$API_ERROR_FILE" 2>/dev/null || true; }
trap cleanup EXIT

main "$@"
