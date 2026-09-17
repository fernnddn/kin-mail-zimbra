#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 09 HARDENING (Part A - low risk / reversible)
#
# Applies ONLY Part A from the Phase 7 hardening brief:
#   1) fail2ban (SSH + Zimbra auth + Z-Push ActiveSync 401s)
#   2) COS password lockout
#   3) Disable IMAP/POP cleartext login (server attrs)
#   4) unattended-upgrades security-only, no auto-reboot
#   5) TLS: ensure TLSv1.2/1.3; tighten reverse-proxy ciphers
#
# Host firewall / SSH key-only / admin-path lockdown live in 10 + 11 (and
# Stage 1 SSH ops), not here. See progress logs 7.1-7.4.
#
#   sudo ./09-hardening.sh              # full Part A on this host
#   sudo ./09-hardening.sh --os-only    # fail2ban + unattended only (Host B)
#   sudo ./09-hardening.sh --status     # print current hardening signals
#
# Dual-node: apply OS pieces via ansible/playbooks/mail-os-hardening.yml
# (role os_hardening) on BOTH mail hosts. Jail file is written by
# install/lib/kin-fail2ban-jails.sh (also called from ocf:kin:zimbra).
# Keep fail2ban filters and the unattended drop-in in sync with that role.
# COS / cleartext / TLS / SMTP rates stay in this script only (shared LDAP).
#
# Idempotent. Proxy restart (if needed) temporarily unmanages kin-zimbra so
# Pacemaker does not tear down the VIP on a brief zmproxy blip.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# apt on a freshly booted host: wait out apt-daily / unattended-upgrades
# instead of failing on a lock that clears itself.
# shellcheck source=lib/apt-lock.sh
. ./lib/apt-lock.sh
# Relay-scope and certificate decisions for stage 8 below.
# shellcheck source=lib/mail-surface-hardening.sh
. ./lib/mail-surface-hardening.sh
need_root

FAIL2BAN_IGNORE_IP="${KIN_FAIL2BAN_IGNORE_IP:-}"
LOCKOUT_MAX="${KIN_LOCKOUT_MAX_FAILURES:-8}"
LOCKOUT_DURATION="${KIN_LOCKOUT_DURATION:-30m}"
LOCKOUT_WINDOW="${KIN_LOCKOUT_FAILURE_LIFETIME:-1h}"
OS_ONLY=0
STATUS_ONLY=0

# Append space-separated tokens to an ignoreip string (dedupe exact tokens).
kin_fail2ban_append_ignore() {
  local out="$1"
  shift
  local tok
  for tok in "$@"; do
    [ -n "$tok" ] || continue
    case " ${out} " in
      *" ${tok} "*) ;;
      *) out="${out}${out:+ }${tok}" ;;
    esac
  done
  printf '%s' "$out"
}

# Default ignore list: loopback + KIN_ADMIN_IPS only. No blanket LAN /24 -
# brute-force protection should apply to every host on the segment except
# the ones explicitly trusted, not the whole subnet by default. A banned IP
# still self-clears via bantime (see kin-mail.jail.j2), so this doesn't need
# its own separate expiry scheme.
kin_fail2ban_default_ignoreip() {
  local out="127.0.0.1/8 ::1"
  # shellcheck disable=SC2086
  kin_fail2ban_append_ignore "$out" ${KIN_ADMIN_IPS:-}
}
[ -n "$FAIL2BAN_IGNORE_IP" ] || FAIL2BAN_IGNORE_IP="$(kin_fail2ban_default_ignoreip)"
# Even with KIN_FAIL2BAN_IGNORE_IP override, always union KIN_ADMIN_IPS (ufw-aligned).
# shellcheck disable=SC2086
FAIL2BAN_IGNORE_IP="$(kin_fail2ban_append_ignore "$FAIL2BAN_IGNORE_IP" ${KIN_ADMIN_IPS:-})"

case "${1:-}" in
  --os-only) OS_ONLY=1 ;;
  --status)  STATUS_ONLY=1 ;;
  -h|--help)
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *)
    fail "Unknown option: $1"
    exit 2
    ;;
esac

# shellcheck disable=SC1091
. ./lib/zimbra-data-disk-probe.sh
if [ "$OS_ONLY" -eq 0 ] && [ "$STATUS_ONLY" -eq 0 ] && zimbra_is_mid_handoff; then
  warn "Mid-handoff: /opt/zimbra is unmounted; switching to --os-only (fail2ban + unattended)."
  OS_ONLY=1
