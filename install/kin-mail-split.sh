#!/usr/bin/env bash
# =============================================================================
# KIN Mail - build a split deployment from the edge
#
# Runs on the EDGE. Builds the mailbox first over SSH, then this machine,
# because the edge has nothing to join until the directory exists and cannot
# join it without passwords that only exist once the mailbox is built.
#
#   sudo ./kin-mail-split.sh            build
#   sudo ./kin-mail-split.sh --check    preflight only; changes nothing
#   sudo ./kin-mail-split.sh --fresh    forget recorded progress, build again
#
# Every step writes a marker, so a re-run resumes instead of reinstalling. A
# marker is never the only evidence a step happened: the machine is asked too,
# and the deployment's identity is recorded beside them so markers are void
# once the addresses change. The output is plain progress lines - kin-mail.sh
# --full-install delegates here, and the console streams them to the browser.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

CHECK_ONLY=0
FRESH=0
case "${1:-}" in
  --check) CHECK_ONLY=1 ;;
  --fresh) FRESH=1 ;;
esac

STATE_DIR="${CONF_DIR}/split-state"
REMOTE_ROOT="/opt/kin-mail-deploy"
LDAP_STORE="${CONF_DIR}/ldap-secrets"

step_done()  { [ -f "${STATE_DIR}/$1" ]; }
mark_done()  { mkdir -p "$STATE_DIR"; : >"${STATE_DIR}/$1"; }

# Markers record what this script did. They must never be the only evidence
# that it happened.
#
# A marker outlives the thing it describes: /etc/kin-mail survives a rebuilt
# mailbox, a swapped disk, a re-run against a different address. A stale
# "mailbox-installed" then skips the entire mailbox build and the run fails
# later, looking for a directory nobody created - which is how this was found,
# on a mailbox with no Zimbra on it and a marker saying there was.
#
# So the identity of the deployment is written alongside them, and markers are
# void the moment it changes.
IDENT_FILE="${STATE_DIR}/deployment-identity"
current_identity() { printf '%s|%s|%s\n' "$MBOX_IP" "$MBOX_HOST" "$(kin_edge_ip)"; }

invalidate_markers_if_moved() {
  local now stored
  now=$(current_identity)
  stored=$(cat "$IDENT_FILE" 2>/dev/null || printf '')
  if [ -n "$stored" ] && [ "$stored" != "$now" ]; then
    warn "This deployment now describes different machines than the recorded progress."
    info "  recorded: ${stored}"
    info "  now:      ${now}"
    info "Discarding the recorded progress; every step will run again."
    rm -rf "$STATE_DIR"
  fi
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$now" >"$IDENT_FILE"
}

# --- who am I, and who is the other machine ----------------------------------
if ! kin_topology_is_split; then
  fail "This appliance is not configured as a split (TOPOLOGY=$(kin_topology 2>/dev/null))."
  info "Nothing has been changed."
  exit 1
fi

ROLE=$(kin_node_role) || {
  fail "Cannot tell which machine this is."
  info "EDGE_IP and MAILBOX_IP must match this host's addresses."
  exit 1
}
if [ "$ROLE" != edge ]; then
  fail "This must run on the edge; this machine is the ${ROLE}."
  info "The edge builds the mailbox, not the other way round."
  exit 1
fi

if _why=$(kin_split_config_problem); then
  fail "The split configuration cannot work: ${_why}"
  exit 1
fi

MBOX_IP=$(kin_mailbox_ip)
MBOX_HOST=$(kin_mailbox_host)

if [ "$FRESH" -eq 1 ]; then
  rm -rf "$STATE_DIR"
  ok "Recorded progress discarded; every step will run from the start."
fi
invalidate_markers_if_moved

# --- how we reach the mailbox -------------------------------------------------
# Password auth, because that is what a freshly installed Ubuntu offers and the
# wizard already collects it. Never in argv: sshpass -e reads SSHPASS from the
# environment, so the password cannot be read out of the process list.
SSH_USER="${MAILBOX_SSH_USER:-${KIN_OS_USER:-kin}}"
SSH_PASS="${MAILBOX_SSH_PASS:-${KIN_USER_PASS:-}}"

