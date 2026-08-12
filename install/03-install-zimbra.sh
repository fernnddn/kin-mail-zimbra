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

_zcs_url="${ZCS_BASE}/${ZCS_FILE}"
_zcs_sum_url="${_zcs_url}.sha256"

# Drop stubs left by a prior 404 (e.g. "Not Found" checksum or 0-byte tarball).
if [ -f "${ZCS_FILE}.sha256" ] && ! grep -qE '^[0-9a-fA-F]{64}[[:space:]]' "${ZCS_FILE}.sha256" 2>/dev/null; then
  warn "Removing invalid checksum stub: ${ZCS_FILE}.sha256"
  rm -f "${ZCS_FILE}.sha256"
fi
if [ -f "$ZCS_FILE" ]; then
  _sz=$(wc -c <"$ZCS_FILE" | tr -d ' ')
  if [ "${_sz:-0}" -lt 1000000 ]; then
    warn "Removing undersized tarball stub (${_sz} bytes): ${ZCS_FILE}"
    rm -f "$ZCS_FILE"
  fi
fi

if [ ! -f "${ZCS_FILE}.sha256" ]; then
  curl -fsSL -m 120 -o "${ZCS_FILE}.sha256" "$_zcs_sum_url" || {
    fail "Could not download checksum from ${_zcs_sum_url}"
    info "Check ZCS_FILE / ZCS_BASE in /etc/kin-mail/config (Maldua filenames include a build timestamp)."
    rm -f "${ZCS_FILE}.sha256"
    exit 1
  }
fi
if [ ! -s "${ZCS_FILE}.sha256" ] || ! grep -qE '^[0-9a-fA-F]{64}[[:space:]]' "${ZCS_FILE}.sha256"; then
  fail "Checksum file invalid (HTTP 404 body or corrupt) — ${_zcs_sum_url}"
  rm -f "${ZCS_FILE}.sha256"
  exit 1
fi

if [ ! -s "$ZCS_FILE" ]; then
  info "Downloading (several hundred MB, please wait)"
  wget -q --show-progress -c -O "$ZCS_FILE" "$_zcs_url" || {
    fail "Download failed: ${_zcs_url}"
    rm -f "$ZCS_FILE"
    exit 1
  }
fi
_sz=$(wc -c <"$ZCS_FILE" | tr -d ' ')
if [ "${_sz:-0}" -lt 1000000 ]; then
  fail "Downloaded tarball too small (${_sz} bytes) — likely a failed URL: ${_zcs_url}"
  rm -f "$ZCS_FILE"
  exit 1
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

# --- 1b. Ubuntu apt packaging key (install.sh skips keyserver when present) ---
# Maldua FOSS install.sh (util/utilfunc.sh) for Ubuntu does NOT use
# files.zimbra.com/downloads/ZCSKeys/zimbra-key.asc (that path is now 404).
# It fetches key 9BE6ED79 from hkp://keyserver.ubuntu.com:80 — which needs a
# working dirmngr + writable GNUPGHOME. Console Deploy runs under
# kin-mail-privhelperd with ProtectHome=yes (so /root is inaccessible) and
# PrivateTmp=yes; gpg then dies with "can't create directory '/root/.gnupg'"
# and install.sh exits while our driver used to sleep forever.
# Pre-seed the same key via the Ubuntu keyserver HTTPS API (no dirmngr), with
# GNUPGHOME under /tmp, matching the fingerprint install.sh checks:
#   254F9170B966D193D6BAD300D5CEF8BF9BE6ED79
# (signing subkey 5234D2B73B6996C7 is the Mar-2025 packaging update — same cert).
ZIMBRA_APT_FPR="254F9170B966D193D6BAD300D5CEF8BF9BE6ED79"
ZIMBRA_APT_KEYRING="/etc/apt/trusted.gpg.d/zimbra.gpg"
# Prefer long-form search; short id 9BE6ED79 is the historical key id.
ZIMBRA_APT_KEY_URL="https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${ZIMBRA_APT_FPR}"

