#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 03 INSTALL ZIMBRA
#
# Downloads the Maldua Zimbra FOSS build and verifies its checksum, then
# installs it one of two ways depending on the topology:
#
#   1vm    drives the interactive installer inside tmux, reading the screen and
#          answering the prompts it recognises. Proven; unchanged.
#   split  writes a per-role defaults file and lets the installer read it, with
#          no terminal involved. A single-node menu tree cannot express "LDAP
#          and store here, MTA and proxy there", and the joining node has an
#          LDAP-join menu that does not exist on a single-node install.
#
#   ./03-install-zimbra.sh            install according to the configured topology
#   ./03-install-zimbra.sh --manual   only download and extract, then print the
#                                     answer sequence and let you drive it
#
# Watch the 1vm driver live from another terminal:  tmux attach -t zcs
# The split path logs to /var/log/kin-mail-install.log instead.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# apt on a freshly booted host: wait out apt-daily / unattended-upgrades
# instead of failing on a lock that clears itself.
# shellcheck source=lib/apt-lock.sh
. ./lib/apt-lock.sh
# Shared mid-handoff probe (data partition holds bin/zmcontrol).
# shellcheck source=lib/zimbra-data-disk-probe.sh
. ./lib/zimbra-data-disk-probe.sh
# Service-id checks used at the end of this stage. Sourced HERE, with every
# other library, because stage 1 cds into $ZCS_SRC and never comes back - a
# relative source further down would silently find nothing.
# Read by the file sourced on the next line.
# shellcheck disable=SC2034
KIN_ALIGN_IDS_SOURCE_ONLY=1
# shellcheck disable=SC1091
. ./lib/align-service-ids.sh
unset KIN_ALIGN_IDS_SOURCE_ONLY
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
mkdir -p "$ZCS_SRC"
cd "$ZCS_SRC" || { fail "Cannot cd into ${ZCS_SRC}"; exit 1; }

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
  fail "Checksum file invalid (HTTP 404 body or corrupt) - ${_zcs_sum_url}"
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
  fail "Downloaded tarball too small (${_sz} bytes) - likely a failed URL: ${_zcs_url}"
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
# files.zimbra.com/downloads/ZCSKeys/zimbra-key.asc (that path is 404 as of
# 2026-08). It expects key 9BE6ED79 in /etc/apt/trusted.gpg.d/zimbra.gpg -
# fingerprint 254F9170B966D193D6BAD300D5CEF8BF9BE6ED79 (signing subkey
# 5234D2B73B6996C7 = Mar-2025 packaging refresh, same primary cert).
#
# Console Deploy runs under kin-mail-privhelperd with ProtectHome=yes (/root
# inaccessible) and PrivateTmp=yes, so install.sh's HKP `gpg --recv-keys`
# fails with "can't create directory '/root/.gnupg'". We pre-seed the same
# key with a writable GNUPGHOME under /tmp.
#
# Source order (same idea as resolving ZCS artefacts remotely first):
#   1) Official remote - Ubuntu keyserver HTTPS (what install.sh + Zimbra's
#      Mar-2025 packaging blog use). Prefer fresh key when the network works.
#   2) Bundled fallback - install/lib/zimbra-key.asc (committed copy of (1),
#      verified fingerprint on 2026-08-12). Used when remote 404/timeout/etc.
#   3) Both fail → hard stop with a clear error (never silent sleep).
#
# Do NOT use files.zimbra.com/downloads/security/public.key here - that file
# is the RPM packaging key (fingerprint B8D6…0F30C305), not the Ubuntu apt key.
#
# Refreshing install/lib/zimbra-key.asc when Zimbra rotates packaging keys:
#   curl -fsSL -o install/lib/zimbra-key.asc \
#     "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x254F9170B966D193D6BAD300D5CEF8BF9BE6ED79"
#   gpg --show-keys install/lib/zimbra-key.asc   # confirm FPR still 254F…9BE6ED79
#   # If Zimbra publishes a NEW primary fingerprint, update ZIMBRA_APT_FPR below
#   # and re-verify against util/utilfunc.sh in the Maldua tarball before commit.
ZIMBRA_APT_FPR="254F9170B966D193D6BAD300D5CEF8BF9BE6ED79"
ZIMBRA_APT_KEYRING="/etc/apt/trusted.gpg.d/zimbra.gpg"
ZIMBRA_APT_KEY_URL="https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${ZIMBRA_APT_FPR}"
# Script already cd'd to install/; keep absolute for clarity under callers.
ZIMBRA_APT_KEY_LOCAL="${PWD}/lib/zimbra-key.asc"