fi

cluster_ok() {
  local code
  code=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/ 2>/dev/null || true)
  [ "$code" = "200" ]
}

# Does nginx live on this machine?
#
# Said once, here, because saying it at each call site is how it drifts: the
# same question got answered three different ways across this stage and the
# orchestrator before, and each copy was fixed separately.
#
# The role comes first and is decisive. On a split the proxy is on the edge
# because that is what a split IS, not because of what happens to be on disk.
# Deriving it from the filesystem instead would mean betting the stage on which
# Zimbra package ships zmproxyconfgen - and if that bet were wrong, the mailbox
# would run it, fail, and abort the build exactly as before. An architecture
# decision that is already recorded should not be re-inferred from evidence.
#
# The filesystem checks below still matter for every node the role cannot
# speak for: a single-server appliance, or an HA secondary, built without
# zimbra-proxy. Both the generator and its templates have to be here, because
# the generator without templates is the failure this is here to prevent.
node_runs_proxy() {
  [ "${KIN_NODE_ROLE:-all}" = "mailbox" ] && return 1
  [ -x /opt/zimbra/libexec/zmproxyconfgen ] || return 1
  [ -d /opt/zimbra/conf/nginx/templates ] || return 1
  return 0
}

# Does this node's GENERATED nginx config already carry the cipher list we
# want? Answers yes when it cannot tell, because the caller uses this to decide
# whether to restart the proxy, and a restart is an outage.
#
# This exists because of a split-brain that is invisible on one machine.
# zimbraReverseProxySSLProtocols and zimbraReverseProxySSLCiphers are GLOBAL -
# zmprov gacf reads them, zmprov mcf writes them - but the nginx config built
# from them is per-node. On a split, stage 09 runs on the mailbox first. The
# mailbox writes the global settings and, having no proxy, correctly does not
# regenerate. The edge then runs, reads the settings the mailbox just wrote,
# finds nothing to change, and reports "No proxy restart required" - so the one
# machine that actually serves TLS never regenerates, and keeps the cipher list
# it was installed with. Green deployment, hardening silently not applied.
#
# So the trigger is not "did this run change the directory" but "does this
# node's nginx match it", which is also idempotent: once it matches, every
# later run is a no-op.
proxy_config_has_ciphers() {
  local want="$1" dir=/opt/zimbra/conf/nginx/includes
  [ -d "$dir" ] || return 0
  grep -rqsF -- "$want" "$dir" 2>/dev/null
}

show_status() {
  echo
  say "Hardening status (Part A signals)"
  if command -v fail2ban-client >/dev/null 2>&1 && systemctl is-active --quiet fail2ban; then
    ok "fail2ban active"
    fail2ban-client status 2>/dev/null | sed 's/^/    /' || true
  else
    warn "fail2ban not active"
  fi
  if [ -d /opt/zimbra ]; then
    su - zimbra -c "zmprov gc default zimbraPasswordLockoutEnabled zimbraPasswordLockoutMaxFailures zimbraPasswordLockoutDuration zimbraPasswordLockoutFailureLifetime" 2>/dev/null \
      | sed 's/^/    /' || true
    su - zimbra -c "zmprov gs \$(zmhostname) zimbraImapCleartextLoginEnabled zimbraPop3CleartextLoginEnabled" 2>/dev/null \
      | sed 's/^/    /' || true
    su - zimbra -c "zmprov gacf" 2>/dev/null | grep -E 'zimbraReverseProxySSLProtocols|zimbraReverseProxySSLCiphers' \
      | sed 's/^/    /' | head -20 || true
  else
    info "/opt/zimbra not mounted - skip Zimbra attrs (expected on Secondary)"
  fi
  grep -E 'Automatic-Reboot|Allowed-Origins|Unattended-Upgrade "' /etc/apt/apt.conf.d/50unattended-upgrades /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null \
    | sed 's/^/    /' | head -20 || true
  if [ -d /opt/zimbra ]; then
    su - zimbra -c "postconf -h smtpd_client_auth_rate_limit smtpd_client_connection_rate_limit smtpd_client_message_rate_limit" 2>/dev/null \
      | sed 's/^/    smtp rates: /' || true
  fi
  if cluster_ok; then ok "https://127.0.0.1/ → 200"; else warn "https check failed"; fi
}

