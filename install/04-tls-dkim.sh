#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 04 TLS + DKIM
#
# Issues or installs a TLS certificate according to TLS_METHOD from config:
#   cloudflare - Let's Encrypt DNS-01 via Cloudflare API (auto-renewable)
#   manual - Let's Encrypt DNS-01 interactive (operator creates TXT)
#   customer - skip certbot; document zmcertmgr install of customer files
#
# Then generates the DKIM key and prints the record to publish.
#
# DNS-01 (cloudflare/manual) is used when the host sits behind NAT with no
# inbound HTTP path for HTTP-01.
# =============================================================================
set -u

# Certbot's --manual-auth-hook is run with stdout/stderr captured
# (subprocess.PIPE) and only reported after the hook exits. Poll-mode waits
# inside that hook for DNS, so tee'd challenge lines never reach the console
# log live. CERTBOT_VALIDATION is also only known when certbot invokes the
# hook, so it cannot be printed before certbot certonly.
#
# Same idea as kin_privhelper.commands._watch_log_file: follow the file the
# writer already tees to, from this script, whose stdout the console streams.
# dd writes via the kernel, so a piped SSH session is not block-buffered the
# way GNU tail -F would be.
follow_file_while() {
  local file="$1"
  shift
  local stop follower_pid rc=0
  [ -n "$file" ] || return 2
  : > "$file" || return 1
  stop=$(mktemp "${TMPDIR:-/tmp}/kin-mail-acme-follow.XXXXXX") || return 1
  rm -f "$stop"

  (
    offset=0
    last_head=""
    drain() {
      local size head do_reset=0
      [ -f "$file" ] || return 0
      size=$(wc -c < "$file" | tr -d '[:space:]')
      case "$size" in
        ''|*[!0-9]*) return 0 ;;
      esac
      head=$(dd if="$file" bs=1 count=64 2>/dev/null || true)
      # Truncate+rewrite can land in one poll with size still >= offset.
      # Reset when the new head is not an extension of the bytes we already
      # saw (same rule as _watch_log_file).
      if [ "$size" -lt "$offset" ]; then
        do_reset=1
      elif [ -n "$last_head" ] && [ -n "$head" ]; then
        if [ "${head#"$last_head"}" = "$head" ] && [ "${last_head#"$head"}" = "$last_head" ]; then
          do_reset=1
        fi
      fi
      if [ "$do_reset" -eq 1 ]; then
        offset=0
      fi
      last_head="$head"
      if [ "$size" -gt "$offset" ]; then
        dd if="$file" bs=1 skip="$offset" count="$((size - offset))" 2>/dev/null || true
        offset=$size
      fi
    }
    while [ ! -f "$stop" ]; do
      drain
      sleep "${KIN_FOLLOW_POLL_S:-0.2}"
    done
    drain
  ) &
  follower_pid=$!
  # shellcheck disable=SC2064
  trap "kill ${follower_pid} 2>/dev/null || true; rm -f '${stop}'" INT TERM
  "$@" || rc=$?
  trap - INT TERM
  : > "$stop"
  wait "$follower_pid" 2>/dev/null || true
  rm -f "$stop"
  return "$rc"
}

if [ "${KIN_TLS_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

cd "$(dirname "$0")" && . ./00-config.sh
# apt on a freshly booted host: wait out apt-daily / unattended-upgrades
# instead of failing on a lock that clears itself.
# shellcheck source=lib/apt-lock.sh
. ./lib/apt-lock.sh
need_root

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
LE_DIR="/etc/letsencrypt/live/${MAIL_HOST}"
ZS="/opt/zimbra/ssl/letsencrypt"

# shellcheck disable=SC1091
. ./lib/zimbra-data-disk-probe.sh
if zimbra_is_mid_handoff; then
  warn "Mid-handoff: /opt/zimbra is unmounted; skipping TLS/DKIM (Pacemaker owns live tree)."
  exit 0
fi

[ -d /opt/zimbra ] || { fail "Zimbra is not installed yet. Run 03 first."; exit 1; }

# Public TLS is served by the proxy, and DKIM is signed by the MTA. Both of
# those are the edge. Issuing a certificate here would put it on a machine
# that does not answer :443, and zmdkimkeyutil would run where opendkim is
# not installed. The edge pipeline is what actually issues them.
if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
  _tls_role=$(kin_node_role 2>/dev/null) || _tls_role=""
  if [ "$_tls_role" = mailbox ]; then
    warn "TLS and DKIM are issued on the edge, which is the machine clients reach."
    info "This is the mailbox node; skipping. Run 04-tls-dkim.sh on the edge."
    exit 0
  fi
fi

: "${TLS_METHOD:=cloudflare}"
case "$TLS_METHOD" in
  cloudflare|manual|customer) ;;
  *) fail "Unknown TLS_METHOD: ${TLS_METHOD} (cloudflare|manual|customer)"; exit 1 ;;