# Writable gnupg home for ProtectHome=yes (must stay set for install.sh in tmux).
if [ -z "${GNUPGHOME:-}" ] || [ ! -d "${GNUPGHOME}" ]; then
  GNUPGHOME=$(mktemp -d /tmp/kin-gnupghome.XXXXXX)
  chmod 700 "$GNUPGHOME"
fi
export GNUPGHOME

ensure_zimbra_ubuntu_gpg_key() {
  local tmp_asc tmp_kr gpg_err
  say "1b. Ensuring Zimbra Ubuntu packaging GPG key"
  info "Source: Ubuntu keyserver HTTPS (not files.zimbra.com/ZCSKeys — that tree 404s)"
  info "Expected fingerprint: ${ZIMBRA_APT_FPR}"
  info "GNUPGHOME=${GNUPGHOME} (required when ProtectHome blocks /root/.gnupg)"

  if [ -f "$ZIMBRA_APT_KEYRING" ] \
    && gpg --batch --no-default-keyring --keyring "$ZIMBRA_APT_KEYRING" --list-keys 2>/dev/null \
         | grep -qw "$ZIMBRA_APT_FPR"; then
    ok "Key already present in ${ZIMBRA_APT_KEYRING}"
    return 0
  fi

  command -v curl >/dev/null || { fail "curl required to fetch packaging GPG key"; exit 1; }
  command -v gpg >/dev/null || { fail "gpg required to import packaging GPG key"; exit 1; }

  tmp_asc=$(mktemp "${TMPDIR:-/tmp}/kin-zimbra-gpg.XXXXXX.asc")
  tmp_kr=$(mktemp "${TMPDIR:-/tmp}/kin-zimbra-gpg.XXXXXX.krring")
  rm -f "$tmp_kr"
  gpg_err=$(mktemp "${TMPDIR:-/tmp}/kin-zimbra-gpg.XXXXXX.err")
  if ! curl -fsSL -m 60 -o "$tmp_asc" "$ZIMBRA_APT_KEY_URL"; then
    rm -f "$tmp_asc" "$gpg_err"
    fail "Could not download Zimbra packaging GPG key from:"
    info "  ${ZIMBRA_APT_KEY_URL}"
    info "Fix network/DNS to keyserver.ubuntu.com, then re-run Deploy."
    exit 1
  fi
  if ! grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$tmp_asc"; then
    rm -f "$tmp_asc" "$gpg_err"
    fail "GPG key download was not a PGP public key (URL may have moved):"
    info "  ${ZIMBRA_APT_KEY_URL}"
    exit 1
  fi
  if ! gpg --batch --no-default-keyring --keyring "$tmp_kr" --import "$tmp_asc" \
        >/dev/null 2>"$gpg_err"; then
    fail "gpg --import failed for Zimbra packaging key (GNUPGHOME=${GNUPGHOME})"
    info "  URL: ${ZIMBRA_APT_KEY_URL}"
    sed 's/^/    /' "$gpg_err" | tail -n 20
    rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
    exit 1
  fi
  if ! gpg --batch --no-default-keyring --keyring "$tmp_kr" --list-keys 2>/dev/null \
       | grep -qw "$ZIMBRA_APT_FPR"; then
    fail "Downloaded key does not contain expected fingerprint ${ZIMBRA_APT_FPR}"
    info "  URL: ${ZIMBRA_APT_KEY_URL}"
    rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
    exit 1
  fi
  mkdir -p "$(dirname "$ZIMBRA_APT_KEYRING")"
  # Binary keyring path install.sh checks (Signed-By + --list-keys --keyring).
  # gpg refuses --export --output when the destination already exists.
  rm -f "$ZIMBRA_APT_KEYRING"
  gpg --batch --no-default-keyring --keyring "$tmp_kr" --export --output "$ZIMBRA_APT_KEYRING"
  chmod 644 "$ZIMBRA_APT_KEYRING"
  rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
  if ! gpg --batch --no-default-keyring --keyring "$ZIMBRA_APT_KEYRING" --list-keys 2>/dev/null \
       | grep -qw "$ZIMBRA_APT_FPR"; then
    fail "Installed ${ZIMBRA_APT_KEYRING} but fingerprint check failed"
    exit 1
  fi
  ok "Installed packaging key → ${ZIMBRA_APT_KEYRING}"
}