# -----------------------------------------------------------------------------
configure_fail2ban() {
  say "1. fail2ban (SSH + Zimbra + Z-Push)"
  export DEBIAN_FRONTEND=noninteractive
  kin_apt -qq update
  kin_apt -y install fail2ban >/dev/null

  mkdir -p /etc/fail2ban/filter.d /etc/fail2ban/jail.d

  cat > /etc/fail2ban/filter.d/zimbra-auth.conf <<'EOF'
# KIN Mail - Zimbra web/SOAP auth failures (mailbox.log carries oip=<client>)
[Definition]
failregex = ^.*oip=<HOST>;.*authentication failed
            ^.*oip=<HOST>;.*invalid password
            ^.*oip=<HOST>;.*Auth failed
ignoreregex =
EOF

  cat > /etc/fail2ban/filter.d/zpush-auth.conf <<'EOF'
# KIN Mail - ActiveSync / Autodiscover HTTP 401 via Zimbra nginx access log
[Definition]
failregex = ^<HOST>:\d+ .* "(?:OPTIONS|POST|GET) https?://[^"]*/(?:Microsoft-Server-ActiveSync|Autodiscover[^"]*)[^"]*" 401
ignoreregex =
EOF

  install -m 0755 ./lib/kin-fail2ban-jails.sh /usr/local/sbin/kin-fail2ban-jails
  if ! FAIL2BAN_IGNORE_IP="${FAIL2BAN_IGNORE_IP}" /usr/local/sbin/kin-fail2ban-jails auto; then
    fail "fail2ban jail render failed"
    exit 1
  fi

  systemctl enable --now fail2ban >/dev/null
  systemctl reload fail2ban 2>/dev/null || systemctl restart fail2ban
  if ! systemctl is-active --quiet fail2ban; then
    fail "fail2ban failed to start"
    exit 1
  fi
  ok "fail2ban active (ignoreip: ${FAIL2BAN_IGNORE_IP})"
  info "Quick unban: fail2ban-client set <jail> unbanip <ip>  OR  fail2ban-client unban --all"
}