# known_hosts lives under /etc/kin-mail, not in root's home.
#
# The console runs this through kin-mail-privhelperd, which has
# ProtectHome=true, so /root is not writable there: ssh reports "Could not
# create directory '/root/.ssh' (Read-only file system)" and the host key has
# nowhere to go. Keeping it here means the same file is used whether the deploy
# is driven from the console or from a shell.
KNOWN_HOSTS="${CONF_DIR}/split-known-hosts"
mkdir -p "$CONF_DIR" 2>/dev/null || true
: >>"$KNOWN_HOSTS" 2>/dev/null || true
chmod 600 "$KNOWN_HOSTS" 2>/dev/null || true

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password
          -o PubkeyAuthentication=no -o ConnectTimeout=15 -o ServerAliveInterval=30
          -o ServerAliveCountMax=120 -o LogLevel=ERROR
          -o "UserKnownHostsFile=${KNOWN_HOSTS}")

mbox_run() {
  SSHPASS="$SSH_PASS" sshpass -e ssh "${SSH_OPTS[@]}" \
    "${SSH_USER}@${MBOX_IP}" "$@"
}
mbox_sudo() {
  # The whole command runs inside ONE root shell.
  #
  # `sudo -S -p '' A && B` elevates only A: the shell splits on && before sudo
  # ever sees it, so everything after the first command runs as the login user.
  # That turned "remove the old tree, make a new one, unpack into it" into a
  # root rm followed by an unprivileged mkdir, which failed with a permission
  # error pointing at the wrong step. A single-command preflight cannot catch
  # it, which is why it passed.
  #
  # printf %q quotes the whole chain so the remote shell hands it to bash -c
  # intact rather than re-splitting it.
  local cmd="$*"
  SSHPASS="$SSH_PASS" sshpass -e ssh "${SSH_OPTS[@]}" \
    "${SSH_USER}@${MBOX_IP}" "sudo -S -p '' bash -c $(printf '%q' "$cmd")" <<EOF
${SSH_PASS}
EOF
}
# Installed and configured are different states, and every check in this script
# has to mean the same one. Defined once: a package-only tree has the binary but
# zmcontrol status fails, because the configuration it reads does not exist yet.
#
# Stating it three separate times is what went wrong today - stage 03 learned to
# resume a half-finished install while the preflight here still refused it, and
# the deploy stopped one line before the work it could have continued.
MBOX_ZIMBRA_WORKS='su - zimbra -c "zmcontrol status" >/dev/null 2>&1'

mailbox_zimbra_configured() { mbox_sudo "$MBOX_ZIMBRA_WORKS"; }

mbox_put() {
  SSHPASS="$SSH_PASS" sshpass -e scp "${SSH_OPTS[@]}" -p "$1" \
    "${SSH_USER}@${MBOX_IP}:$2"
}

# --- 1. preflight -------------------------------------------------------------
say "1/8 Preflight"

command -v sshpass >/dev/null 2>&1 || {
  fail "sshpass is not installed on this edge node."
  info "  sudo apt-get install -y sshpass"
  exit 1
}
ok "sshpass present"

if [ -z "$SSH_PASS" ]; then
  fail "No password for reaching the mailbox."
  info "Set MAILBOX_SSH_PASS in ${CONF_FILE}, or re-run 00-config.sh --reset."
  exit 1
fi

# Which password does the mailbox answer to right now?
#
# There are two, and which one is live depends on how far a previous run got.
# MAILBOX_SSH_PASS is what the operator typed on the Topology step. KIN_USER_PASS
# is what 02-prepare-os.sh sets the account to when it runs. So a machine that
# has already been prepared - by an earlier attempt that failed later on -
# answers to the second, not the first, and a preflight that only tries the
# first refuses to start over work it already did.
#
# Try both, in the order they become true, and carry on with whichever answers.
try_mailbox_password() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  SSHPASS="$candidate" sshpass -e ssh "${SSH_OPTS[@]}" \
    "${SSH_USER}@${MBOX_IP}" true 2>/dev/null
}