ensure_zimbra_ubuntu_gpg_key

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
# Export GNUPGHOME into the pane so install.sh's gpg --list-keys/--recv-keys
# does not try to create /root/.gnupg under ProtectHome=yes.
tmux send-keys -t "$SESS" "export GNUPGHOME=$(printf %q "$GNUPGHOME"); cd $ZDIR && ./install.sh --platform-override --skip-activation-check" Enter

send()   { tmux send-keys -t "$SESS" "$1" Enter; sleep "${2:-2}"; }
scr()    { tmux capture-pane -p -t "$SESS" | grep -v '^[[:space:]]*$'; }
lastln() { scr | tail -1; }

STAGE="packages"
DEADLINE=$(( $(date +%s) + 3600 ))
INSTALL_STARTED=1

installer_pane() { tmux capture-pane -p -S -80 -t "$SESS" 2>/dev/null || true; }

installer_failed_hard() {
  # install.sh exited with a fatal message — do not keep sleeping on prompts.
  local pane
  pane="$(installer_pane)"
  printf '%s' "$pane" | grep -q "Unable to retrive Zimbra GPG key" && return 0
  printf '%s' "$pane" | grep -q "Unable to export Zimbra GPG key" && return 0
  printf '%s' "$pane" | grep -q "Please fix system to allow normal package installation" && return 0
  printf '%s' "$pane" | grep -q "ERROR: Unable to install packages via apt-get" && return 0
  return 1
}

installer_still_running() {
  # install.sh is started as `./install.sh ...` from $ZDIR — argv usually does
  # NOT contain the absolute $ZDIR path, so only match the flags we pass.
  pgrep -f "install\\.sh --platform-override --skip-activation-check" >/dev/null 2>&1
}

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 4
  L="$(lastln)"
  PANE="$(installer_pane)"

  if installer_failed_hard; then
    fail "Zimbra install.sh aborted (see pane / ${LOG})."
    info "Typical cause: packaging GPG key import failed (keyserver/dirmngr)."
    info "KIN pre-seeds the key from: ${ZIMBRA_APT_KEY_URL}"
    printf '%s\n' "$PANE" | tail -n 25 | sed 's/^/    /'
    scrub_install_log "$LOG"
    exit 1
  fi

  # After we have answered past EULA, if install.sh is gone and we never reached
  # the config menu / success path, treat as unexpected exit (no silent sleep).
  if [ "$INSTALL_STARTED" -eq 1 ] && ! installer_still_running; then
    case "$L" in
      *"press return to exit"*|*"Configuration complete"*|*"Address unconfigured"*|*"press 'a' to apply"*|*"agree with the terms"*|*"Use Zimbra's package repository"*|*"Install zimbra-"*|*"system will be modified"*|*"Change hostname"*|*"Change domain name"*|*"Create domain:"*|*"Notify Zimbra"*)
        # Prompt still on screen — process check can race; keep looping.
        ;;
      *)
        if [ "$STAGE" = "packages" ]; then
          fail "Zimbra install.sh exited before package selection finished (last line: ${L:-empty})"
          info "Attach with: tmux attach -t ${SESS}  (socket may be under privhelper PrivateTmp)"
          printf '%s\n' "$PANE" | tail -n 30 | sed 's/^/    /'
          scrub_install_log "$LOG"
          exit 1
        fi
        ;;
    esac
  fi

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

if [ "$(date +%s)" -ge "$DEADLINE" ]; then
  fail "Timed out waiting for Zimbra installer (1h). Last line: ${L:-empty}"
  scrub_install_log "$LOG"
  exit 1
fi
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