# -----------------------------------------------------------------------------
configure_unattended() {
  say "2. unattended-upgrades (security-only, no auto-reboot)"
  export DEBIAN_FRONTEND=noninteractive
  kin_apt -y install unattended-upgrades >/dev/null

  cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
EOF

  # Drop-in that always wins over commented defaults in 50unattended-upgrades.
  cat > /etc/apt/apt.conf.d/52kin-mail-unattended <<'EOF'
// KIN Mail - security origins only; never auto-reboot (HA / VIP safety).
Unattended-Upgrade::Allowed-Origins {
        "${distro_id}:${distro_codename}-security";
        "${distro_id}ESMApps:${distro_codename}-apps-security";
        "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
EOF

  systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
  ok "unattended-upgrades configured (Automatic-Reboot=false)"
}

# -----------------------------------------------------------------------------
configure_lockout() {
  say "3. COS password lockout (default COS)"
  if [ ! -d /opt/zimbra ]; then
    warn "Skipping COS lockout - /opt/zimbra not present"
    return 0
  fi
  zimbra_cmd zmprov mc default \
    zimbraPasswordLockoutEnabled TRUE \
    zimbraPasswordLockoutMaxFailures "$LOCKOUT_MAX" \
    zimbraPasswordLockoutDuration "$LOCKOUT_DURATION" \
    zimbraPasswordLockoutFailureLifetime "$LOCKOUT_WINDOW"
  ok "default COS lockout: enabled max=${LOCKOUT_MAX} duration=${LOCKOUT_DURATION} window=${LOCKOUT_WINDOW}"
}

configure_cleartext() {
  say "4. Disable IMAP/POP cleartext login (server attrs)"
  if [ ! -d /opt/zimbra ]; then
    warn "Skipping cleartext attrs - /opt/zimbra not present"
    return 0
  fi
  # Proxy already uses starttls=only (LOGINDISABLED). Align mailbox server attrs.
  local host
  host=$(zimbra_cmd zmhostname)
  local cur_imap cur_pop
  cur_imap=$(zimbra_cmd zmprov gs "$host" zimbraImapCleartextLoginEnabled 2>/dev/null | awk '/zimbraImapCleartextLoginEnabled:/{print $2}')
  cur_pop=$(zimbra_cmd zmprov gs "$host" zimbraPop3CleartextLoginEnabled 2>/dev/null | awk '/zimbraPop3CleartextLoginEnabled:/{print $2}')
  info "Before: IMAP cleartext=${cur_imap:-?} POP cleartext=${cur_pop:-?}"
  info "Proxy StartTLS mode already 'only' (cleartext LOGIN rejected at nginx)."
  zimbra_cmd zmprov ms "$host" \
    zimbraImapCleartextLoginEnabled FALSE \
    zimbraPop3CleartextLoginEnabled FALSE
  ok "zimbraImap/Pop3CleartextLoginEnabled=FALSE on ${host}"
}

configure_tls() {
  say "5. TLS protocols + proxy ciphers"
  if [ ! -d /opt/zimbra ]; then
    warn "Skipping TLS - /opt/zimbra not present"
    return 0
  fi

  # Modern AEAD-focused suite (TLS1.2 GCM + TLS1.3). No RC4/3DES/MD5; no bare AES128 CBC.
  local desired_ciphers="ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:!aNULL:!eNULL:!EXPORT:!DES:!MD5:!PSK:!RC4"
  local need_proxy_reload=0
  local protos ciphers
  protos=$(zimbra_cmd zmprov gacf 2>/dev/null | awk '/zimbraReverseProxySSLProtocols:/{print $2}' | tr '\n' ' ')
  ciphers=$(zimbra_cmd zmprov gacf 2>/dev/null | awk '/^zimbraReverseProxySSLCiphers:/{print substr($0,index($0,$2))}')

  info "Current SSLProtocols: ${protos:-unset}"
  if ! printf '%s' "$protos" | grep -q 'TLSv1.2' || ! printf '%s' "$protos" | grep -q 'TLSv1.3'; then
    zimbra_cmd zmprov mcf zimbraReverseProxySSLProtocols TLSv1.2
    zimbra_cmd zmprov mcf +zimbraReverseProxySSLProtocols TLSv1.3
    need_proxy_reload=1
    ok "Set zimbraReverseProxySSLProtocols=TLSv1.2+TLSv1.3"
  else
    ok "Proxy SSL protocols already TLSv1.2 + TLSv1.3"
  fi

  if [ "$ciphers" = "$desired_ciphers" ]; then
    ok "zimbraReverseProxySSLCiphers already modern AEAD suite"
  else
    zimbra_cmd zmprov mcf zimbraReverseProxySSLCiphers "$desired_ciphers"
    need_proxy_reload=1
    ok "Set modern zimbraReverseProxySSLCiphers (AEAD / no legacy CBC suite)"
  fi

  # Ensure mailboxd advertises TLSv1.3 alongside 1.2 (java options already set in this lab).
  local mbproto
  mbproto=$(zimbra_cmd zmprov gs "$(zimbra_cmd zmhostname)" zimbraMailboxdSSLProtocols 2>/dev/null | awk '/zimbraMailboxdSSLProtocols:/{print $2}' | tr '\n' ' ')
  if ! printf '%s' "$mbproto" | grep -q 'TLSv1.3'; then
    zimbra_cmd zmprov ms "$(zimbra_cmd zmhostname)" +zimbraMailboxdSSLProtocols TLSv1.3 || true
    info "Added TLSv1.3 to zimbraMailboxdSSLProtocols (takes full effect on next mailboxd restart - not forced here)"
  else
    ok "mailboxd SSL protocols include TLSv1.3"
  fi

  # Nothing changed in the directory THIS run, but that says nothing about
  # whether this node's nginx was ever rebuilt from it. See
  # proxy_config_has_ciphers: on a split the mailbox writes these settings
  # first, so the edge - the only machine serving TLS - finds them already
  # correct and would never regenerate.
  #
  # Split only, deliberately. One appliance writes and regenerates in the same
  # run, so the gap cannot open there, and this check would be the only thing
  # deciding whether to restart the proxy on the product's main path. If the
  # generated config ever spelled the cipher list differently from the
  # directory, that would mean a proxy restart on every single run of this
  # stage. Not a risk worth taking to fix a bug that shape of deployment
  # cannot have.
  if [ "$KIN_NODE_ROLE" != "all" ] && [ "$need_proxy_reload" -eq 0 ] && node_runs_proxy \
     && ! proxy_config_has_ciphers "$desired_ciphers"; then
    need_proxy_reload=1
    info "Directory already has the modern cipher list, but this node's nginx"
    info "does not. Regenerating so the machine that serves TLS actually uses it."
  fi

  if [ "$need_proxy_reload" -eq 1 ]; then
    if [ "${KIN_HARDENING_SKIP_PROXY_RESTART:-0}" = "1" ]; then
      warn "TLS LDAP changed but KIN_HARDENING_SKIP_PROXY_RESTART=1 - not restarting zmproxy"
      return 0
    fi
    # The settings above are global; this regeneration is not.
    #
    # zimbraReverseProxySSL* live in the directory and apply to whichever node
    # runs nginx. Rewriting nginx's config and restarting it can only happen on
    # that node. Calling it where there is no proxy failed the whole stage after
    # every setting had already been written correctly, on a deployment where
    # mail was working.
    if ! node_runs_proxy; then
      ok "Proxy TLS settings written to the directory"
      info "This node does not run the proxy, so there is no nginx here to"
      info "regenerate. They take effect on the node that does - on a split,"
      info "the edge - when 09-hardening runs there."
      return 0
    fi
    warn "Regenerating nginx + restarting zmproxy (kin-zimbra temporarily unmanaged)"
    kin_zimbra_unmanage
    trap kin_zimbra_remanage EXIT
    if ! zimbra_cmd /opt/zimbra/libexec/zmproxyconfgen >/tmp/kin-hardening-confgen.out 2>&1; then
      fail "zmproxyconfgen failed - see /tmp/kin-hardening-confgen.out"
      kin_zimbra_remanage
      trap - EXIT
      exit 1
    fi
    if ! zimbra_cmd zmproxyctl restart >/tmp/kin-hardening-proxy.out 2>&1; then
      fail "zmproxyctl restart failed - see /tmp/kin-hardening-proxy.out"
      kin_zimbra_remanage
      trap - EXIT
      exit 1
    fi
    if ! kin_zimbra_wait_healthy; then
      fail "https://127.0.0.1/ not 200 after proxy restart"
      kin_zimbra_remanage
      trap - EXIT
      exit 1
    fi
    kin_zimbra_remanage
    trap - EXIT
    ok "Proxy restarted; https=200; kin-zimbra managed again"
  else
    ok "No proxy restart required"
  fi

  # Prove TLS1.2 works (TLS1.0/1.1 already rejected by proxy ssl_protocols).
  # Nothing answers :443 on a node without the proxy, and a warning there would
  # be reporting the absence of a service this node was never given.
  if ! node_runs_proxy; then
    info "No proxy on this node, so no :443 handshake to prove here."
  elif echo | timeout 5 openssl s_client -connect 127.0.0.1:443 -tls1_2 2>/dev/null | grep -q 'Protocol  : TLSv1.2'; then
    ok "openssl s_client -tls1_2 succeeds"
  else
    warn "Could not confirm TLS1.2 handshake via openssl s_client"
  fi
}

configure_mail_surface() {
  say "8. Mail-borne and browser-facing surface"
  if [ ! -d /opt/zimbra ]; then
    warn "Skipping mail surface - /opt/zimbra not present"
    return 0
  fi

  # --- relay scope ----------------------------------------------------------
  local nets
  nets=$(zimbra_cmd zmprov gacf zimbraMtaMyNetworks 2>/dev/null |
         sed -n 's/^zimbraMtaMyNetworks: //p' | tr '\n' ' ')
  if [ -z "$nets" ]; then
    warn "zimbraMtaMyNetworks is unset; Postfix falls back to its own default"
  elif relay_scope_too_wide "$nets"; then
    fail "zimbraMtaMyNetworks relays for a very wide range: ${nets}"
    info "Anything listed there can send through this server without"
    info "authenticating. Narrow it to this host and its LAN:"
    info "  su - zimbra -c 'zmprov mcf zimbraMtaMyNetworks \"127.0.0.0/8 <lan>/24\"'"
    info "Not changed automatically: relay scope is a deployment decision."
  else
    ok "Relay scope is bounded (${nets})"
  fi

  # --- attachment types -----------------------------------------------------
  # Executable content delivered by mail is still the most common way into a
  # network. Zimbra rejects these at the MTA, before they reach a mailbox.
  if [ "${KIN_BLOCK_EXECUTABLE_ATTACHMENTS:-1}" = "1" ]; then
    local want="exe scr vbs vbe js jse bat cmd com pif cpl msi msp hta lnk reg jar wsf wsh"
    local have missing=0 ext
    have=$(zimbra_cmd zmprov gacf zimbraMtaBlockedExtension 2>/dev/null |
           sed -n 's/^zimbraMtaBlockedExtension: //p' | tr '\n' ' ')
    for ext in $want; do
      printf '%s' " $have " | grep -q " ${ext} " || missing=1
    done
    if [ "$missing" -eq 0 ]; then
      ok "Executable attachment types already blocked at the MTA"
    else
      for ext in $want; do
        printf '%s' " $have " | grep -q " ${ext} " && continue
        zimbra_cmd zmprov mcf +zimbraMtaBlockedExtension "$ext" >/dev/null 2>&1 || true
      done
      ok "Blocked executable attachment types at the MTA (${want})"
      info "Set KIN_BLOCK_EXECUTABLE_ATTACHMENTS=0 and re-run to leave them alone."
    fi
  else
    warn "KIN_BLOCK_EXECUTABLE_ATTACHMENTS=0: executable attachments are delivered"
  fi

  # --- browser-facing headers ----------------------------------------------
  # Added one at a time with `+`, never assigned wholesale: this attribute is
  # multi-valued and Zimbra puts its own entries in it.
  local cur headers_changed=0
  cur=$(zimbra_cmd zmprov gacf zimbraResponseHeader 2>/dev/null |
        sed -n 's/^zimbraResponseHeader: //p')
  add_response_header() {
    local name="$1" value="$2"
    if printf '%s\n' "$cur" | grep -qi "^${name}:"; then
      ok "Response header already set: ${name}"
      return 0
    fi
    if zimbra_cmd zmprov mcf +zimbraResponseHeader "${name}: ${value}" >/dev/null 2>&1; then
      ok "Set response header ${name}: ${value}"
      headers_changed=1
    else
      warn "Could not set response header ${name}"
    fi
  }
  # SAMEORIGIN, not DENY: the web client frames its own documents.
  add_response_header "X-Content-Type-Options" "nosniff"
  add_response_header "X-Frame-Options" "SAMEORIGIN"
  add_response_header "Referrer-Policy" "strict-origin-when-cross-origin"

  if cert_is_trusted; then
    add_response_header "Strict-Transport-Security" "max-age=31536000"
  else
    warn "Skipping HSTS: the certificate on :443 is still self-signed."
    warn "HSTS would remove the browser's 'proceed anyway' option and lock you"
    warn "out of the console. Finish stage 04, then re-run this stage."
  fi

  if [ "$headers_changed" -eq 0 ]; then
    ok "Response headers already in place; not restarting zmproxy"
    return 0
  fi
  if [ "${KIN_HARDENING_SKIP_PROXY_RESTART:-0}" = "1" ]; then
    warn "Headers set but KIN_HARDENING_SKIP_PROXY_RESTART=1 - not restarting zmproxy"
    warn "They take effect on the next proxy restart."
    return 0
  fi
  # Never restart zmproxy while Pacemaker is watching it. The monitor sees the
  # blip, decides kin-zimbra has failed, and tears down the VIP - or fences the
  # node. Same unmanage/remanage dance stage 5 above already uses.
  warn "Restarting zmproxy for the new headers (kin-zimbra temporarily unmanaged)"
  kin_zimbra_unmanage
  trap kin_zimbra_remanage EXIT
  if ! zimbra_cmd zmproxyctl restart >/tmp/kin-hardening-headers.out 2>&1; then
    warn "zmproxyctl restart failed - see /tmp/kin-hardening-headers.out"
    warn "Headers are stored and take effect on the next proxy restart."
  elif ! kin_zimbra_wait_healthy; then
    warn "Zimbra did not report healthy after the proxy restart; check zmcontrol status"
  else
    ok "zmproxy restarted; response headers are live"
  fi
  kin_zimbra_remanage
  trap - EXIT
}

configure_smtp_rates() {
  say "6. SMTP client rate limits (Postfix / Zimbra MTA)"
  # Auth limit is a first-class LDAP attr (zmconfigd → main.cf). Connection/message
  # rates are not mapped in stock zmconfigd.cf - set via postconf after rewrite and
  # re-assert on each run so upgrades that rewrite main.cf get corrected.
  local auth="${KIN_SMTP_AUTH_RATE_LIMIT:-30}"
  local conn="${KIN_SMTP_CONN_RATE_LIMIT:-30}"
  local msg="${KIN_SMTP_MSG_RATE_LIMIT:-120}"
  local cur_auth cur_conn cur_msg ldap_auth need_rewrite=0

  ldap_auth=$(zimbra_cmd zmprov gs "$(zimbra_cmd zmhostname)" zimbraMtaSmtpdClientAuthRateLimit 2>/dev/null \
    | awk '/zimbraMtaSmtpdClientAuthRateLimit:/{print $2; exit}')
  cur_auth=$(zimbra_cmd postconf -h smtpd_client_auth_rate_limit 2>/dev/null || echo "")
  cur_conn=$(zimbra_cmd postconf -h smtpd_client_connection_rate_limit 2>/dev/null || echo "")
  cur_msg=$(zimbra_cmd postconf -h smtpd_client_message_rate_limit 2>/dev/null || echo "")

  if [ "${ldap_auth:-}" = "$auth" ] && [ "$cur_auth" = "$auth" ] \
    && [ "$cur_conn" = "$conn" ] && [ "$cur_msg" = "$msg" ]; then
    ok "SMTP rate limits already auth=${auth} conn=${conn} msg=${msg}"
    return 0
  fi

  if [ "${ldap_auth:-}" != "$auth" ]; then
    zimbra_cmd zmprov ms "$(zimbra_cmd zmhostname)" zimbraMtaSmtpdClientAuthRateLimit "$auth"
    need_rewrite=1
    ok "Set zimbraMtaSmtpdClientAuthRateLimit=${auth}"
  else
    ok "zimbraMtaSmtpdClientAuthRateLimit already ${auth}"
  fi

  # Keep localconfig in sync for operators reading zmlocalconfig (auth also mirrored).
  zimbra_cmd zmlocalconfig -e "postfix_smtpd_client_auth_rate_limit=${auth}"
  zimbra_cmd zmlocalconfig -e "postfix_smtpd_client_connection_rate_limit=${conn}"
  zimbra_cmd zmlocalconfig -e "postfix_smtpd_client_message_rate_limit=${msg}"

  if [ "${KIN_HARDENING_SKIP_MTA_RELOAD:-0}" = "1" ]; then
    warn "Rates updated in LDAP/localconfig but KIN_HARDENING_SKIP_MTA_RELOAD=1 - not rewriting MTA"
    return 0
  fi

  # Prefer configrewrite + postfix reload. Avoid zmmtactl reload - it can stop MTA
  # mid-flight on some FOSS builds without a clean restart.
  if [ "$need_rewrite" -eq 1 ] || [ "$cur_auth" != "$auth" ]; then
    if ! zimbra_cmd /opt/zimbra/libexec/configrewrite mta >/tmp/kin-hardening-mta-rewrite.out 2>&1; then
      fail "configrewrite mta failed - see /tmp/kin-hardening-mta-rewrite.out"
      exit 1
    fi
    ok "Rewrote MTA config (auth rate via zmconfigd)"
  fi

  # Connection/message: not in stock zmconfigd mapping - apply directly.
  if ! zimbra_cmd postconf -e \
    "smtpd_client_connection_rate_limit=${conn}" \
    "smtpd_client_message_rate_limit=${msg}" >/tmp/kin-hardening-postconf.out 2>&1; then
    fail "postconf -e failed - see /tmp/kin-hardening-postconf.out"
    exit 1
  fi

  if ! zimbra_cmd postfix reload >/tmp/kin-hardening-postfix-reload.out 2>&1; then
    fail "postfix reload failed - see /tmp/kin-hardening-postfix-reload.out"
    exit 1
  fi

  cur_auth=$(zimbra_cmd postconf -h smtpd_client_auth_rate_limit 2>/dev/null || echo "")
  cur_conn=$(zimbra_cmd postconf -h smtpd_client_connection_rate_limit 2>/dev/null || echo "")
  cur_msg=$(zimbra_cmd postconf -h smtpd_client_message_rate_limit 2>/dev/null || echo "")
  info "Effective postconf: auth=${cur_auth} conn=${cur_conn} msg=${cur_msg}"
  if [ "$cur_auth" != "$auth" ] || [ "$cur_conn" != "$conn" ] || [ "$cur_msg" != "$msg" ]; then
    fail "postconf values did not match desired limits after reload"
    exit 1
  fi
  ok "MTA reloaded; SMTP client rate limits active"
}

test_fail2ban_ban_unban() {
  say "7. fail2ban ban/unban smoke test (TEST-NET IP only)"
  local test_ip="203.0.113.77" admin_tok ignore_cfg
  if ! fail2ban-client status zpush-auth >/dev/null 2>&1; then
    warn "zpush-auth jail not running - skip ban smoke test"
    return 0
  fi
  ignore_cfg=$(grep -E '^ignoreip[[:space:]]*=' /etc/fail2ban/jail.d/kin-mail.conf 2>/dev/null || true)
  for admin_tok in ${KIN_ADMIN_IPS:-}; do
    [ -n "$admin_tok" ] || continue
    case " ${ignore_cfg#ignoreip=} " in
      *" ${admin_tok} "*) ok "ignoreip includes KIN_ADMIN_IPS token ${admin_tok}" ;;
      *)
        fail "ignoreip missing KIN_ADMIN_IPS token ${admin_tok}: ${ignore_cfg}"
        exit 1
        ;;
    esac
  done
  # Note: fail2ban-client banip bypasses ignoreip (operator override). Protection is
  # log-driven Ban actions - verified live with injected auth fails in progress logs.
  fail2ban-client set zpush-auth banip "$test_ip" >/dev/null
  if fail2ban-client status zpush-auth 2>/dev/null | grep -q "$test_ip"; then
    ok "Banned test IP ${test_ip} in zpush-auth (non-admin still protected)"
  else
    warn "Ban of ${test_ip} not visible in jail status"
  fi
  fail2ban-client set zpush-auth unbanip "$test_ip" >/dev/null
  if fail2ban-client status zpush-auth 2>/dev/null | grep -q "$test_ip"; then
    warn "Unban of ${test_ip} not visible yet (jail still listed it)"
    return 0
  fi
  ok "Unbanned ${test_ip} - recovery path verified"
  info "Operator unban: fail2ban-client set zpush-auth unbanip <ip>"
}