# Writable gnupg home for ProtectHome=yes (must stay set for install.sh in tmux).
if [ -z "${GNUPGHOME:-}" ] || [ ! -d "${GNUPGHOME}" ]; then
  GNUPGHOME=$(mktemp -d /tmp/kin-gnupghome.XXXXXX)
  chmod 700 "$GNUPGHOME"
fi
export GNUPGHOME

# Populate $1 with key ASCII-armor. Sets ZIMBRA_APT_KEY_SOURCE to a short label.
# Returns 0 on success, 1 if official remote and bundled fallback both fail.
fetch_zimbra_apt_key_asc() {
  local dest="$1"
  ZIMBRA_APT_KEY_SOURCE=""

  if curl -fsSL -m 60 -o "$dest" "$ZIMBRA_APT_KEY_URL" \
    && grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$dest"; then
    ZIMBRA_APT_KEY_SOURCE="official remote (${ZIMBRA_APT_KEY_URL})"
    return 0
  fi
  warn "Official packaging-key URL failed (404/timeout/invalid body):"
  info "  ${ZIMBRA_APT_KEY_URL}"
  rm -f "$dest"

  if [ -f "$ZIMBRA_APT_KEY_LOCAL" ] \
    && grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$ZIMBRA_APT_KEY_LOCAL"; then
    cp -f "$ZIMBRA_APT_KEY_LOCAL" "$dest"
    ZIMBRA_APT_KEY_SOURCE="bundled fallback (${ZIMBRA_APT_KEY_LOCAL})"
    warn "Using bundled key copy - refresh install/lib/zimbra-key.asc when Zimbra rotates keys"
    return 0
  fi

  fail "Could not obtain Zimbra Ubuntu packaging GPG key from any source."
  info "  Tried official:  ${ZIMBRA_APT_KEY_URL}"
  info "  Tried fallback:  ${ZIMBRA_APT_KEY_LOCAL}"
  info "Fix network to keyserver.ubuntu.com, or restore install/lib/zimbra-key.asc, then re-run Deploy."
  return 1
}

