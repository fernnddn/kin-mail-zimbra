#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 03 INSTALL ZIMBRA
#
# Downloads the Maldua Zimbra FOSS build, verifies its checksum, and drives the
# interactive installer inside tmux by reading the screen and answering the
# prompts it recognises.
#
#   ./03-install-zimbra.sh            drive the installer automatically
#   ./03-install-zimbra.sh --manual   only download and extract, then print the
#                                     answer sequence and let you drive it
#
# Watch it live from another terminal:  tmux attach -t zcs
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

MANUAL=0; [ "${1:-}" = "--manual" ] && MANUAL=1
SESS="zcs"
LOG="/var/log/kin-mail-install.log"

# The interactive installer may echo the admin password (typed via tmux) and can
# dump CREATEADMINPASS / LDAP*PASS into the apply/save stream. Bare `tee` would
# leave those values plaintext in $LOG. Redact known secrets before anything is
# written, keep the log mode 0600, and scrub again after verification.
scrub_install_log() {
  local f="${1:-$LOG}"
  [ -f "$f" ] || return 0
  ADMIN_PASS="$ADMIN_PASS" perl -i -pe '
    BEGIN { $p = quotemeta($ENV{ADMIN_PASS} // ""); }
    s/$p/***REDACTED***/g if length $p;
    s/^(\s*(?:CREATEADMINPASS|LDAPROOTPASS|LDAPADMINPASS|LDAPPOSTPASS|LDAPAMAVISPASS|LDAPREPPASS|zimbra_store_adminpass)\s*=\s*).*/$1***REDACTED***/i;
  ' "$f"
  chmod 600 "$f" 2>/dev/null || true
}

# --- 1. download and verify --------------------------------------------------
say "1. Downloading ${ZCS_FILE}"
mkdir -p "$ZCS_SRC"; cd "$ZCS_SRC"

[ -f "${ZCS_FILE}.sha256" ] || curl -sL -m 120 -o "${ZCS_FILE}.sha256" "${ZCS_BASE}/${ZCS_FILE}.sha256"
if [ ! -s "${ZCS_FILE}.sha256" ]; then fail "Could not download checksum"; exit 1; fi

if [ ! -s "$ZCS_FILE" ]; then
  info "Downloading (several hundred MB, please wait)"
  wget -q --show-progress -c -O "$ZCS_FILE" "${ZCS_BASE}/${ZCS_FILE}" || { fail "Download failed"; exit 1; }
fi

# Never install an unverified tarball: this is a community rebuild, so the
# checksum is the only integrity control available.
if sha256sum -c "${ZCS_FILE}.sha256" >/dev/null 2>&1; then
  ok "SHA-256 match"
else
  fail "SHA-256 MISMATCH - file corrupt or modified. Stopping."
  exit 1
fi

ZDIR="${ZCS_SRC}/${ZCS_FILE%.tgz}"
[ -d "$ZDIR" ] || tar xzf "$ZCS_FILE"
[ -x "$ZDIR/install.sh" ] || { fail "install.sh not found in $ZDIR"; exit 1; }
ok "Extracted to $ZDIR"

# --- manual mode -------------------------------------------------------------
if [ $MANUAL -eq 1 ]; then
  echo; say "MANUAL MODE - run yourself:"
  echo "    cd $ZDIR && ./install.sh --platform-override --skip-activation-check"
  echo
  say "Correct answer sequence:"
  cat <<EOF
    Do you agree with the terms ..............  Y
    Use Zimbra's package repository ..........  Y   <- MUST be Y. *-components packages
                                                     exist only in that repo; if N
                                                     installation stops.
    Install zimbra-ldap / logger / mta .......  Y
    Install zimbra-dnscache ..................  N   <- avoid port 53 conflict
    Install zimbra-snmp / store / apache .....  Y
    Install zimbra-spell / memcached / proxy ..  Y   <- memcached appears as a
                                                     separate prompt when from repo
    The system will be modified. Continue? ...  Y
    Change hostname ..........................  No
    Change domain name? ......................  Yes -> ${MAIL_DOMAIN}
    Menu 1 -> 7 TimeZone .....................  ${ZIMBRA_TZ_NAME}
    Menu 6 -> 4 Admin Password ...............  (your password)
    a -> Yes -> Enter -> Yes .................  apply
    Notify Zimbra of your installation? ......  No  <- sends admin email to Zimbra
EOF
  echo; exit 0
fi

# --- 2. drive the installer --------------------------------------------------
# The download/extract steps above are safe to re-run. The tmux driver is not:
# a second pass against an already-installed system hits upgrade/reconfigure
# prompts the state machine does not recognise (hang or wrong answers).
SKIP_INSTALLER=0
if [ -d /opt/zimbra ]; then
  STATUS_PRE=$(su - zimbra -c "zmcontrol status" 2>&1) || STATUS_PRE=""
  STOPPED_PRE=$(printf '%s' "$STATUS_PRE" | grep -v Running | grep -vE "^Host|^$" | wc -l | tr -d ' ')
  if [ -n "$STATUS_PRE" ] && [ "${STOPPED_PRE:-1}" -eq 0 ]; then
    say "2. Installer skipped — Zimbra already installed and healthy"
    ok "/opt/zimbra exists; zmcontrol status: all services Running"
    info "Re-run does not re-run install.sh (avoids upgrade/reconfigure flow)."
    SKIP_INSTALLER=1
  else
    fail "Zimbra exists at /opt/zimbra but not all services are Running (or status unreadable)."
    info "This script will not re-run the installer on a partial/broken installation."
    info "Fix manually first: repair stopped services, or uninstall cleanly then run 03 again."
    if [ -n "$STATUS_PRE" ]; then
      printf '%s\n' "$STATUS_PRE" | sed 's/^/    /'
    fi
    exit 1
  fi
fi

if [ "$SKIP_INSTALLER" -eq 0 ]; then
say "2. Running installer in tmux (attach: tmux attach -t ${SESS})"

# Prepare a mode-0600 log and a redacting filter so ADMIN_PASS never lands in
# $LOG even if the installer echoes it or dumps CREATEADMINPASS=... on apply.
: > "$LOG"
chmod 600 "$LOG"

ENVF=$(mktemp /tmp/kin-mail-install.env.XXXXXX)
REDACTOR=$(mktemp /tmp/kin-mail-redact.XXXXXX)
chmod 600 "$ENVF"
chmod 700 "$REDACTOR"
printf 'ADMIN_PASS=%q\n' "$ADMIN_PASS" > "$ENVF"
cat > "$REDACTOR" <<EOF
#!/usr/bin/env bash
set -u
# shellcheck disable=SC1090
. "$ENVF"
export ADMIN_PASS
exec perl -pe '
  BEGIN { \$p = quotemeta(\$ENV{ADMIN_PASS} // ""); }
  s/\$p/***REDACTED***/g if length \$p;
  s/^(\\s*(?:CREATEADMINPASS|LDAPROOTPASS|LDAPADMINPASS|LDAPPOSTPASS|LDAPAMAVISPASS|LDAPREPPASS|zimbra_store_adminpass)\\s*=\\s*).*/\$1***REDACTED***/i;
'
EOF

cleanup_redactor() {
  rm -f "$ENVF" "$REDACTOR"
}
trap cleanup_redactor EXIT

tmux kill-session -t "$SESS" 2>/dev/null
tmux new-session -d -s "$SESS" -x 200 -y 50
# Run install.sh on a real TTY (the tmux pane). Piping stdout breaks the driver:
# bash fully-buffers when stdout is a pipe, so prompts never reach capture-pane
# and the installer blocks forever on read. Log via pipe-pane instead (redact then tee).
#
# Security trade-off: before this change, both the live tmux pane and $LOG went
# through the redactor, so ADMIN_PASS never appeared in either place. With
# pipe-pane, only $LOG is redacted — the live pane (tmux capture-pane / attach)
# shows the admin password in cleartext for the whole typed-password prompt
# window (not just a flash). Accepted because the session is root-only on the
# mail host, attach requires root, and the durable artifact operators copy off
# box is $LOG (mode 0600, scrubbed). Do not widen pane access (e.g. shared
# read-only attach) without restoring pane-level redaction.
tmux pipe-pane -t "$SESS" -o "$REDACTOR | tee -a $LOG"
tmux send-keys -t "$SESS" "cd $ZDIR && ./install.sh --platform-override --skip-activation-check" Enter

send()   { tmux send-keys -t "$SESS" "$1" Enter; sleep "${2:-2}"; }
scr()    { tmux capture-pane -p -t "$SESS" | grep -v '^[[:space:]]*$'; }
lastln() { scr | tail -1; }

STAGE="packages"
DEADLINE=$(( $(date +%s) + 3600 ))

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 4
  L="$(lastln)"

  case "$L" in
    *"agree with the terms"*)              info "EULA -> Y";              send Y 6 ;;
    *"Use Zimbra's package repository"*)   info "repo Zimbra -> Y";       send Y 45 ;;
    *"Install zimbra-dnscache"*)           info "dnscache -> N";          send N 3 ;;
    *"Install zimbra-"*)                   send Y 3 ;;
    *"system will be modified"*)           info "commit install";       send Y 20 ;;
    *"Change hostname"*)                   info "hostname kept"; send No 6 ;;
    *"Change domain name"*)                info "change domain -> Yes";    send Yes 5 ;;
    *"Create domain:"*)                    info "domain -> ${MAIL_DOMAIN}"; send "$MAIL_DOMAIN" 12 ;;
    *"Notify Zimbra of your installation"*) info "telemetry -> No";       send No 10 ;;
    *"press return to exit"*)              info "done";                send "" 5; break ;;

    *"Address unconfigured"*|*"press 'a' to apply"*)
        if [ "$STAGE" = "packages" ]; then
          STAGE="menus"
          info "Configuration menu reached - setting timezone, domain, password"

          # --- timezone (Common Configuration -> 7) ---
          send 1 4
          send 7 4
          TZIDX=$(tmux capture-pane -p -S -400 -t "$SESS" \
                  | grep -E "^[0-9]+ ${ZIMBRA_TZ_NAME}$" | tail -1 | awk '{print $1}')
          if [ -n "$TZIDX" ]; then
            info "timezone ${ZIMBRA_TZ_NAME} -> option ${TZIDX}"; send "$TZIDX" 5
          else
            warn "Timezone ${ZIMBRA_TZ_NAME} not in list, skipped"; send "" 3
          fi
          send r 4

          # --- admin password (Store -> 4) ---
          send 6 4
          send 4 3
          tmux send-keys -t "$SESS" "$ADMIN_PASS" Enter; sleep 5
          send r 4

          # --- apply ---
          info "apply"
          send a 6
          send Yes 6      # save config to file
          send "" 6       # accept default filename
          send Yes 30     # the system will be modified
        fi
        ;;
  esac