if ! try_mailbox_password "$SSH_PASS"; then
  if try_mailbox_password "${KIN_USER_PASS:-}"; then
    SSH_PASS="$KIN_USER_PASS"
    ok "mailbox answers to the host password set by an earlier 02-prepare-os"
  else
  fail "Cannot log in as ${SSH_USER}@${MBOX_IP}."
  info "Neither the mailbox password from the wizard nor the host password worked."
  info "Check it by hand first:  ssh ${SSH_USER}@${MBOX_IP}"
  echo
  # Where to fix it depends on how the deploy was started, and getting that
  # wrong wastes a whole attempt: pressing Deploy in the console runs
  # apply_wizard_draft first, which rewrites this file from the saved wizard
  # answers. Editing the config by hand and then deploying from the browser
  # therefore reverts the edit before the first step runs.
  if [ -f /var/lib/kin-mail-console/wizard-draft.json ]; then
    warn "This appliance has a saved wizard draft."
    info "If you deploy from the console, the password comes from the WIZARD,"
    info "not from ${CONF_FILE} - pressing Deploy rewrites that file from the"
    info "draft before this script runs. Fix it in the browser:"
    info "    Setup > Topology > Password for that account"
    info "Editing ${CONF_FILE} only helps when running this script by hand."
  else
    info "Set MAILBOX_SSH_USER / MAILBOX_SSH_PASS in ${CONF_FILE},"
    info "or re-run:  sudo ./00-config.sh --reset"
  fi
  exit 1
  fi
fi
ok "mailbox reachable as ${SSH_USER}@${MBOX_IP}"

if ! mbox_sudo true >/dev/null 2>&1; then
  fail "${SSH_USER} cannot use sudo on the mailbox."
  exit 1
fi
# Prove it for a CHAIN, not just one command. `sudo A && B` elevates only A,
# and a preflight that tests a single command declares sudo working while every
# multi-step operation this script performs still runs unprivileged.
if ! mbox_sudo 'test "$(id -u)" -eq 0 && test "$(id -u)" -eq 0' >/dev/null 2>&1; then
  fail "sudo on the mailbox does not carry through a multi-command step."
  exit 1
fi
ok "sudo works on the mailbox, including multi-command steps"

# Clocks. Zimbra's LDAP and TLS both care, and a skewed pair fails in ways that
# read as authentication problems hours later.
_local_now=$(date +%s)
_remote_now=$(mbox_run 'date +%s' 2>/dev/null | tr -dc '0-9')
if [ -n "$_remote_now" ]; then
  _skew=$(( _local_now > _remote_now ? _local_now - _remote_now : _remote_now - _local_now ))
  if [ "$_skew" -gt 120 ]; then
    fail "The two machines' clocks differ by ${_skew}s."
    info "Fix time sync before installing; Zimbra's TLS and LDAP both depend on it."
    exit 1
  fi
  ok "clocks agree (${_skew}s apart)"
else
  warn "Could not read the mailbox clock"
fi

# A mailbox that already has Zimbra is not something to build onto.
#
# The test is for an install, not for the directory. 02-prepare-os.sh mounts the
# spare data disk at /opt/zimbra and leaves it empty, on purpose, so the
# directory existing is the normal state of a prepared machine - refusing on
# that alone rejected a mailbox this deployment had just finished preparing.
# bin/zmcontrol is the thing that means "Zimbra lives here".
if mailbox_zimbra_configured 2>/dev/null; then
  if step_done mailbox-installed; then
    info "mailbox already built (marker present, confirmed on the machine)"
  else
    fail "The mailbox already has a working Zimbra, but this deployment did not build it."
    info "Wipe it first:  sudo ${REMOTE_ROOT}/install/kin-mail-uninstall.sh --detach"
    exit 1
  fi
elif mbox_run 'test -x /opt/zimbra/bin/zmcontrol' 2>/dev/null; then
  info "mailbox has Zimbra packages but no configuration; the install will resume"
elif mbox_run 'test -d /opt/zimbra' 2>/dev/null; then
  info "/opt/zimbra exists on the mailbox but holds no install (prepared data disk)"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo
  say "Preflight only - nothing was changed"
  exit 0
fi