ensure_zimbra_ubuntu_gpg_key() {
  local tmp_asc tmp_kr gpg_err
  say "1b. Ensuring Zimbra Ubuntu packaging GPG key"
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

  if ! fetch_zimbra_apt_key_asc "$tmp_asc"; then
    rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
    exit 1
  fi
  info "Key material from: ${ZIMBRA_APT_KEY_SOURCE}"

  if ! gpg --batch --no-default-keyring --keyring "$tmp_kr" --import "$tmp_asc" \
        >/dev/null 2>"$gpg_err"; then
    fail "gpg --import failed for Zimbra packaging key (GNUPGHOME=${GNUPGHOME})"
    info "  Source: ${ZIMBRA_APT_KEY_SOURCE}"
    sed 's/^/    /' "$gpg_err" | tail -n 20
    rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
    exit 1
  fi
  if ! gpg --batch --no-default-keyring --keyring "$tmp_kr" --list-keys 2>/dev/null \
       | grep -qw "$ZIMBRA_APT_FPR"; then
    fail "Key does not contain expected fingerprint ${ZIMBRA_APT_FPR}"
    info "  Source: ${ZIMBRA_APT_KEY_SOURCE}"
    info "  Refusing to install an unverified key."
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
  ok "Installed packaging key → ${ZIMBRA_APT_KEYRING} (via ${ZIMBRA_APT_KEY_SOURCE})"
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
    Menu 1 -> TimeZone (number from live menu)  ${TIMEZONE} / ${ZIMBRA_TZ_NAME}
    Menu 6 -> 4 Admin Password ...............  (your password)
    a -> Yes -> Enter -> Yes .................  apply
    Notify Zimbra of your installation? ......  No  <- sends admin email to Zimbra
EOF
  echo; exit 0
fi

# --- 1c. split topology: install from a file, not from a terminal -------------
# Deliberately below --manual, so downloading and extracting the tarball stays
# reachable for check-unattended-install.sh.
#
# The tmux driver below cannot serve a split: each role has a different menu
# tree and the joining node has an LDAP-join menu that does not exist on a
# single-node install. Zimbra's own answer is a defaults file - install.sh
# sources it, skips the package prompts, installs what INSTALL_PACKAGES names,
# and hands the same file to zmsetup.pl -c. One file per role is the whole
# difference between a mailbox and an edge.
if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
  # shellcheck source=lib/zcs-defaults.sh
  . "${KIN_MAIL_INSTALL_DIR}/lib/zcs-defaults.sh"

  say "2. Split install (role decided by this machine's address, not its name)"
  ZROLE=$(kin_node_role) || {
    fail "Cannot tell which half of the split this machine is."
    info "Check EDGE_IP and MAILBOX_IP in /etc/kin-mail/config against this host's addresses."
    exit 1
  }
  ok "role: ${ZROLE}"

  if [ -d /opt/zimbra ]; then
    fail "/opt/zimbra already exists on this ${ZROLE} node."
    info "This stage will not re-drive an installer over an existing tree."
    exit 1
  fi

  DEFAULTS="${CONF_DIR}/zcs-${ZROLE}.cfg"
  LDAP_STORE="${CONF_DIR}/ldap-secrets"
  case "$ZROLE" in
    mailbox)
      # This node creates the directory, so it decides its passwords and writes
      # them down. The edge is built minutes later and must present the same
      # ones.
      kin_zcs_ensure_ldap_secrets "$LDAP_STORE"
      ok "directory passwords ready (${LDAP_STORE}, mode $(stat -c %a "$LDAP_STORE" 2>/dev/null))"
      kin_zcs_defaults_mailbox "$DEFAULTS"
      ;;
    edge)
      # The directory already exists on the mailbox. This node must present its
      # passwords, not invent new ones - inventing them produces a node that
      # installs cleanly, binds to nothing, and shows up as mail not moving,
      # hours later and on the other machine. So it is an error to be missing
      # them here, never a reason to generate.
      if [ -r "$LDAP_STORE" ]; then
        # shellcheck disable=SC1090
        . "$LDAP_STORE"
      fi
      for _s in KIN_LDAP_ADMIN_PASS KIN_LDAP_ROOT_PASS KIN_LDAP_REP_PASS \
                KIN_LDAP_POST_PASS KIN_LDAP_AMAVIS_PASS KIN_LDAP_NGINX_PASS; do
        if [ -z "${!_s:-}" ]; then
          fail "${_s} is not available; the edge cannot join the mailbox's directory."
          info "Expected ${LDAP_STORE}, copied from the mailbox after it was built."
          info "Build the mailbox first; the orchestrator carries this file across."
          exit 1
        fi
      done
      kin_zcs_defaults_edge "$DEFAULTS"
      ;;
    *)
      fail "Unexpected role '${ZROLE}' for a split install"
      exit 1
      ;;
  esac
  ok "wrote ${DEFAULTS} (mode $(stat -c %a "$DEFAULTS" 2>/dev/null))"

  if _why=$(kin_zcs_defaults_problem "$DEFAULTS"); then
    fail "The generated install file would not produce a working ${ZROLE}:"
    info "  ${_why}"
    rm -f "$DEFAULTS"
    exit 1
  fi
  ok "install file checked"

  # The file carries the directory's passwords and is sourced as root. It is
  # removed on every exit path, including failure.
  cleanup_defaults() { rm -f "$DEFAULTS"; }
  trap cleanup_defaults EXIT

  # Deliberately two steps, not one.
  #
  # install.sh can do the whole thing in a single pass - given a defaults file
  # it installs the packages and then calls zmsetup.pl -c itself. That pass
  # fails on this build: the packages leave /opt/zimbra/conf/ca owned by
  # root:root, zmsetup immediately runs `zmcertmgr createca` AS THE ZIMBRA USER,
  # and it cannot write there. The install dies at "Setting up CA...failed", and
  # the error that gets printed is a Java LDAP "Connection refused" stack -
  # because slapd never started - which points at the wrong thing entirely.
  #
  # Splitting the pass lets zmfixperms run in between, which is what moves
  # conf/ca to zimbra:zimbra. Observed on zcs-10.1.20 / Ubuntu 22.04,
  # 16 September 2026.
  say "3. Installing packages (no prompts; 10-20 minutes)"
  : >"$LOG"
  chmod 600 "$LOG"
  apt_prepare || warn "Package lock still busy; the Zimbra installer may contend for it"

  if ! (cd "$ZDIR" && ./install.sh -s --platform-override --skip-activation-check \
        "$DEFAULTS" >>"$LOG" 2>&1); then
    fail "Package installation failed on this ${ZROLE} node."
    tail -n 25 "$LOG" | sed 's/^/    /'
    scrub_install_log "$LOG"
    apt_resume_background_upgrades 2>/dev/null || true
    exit 1
  fi
  ok "packages installed"

  say "4. Repairing ownership before configuration"
  if [ ! -x /opt/zimbra/libexec/zmfixperms ]; then
    fail "zmfixperms is missing; configuration would fail at 'Setting up CA'"
    exit 1
  fi
  /opt/zimbra/libexec/zmfixperms >>"$LOG" 2>&1 || {
    fail "zmfixperms failed"
    tail -n 15 "$LOG" | sed 's/^/    /'
    exit 1
  }
  # The one that actually blocks the install. Asserted rather than assumed,
  # because the failure it causes names LDAP rather than a permission.
  _ca_owner=$(stat -c '%U' /opt/zimbra/conf/ca 2>/dev/null || printf '?')
  if [ "$_ca_owner" != zimbra ]; then
    fail "/opt/zimbra/conf/ca is owned by ${_ca_owner}, not zimbra."
    info "zmcertmgr runs as zimbra and cannot write there; configuration would fail."
    exit 1
  fi
  ok "conf/ca owned by zimbra"

  say "5. Configuring from the generated file (10-20 minutes)"
  if ! /opt/zimbra/libexec/zmsetup.pl -c "$DEFAULTS" >>"$LOG" 2>&1; then
    fail "Configuration failed on this ${ZROLE} node."
    info "Last lines of ${LOG}:"
    tail -n 25 "$LOG" | sed 's/^/    /'
    scrub_install_log "$LOG"
    apt_resume_background_upgrades 2>/dev/null || true
    exit 1
  fi
  scrub_install_log "$LOG"
  apt_resume_background_upgrades 2>/dev/null || true

  say "6. Verification"
  if [ ! -d /opt/zimbra ]; then
    fail "/opt/zimbra missing after a successful installer run"
    exit 1
  fi
  STATUS=$(su - zimbra -c "zmcontrol status" 2>&1)
  printf '%s\n' "$STATUS" | sed 's/^/    /'
  STOPPED=$(printf '%s' "$STATUS" | grep -v Running | grep -vE "^Host|^$" | wc -l)
  if [ "${STOPPED:-1}" -ne 0 ]; then
    fail "${STOPPED} service(s) not running on this ${ZROLE} node"
    exit 1
  fi
  ok "all services running"

  if [ "$ZROLE" = mailbox ]; then
    info "domain: $(su - zimbra -c 'zmprov gad' 2>/dev/null | tr '\n' ' ')"
    info "The edge joins this directory next; its passwords are read from here."
  fi

  if [ -f "$LOG" ] && grep -Fq -- "$ADMIN_PASS" "$LOG" 2>/dev/null; then
    fail "Admin password still present in ${LOG} after redact"
    exit 1
  fi
  ok "install log clean of the admin password"

  echo
  say "DONE (${ZROLE})"
  cleanup_defaults
  trap - EXIT
  exit 0