esac

say "TLS method: ${TLS_METHOD}"

# The root that gets appended to every renewed chain, so a bad copy here is a
# bad copy at 3am ninety days from now, not today.
#
# `curl -s` without -f exits 0 on an HTTP error and writes the error page to
# the output file. Let's Encrypt's 404 is 23KB of HTML, so a "is the file
# non-empty" check passes happily and the deploy hook then appends that HTML to
# chain.pem. Fail on the status code, and then confirm the bytes really are the
# certificate we asked for rather than trusting their length.
install_isrg_root() {
  mkdir -p "$ZS" /etc/letsencrypt/renewal-hooks/deploy
  local dest="${ZS}/isrgrootx1.pem" tmp
  local url="${KIN_ISRG_ROOT_URL:-https://letsencrypt.org/certs/isrgrootx1.pem}"

  # A good copy already here is reused, so a re-run does not depend on the
  # network and cannot replace a working root with a worse download.
  if isrg_root_is_valid "$dest"; then
    ok "ISRG Root X1 already present and valid"
    return 0
  fi

  tmp=$(mktemp "${TMPDIR:-/tmp}/kin-isrg.XXXXXX") || {
    fail "Could not create a temporary file for the ISRG root"
    exit 1
  }
  if ! curl -fsS -m 30 -o "$tmp" "$url"; then
    rm -f "$tmp"
    fail "Could not download ISRG Root X1 from ${url}"
    info "Renewal appends this root to the chain Zimbra serves, so the install"
    info "stops here rather than continuing without it."
    exit 1
  fi
  if ! isrg_root_is_valid "$tmp"; then
    rm -f "$tmp"
    fail "What ${url} returned is not the ISRG Root X1 certificate"
    info "That is usually a captive portal or a proxy answering instead of"
    info "Let's Encrypt. Check outbound HTTPS and retry."
    exit 1
  fi
  install -m 0644 "$tmp" "$dest"
  rm -f "$tmp"
  ok "ISRG Root X1 downloaded and verified"
}

# Is this file a PEM certificate, and is it the root we expect?
isrg_root_is_valid() {
  local path="$1" subject
  [ -s "$path" ] || return 1
  subject=$(openssl x509 -in "$path" -noout -subject 2>/dev/null) || return 1
  case "$subject" in
  *"ISRG Root X1"*) return 0 ;;
  esac
  return 1
}

