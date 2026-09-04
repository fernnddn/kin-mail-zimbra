#!/usr/bin/env bash
# =============================================================================
# KIN Mail - set up the backup repository VM
#
# HA-RUNBOOK.md has described /usr/local/sbin/kin-mail-backup.sh and a nightly
# /etc/cron.d/kin-mail-backup since the backup scripts were written, but
# nothing ever installed them. The scripts sat in the repo and ran only when
# somebody remembered to run them by hand. A backup that is documented and not
# scheduled is worse than no backup, because the runbook says it exists.
#
#   install-backup-vm.sh                 install/refresh everything
#   install-backup-vm.sh --check         report what is and is not in place
#   install-backup-vm.sh --uninstall     remove the schedule (keeps backups)
#
# Idempotent: safe to re-run after every upgrade. It never overwrites the
# config or the SSH key once they exist, and it never deletes a backup.
# =============================================================================
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# DESTDIR stages the whole install under a directory instead of the live
# filesystem, the same way it works in a Makefile. It is how this script's own
# tests exercise the real code paths without needing root or a spare VM.
DESTDIR="${DESTDIR:-}"
SBIN="$DESTDIR/usr/local/sbin"
CONF_DIR="$DESTDIR/etc/kin-mail-backup"
CONF="$CONF_DIR/config"
IDENTITY="$CONF_DIR/id_ed25519"
BACKUP_ROOT="$DESTDIR/var/lib/kin-mail-backup"
LOG="$DESTDIR/var/log/kin-mail-backup.log"
CRON="$DESTDIR/etc/cron.d/kin-mail-backup"
LOGROTATE="$DESTDIR/etc/logrotate.d/kin-mail-backup"
ZBX_LIB="$DESTDIR/usr/local/lib/kin-zabbix"
ZBX_ETC="$DESTDIR/etc/zabbix"

# 02:15 UTC. The runbook's freshness trigger warns at 26h, which only makes
# sense against a daily schedule; both numbers live here and in HA-RUNBOOK.md.
CRON_SCHEDULE="15 2 * * *"

MODE=install
case "${1:-}" in
  --check) MODE=check ;;
  --uninstall) MODE=uninstall ;;
  -h | --help)
    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *)
    fail "Unknown argument: $1"
    exit 2
    ;;
esac

need_root() {
  # A staged install writes nothing outside DESTDIR, so it does not need root.
  [ -n "$DESTDIR" ] && return 0
  [ "$(id -u)" -eq 0 ] || { fail "Run as root"; exit 1; }
}

# install(1) cannot chown to root as a normal user, which a staged install is.
inst() {
  if [ -n "$DESTDIR" ]; then
    install "$@"
  else
    install -o root -g root "$@"
  fi
}