fi

# --- 2. drive the installer --------------------------------------------------
# The download/extract steps above are safe to re-run. The tmux driver is not:
# a second pass against an already-installed system hits upgrade/reconfigure
# prompts the state machine does not recognise (hang or wrong answers).
SKIP_INSTALLER=0
# none | healthy | mid_handoff | drbd_secondary - drives section 3 verification
SKIP_REASON=none
DATA_DISK="${KIN_DRBD_DATA_DISK:-/dev/sdb1}"
SETUP_COMPLETE_MARKER="${KIN_SETUP_COMPLETE_MARKER:-/etc/kin-mail/setup-complete}"
if [ -d /opt/zimbra ]; then
  STATUS_PRE=$(su - zimbra -c "zmcontrol status" 2>&1) || STATUS_PRE=""
  STOPPED_PRE=$(printf '%s' "$STATUS_PRE" | grep -v Running | grep -vE "^Host|^$" | wc -l | tr -d ' ')
  HEALTHY_PRE=0
  if [ -n "$STATUS_PRE" ] && [ "${STOPPED_PRE:-1}" -eq 0 ]; then
    HEALTHY_PRE=1
  fi
  DRBD_HELD_PRE=0
  if data_disk_held_by_drbd "$DATA_DISK"; then
    DRBD_HELD_PRE=1
  fi
  LOCAL_ZM=0
  if mount_has_zmcontrol /opt/zimbra; then
    LOCAL_ZM=1
  fi
  ON_DATA_PRE=0
  if [ "$HEALTHY_PRE" -eq 0 ] && [ "$LOCAL_ZM" -eq 0 ] && zimbra_data_disk_has_real_install "$DATA_DISK"; then
    ON_DATA_PRE=1
  fi
  case "$(zimbra_install_skip_action "$HEALTHY_PRE" "$ON_DATA_PRE" "$DRBD_HELD_PRE" "$LOCAL_ZM")" in
    skip_healthy)
      say "2. Installer skipped - Zimbra already installed and healthy"
      ok "/opt/zimbra exists; zmcontrol status: all services Running"
      info "Re-run does not re-run install.sh (avoids upgrade/reconfigure flow)."
      SKIP_INSTALLER=1
      SKIP_REASON=healthy
      ;;
    skip_drbd_secondary)
      say "2. Installer skipped - data disk is DRBD-attached (cluster Secondary or Promoted peer)"
      ok "${DATA_DISK} is held by a drbd* device; local Zimbra is not expected to run here"
      info "Pacemaker runs kin-zimbra only on the Promoted node; do not remount the raw disk."
      SKIP_INSTALLER=1
      SKIP_REASON=drbd_secondary
      ;;
    skip_mid_handoff)
      say "2. Installer skipped - Zimbra already on the data partition (mid-handoff)"
      ok "bin/zmcontrol present on ${DATA_DISK}; /opt/zimbra is currently unmounted"
      info "Not remounting or starting Zimbra here; DRBD attach and Pacemaker own that."
      SKIP_INSTALLER=1
      SKIP_REASON=mid_handoff
      ;;
    run_fresh)
      info "/opt/zimbra is a data-disk mount without a Zimbra tree (typical after 02-prepare-os)."
      info "Running the installer into that mount."
      ;;
    *)
      CONSOLE_CONFIRMED=0
      case "${KIN_CONSOLE_CONFIRMED:-0}" in
        1|yes|YES|true|TRUE) CONSOLE_CONFIRMED=1 ;;
      esac
      SETUP_DONE=0
      if [ -f "$SETUP_COMPLETE_MARKER" ]; then
        SETUP_DONE=1
      fi
      if [ "$(zimbra_fail_broken_recover_action "$CONSOLE_CONFIRMED" "$SETUP_DONE")" = "wipe_and_run" ]; then
        warn "Incomplete Zimbra tree at /opt/zimbra (not all services Running, or status unreadable)."
        info "First-time console Deploy: clearing the tree (keeping the mount) and re-running the installer."
        if id zimbra >/dev/null 2>&1 && [ -x /opt/zimbra/bin/zmcontrol ]; then
          su - zimbra -c "zmcontrol stop" >/dev/null 2>&1 || true
        fi
        tmux kill-session -t "$SESS" 2>/dev/null || true
        pkill -f 'install\.sh --platform-override --skip-activation-check' >/dev/null 2>&1 || true
        pkill -f 'zmsetup\.pl' >/dev/null 2>&1 || true
        if ! wipe_incomplete_zimbra_tree /opt/zimbra; then
          fail "Could not clear the incomplete tree at /opt/zimbra"
          exit 1
        fi
        ok "Cleared incomplete /opt/zimbra contents (lost+found kept)"
      else
        fail "Zimbra exists at /opt/zimbra but not all services are Running (or status unreadable)."
        info "This script will not re-run the installer on a partial/broken installation."
        info "Fix manually first: repair stopped services, or uninstall cleanly then run 03 again."
        if [ -n "$STATUS_PRE" ]; then
          printf '%s\n' "$STATUS_PRE" | sed 's/^/    /'
        else
          info "zmcontrol produced no output (zimbra user missing, or binary not runnable)."
        fi
        exit 1
      fi
      ;;
  esac
