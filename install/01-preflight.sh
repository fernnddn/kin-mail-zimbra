#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 01 PREFLIGHT
# Decides whether this site can host a mail server at all. Run this FIRST,
# before installing anything. Every check here is one that cost us time when
# it was discovered late.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# shellcheck source=lib/apt-lock.sh
. ./lib/apt-lock.sh

echo
say "PREFLIGHT for ${MAIL_HOST} (${SERVER_IP})"
echo

FATAL=0
BLOCKED_SMTP=0

# --- 1. host sizing ----------------------------------------------------------
say "1. Host resources"
CPUS=$(nproc)
MEM_GB=$(awk '/MemTotal/{printf "%.0f", $2/1024/1024}' /proc/meminfo)
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')

[ "$CPUS"    -ge 2  ] && ok "vCPU: $CPUS"        || { fail "vCPU: $CPUS (minimum 2)"; FATAL=1; }
[ "$MEM_GB"  -ge 8  ] && ok "RAM: ${MEM_GB} GB"  || { fail "RAM: ${MEM_GB} GB (minimum 8)"; FATAL=1; }
[ "$DISK_GB" -ge 40 ] && ok "Disk free: ${DISK_GB} GB" || { fail "Disk free: ${DISK_GB} GB (minimum 40)"; FATAL=1; }

if ! mountpoint -q /opt 2>/dev/null; then
  info "/opt is on the root volume right now. 02-prepare-os.sh will GPT-partition"
  info "a unique blank spare disk (same layout as Build HA pair) and mount it at"
  info "/opt/zimbra when that disk is unambiguous; otherwise Zimbra stays on root."
fi

# --- 2. operating system -----------------------------------------------------
echo; say "2. Operating system"
. /etc/os-release
case "${VERSION_ID:-}" in
  22.04|24.04)
    ok "Ubuntu ${VERSION_ID} - supported (Maldua FOSS ${VERSION_ID} build)"
    ;;
  *)
    fail "Ubuntu ${VERSION_ID:-unknown}. Supported: 22.04 and 24.04 LTS."
    FATAL=1
    ;;
esac

# --- 3. clean host -----------------------------------------------------------
# Ubuntu images often ship postfix. 02 purges these later; stopping them here
# lets a greenfield Deploy continue instead of dying before Zimbra.
echo; say "3. Conflicting services"
CONFLICT=0
for s in postfix apache2 nginx bind9 exim4 dovecot-core sendmail; do
  if systemctl is-active --quiet "$s" 2>/dev/null; then
    warn "$s is running - stopping it (Zimbra ships its own)"
    systemctl disable --now "$s" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$s" 2>/dev/null; then
      fail "$s is still running after stop - Zimbra will conflict"
      CONFLICT=1
      FATAL=1
    else
      ok "$s stopped"
    fi
  fi
done
[ $CONFLICT -eq 0 ] && ok "No conflicting mail or web services running"
[ -d /opt/zimbra ] && { warn "/opt/zimbra already exists - this is not a clean host"; } || ok "/opt/zimbra absent"

# --- 4. outbound reachability for installation -------------------------------
# Blank VMs often have a broken system resolver while dig @$DNS_UPSTREAM works.
# Repair that before curl checks so HA peer install does not FAIL as "unreachable".
echo; say "4. Outbound access needed to INSTALL"
# shellcheck disable=SC1091
. ./lib/ensure-outbound-dns.sh
if kin_ensure_outbound_dns; then
  ok "System DNS can resolve public names (upstream ${DNS_UPSTREAM_1:-1.1.1.1} / ${DNS_UPSTREAM_2:-8.8.8.8})"
else
  warn "System DNS still broken after repair attempt - outbound checks may FAIL as unreachable"
  warn "Set nameserver ${DNS_UPSTREAM_1:-1.1.1.1} (and ${DNS_UPSTREAM_2:-8.8.8.8}) in resolv.conf / netplan, then re-run"
fi
for u in "https://github.com" "http://archive.ubuntu.com" "https://repo.zimbra.com"; do
  code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$u" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    ok "$(printf '%-46s' "$u") HTTP $code"
  else
    fail "$(printf '%-46s' "$u") unreachable"; FATAL=1
  fi
done
if [ "${TLS_METHOD:-}" != "customer" ]; then
  u="https://acme-v02.api.letsencrypt.org/directory"
  code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$u" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    ok "$(printf '%-46s' "$u") HTTP $code"
  else
    fail "$(printf '%-46s' "$u") unreachable"; FATAL=1
  fi
else
  info "TLS_METHOD=customer - skipping Let's Encrypt API reachability"
fi