write_deploy_hook() {
  # Zimbra will not verify a chain that does not reach the root, so ISRG Root X1
  # is appended. The hook exists because certbot renewing the file while Zimbra
  # keeps serving the old certificate is a silent outage 90 days later.
  #
  # zmcertmgr deploycrt + zmcontrol restart stop services briefly. On HA Primary,
  # Pacemaker must unmanage kin-zimbra first (same idea as 09-hardening /
  # 11-admin-path-lockdown) or the monitor races into FAILED and drops the VIP.
  cat > /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh <<'HOOK'
#!/bin/bash
# KIN Mail - redeploy the renewed certificate into Zimbra.
# Pacemaker-aware: unmanage kin-zimbra around deploy/restart when HA-managed.
set -eu

LE="/etc/letsencrypt/live/MAIL_HOST_PLACEHOLDER"
ZS="/opt/zimbra/ssl/letsencrypt"

[ "${RENEWED_LINEAGE:-$LE}" = "$LE" ] || exit 0

# On an HA pair this node may not be the one serving mail. /opt/zimbra is the
# replicated volume: on a Secondary it is an empty mountpoint, so every command
# below would fail and certbot would report a renewal failure twice a day for
# something that is not wrong. The certificate itself renewed fine; it is
# deployed by whichever node is Primary at the time.
#
# Exit 0, and say why, rather than manufacturing a failure.
if [ ! -x /opt/zimbra/bin/zmcertmgr ]; then
  logger -t kin-mail "cert renewed but this node is not serving Zimbra (/opt/zimbra not mounted); deploy skipped"
  echo "kin-mail deploy hook: /opt/zimbra is not mounted here - this node is not"
  echo "currently serving. The certificate renewed; the serving node deploys it."
  exit 0
fi

pcs_has_kin_zimbra() {
  command -v pcs >/dev/null 2>&1 || return 1
  # pcs 0.11 replaced `resource status <id>`; prefer config, fall back to status.
  pcs resource config kin-zimbra >/dev/null 2>&1 \
    || pcs resource status kin-zimbra >/dev/null 2>&1
}

PCS_UNMANAGED=0
remanage_kin_zimbra() {
  if [ "$PCS_UNMANAGED" = "1" ]; then
    # manage alone leaves monitors enabled=0 after unmanage --monitor
    pcs resource manage kin-zimbra --monitor 2>/dev/null \
      || pcs resource manage kin-zimbra 2>/dev/null \
      || true
    PCS_UNMANAGED=0
    logger -t kin-mail "kin-zimbra re-managed (--monitor) after cert deploy"
  fi
}
trap remanage_kin_zimbra EXIT

cp "$LE/privkey.pem" "$LE/cert.pem" "$LE/chain.pem" "$LE/fullchain.pem" "$ZS/"
cat "$ZS/isrgrootx1.pem" >> "$ZS/chain.pem"
chown -R zimbra:zimbra "$ZS"
chmod 640 "$ZS"/*

su - zimbra -c "/opt/zimbra/bin/zmcertmgr verifycrt comm $ZS/privkey.pem $ZS/cert.pem $ZS/chain.pem"

cp "$ZS/privkey.pem" /opt/zimbra/ssl/zimbra/commercial/commercial.key
chown zimbra:zimbra /opt/zimbra/ssl/zimbra/commercial/commercial.key
chmod 640 /opt/zimbra/ssl/zimbra/commercial/commercial.key

if pcs_has_kin_zimbra; then
  logger -t kin-mail "kin-zimbra is Pacemaker-managed - unmanage --monitor before cert deploy/restart"
  # Without --monitor, the 30s OCF probe still fires during zmcontrol restart,
  # marks FAILED, and Pacemaker stops later group members (kin-vip). Proxy-only
  # restarts in 09/11 are short enough to often dodge that window; full restart is not.
  pcs resource unmanage kin-zimbra --monitor
  PCS_UNMANAGED=1
fi

su - zimbra -c "/opt/zimbra/bin/zmcertmgr deploycrt comm $ZS/cert.pem $ZS/chain.pem"
su - zimbra -c "zmcontrol restart"

# Wait for mailboxd/proxy to come back (restart can take minutes).
ok_https=0
i=0
while [ "$i" -lt 90 ]; do
  code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 https://127.0.0.1/ || true)
  if [ "$code" = "200" ]; then
    ok_https=1
    break
  fi
  i=$((i + 1))
  sleep 2
done
[ "$ok_https" = "1" ] || {
  echo "kin-mail deploy hook: https://127.0.0.1/ not 200 after restart" >&2
  exit 1
}

# All zmcontrol services must report Running (exclude blank/Host lines).
status_out=$(su - zimbra -c "zmcontrol status" 2>/dev/null || true)
if printf '%s\n' "$status_out" | grep -vE '^Host|^$' | grep -qv Running; then
  echo "kin-mail deploy hook: zmcontrol status not all Running:" >&2
  printf '%s\n' "$status_out" >&2
  exit 1
fi

remanage_kin_zimbra
trap - EXIT

logger -t kin-mail "Zimbra certificate redeployed after Let's Encrypt renewal"
HOOK
  # Bake MAIL_HOST into the installed hook (quoted heredoc above keeps runtime logic intact).
  sed -i "s|MAIL_HOST_PLACEHOLDER|${MAIL_HOST}|g" \
    /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh
  ok "Deploy hook installed (Pacemaker-aware)"
}

run_deploy_hook() {
  say "Deploy certificate to Zimbra (hook)"
  if /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh; then
    ok "Hook succeeded, certificate deployed"
  else
    fail "Hook failed - run manually to see its message"
    exit 1
  fi
}

verify_tls_ports() {
  say "Verify TLS on live ports"
  local spec S
  for spec in "443 https" "587 starttls-smtp" "993 imaps"; do
    # shellcheck disable=SC2086
    set -- $spec
    if [ "$2" = "starttls-smtp" ]; then
      S=$(echo | timeout 20 openssl s_client -connect 127.0.0.1:$1 -starttls smtp 2>/dev/null \
          | openssl x509 -noout -subject 2>/dev/null)
    else
      S=$(echo | timeout 20 openssl s_client -connect 127.0.0.1:$1 -servername "$MAIL_HOST" 2>/dev/null \
          | openssl x509 -noout -subject 2>/dev/null)
    fi
    [ -n "$S" ] && ok "port $1 : $S" || warn "port $1 : handshake failed"
  done
}

# --- Cloudflare DNS-01 -------------------------------------------------------
tls_cloudflare() {
  # A config file written by an older build has no CF_* keys, and this script
  # runs under set -u, so referencing them would abort with a bare "unbound
  # variable" instead of anything an operator can act on. Retrying TLS on a
  # host deployed by an earlier version is a normal thing to do, so default
  # them to the same values 00-config.sh writes for a fresh host.
  : "${CF_CREDS:=/etc/letsencrypt/cloudflare.ini}"
  : "${CF_PROPAGATION:=40}"

  say "1. certbot and Cloudflare plugin"
  kin_apt -qq update
  kin_apt -y install certbot python3-certbot-dns-cloudflare >/dev/null 2>&1
  certbot plugins 2>/dev/null | grep -qi cloudflare \
    && ok "dns-cloudflare plugin installed" \
    || { fail "dns-cloudflare plugin not available"; exit 1; }

  say "2. Cloudflare API token"
  if [ -s "$CF_CREDS" ] && ! grep -q "PASTE" "$CF_CREDS"; then
    ok "Credentials already at ${CF_CREDS}"
  else
    if [ ! -t 0 ]; then
      fail "TLS_METHOD=cloudflare needs ${CF_CREDS} with a real API token."
      info "Go back to the console wizard, TLS step, and paste the token there:"
      info "it writes ${CF_CREDS} for you, mode 600, before this script runs."
      info "Cloudflare: My Profile -> API Tokens -> Create Token, template"
      info "'Edit zone DNS', restricted to zone ${MAIL_DOMAIN} only."
      info "Or choose Manual DNS-01 in the wizard to enter the record by hand."
      exit 1
    fi
    info "Create at Cloudflare: My Profile -> API Tokens -> Create Token"
    info "Template 'Edit zone DNS', restrict to zone ${MAIL_DOMAIN} only."
    ask_secret CF_TOKEN "Paste Cloudflare API token"
    [ -z "$CF_TOKEN" ] && { fail "Token is empty."; exit 1; }
    mkdir -p /etc/letsencrypt
    printf 'dns_cloudflare_api_token = %s\n' "$CF_TOKEN" > "$CF_CREDS"
    chmod 600 "$CF_CREDS"; chown root:root "$CF_CREDS"
    ok "Saved with mode 600"
  fi

  say "3. Issuing certificate (DNS-01 Cloudflare)"
  if [ -f "${LE_DIR}/cert.pem" ] && [ "${KIN_TLS_FORCE_RENEW:-0}" != "1" ]; then
    ok "Certificate already exists, issuance skipped"
  else
    CF_FORCE=()
    [ "${KIN_TLS_FORCE_RENEW:-0}" = "1" ] && CF_FORCE+=(--force-renewal)
    certbot certonly \
      --dns-cloudflare \
      --dns-cloudflare-credentials "$CF_CREDS" \
      --dns-cloudflare-propagation-seconds "$CF_PROPAGATION" \
      -d "$MAIL_HOST" \
      --preferred-chain "ISRG Root X1" \
      --agree-tos --no-eff-email -m "$LE_EMAIL" \
      --non-interactive \
      "${CF_FORCE[@]}" 2>&1 | tail -8
    [ -f "${LE_DIR}/cert.pem" ] || { fail "Issuance failed. See /var/log/letsencrypt/"; exit 1; }
  fi
  openssl x509 -in "${LE_DIR}/cert.pem" -noout -subject -dates | sed 's/^/    /'

  say "4. Installing deploy hook"
  install_isrg_root
  write_deploy_hook
  run_deploy_hook
  verify_tls_ports

  say "5. Test automatic renewal"
  certbot renew --dry-run 2>&1 | grep -qi "simulated renewal" \
    && ok "certbot renew --dry-run succeeded" \
    || warn "dry-run inconclusive, check /var/log/letsencrypt/"

  # A passing dry-run only proves the renewal WOULD work. Something has to
  # actually run it, and nothing here ever checked that. A masked or disabled
  # timer means the certificate expires in 90 days with every other signal
  # green.
  local timer=""
  for candidate in certbot.timer snap.certbot.renew.timer; do
    if systemctl list-unit-files "$candidate" >/dev/null 2>&1 &&
      systemctl cat "$candidate" >/dev/null 2>&1; then
      timer="$candidate"
      break
    fi
  done
  if [ -z "$timer" ]; then
    warn "No certbot renewal timer found on this host."
    warn "Renewal will NOT happen on its own. Add a cron entry or install the"
    warn "certbot package that ships certbot.timer."
  elif [ "$(systemctl is-enabled "$timer" 2>/dev/null)" = "masked" ]; then
    fail "${timer} is masked; renewal will never run."
    info "Unmask and enable it:  systemctl unmask ${timer} && systemctl enable --now ${timer}"
  elif systemctl is-active --quiet "$timer" 2>/dev/null; then
    ok "${timer} is active ($(systemctl show -p NextElapseUSecRealtime --value "$timer" 2>/dev/null || echo 'next run scheduled'))"
  else
    warn "${timer} exists but is not active; enabling it"
    systemctl enable --now "$timer" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "$timer" 2>/dev/null; then
      ok "${timer} enabled and active"
    else
      fail "Could not start ${timer}; renewal will not happen automatically."
    fi
  fi
}

# --- Manual DNS-01 (any provider) --------------------------------------------
tls_manual() {
  warn "TLS_METHOD=manual - renewal is NOT automatic."
  warn "certbot --manual requires a human at the terminal for each issue/renew,"
  warn "unless a provider-specific --manual-auth-hook is added later."
  info "Do not rely on cron 'certbot renew' for this mode."

  say "1. certbot"
  kin_apt -qq update
  kin_apt -y install certbot >/dev/null 2>&1
  command -v certbot >/dev/null || { fail "certbot is not installed"; exit 1; }
  ok "certbot ready"

  say "2. Issuing certificate (DNS-01 manual)"
  # An unquoted $(...) that expands to nothing relies on word splitting to
  # vanish, which is exactly the construct that silently passes an empty
  # argument when someone later quotes it. tls_cloudflare already uses an
  # array here; so does this now.
  local MANUAL_FORCE=()
  [ "${KIN_TLS_FORCE_RENEW:-0}" = "1" ] && MANUAL_FORCE+=(--force-renewal)
  if [ -f "${LE_DIR}/cert.pem" ] && [ "${KIN_TLS_FORCE_RENEW:-0}" != "1" ]; then
    ok "Certificate already exists, issuance skipped"
  else
    # Interactive TTY: classic certbot prompts.
    # Non-TTY / KIN_MAIL_DNS_POLL=1: print TXT + poll public DNS until the
    # operator creates the record (needed for remote/bootstrap deploys).
    if [ -t 0 ] && [ "${KIN_MAIL_DNS_POLL:-0}" != "1" ]; then
      info "Interactive mode: create TXT in the DNS panel when prompted, then press Enter."
      certbot certonly \
        --manual \
        --preferred-challenges dns \
        -d "$MAIL_HOST" \
        --preferred-chain "ISRG Root X1" \
        --agree-tos --no-eff-email -m "$LE_EMAIL" \
        --manual-public-ip-logging-ok \
        ${MANUAL_FORCE[@]+"${MANUAL_FORCE[@]}"}
    else
      info "Poll mode: the DNS-01 TXT name and value will print in this log when certbot issues them."
      info "Create that TXT in the DNS panel for zone ${MAIL_DOMAIN}, then wait for propagation."
      info "The same text is also written to /tmp/kin-mail-acme-challenge.txt"
      AUTH_HOOK=$(mktemp /tmp/kin-mail-acme-auth.XXXXXX)
      CLEAN_HOOK=$(mktemp /tmp/kin-mail-acme-clean.XXXXXX)
      chmod 700 "$AUTH_HOOK" "$CLEAN_HOOK"
      cat > "$AUTH_HOOK" <<'HOOK'
#!/bin/bash
set -u
printf '%s\n' "=== KIN Mail ACME DNS-01 challenge ===" | tee /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Host/Name : _acme-challenge.${CERTBOT_DOMAIN}" | tee -a /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Type      : TXT" | tee -a /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Value     : ${CERTBOT_VALIDATION}" | tee -a /tmp/kin-mail-acme-challenge.txt
printf '%s\n' "Waiting up to 25 minutes for public DNS (1.1.1.1 + 8.8.8.8)…" | tee -a /tmp/kin-mail-acme-challenge.txt
# Slow DNS panels (e.g. legacy Rumahweb) can leave resolvers split for several
# minutes. Let's Encrypt multi-perspective validation fails if any vantage still
# sees a previous TXT - so require both public resolvers, then hold before exit.
_seen_cf=0
for _i in $(seq 1 150); do
  if dig +short TXT "_acme-challenge.${CERTBOT_DOMAIN}" @1.1.1.1 2>/dev/null \
       | grep -F "\"${CERTBOT_VALIDATION}\"" >/dev/null; then
    _seen_cf=1
    if dig +short TXT "_acme-challenge.${CERTBOT_DOMAIN}" @8.8.8.8 2>/dev/null \
         | grep -F "\"${CERTBOT_VALIDATION}\"" >/dev/null; then
      echo "TXT visible on 1.1.1.1 and 8.8.8.8 - holding 2 minutes for LE vantage points…" \
        | tee -a /tmp/kin-mail-acme-challenge.txt
      sleep 120
      # Re-check after hold (catch mid-propagation flip back to a stale token).
      if dig +short TXT "_acme-challenge.${CERTBOT_DOMAIN}" @1.1.1.1 2>/dev/null \
           | grep -F "\"${CERTBOT_VALIDATION}\"" >/dev/null \
         && dig +short TXT "_acme-challenge.${CERTBOT_DOMAIN}" @8.8.8.8 2>/dev/null \
           | grep -F "\"${CERTBOT_VALIDATION}\"" >/dev/null; then
        echo "TXT still correct after hold - proceeding" | tee -a /tmp/kin-mail-acme-challenge.txt
        exit 0
      fi
      echo "TXT changed during hold - keep waiting…" | tee -a /tmp/kin-mail-acme-challenge.txt
    elif [ "$_seen_cf" -eq 1 ]; then
      echo "TXT on 1.1.1.1 but not yet on 8.8.8.8 - keep waiting…" \
        | tee -a /tmp/kin-mail-acme-challenge.txt
    fi
  fi
  sleep 10
done
echo "TIMEOUT waiting for TXT _acme-challenge.${CERTBOT_DOMAIN}" | tee -a /tmp/kin-mail-acme-challenge.txt
exit 1
HOOK
      cat > "$CLEAN_HOOK" <<'HOOK'
#!/bin/bash
exit 0
HOOK
      follow_file_while /tmp/kin-mail-acme-challenge.txt \
        certbot certonly \
          --manual \
          --preferred-challenges dns \
          --manual-auth-hook "$AUTH_HOOK" \
          --manual-cleanup-hook "$CLEAN_HOOK" \
          -d "$MAIL_HOST" \
          --preferred-chain "ISRG Root X1" \
          --agree-tos --no-eff-email -m "$LE_EMAIL" \
          --manual-public-ip-logging-ok \
          --non-interactive \
          ${MANUAL_FORCE[@]+"${MANUAL_FORCE[@]}"}
      rm -f "$AUTH_HOOK" "$CLEAN_HOOK"
    fi
    [ -f "${LE_DIR}/cert.pem" ] || { fail "Issuance failed. See /var/log/letsencrypt/ and /tmp/kin-mail-acme-challenge.txt"; exit 1; }
  fi
  openssl x509 -in "${LE_DIR}/cert.pem" -noout -subject -dates | sed 's/^/    /'

  say "3. Installing deploy hook (for current deploy; not auto-renew)"
  install_isrg_root
  write_deploy_hook
  run_deploy_hook
  verify_tls_ports

  say "4. Automatic renewal"
  warn "Skipped / not relied upon for TLS_METHOD=manual."
  info "Before expiry: re-run 04 (or certbot certonly --manual …) with an operator present."
}

# --- Customer-provided certificate -------------------------------------------
tls_customer() {
  say "1. Customer certificate - certbot skipped"
  warn "No automated issuance. Operator installs files from the customer."
  echo
  printf '%s\n' "${BLD}  MANUAL STEPS (zmcertmgr)${RST}"
  echo   "  ----------------------------------------------------------------"
  info "1. Save files from the customer, for example:"
  info "     /root/customer-tls/${MAIL_HOST}.crt   (certificate + intermediate if needed)"
  info "     /root/customer-tls/${MAIL_HOST}.key   (private key)"
  info "     /root/customer-tls/ca-chain.pem       (CA chain to root, if separate)"
  info "2. Combine chain if needed, then verify:"
  info "     mkdir -p ${ZS} && cp … ${ZS}/"
  info "     su - zimbra -c '/opt/zimbra/bin/zmcertmgr verifycrt comm ${ZS}/privkey.pem ${ZS}/cert.pem ${ZS}/chain.pem'"
  info "3. Deploy:"
  info "     cp ${ZS}/privkey.pem /opt/zimbra/ssl/zimbra/commercial/commercial.key"
  info "     chown zimbra:zimbra /opt/zimbra/ssl/zimbra/commercial/commercial.key"
  info "     chmod 640 /opt/zimbra/ssl/zimbra/commercial/commercial.key"
  info "     su - zimbra -c '/opt/zimbra/bin/zmcertmgr deploycrt comm ${ZS}/cert.pem ${ZS}/chain.pem'"
  info "     su - zimbra -c 'zmcontrol restart'"
  echo   "  ----------------------------------------------------------------"
  echo

  if [ -f /opt/zimbra/ssl/zimbra/commercial/commercial.crt ] || \
     [ -f /opt/zimbra/ssl/zimbra/server/server.crt ]; then
    ok "Certificate found in Zimbra tree - continuing with port verification (if already deployed)"
    verify_tls_ports || true
  else
    warn "No deployed commercial cert detected - complete the steps above before production."
  fi
}

# An HA pair serves ONE certificate, for the service hostname, and it lives on
# the replicated volume with the rest of /opt/zimbra. A peer must never obtain
# its own.
#
# Left unguarded, a peer installed with TLS_METHOD=cloudflare issued a
# Let's Encrypt certificate for its OWN name (mail2.<domain>) and installed a
# renewal deploy hook baked to that lineage. The hook then writes into
# /opt/zimbra/ssl - which is the replicated volume - so the first renewal after
# the peer became Primary would have replaced the service certificate with one
# issued for mail2, and every client connecting to the service name would get a
# name mismatch. It also spends a public CA issuance on a hostname that never
# serves anything.
#
# Same reasoning as the DKIM block below: the primary and public DNS own this.
TLS_PHASE_OK=1
if kin_ha_peer_install; then
  warn "HA peer: not issuing a TLS certificate for ${MAIL_HOST}"
  info "The pair serves one certificate, for the service hostname, and it is"
  info "already on the replicated volume. This node presents it after failover."
  info "Renewal stays with the node that issued it."
else
  # A subshell, deliberately.
  #
  # Every failure inside these methods calls exit 1, which ended the stage - and
  # because this stage is in the hard-failing part of the pipeline, it ended the
  # whole install. A certificate that could not be issued then meant: no DKIM
  # key (it is created below, and has nothing to do with TLS), no hardening, no
  # host firewall, no admin-path lockdown, no healthcheck. On a split that is
  # the EDGE - the machine facing the internet - left running with none of them.
  #
  # Issuance depends on public DNS, on propagation, on a CA's rate limits and,
  # for TLS_METHOD=manual, on a human publishing a TXT record within 25 minutes.
  # Those are all outside this machine. A self-signed certificate means clients
  # see a warning; an unfirewalled edge means anyone can reach it. Letting the
  # smaller failure cause the larger one is the wrong way round.
  #
  # So the TLS phase ends here, the stage carries on, and the stage still exits
  # non-zero at the end so nobody is told TLS worked when it did not. Nothing
  # after this point reads a shell variable the subshell would have set.
  (
    case "$TLS_METHOD" in
      cloudflare) tls_cloudflare ;;
      manual)     tls_manual ;;
      customer)   tls_customer ;;
    esac
  ) || TLS_PHASE_OK=0

  if [ "$TLS_PHASE_OK" -eq 0 ]; then
    echo
    fail "TLS was not completed for ${MAIL_HOST}"
    info "Zimbra keeps serving its self-signed certificate, so mail and webmail"
    info "still work - clients will see a certificate warning until this is done."
    info "The rest of this install continues, including the host firewall."
    case "$TLS_METHOD" in
      manual)
        info "TLS_METHOD=manual waits for you to publish a DNS TXT record while"
        info "the install is running. Publish it, then re-run this stage alone:"
        info "  sudo ./04-tls-dkim.sh"
        # Let's Encrypt allows 5 failed validations per hostname per hour.
        # Re-running Deploy in a loop is the natural reaction to a red stage and
        # it is how an operator gets locked out for an hour, with a different
        # error that reads like a new problem.
        warn "Do not retry repeatedly: the CA allows 5 failed validations per"
        warn "hostname per hour. Publish the record first, then run it once."
        ;;
      cloudflare)
        info "Check the API token and that the zone is the one being served,"
        info "then re-run this stage alone: sudo ./04-tls-dkim.sh"
        ;;
    esac
    echo
  fi
fi

# --- DKIM (independent of TLS method) ----------------------------------------
# HA peer: skip. A second zmdkimkeyutil -a here would sign mail with a key that
# is not in public DNS (the primary already published one). 05 would then see
# dkim=fail and stop the HA sequence after ~90 min of zmsetup.
if kin_ha_peer_install; then
  warn "HA peer: not creating a DKIM key for ${MAIL_DOMAIN} (primary + public DNS own it)"
else
  say "DKIM"
  if su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" >/dev/null 2>&1; then
    ok "DKIM key already exists"
  else
    # The result was discarded and success announced either way. When creation
    # actually failed, this stage said "DKIM key created", printed a record
    # with an empty selector for the operator to paste, and 05-healthcheck then
    # stopped the pipeline with "DKIM key not created yet" - two stages later,
    # pointing at the wrong moment.
    if out=$(su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -a -d ${MAIL_DOMAIN}" 2>&1); then
      ok "DKIM key created"
    else
      fail "Could not create the DKIM key for ${MAIL_DOMAIN}"
      printf '%s\n' "$out" | tail -5 | sed 's/^/    /'
      info "Outbound mail will send unsigned until this is fixed. On a split,"
      info "DKIM is signed by the MTA, so run this stage on the edge."
      exit 1
    fi
  fi

  Q=$(su - zimbra -c "/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}" 2>/dev/null)
  SEL=$(printf '%s' "$Q" | awk '/DKIM Selector/{getline; while($0==""){getline}; print; exit}')
  VAL=$(printf '%s' "$Q" | awk '/DKIM Public signature/,0' | grep -oE '"[^"]*"' | tr -d '"' | tr -d '\n')

  # A record with an empty selector or empty value is not a record. Printing it
  # inside a PASTE THIS box is how it ends up in a DNS zone.
  if [ -z "$SEL" ] || [ -z "$VAL" ]; then
    fail "The DKIM key exists but its selector or public value could not be read"
    info "Read it by hand before publishing anything:"
    info "  su - zimbra -c '/opt/zimbra/libexec/zmdkimkeyutil -q -d ${MAIL_DOMAIN}'"
    exit 1
  fi

  echo
  printf '%s\n' "${BLD}  PASTE DKIM RECORD IN DNS PANEL (zone ${MAIL_DOMAIN})${RST}"
  echo   "  ----------------------------------------------------------------"
  echo   "  Type    : TXT"
  echo   "  Name    : ${SEL}._domainkey"
  echo   "  TTL     : Auto / 300"
  echo   "  Content :"
  echo   "$VAL" | fold -w 76 | sed 's/^/    /'
  echo   "  ----------------------------------------------------------------"
  info "One line, without quotes or parentheses. The DNS provider splits it as needed."
  info "Paste the PUBLIC key. The private key stays in Zimbra LDAP."
  echo
  warn "After the record is published, run: ./05-healthcheck.sh (from the install/ folder)"
  info "Running processes cache negative DNS results, so 05 will"
  info "restart dnsmasq, amavis, and opendkim before verifying DKIM."
  echo
fi

# DKIM is done, which is why this is here and not where TLS failed. The exit
# code is still the truth: the stage did not finish its job. What changed is
# that the rest of the stage, and the rest of the install, no longer stop for it.
if [ "$TLS_PHASE_OK" -eq 0 ]; then
  exit 1
fi
exit 0