fi

# Normal umount leaves the mountpoint directory. If it was removed, still skip
# the installer when the data disk already holds Zimbra (do not run_fresh onto
# a new OS-disk tree while the mapper has production mail).
if [ "$SKIP_INSTALLER" -eq 0 ] && [ ! -d /opt/zimbra ]; then
  if data_disk_held_by_drbd "$DATA_DISK"; then
    say "2. Installer skipped - data disk is DRBD-attached (no /opt/zimbra mountpoint)"
    SKIP_INSTALLER=1
    SKIP_REASON=drbd_secondary
  elif zimbra_data_disk_has_real_install "$DATA_DISK"; then
    say "2. Installer skipped - Zimbra already on the data partition (mountpoint missing)"
    SKIP_INSTALLER=1
    SKIP_REASON=mid_handoff
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
  # The Zimbra installer runs for twenty minutes or more and drives dpkg the
  # whole time. unattended-upgrades waking up in the middle of that is the one
  # collision that costs a whole install, so it is stood down for the duration
  # and put back here, on every exit path.
  apt_resume_background_upgrades 2>/dev/null || true
}
trap cleanup_redactor EXIT

apt_prepare || warn "Package lock still busy; the Zimbra installer may contend for it"

tmux kill-session -t "$SESS" 2>/dev/null
tmux new-session -d -s "$SESS" -x 200 -y 50
# Run install.sh on a real TTY (the tmux pane). Piping stdout breaks the driver:
# bash fully-buffers when stdout is a pipe, so prompts never reach capture-pane
# and the installer blocks forever on read. Log via pipe-pane instead (redact then tee).
# See docs/progress/1.6-hosta-03-pipepane-fix.md - do not revert to piping install.sh.
#
# pipe-pane writes to $LOG only; this script's stdout is just driver messages
# ("apply", prompt answers). Console SSE follows $LOG separately so the wizard
# log viewer matches `tail -f $LOG` without attaching to tmux.
#
# Security trade-off: before this change, both the live tmux pane and $LOG went
# through the redactor, so ADMIN_PASS never appeared in either place. With
# pipe-pane, only $LOG is redacted - the live pane (tmux capture-pane / attach)
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
installer_hist() { tmux capture-pane -p -S - -t "$SESS" 2>/dev/null || true; }