# -----------------------------------------------------------------------------
if [ "$STATUS_ONLY" -eq 1 ]; then
  show_status
  exit 0
fi

echo
say "KIN Mail hardening Part A"
info "Host firewall (10) and admin-path lockdown (11) are separate stages."

configure_fail2ban
configure_unattended

if [ "$OS_ONLY" -eq 1 ]; then
  ok "OS-only mode complete (fail2ban + unattended-upgrades)"
  show_status
  exit 0
fi

if [ ! -d /opt/zimbra ]; then
  fail "Zimbra tree missing - use --os-only on Secondary, or run on Primary with /opt/zimbra mounted"
  exit 1
fi

# On a split, these are not all the same machine's business.
#
#   configure_smtp_rates   Postfix rate limits. Postfix is on the EDGE; the
#                          mailbox has zimbra-ldap and zimbra-store and no MTA
#                          at all, so zmprov would write attributes that
#                          nothing reads and the sanity checks would report a
#                          misconfiguration that is really an absence.
#   configure_mail_surface zimbraMtaMyNetworks, blocked attachment extensions,
#                          the open-relay guard: all MTA settings, all edge.
#
# The rest - password lockout, cleartext login, TLS protocols and ciphers - are
# directory and mailboxd settings and belong on whichever node is running.
# They are global in LDAP, so setting them from either node is correct.
KIN_NODE_ROLE="all"
if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
  KIN_NODE_ROLE=$(kin_node_role) || {
    fail "TOPOLOGY=split but this host's role cannot be determined."
    info "Check EDGE_IP and MAILBOX_IP in ${CONF_FILE} against this machine's addresses."
    exit 1
  }
  info "Split deployment; this host is the ${KIN_NODE_ROLE} node"
fi

configure_lockout
configure_cleartext
configure_tls
if [ "$KIN_NODE_ROLE" = "mailbox" ]; then
  say "6/8. MTA hardening - skipped on the mailbox node"
  info "Rate limits, blocked attachments and the relay scope live on the edge,"
  info "which is the machine that runs Postfix. Run this stage there too."
else
  configure_smtp_rates
  configure_mail_surface
fi
test_fail2ban_ban_unban

echo
say "Part A complete"
if command -v pcs >/dev/null 2>&1; then
  pcs status 2>/dev/null | grep -E 'kin-zimbra|kin-vip|FAILED|Promoted' | sed 's/^/    /' || true
fi
if ! node_runs_proxy; then
  info "Web health is checked on the node that serves it, not this one."
elif cluster_ok; then
  ok "Cluster web health https=200"
else
  warn "https check failed - investigate"
fi
exit 0