# --- 2. ship the tree ---------------------------------------------------------
say "2/8 Copying the installer to the mailbox"
# Always copied, never cached.
#
# A marker here bought nothing - the tree is 1.4 MB over a LAN - and cost
# correctness twice: once when the uninstaller removed it and the marker still
# claimed it was there, and once when the marker was right that a tree existed
# but wrong that it was THIS one, so the mailbox ran a stale stage while the
# edge ran the fixed copy. Freshness is not something a marker can attest to.
_tgz=$(mktemp /tmp/kin-split-tree.XXXXXX.tgz)
# install/ alone is not enough: prepare-zimbra-data-disk.sh reads the disk
# selector out of ansible/, and without it the second disk is silently
# ignored and Zimbra lands on the OS volume instead.
_root="$(cd "${KIN_MAIL_INSTALL_DIR}/.." && pwd)"
_sel="ansible/roles/drbd_disk_prep/files/select_drbd_disk.py"
# The selector is not always beside install/. console/bootstrap.sh syncs the
# install tree to /opt/kin-mail-deploy and the ansible tree to
# /opt/kin-mail-console, so a deploy driven from the console finds install/
# with no ansible/ next to it - and the mailbox's spare disk goes unused
# while the log says only that the file was not found.
_seldir=""
for _cand in "$_root" /opt/kin-mail-console /opt/kin-mail-deploy; do
  if [ -r "${_cand}/${_sel}" ]; then _seldir="$_cand"; break; fi
done
tar -C "$_root" -czf "$_tgz" install
if [ -n "$_seldir" ]; then
  _plain="${_tgz%.tgz}.tar"
  gunzip -c "$_tgz" >"$_plain" \
    && tar -C "$_seldir" -rf "$_plain" "$_sel" \
    && gzip -c "$_plain" >"$_tgz" \
    && rm -f "$_plain"
  ok "including the data-disk selector from ${_seldir}"
else
  warn "no ${_sel} anywhere - the mailbox's second disk will NOT be used"
fi
mbox_put "$_tgz" /tmp/kin-split-tree.tgz >/dev/null || {
  fail "Could not copy the installer to the mailbox"; rm -f "$_tgz"; exit 1; }
rm -f "$_tgz"
mbox_sudo "rm -rf ${REMOTE_ROOT}/install && mkdir -p ${REMOTE_ROOT} && tar -C ${REMOTE_ROOT} -xzf /tmp/kin-split-tree.tgz && chmod -R a+rX ${REMOTE_ROOT} && rm -f /tmp/kin-split-tree.tgz" >/dev/null || {
  fail "Could not unpack the installer on the mailbox"; exit 1; }
ok "installer copied to ${REMOTE_ROOT} (fresh every run)"

say "2b/8 Copying the appliance configuration"
if step_done config-pushed && ! mbox_run "test -r ${CONF_FILE}" 2>/dev/null; then
  warn "the configuration is recorded as copied, but the mailbox does not have it. Copying again."
  rm -f "${STATE_DIR}/config-pushed"
fi
if step_done config-pushed; then
  ok "already copied"
else
  # Both machines read the same file; each works out its own role from the
  # addresses in it.
  mbox_put "$CONF_FILE" /tmp/kin-config >/dev/null || {
    fail "Could not copy ${CONF_FILE} to the mailbox"; exit 1; }
  mbox_sudo "install -d -m 755 ${CONF_DIR} && install -m 600 -o root -g root /tmp/kin-config ${CONF_FILE} && rm -f /tmp/kin-config" >/dev/null || {
    fail "Could not install the configuration on the mailbox"; exit 1; }
  _remote_role=$(mbox_sudo "bash -c 'cd ${REMOTE_ROOT}/install && . ./00-config.sh >/dev/null 2>&1 && kin_node_role'" 2>/dev/null | tr -dc 'a-z')
  if [ "$_remote_role" != mailbox ]; then
    fail "The mailbox does not recognise itself (it reported '${_remote_role:-nothing}')."
    info "MAILBOX_IP in the config must be an address that machine actually holds."
    exit 1
  fi
  mark_done config-pushed
  ok "mailbox agrees it is the mailbox"
fi