# Visible pane only - do not grep 400 lines of timezone dump for menu numbers.
# zmsetup marks unconfigured items with a "** " prefix (Admin Password is always
# UNSET on a fresh install). TimeZone / zimbra-store are usually already set.
menu_item_number() {
  local label="$1"
  tmux capture-pane -p -t "$SESS" 2>/dev/null \
    | grep -E "^[[:space:]]*(\*\*)?[[:space:]]*[0-9]+\)[[:space:]]+${label}" \
    | tail -1 \
    | sed -E 's/^[[:space:]]*(\*\*)?[[:space:]]*([0-9]+)\).*/\2/'
}

dump_installer_and_exit() {
  info "Last line: $(lastln)"
  printf '%s\n' "$(installer_pane)" | tail -n 40 | sed 's/^/    /'
  scrub_install_log "$LOG"
  tmux kill-session -t "$SESS" 2>/dev/null || true
  exit 1
}

abort_invalid_selection() {
  fail "Zimbra installer rejected a menu input (Invalid selection)."
  info "The driver sent a key the current menu does not accept - often a stale timezone index."
  dump_installer_and_exit
}

wait_for_pane() {
  local pat="$1" timeout="${2:-60}"
  local start now
  start=$(date +%s)
  while true; do
    now=$(date +%s)
    PANE="$(installer_pane)"
    if printf '%s\n' "$PANE" | grep -q "Invalid selection"; then
      abort_invalid_selection
    fi
    if printf '%s\n' "$PANE" | grep -qE -- "$pat"; then
      return 0
    fi
    if [ $((now - start)) -ge "$timeout" ]; then
      return 1
    fi
    sleep 2
  done
}

tz_list_index() {
  # Numbered list is printed then scrolls off a 50-row pane; read full history
  # only AFTER "Enter the number for the local timezone" is on screen.
  local name="$1"
  installer_hist | grep -E "^[0-9]+ ${name}$" | tail -1 | awk '{print $1}'
}

installer_failed_hard() {
  # install.sh exited with a fatal message - do not keep sleeping on prompts.
  local pane
  pane="$(installer_pane)"
  printf '%s' "$pane" | grep -q "Unable to retrive Zimbra GPG key" && return 0
  printf '%s' "$pane" | grep -q "Unable to export Zimbra GPG key" && return 0
  printf '%s' "$pane" | grep -q "Please fix system to allow normal package installation" && return 0
  printf '%s' "$pane" | grep -q "ERROR: Unable to install packages via apt-get" && return 0
  return 1
}