done
fi

# --- 3. verify ---------------------------------------------------------------
echo; say "3. Verification"
if [ "$SKIP_INSTALLER" -eq 0 ]; then
  sleep 5
fi
# Scrub even on failure paths so a broken install cannot leave a dirty log behind.
scrub_install_log "$LOG"
if [ ! -d /opt/zimbra ]; then fail "/opt/zimbra missing - installation failed. See $LOG"; exit 1; fi

STATUS=$(su - zimbra -c "zmcontrol status" 2>&1)
STOPPED=$(printf '%s' "$STATUS" | grep -v Running | grep -vE "^Host|^$" | wc -l)
printf '%s\n' "$STATUS" | sed 's/^/    /'

if [ "$STOPPED" -eq 0 ]; then
  ok "All services running"
else
  fail "$STOPPED service(s) not running"
fi

su - zimbra -c "zmcontrol -v" 2>/dev/null | sed 's/^/    /'
echo "    domain: $(su - zimbra -c 'zmprov gad' 2>/dev/null | tr '\n' ' ')"

# Final scrub + proof the admin password is absent from the install log.
# On a healthy skip there may be no fresh tee log; only gate when the file exists.
if [ -f "$LOG" ]; then
  scrub_install_log "$LOG"
  if grep -Fq -- "$ADMIN_PASS" "$LOG" 2>/dev/null; then
    fail "Admin password still detected in $LOG after redact - check filter"
    exit 1
  fi
  ok "Install log clean of admin password (${LOG}, mode $(stat -c %a "$LOG" 2>/dev/null || stat -f %Lp "$LOG"))"