# --- 5. outbound SMTP - the decisive test ------------------------------------
echo; say "5. Outbound SMTP  ${DIM}(banner grab, not a connect test)${RST}"
info "A TCP connect can succeed while an inline device silently drops the"
info "session. Only a 220 banner proves the port is actually usable."
for target in "alt1.aspmx.l.google.com 25" "gmail-smtp-in.l.google.com 25"; do
  set -- $target
  b=$(smtp_banner "$1" "$2")
  if [ -n "$b" ]; then
    ok "$(printf '%-32s:%s' "$1" "$2")  ${b%%$'\n'*}"
  else
    warn "$(printf '%-32s:%s' "$1" "$2")  no banner - FILTERED"; BLOCKED_SMTP=1
  fi
done

# --- 6. public DNS state -----------------------------------------------------
echo; say "6. Public DNS for ${MAIL_DOMAIN}"
command -v dig >/dev/null || kin_apt -qq -y install dnsutils >/dev/null 2>&1

ns=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$MAIL_DOMAIN" NS 2>/dev/null | tr '\n' ' ')
[ -n "$ns" ] && ok "NS     : $ns" || { fail "NS     : none - domain is not delegated"; FATAL=1; }

# A mixed delegation answers from whichever nameserver a resolver happens to
# pick, so mail fails intermittently. Brand comparison is shared with
# 05-healthcheck.sh via dns_ns_brand / dns_ns_provider_count in 00-config.sh.
if [ "$(echo "$ns" | tr ' ' '\n' | grep -c . )" -gt 0 ]; then
  providers=$(dns_ns_provider_count "$ns")
  if [ "$providers" -gt 1 ]; then
    fail "Delegation is SPLIT across $providers providers - mail will fail at random"
    FATAL=1
  else
    ok "Delegation consistent on one provider brand"
  fi
fi

for rec in "MX ${MAIL_DOMAIN}" "TXT ${MAIL_DOMAIN}" "A ${MAIL_HOST}" "TXT _dmarc.${MAIL_DOMAIN}"; do
  set -- $rec
  v=$(dig +short +time=5 @"$DNS_UPSTREAM_1" "$2" "$1" 2>/dev/null | tr '\n' ' ')
  [ -n "$v" ] && ok "$(printf '%-6s' "$1") : $v" || warn "$(printf '%-6s' "$1") : not published yet"
done

# --- 7. public address and PTR ----------------------------------------------
echo; say "7. Public address"
# -f so an HTTP error is an error, and a shape check so a captive portal or a
# proxy error page cannot be printed to the operator as this host's public
# address - or worse, matched against the other one and reported as "stable".
only_ipv4() {
  case "$1" in
  '' | *[!0-9.]*) printf '' ;;
  *.*.*.*) printf '%s' "$1" ;;
  *) printf '' ;;
  esac
}
ip1=$(only_ipv4 "$(curl -fsS -m 12 https://ifconfig.me 2>/dev/null | tr -d '[:space:]')")
ip2=$(only_ipv4 "$(curl -fsS -m 12 https://api.ipify.org 2>/dev/null | tr -d '[:space:]')")
info "seen by ifconfig.me : ${ip1:-unknown}"
info "seen by ipify       : ${ip2:-unknown}"
if [ -n "$ip1" ] && [ -n "$ip2" ] && [ "$ip1" != "$ip2" ]; then
  warn "HTTP egress is NOT STABLE (${ip1} vs ${ip2})"
  info "Multiple uplinks are load-balancing outbound traffic. Worth flagging,"
  info "but it does not settle the question: a host can egress HTTP and SMTP"
  info "from different addresses, and only the SMTP one decides deliverability."
elif [ -n "$ip1" ]; then
  info "HTTP egress looks stable at ${ip1}"
fi

# The address a mail server presents is the only one that matters here, and it
# has to be measured by asking a real mail server, not an HTTP echo service.
if [ $BLOCKED_SMTP -eq 0 ] && command -v swaks >/dev/null 2>&1; then
  smtp_ip() {
    timeout 60 swaks --server gmail-smtp-in.l.google.com --port 25 \
      --from "postmaster@${MAIL_DOMAIN}" --to "postmaster@gmail.com" \
      --quit-after RCPT --timeout 30 2>/dev/null \
      | grep -oE 'at your service, \[[0-9.]+\]' \
      | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' | head -1
  }
  s1=$(smtp_ip); s2=$(smtp_ip)
  if [ -n "$s1" ] && [ "$s1" = "$s2" ]; then
    ok "SMTP sending address is consistent: ${s1}"
    a_mail=$(dig +short +time=5 @"$DNS_UPSTREAM_1" A "$MAIL_HOST" 2>/dev/null | head -1)
    if [ -n "$a_mail" ] && [ "$a_mail" != "$s1" ]; then
      warn "A ${MAIL_HOST} is ${a_mail}, SMTP sends from ${s1}"
      info "Inbound MX follows the A record. Outbound SPF alignment uses ${s1}."
      info "Point A at ${s1}, or add ip4:${s1} to SPF if this NAT is intentional."
    fi
    ptr=$(dig +short +time=5 -x "$s1" 2>/dev/null | head -1)
    if [ -z "$ptr" ]; then
      warn "No PTR for ${s1}. Request it from the owner of the IP block (the ISP)."
      info "PTR lives in the reverse zone, not in your domain's DNS provider."
    else
      ok "PTR ${s1} -> ${ptr}"
      back=$(dig +short +time=5 A "${ptr%.}" 2>/dev/null | head -1)
      [ "$back" = "$s1" ] && ok "Forward-confirmed chain matches" \
                          || warn "Chain broken: ${ptr%.} resolves to ${back:-nothing}, not ${s1}"
    fi
  elif [ -n "$s1" ]; then
    warn "SMTP sending address is NOT consistent (${s1} vs ${s2})"
    info "PTR can only name one address, so a share of outbound mail will be"
    info "rejected. Pin this host to one uplink with a policy route or IP pool."
  fi