installer_still_running() {
  # install.sh is started as `./install.sh ...` from $ZDIR - argv usually does
  # NOT contain the absolute $ZDIR path, so only match the flags we pass.
  pgrep -f "install\\.sh --platform-override --skip-activation-check" >/dev/null 2>&1
}

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 4
  L="$(lastln)"
  PANE="$(installer_pane)"

  if printf '%s\n' "$PANE" | grep -q "Invalid selection"; then
    abort_invalid_selection
  fi

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
        # Prompt still on screen - process check can race; keep looping.
        ;;
      *)
        if [ "$STAGE" = "packages" ]; then
          fail "Zimbra install.sh exited before package selection finished (last line: ${L:-empty})"
          info "Attach with: tmux attach -t ${SESS}  (socket may be under privhelper PrivateTmp)"
          printf '%s\n' "$PANE" | tail -n 30 | sed 's/^/    /'
          scrub_install_log "$LOG"
          exit 1
        fi
        fail "Zimbra installer process ended unexpectedly during configuration/apply (last line: ${L:-empty})"
        dump_installer_and_exit
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
    *"Notify Zimbra of your installation"*)
        info "telemetry -> No"
        send No 10
        if ! wait_for_pane "press return to exit" 120; then
          fail "After declining telemetry, did not see 'press return to exit' (installer may have died)"
          dump_installer_and_exit
        fi
        ;;
    *"press return to exit"*)              info "done";                send "" 5; break ;;
    *"Invalid selection"*)                 abort_invalid_selection ;;

    *"Address unconfigured"*|*"press 'a' to apply"*)
        if [ "$STAGE" = "packages" ]; then
          STAGE="menus"
          info "Configuration menu reached - setting timezone, domain, password"

          # --- timezone: resolve the Common Configuration item by label (not hardcoded 7).
          # Zimbra 10.1 createCommonMenu() inserts LDAP Base DN / ephemeral URL
          # conditionally, so TimeZone is 7 only on a typical ldap+ephemeral=no host.
          send 1 4
          if ! wait_for_pane '[0-9]+\)[[:space:]]+TimeZone:' 45; then
            fail "Timed out waiting for Common configuration / TimeZone"
            dump_installer_and_exit
          fi
          TZMENU="$(menu_item_number "TimeZone:")"
          if [ -z "$TZMENU" ]; then
            fail "Could not read the TimeZone menu number from the installer pane"
            dump_installer_and_exit
          fi
          info "Common configuration -> TimeZone is item ${TZMENU}"
          send "$TZMENU" 3
          if ! wait_for_pane "Enter the number for the local timezone" 90; then
            fail "Timed out waiting for the timezone numbered list"
            dump_installer_and_exit
          fi
          # Prefer the OS/wizard timezone if zmsetup lists it. Asia/Jakarta is a
          # real TZID but is not X-ZIMBRA-TZ-PRIMARY, so the menu usually omits
          # it; Asia/Bangkok is the UTC+7 fallback (same offset, no DST). Never
          # hardcode the index - primary slots shift when timezones.ics changes.
          TZIDX=""
          TZCHOSEN=""
          for cand in "$TIMEZONE" "$ZIMBRA_TZ_NAME" "Asia/Jakarta" "Asia/Bangkok"; do
            [ -n "$cand" ] || continue
            TZIDX="$(tz_list_index "$cand")"
            if [ -n "$TZIDX" ]; then
              TZCHOSEN="$cand"
              break
            fi
          done
          if [ -n "$TZIDX" ]; then
            info "timezone ${TZCHOSEN} -> option ${TZIDX} (from live zmsetup list)"
            send "$TZIDX" 5
          else
            warn "No Asia/Jakarta or Asia/Bangkok in the live timezone list; keeping default"
            send "" 3
          fi
          if ! wait_for_pane '[0-9]+\)[[:space:]]+TimeZone:' 45; then
            fail "Did not return to Common configuration after timezone"
            dump_installer_and_exit
          fi
          send r 4

          # --- admin password: find zimbra-store by label (package list order varies).
          if ! wait_for_pane "zimbra-store:" 45; then
            fail "Timed out waiting for Main menu / zimbra-store"
            dump_installer_and_exit
          fi
          STOREMENU="$(menu_item_number "zimbra-store:")"
          if [ -z "$STOREMENU" ]; then
            fail "Could not read the zimbra-store menu number"
            dump_installer_and_exit
          fi
          info "Main menu -> zimbra-store is item ${STOREMENU}"
          send "$STOREMENU" 4
          if ! wait_for_pane "Admin Password" 45; then
            fail "Timed out waiting for Store configuration / Admin Password"
            dump_installer_and_exit
          fi
          APWMENU="$(menu_item_number "Admin Password")"
          if [ -z "$APWMENU" ]; then
            fail "Could not read the Admin Password menu number"
            dump_installer_and_exit
          fi
          info "Store -> Admin Password is item ${APWMENU}"
          send "$APWMENU" 3
          tmux send-keys -t "$SESS" "$ADMIN_PASS" Enter; sleep 5
          # Visible pane only - do not match an old error still in capture-pane history.
          PANE="$(tmux capture-pane -p -t "$SESS" 2>/dev/null || true)"
          if printf '%s\n' "$PANE" | grep -qE 'Invalid metacharater used|Invalid metacharacter used|Minimum length of'; then
            fail 'Admin password rejected by Zimbra installer -- avoid characters like ! * & $, use letters/digits and simple symbols only'
            dump_installer_and_exit
          fi
          send r 4

          # --- apply only from the main menu ---
          if ! wait_for_pane "press 'a' to apply|Address unconfigured" 45; then
            fail "Did not return to Main menu before apply"
            dump_installer_and_exit
          fi
          info "apply"
          send a 6
          if ! wait_for_pane "Save configuration data to a file" 45; then
            fail "Apply step 1/4 failed: after sending 'a', did not see 'Save configuration data to a file'"
            dump_installer_and_exit
          fi
          send Yes 6      # save config to file
          if ! wait_for_pane "Save config in file" 45; then
            fail "Apply step 2/4 failed: after sending Yes (save config), did not see 'Save config in file'"
            dump_installer_and_exit
          fi
          send "" 6       # accept default filename
          if ! wait_for_pane "The system will be modified - continue|system will be modified" 45; then
            fail "Apply step 3/4 failed: after accepting the default config filename, did not see 'The system will be modified'"
            dump_installer_and_exit
          fi
          send Yes 30     # the system will be modified
          if ! wait_for_pane "Operations logged|Setting local config" 90; then
            fail "Apply step 4/4 failed: after sending Yes (modify system), installer did not start applying"
            dump_installer_and_exit
          fi
        fi
        ;;
  esac