elif [ "$SKIP_INSTALLER" -eq 1 ]; then
  info "No new installer log (driver skipped)"
fi

# The installer "save config to file" step writes /opt/zimbra/config.<pid> with
# plaintext CREATEADMINPASS / LDAP*PASS. That file is not used by a running
# mailbox; delete it after a successful verify so secrets are not left under
# /opt/zimbra with the installer's default permissions.
secure_zimbra_install_config() {
  local f mode removed=0

  while IFS= read -r -d '' f; do
    mode=$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")
    info "Removing installer config ${f} (mode ${mode})"
    rm -f -- "$f"
    removed=$((removed + 1))
  done < <(find /opt/zimbra -maxdepth 1 -type f -name 'config.*' -print0 2>/dev/null)

  if [ "$removed" -eq 0 ]; then
    info "No /opt/zimbra/config.* (nothing to clean)"
  else
    ok "Installer config file(s) removed (${removed} file(s))"
  fi

  # Gate: nothing may remain world-readable or containing ADMIN_PASS.
  while IFS= read -r -d '' f; do
    mode=$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")
    case "$mode" in
      *[4567])
        fail "Remaining ${f} is still world-readable (mode ${mode})"
        return 1
        ;;
    esac
    if grep -Fq -- "$ADMIN_PASS" "$f" 2>/dev/null; then
      fail "Remaining ${f} still contains plaintext ADMIN_PASS"
      return 1
    fi
    fail "Remaining ${f} after cleanup (mode ${mode}) - should have been deleted"
    return 1
  done < <(find /opt/zimbra -maxdepth 1 -type f -name 'config.*' -print0 2>/dev/null)

  ok "Gate /opt/zimbra/config.*: no files left behind"
  return 0
}

if ! secure_zimbra_install_config; then
  exit 1
fi

echo
say "DONE"
info "Webmail : https://${MAIL_HOST}"
info "Admin   : https://${MAIL_HOST}:7071  (admin@${MAIL_DOMAIN})"
info "Certificate is still self-signed. Continue to 04-tls-dkim.sh"
echo
tmux kill-session -t "$SESS" 2>/dev/null
if declare -F cleanup_redactor >/dev/null 2>&1; then
  cleanup_redactor
  trap - EXIT
fi