else
  info "SMTP sending address not measured (swaks unavailable or port 25 filtered)"
fi

# --- 8. the directory has somewhere to land ----------------------------------
# Checked here, in the first half-minute, because the alternative is finding out
# in the fortieth.
#
# With Active Directory configured, the sync creates a mailbox per person and
# each one spends a contracted seat. An unset seat count refuses all of them, so
# the install runs to completion, authentication is configured correctly, the
# sweep timer is installed - and the admin console lists nobody, so not one
# person can sign in. That happened on three deploys in a row (24-26 Sep 2026).
#
# The wizard now refuses to leave the count unset when AD is on, in the browser
# and in validate_draft and in the CLI wizard. This catches the path none of
# those cover: a config carried over from an earlier build, or edited by hand.
if [ "${AD_AUTH_ENABLED:-no}" = "yes" ]; then
  echo
  say "8. Directory sync has seats to use"
  case "${CONTRACTED_SEATS:-}" in
    '' | PLACEHOLDER_UNSET)
      fail "AD is enabled but CONTRACTED_SEATS is not set."
      info "The directory sync spends one seat per person, so with no count it"
      info "will create nobody and no AD user will be able to sign in."
      info "Set CONTRACTED_SEATS in ${CONF_FILE} to the contracted number, or"
      info "Mailbox seats in the console wizard, then run this again."
      FATAL=$((FATAL + 1))
      ;;
    *[!0-9]*)
      fail "CONTRACTED_SEATS='${CONTRACTED_SEATS}' is not a whole number."
      info "Set it in ${CONF_FILE} to the contracted number of mailboxes."
      FATAL=$((FATAL + 1))
      ;;
    0)
      fail "CONTRACTED_SEATS is 0 while AD is enabled."
      info "No mailbox can be created at all, so no AD user could sign in."
      FATAL=$((FATAL + 1))
      ;;
    *)
      ok "${CONTRACTED_SEATS} seats available for the directory sync"
      ;;
  esac
  # And that the directory address is an LDAP address at all.
  #
  # AD_LDAP_URL="daps://<host>:636" - one character short of ldaps:// - was
  # accepted by every layer, written into the config, copied onto the Zimbra
  # domain, and surfaced forty minutes later as "Could not parse LDAP URI(s)".
  # It cost the whole feature three different ways: nobody was created from the
  # directory, 14-ad-trust skipped itself because the scheme was not ldaps://,
  # and no AD user could sign in. Nothing named the cause (26 Sep 2026).
  case "${AD_LDAP_URL:-}" in
    ldaps://?*|ldap://?*)
      ok "Directory address: ${AD_LDAP_URL}"
      case "${AD_LDAP_URL}" in
        ldap://*)
          info "Plain ldap:// - Windows refuses unsigned binds by default since"
          info "2020. Use ldaps:// unless this directory is known to allow it."
          ;;
      esac
      ;;
    '')
      fail "AD is enabled but AD_LDAP_URL is empty."
      info "Set it in ${CONF_FILE}, e.g. ldaps://dc.example.test:636"
      FATAL=$((FATAL + 1))
      ;;
    *)
      fail "AD_LDAP_URL='${AD_LDAP_URL}' is not an LDAP address."
      info "It must start with ldaps:// (or ldap://) and name a host."
      info "A single wrong character here stops every AD user being created,"
      info "skips the certificate trust step, and blocks every AD sign-in."
      info "Set it in ${CONF_FILE}, e.g. ldaps://dc.example.test:636"
      FATAL=$((FATAL + 1))
      ;;
  esac
fi

# --- verdict -----------------------------------------------------------------
echo
say "VERDICT"
if [ $FATAL -ne 0 ]; then
  fail "Do not install yet. Fix the failures above first."
  exit 1
fi
if [ $BLOCKED_SMTP -ne 0 ]; then
  warn "Installation can proceed, but this server CANNOT SEND MAIL until"
  warn "outbound SMTP is permitted. Request one of:"
  info "  - outbound TCP 25 to any destination      (direct delivery), or"
  info "  - outbound TCP 587 to one nominated relay (narrower, easier to approve)"
else
  ok "All checks passed. Proceed to 02-prepare-os.sh"
fi
echo