done

if [ "$(date +%s)" -ge "$DEADLINE" ]; then
  fail "Timed out waiting for Zimbra installer (1h). Last line: ${L:-empty}"
  dump_installer_and_exit
fi
fi

# --- 3. verify ---------------------------------------------------------------
echo; say "3. Verification"
if [ "$SKIP_INSTALLER" -eq 0 ]; then
  sleep 5
fi
# Scrub even on failure paths so a broken install cannot leave a dirty log behind.
scrub_install_log "$LOG"

case "$(zimbra_install_verify_action "$SKIP_REASON")" in
  skip_offline)
    if [ "$SKIP_REASON" = "drbd_secondary" ]; then
      ok "Live zmcontrol verify skipped (DRBD-attached; Zimbra not local)"
      info "${DATA_DISK} is cluster-managed; only the Promoted node runs kin-zimbra."
    else
      ok "Live zmcontrol verify skipped (mid-handoff)"
      info "Real install is on ${DATA_DISK}; /opt/zimbra is an empty mountpoint by design."
      info "DRBD attach and Pacemaker (mail-pacemaker.yml) bring Zimbra back; not this stage."
    fi
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
    echo
    if [ "$SKIP_REASON" = "drbd_secondary" ]; then
      say "DONE (DRBD Secondary / attached)"
      info "Full-install skips Zimbra-touching stages; Pacemaker owns the live tree on the Promoted node."
    else
      say "DONE (mid-handoff)"
      info "Full-install will skip Zimbra-touching stages 04/05/06/07/11 until the tree is live again."
    fi
    tmux kill-session -t "$SESS" 2>/dev/null
    if declare -F cleanup_redactor >/dev/null 2>&1; then
      cleanup_redactor
      trap - EXIT
    fi
    exit 0
    ;;
esac

if [ ! -d /opt/zimbra ]; then fail "/opt/zimbra missing - installation failed. See $LOG"; exit 1; fi

STATUS=$(su - zimbra -c "zmcontrol status" 2>&1)
STOPPED=$(printf '%s' "$STATUS" | grep -v Running | grep -vE "^Host|^$" | wc -l)
printf '%s\n' "$STATUS" | sed 's/^/    /'

if [ "$STOPPED" -eq 0 ]; then
  ok "All services running"
else
  fail "$STOPPED service(s) not running"
  exit 1
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

# The installer has just created (or reused) the accounts that own every file
# on the replicated volume. On a peer those numbers have to be the primary's,
# and 02-prepare-os.sh reserved them for exactly this moment. Check that they
# survived, here, on the node it happened on - the alternative is finding out
# eleven orchestration steps later, when the pair refuses to build and half an
# hour of Zimbra install has to be repeated.
if kin_ha_peer_install; then
  say "Service account ids"
  if align_verify_ids_from_env; then
    ok "Service account ids still match the primary after the Zimbra install"
  else
    fail "The Zimbra install moved a service account id away from the primary's."
    info "This node cannot run Zimbra off the replicated volume until they agree."
    info "Re-run 02-prepare-os.sh on this host to renumber, then re-run this stage."
    exit 1
  fi
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