# --- check ------------------------------------------------------------------
# Reports rather than changes. Used by the operator and by anyone asking "is
# this VM actually taking backups", which until now had no answer short of
# reading cron by hand.
run_check() {
  local problems=0
  say "Scripts"
  for f in kin-mail-backup.sh kin-mail-backup-remote.sh kin-mail-restore.sh; do
    if [ -x "$SBIN/$f" ]; then ok "$SBIN/$f"
    else fail "$SBIN/$f is missing"; problems=$((problems + 1)); fi
  done

  say "Configuration"
  if [ -f "$CONF" ]; then
    ok "$CONF"
    local perm
    perm=$(stat -c '%a' "$CONF" 2>/dev/null || echo '?')
    [ "$perm" = "600" ] && ok "mode 0600" || warn "mode is $perm, want 600"
    if grep -q '^MAIL_NODES="192\.0\.2\.' "$CONF" 2>/dev/null; then
      fail "MAIL_NODES still holds the example addresses - no real node is backed up"
      problems=$((problems + 1))
    fi
  else
    fail "$CONF is missing - copy kin-mail-backup.conf.example and set MAIL_NODES"
    problems=$((problems + 1))
  fi
  if [ -f "$IDENTITY" ]; then ok "SSH identity $IDENTITY"
  else fail "SSH identity $IDENTITY is missing"; problems=$((problems + 1)); fi

  say "Schedule"
  if [ -f "$CRON" ]; then
    ok "$CRON"
    printf '           %s\n' "$(grep -v '^#' "$CRON" | grep -v '^$' | head -1)"
  else
    fail "$CRON is missing - nothing is taking a nightly backup"
    problems=$((problems + 1))
  fi
  if [ -n "$DESTDIR" ]; then
    warn "staged install (DESTDIR) - not checking whether cron is running"
  elif systemctl is-active --quiet cron 2>/dev/null || systemctl is-active --quiet crond 2>/dev/null; then
    ok "cron is running"
  else
    fail "cron is not running - the schedule above will not fire"
    problems=$((problems + 1))
  fi

  say "Backups"
  if [ -d "$BACKUP_ROOT/daily" ]; then
    local n newest age
    n=$(find "$BACKUP_ROOT/daily" -mindepth 2 -maxdepth 2 -name '.backup-ok' 2>/dev/null | wc -l | tr -d '[:space:]')
    ok "$n verified set(s) in $BACKUP_ROOT/daily"
    newest=$(find "$BACKUP_ROOT/daily" -mindepth 2 -maxdepth 2 -name '.backup-ok' -printf '%T@ %h\n' 2>/dev/null | sort -n | tail -1)
    if [ -n "$newest" ]; then
      age=$(( $(date +%s) - ${newest%% *} ))
      age=${age%%.*}
      printf '           newest: %s (%sh ago)\n' "$(basename "${newest#* }")" "$((age / 3600))"
      if [ "$age" -gt 93600 ]; then
        fail "the newest verified backup is older than 26h"
        problems=$((problems + 1))
      fi
    else
      fail "no verified backup set exists yet"
      problems=$((problems + 1))
    fi
    # Checksums prove a set arrived intact, not that it holds every mailbox.
    if [ -n "$newest" ] && grep -q '^complete=false' "${newest#* }/MANIFEST.txt" 2>/dev/null; then
      local got want
      got=$(grep -m1 '^mailboxes_exported=' "${newest#* }/MANIFEST.txt" | cut -d= -f2)
      want=$(grep -m1 '^mailboxes_expected=' "${newest#* }/MANIFEST.txt" | cut -d= -f2)
      fail "the newest backup is INCOMPLETE: ${got:-0} of ${want:-0} mailboxes exported"
      printf '           %s\n' "restorable from store/MySQL/LDAP; see MANIFEST.txt for the accounts"
      problems=$((problems + 1))
    fi
    local partial
    partial=$(find "$BACKUP_ROOT/daily" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d '[:space:]')
    [ "$partial" -gt "$n" ] && warn "$((partial - n)) set(s) are unverified (an interrupted run leaves these)"
    [ -d "$BACKUP_ROOT/failed" ] && warn "sets that failed checksum are kept in $BACKUP_ROOT/failed"
  else
    warn "$BACKUP_ROOT/daily does not exist yet - no backup has run"
  fi

  echo
  if [ "$problems" -eq 0 ]; then
    ok "Backup repository is configured and current."
    return 0
  fi
  fail "$problems problem(s) above. Backups are NOT dependable until these are fixed."
  return 1
}

# --- uninstall --------------------------------------------------------------
run_uninstall() {
  need_root
  say "Removing the backup schedule"
  rm -f "$CRON" && ok "removed $CRON"
  rm -f "$LOGROTATE" && ok "removed $LOGROTATE"
  warn "Scripts, config and existing backups under $BACKUP_ROOT are left alone."
  warn "Delete them by hand if this VM is being decommissioned."
}

# --- install ----------------------------------------------------------------
run_install() {
  need_root

  say "Installing scripts to $SBIN"
  inst -d -m 0755 "$SBIN"
  for f in kin-mail-backup.sh kin-mail-backup-remote.sh kin-mail-restore.sh; do
    if [ ! -f "$SRC_DIR/$f" ]; then
      fail "$SRC_DIR/$f not found - run this from a checkout of the repo"
      exit 2
    fi
    inst -m 0755 "$SRC_DIR/$f" "$SBIN/$f"
    ok "$SBIN/$f"
  done
  # The orchestrator finds the collector next to itself, so both must land in
  # the same directory. This is that assumption, checked rather than assumed.
  if [ ! -f "$SBIN/kin-mail-backup-remote.sh" ]; then
    fail "collector did not install next to the orchestrator"
    exit 1
  fi

  say "Configuration"
  inst -d -m 0700 "$CONF_DIR"
  if [ -f "$CONF" ]; then
    chmod 600 "$CONF"
    ok "$CONF already exists, left unchanged"
  else
    inst -m 0600 "$SRC_DIR/kin-mail-backup.conf.example" "$CONF"
    warn "wrote a TEMPLATE $CONF - set MAIL_NODES to the real mail nodes before it can run"
  fi

  if [ -f "$IDENTITY" ]; then
    ok "SSH identity already exists, left unchanged"
  else
    ssh-keygen -t ed25519 -N '' -C "kin-mail-backup@$(hostname -s)" -f "$IDENTITY" >/dev/null
    chmod 600 "$IDENTITY"
    ok "generated $IDENTITY"
    warn "Trust this key on every mail node as the '${SSH_USER:-kin}' user:"
    printf '\n           %s\n\n' "$(cat "${IDENTITY}.pub")"
  fi

  inst -d -m 0700 "$BACKUP_ROOT"
  ok "$BACKUP_ROOT"

  say "Schedule"
  inst -d -m 0755 "$(dirname "$CRON")" "$(dirname "$LOGROTATE")" "$(dirname "$LOG")"
  # A cron job writes no log unless told to. Without this redirect the runbook's
  # "check /var/log/kin-mail-backup.log" step has nothing to read, which is
  # exactly the moment somebody needs it.
  cat > "$CRON" <<CRONFILE
# KIN Mail nightly backup. Installed by install-backup-vm.sh - do not edit by
# hand; re-run that script instead. The 26h freshness warning in HA-RUNBOOK.md
# assumes this daily schedule.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
${CRON_SCHEDULE} root ${SBIN}/kin-mail-backup.sh >> ${LOG} 2>&1
CRONFILE
  chmod 0644 "$CRON"
  ok "$CRON  (${CRON_SCHEDULE} UTC)"

  touch "$LOG"
  chmod 0640 "$LOG"
  cat > "$LOGROTATE" <<'ROTATE'
/var/log/kin-mail-backup.log {
  weekly
  rotate 12
  compress
  delaycompress
  missingok
  notifempty
  create 0640 root root
}
ROTATE
  chmod 0644 "$LOGROTATE"
  ok "$LOGROTATE"

  # The freshness metric only reports if the agent can run the probe. Install
  # it when a Zabbix agent is present; skip quietly when there is none, since
  # a backup VM without monitoring is still a working backup VM.
  say "Freshness monitor"
  local probe="$SRC_DIR/../monitoring/zabbix/scripts/kin-backup-age.sh"
  if [ -d "$ZBX_ETC" ] && [ -f "$probe" ]; then
    inst -d -m 0755 "$ZBX_LIB"
    inst -m 0755 "$probe" "$ZBX_LIB/kin-backup-age.sh"
    ok "$ZBX_LIB/kin-backup-age.sh"
    if [ -d "$ZBX_ETC/zabbix_agent2.d" ]; then
      printf 'UserParameter=kin.backup.age_seconds,sudo %s/kin-backup-age.sh\n' "$ZBX_LIB" \
        > "$ZBX_ETC/zabbix_agent2.d/kin-backup.conf"
      ok "UserParameter kin.backup.age_seconds"
    elif [ -d "$ZBX_ETC/zabbix_agentd.d" ]; then
      printf 'UserParameter=kin.backup.age_seconds,sudo %s/kin-backup-age.sh\n' "$ZBX_LIB" \
        > "$ZBX_ETC/zabbix_agentd.d/kin-backup.conf"
      ok "UserParameter kin.backup.age_seconds"
    else
      warn "no zabbix agent include directory found - add the UserParameter by hand"
    fi
  else
    warn "no Zabbix agent here; the nightly run still logs to $LOG"
    warn "Check freshness with: $0 --check"
  fi

  echo
  say "Verifying"
  if bash -n "$SBIN/kin-mail-backup.sh"; then ok "orchestrator parses"; fi
  if grep -q '^MAIL_NODES="192\.0\.2\.' "$CONF" 2>/dev/null; then
    echo
    warn "NOT READY: $CONF still has the example addresses."
    warn "Set MAIL_NODES, then run:  $SBIN/kin-mail-backup.sh --probe"
  else
    ok "MAIL_NODES is set"
    echo
    say "Next: confirm the pull path end to end"
    printf '           %s --probe\n' "$SBIN/kin-mail-backup.sh"
  fi
}

case "$MODE" in
  check) run_check ;;
  uninstall) run_uninstall ;;
  install) run_install ;;
esac