# --- 3-4. build the mailbox ---------------------------------------------------
run_remote_stage() {
  local marker="$1" stage="$2" label="$3" proof="${4:-}"
  if step_done "$marker"; then
    # A marker alone is not evidence. Where there is something cheap to look at
    # on the far machine, look: skipping a step that never happened costs far
    # more than repeating one that did.
    if [ -n "$proof" ] && ! mbox_sudo "$proof" >/dev/null 2>&1; then
      warn "${label}: recorded as done, but the mailbox does not show it. Running it again."
      rm -f "${STATE_DIR}/${marker}"
    else
      ok "${label}: already done"
      return 0
    fi
  fi
  info "${label}: running on ${MBOX_HOST} (output follows)"
  # PIPESTATUS, not the pipeline's own status. A pipeline reports its LAST
  # command, and the last command here is sed, which succeeds whatever the
  # stage did - so a mailbox install that died on a permission error was
  # reported as done, and the run carried on to look for the passwords it
  # never wrote.
  mbox_sudo "${REMOTE_ROOT}/install/${stage} ${5:-}" 2>&1 | sed 's/^/    | /'
  if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    fail "${label} failed on the mailbox"
    return 1
  fi
  mark_done "$marker"
  ok "${label}: done"
}

say "3/8 Preparing the mailbox operating system"
run_remote_stage mailbox-os 02-prepare-os.sh "mailbox 02-prepare-os" \
  "test \"\$(hostname -f)\" = '${MBOX_HOST}'" || exit 1

# Stage 02 just changed the password we are logging in with.
#
# It calls chpasswd to set the OS account to KIN_USER_PASS, which is what the
# console collects on its Host credentials step. MAILBOX_SSH_PASS - what the
# operator typed on the Topology step, and what got us in - stops working the
# moment that runs. Every step after this one would be refused, and the failure
# reads as a wrong password when nothing was typed wrong: the deploy changed it.
#
# So follow it. The account is the same; only the secret moved.
if [ -n "${KIN_USER_PASS:-}" ] && [ "$KIN_USER_PASS" != "$SSH_PASS" ]; then
  if SSHPASS="$KIN_USER_PASS" sshpass -e ssh "${SSH_OPTS[@]}" \
       "${SSH_USER}@${MBOX_IP}" true 2>/dev/null; then
    SSH_PASS="$KIN_USER_PASS"
    ok "mailbox password was reset by 02-prepare-os; continuing with the new one"
  elif mbox_run true 2>/dev/null; then
    info "mailbox still accepts the original password"
  else
    fail "The mailbox now refuses both the original password and KIN_USER_PASS."
    info "02-prepare-os.sh sets the OS account password from KIN_USER_PASS."
    info "Check Host credentials in the wizard against the account on ${MBOX_IP}."
    exit 1
  fi
fi

say "4/8 Installing Zimbra on the mailbox (directory + mail store; 20-40 minutes)"
run_remote_stage mailbox-installed 03-install-zimbra.sh "mailbox 03-install-zimbra" \
  "$MBOX_ZIMBRA_WORKS" || exit 1

# --- 5. carry the directory passwords across ----------------------------------
say "5/8 Carrying the directory passwords to the edge"
if step_done secrets-fetched && [ -r "$LDAP_STORE" ]; then
  ok "already here"
else
  _tmp=$(mktemp "${CONF_DIR}/.ldap-secrets.XXXXXX")
  chmod 600 "$_tmp"
  if ! mbox_sudo "cat ${LDAP_STORE}" >"$_tmp" 2>/dev/null || [ ! -s "$_tmp" ]; then
    rm -f "$_tmp"
    fail "Could not read the directory passwords from the mailbox."
    info "Expected ${LDAP_STORE} there, written when the mailbox was built."
    exit 1
  fi
  # Must look like the store, not like an error message.
  if ! grep -q '^KIN_LDAP_ADMIN_PASS=' "$_tmp"; then
    rm -f "$_tmp"
    fail "What came back from the mailbox is not the password store."
    exit 1
  fi
  mv -f "$_tmp" "$LDAP_STORE"
  chmod 600 "$LDAP_STORE"
  # Kept, not deleted. A re-run of the edge installer has to present the
  # same passwords the directory already has; minting a second set is how
  # the edge installs cleanly and then cannot bind. Mode 600, root only.
  mark_done secrets-fetched
  ok "directory passwords in place (mode 600, kept for re-runs)"
fi

# --- 6-7. build this machine --------------------------------------------------
run_local_stage() {
  local marker="$1" stage="$2" label="$3" proof="${4:-}"
  if step_done "$marker"; then
    if [ -n "$proof" ] && ! eval "$proof" >/dev/null 2>&1; then
      warn "${label}: recorded as done, but this machine does not show it. Running it again."
      rm -f "${STATE_DIR}/${marker}"
    else
      ok "${label}: already done"
      return 0
    fi
  fi
  info "${label}: running here (output follows)"
  "${KIN_MAIL_INSTALL_DIR}/${stage}" 2>&1 | sed 's/^/    | /'
  if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    fail "${label} failed on the edge"
    return 1
  fi
  mark_done "$marker"
  ok "${label}: done"
}

say "6/8 Preparing the edge operating system"
run_local_stage edge-os 02-prepare-os.sh "edge 02-prepare-os" \
  "test \"\$(hostname -f)\" = \"$(kin_edge_host)\"" || exit 1

say "7/8 Installing Zimbra on the edge (MTA + proxy, joining the directory)"
run_local_stage edge-installed 03-install-zimbra.sh "edge 03-install-zimbra" \
  "test -x /opt/zimbra/bin/zmcontrol" || exit 1

# --- 8. does the pair actually work -------------------------------------------
say "8/8 Checking the pair"
_fail=0

_mbox_status=$(mbox_sudo "su - zimbra -c 'zmcontrol status'" 2>/dev/null || printf '')
if printf '%s' "$_mbox_status" | grep -q Running; then
  ok "mailbox services running"
else
  fail "mailbox services are not running"
  _fail=1
fi

if su - zimbra -c "zmcontrol status" 2>/dev/null | grep -q Running; then
  ok "edge services running"
else
  fail "edge services are not running"
  _fail=1
fi

# The edge must be able to reach the directory it just joined. This is the link
# that fails silently: the edge installs cleanly and then binds to nothing.
if timeout 10 bash -c "exec 3<>/dev/tcp/${MBOX_IP}/389" 2>/dev/null; then
  ok "edge can reach the directory on ${MBOX_IP}:389"
else
  fail "edge cannot reach LDAP on ${MBOX_IP}:389"
  _fail=1
fi

# And hand mail over.
if timeout 10 bash -c "exec 3<>/dev/tcp/${MBOX_IP}/7025" 2>/dev/null; then
  ok "edge can reach LMTP on ${MBOX_IP}:7025"
else
  warn "edge cannot reach LMTP on ${MBOX_IP}:7025 yet"
fi

# Both machines must agree on how many servers exist, or one of them joined
# something else. An edge rebuild that keeps the mailbox leaves the old edge
# server object in LDAP; routing then has two MTAs and mail goes to the one
# that no longer exists.
_servers=$(su - zimbra -c "zmprov -l gas" 2>/dev/null | tr '\n' ' ')
_edge_host=$(kin_edge_host)
if [ -n "$_servers" ]; then
  ok "directory knows these servers: ${_servers}"
  case " ${_servers} " in
    *" ${_edge_host} "*) ;;
    *)
      fail "The directory does not list this edge (${_edge_host})."
      info "The edge installed but did not join. Mail will not move."
      _fail=1
      ;;
  esac
  case " ${_servers} " in
    *" ${MBOX_HOST} "*) ;;
    *)
      fail "The directory does not list the mailbox (${MBOX_HOST})."
      _fail=1
      ;;
  esac
  _n=0
  for _s in ${_servers}; do _n=$((_n + 1)); done
  if [ "$_n" -gt 2 ]; then
    warn "The directory lists ${_n} servers. A split has two."
    info "A leftover server object from a previous edge will confuse routing."
    info "Inspect with: su - zimbra -c 'zmprov -l gas'"
  fi
else
  warn "could not list servers from the directory"
fi

# The mailbox is built, and nothing has hardened or firewalled it.
#
# kin-mail.sh --full-install delegates the whole pipeline here, so the stages
# it would normally run after 03 never touched this machine. On the edge they
# run afterwards, because kin-mail.sh continues its own pipeline once this
# returns. The mailbox has no such second pass - it is only ever reached over
# SSH from here - so it happens now.
#
# Firewall last, and last on purpose: it is the stage that can cut this very
# SSH session. It arms a dead man that disables ufw again after five minutes,
# and the rules it writes allow everything from the edge, which is where this
# connection comes from.
if [ "$_fail" -eq 0 ]; then
  echo
  say "9/9 Metrics, status reporting, hardening and firewalling the mailbox"

  # Metrics first, because the firewall below is the last thing that runs here
  # and this needs a port open to the edge before that rule set is written.
  #
  # The console lives on the edge and nobody logs into the mailbox, so without
  # this the one machine holding every message is the one whose CPU, memory and
  # disk nobody can see. Not fatal: a mailbox with no metrics still delivers
  # mail, and failing the build over a monitoring agent would be the same
  # inversion that a certificate caused two releases ago.
  run_remote_stage mailbox-metrics 12-node-metrics.sh "mailbox 12-node-metrics" "" || {
    warn "Node metrics did not install on the mailbox."
    info "Mail is unaffected; the Monitoring tab will have no Mailbox view."
    info "Re-run: sudo ${REMOTE_ROOT}/install/12-node-metrics.sh on ${MBOX_HOST}"
  }

  # Zimbra's own status reporting does not survive a split without this. Each
  # node writes its service status to syslog and forwards it to the logger, and
  # on Ubuntu nothing in Zimbra ever switches the logger's receiver on - so the
  # admin console draws a perfectly healthy edge as entirely down. Not fatal to
  # mail, and it is not allowed to fail the build for the same reason metrics
  # are not: a reporting gap is not an outage.
  run_remote_stage mailbox-log-relay 13-log-relay.sh "mailbox 13-log-relay" "" || {
    warn "The log relay did not install on the mailbox."
    info "Mail is unaffected. Zimbra Administration will show the edge's services"
    info "as down even while they are running."
    info "Re-run: sudo ${REMOTE_ROOT}/install/13-log-relay.sh on ${MBOX_HOST}"
  }

  run_remote_stage mailbox-hardened 09-hardening.sh "mailbox 09-hardening" "" || {
    warn "Hardening did not complete on the mailbox."
    info "Mail still works. Re-run: ${REMOTE_ROOT}/install/09-hardening.sh on ${MBOX_HOST}"
    _fail=1
  }

  if [ "$_fail" -eq 0 ]; then
    run_remote_stage mailbox-firewalled 10-host-firewall.sh "mailbox 10-host-firewall" "" apply || {
      fail "The firewall did not apply on the mailbox."
      info "That machine holds every message and is currently unfirewalled."
      info "Run it by hand: sudo ${REMOTE_ROOT}/install/10-host-firewall.sh apply"
      _fail=1
    }
    if [ "$_fail" -eq 0 ]; then
      # The dead man disables ufw again in five minutes unless it is cancelled,
      # and nobody is going to be watching the mailbox to do that. The proof
      # that the rules are right is this very connection still working.
      if mbox_sudo "${REMOTE_ROOT}/install/10-host-firewall.sh cancel-deadman" >/dev/null 2>&1; then
        ok "mailbox firewall kept (this connection survived it, which is the proof)"
      else
        warn "Could not cancel the mailbox dead-man; ufw there will switch itself off shortly."
        info "Run: sudo ${REMOTE_ROOT}/install/10-host-firewall.sh cancel-deadman"
      fi
    fi
  fi
fi

echo
if [ "$_fail" -ne 0 ]; then
  say "SPLIT BUILD INCOMPLETE"
  info "The checks above name what is wrong. Re-running resumes from the last good step."
  exit 1
fi

say "SPLIT BUILD DONE"
ok "mailbox ${MBOX_HOST} (${MBOX_IP}) - directory and mail store"
ok "edge    $(kin_edge_host) ($(kin_edge_ip)) - MTA and proxy"
info "The mailbox is hardened and firewalled: nothing from the internet, everything"
info "from the edge. The edge's own TLS, DKIM, hardening and firewall follow now."
