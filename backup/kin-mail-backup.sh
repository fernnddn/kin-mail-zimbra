#!/usr/bin/env bash
# =============================================================================
# KIN Mail - backup orchestrator (runs on the backup repository VM)
#
# Pull model: this host SSHs to every configured mail node, finds the one
# with /opt/zimbra mounted (Promoted), runs the collector there, and rsyncs
# the staging tree here. Cron lives on the backup VM so copies survive a
# dual-mail-node event. Node identity is never hardcoded.
#
#   kin-mail-backup.sh              take a daily set + apply retention
#   kin-mail-backup.sh --probe      print which node would be backed up
#
# Config: /etc/kin-mail-backup/config  (see kin-mail-backup.conf.example)
# =============================================================================
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }

CONF="${KIN_MAIL_BACKUP_CONF:-/etc/kin-mail-backup/config}"
REMOTE_SCRIPT_SRC="$(cd "$(dirname "$0")" && pwd)/kin-mail-backup-remote.sh"
REMOTE_SCRIPT_DST="/usr/local/sbin/kin-mail-backup-remote.sh"

MAIL_NODES=""
SSH_USER="kin"
SSH_IDENTITY="/etc/kin-mail-backup/id_ed25519"
BACKUP_ROOT="/var/lib/kin-mail-backup"
REMOTE_STAGING="/var/tmp/kin-mail-backup-staging"
DAILY_KEEP=7
WEEKLY_KEEP=4

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Run as root"; exit 1; }
}

# require_count <name> <value> <minimum>
#
# A retention count that is empty, not a number, or zero would either abort
# this script under `set -u` at 2am on a backup VM, or - worse - be taken as
# "keep none" and delete every backup there is.
require_count() {
  local name="$1" value="$2" min="$3"
  case "$value" in
  '' | *[!0-9]*)
    fail "${name} must be a whole number in ${CONF} (got '${value}')"
    exit 2
    ;;
  esac
  if [ "$value" -lt "$min" ]; then
    fail "${name}=${value} is below the minimum of ${min}; refusing to run"
    exit 2
  fi
}

load_conf() {
  if [ ! -f "$CONF" ]; then
    fail "Missing $CONF - copy kin-mail-backup.conf.example and set MAIL_NODES"
    exit 2
  fi
  # shellcheck disable=SC1090
  . "$CONF"
  if [ -z "${MAIL_NODES:-}" ]; then
    fail "MAIL_NODES is empty in $CONF"
    exit 2
  fi
  # Defaults for keys a config written before these existed does not carry.
  # This script runs under `set -u`, so an unset one would abort with a bare
  # "unbound variable" on a backup VM at 2am.
  : "${DAILY_KEEP:=7}"
  : "${WEEKLY_KEEP:=4}"
  : "${UNVERIFIED_KEEP:=2}"
  : "${BACKUP_ROOT:=/var/lib/kin-mail-backup}"
  : "${REMOTE_STAGING:=/var/tmp/kin-mail-backup-staging}"
  : "${SSH_USER:=kin}"
  : "${SSH_IDENTITY:=/etc/kin-mail-backup/id_ed25519}"
  require_count DAILY_KEEP "$DAILY_KEEP" 1
  require_count WEEKLY_KEEP "$WEEKLY_KEEP" 1
  require_count UNVERIFIED_KEEP "$UNVERIFIED_KEEP" 0
}

backup_ssh() {
  local node="$1"; shift
  ssh -i "$SSH_IDENTITY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
    "${SSH_USER}@${node}" "$@"
}

install_remote() {
  local node="$1"
  scp -q -i "$SSH_IDENTITY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    "$REMOTE_SCRIPT_SRC" "${SSH_USER}@${node}:/tmp/kin-mail-backup-remote.sh"
  backup_ssh "$node" "sudo install -m 0755 /tmp/kin-mail-backup-remote.sh '$REMOTE_SCRIPT_DST' && rm -f /tmp/kin-mail-backup-remote.sh"
}

find_promoted() {
  local node rc out found="" n=0
  for node in $MAIL_NODES; do
    out=$(backup_ssh "$node" "sudo $REMOTE_SCRIPT_DST --probe" 2>/dev/null) && rc=0 || rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '^PROMOTED'; then
      found="$node"
      n=$((n + 1))
      printf '%s\n' "$out"
    else
      printf '%s\n' "${out:-NOT_PROMOTED host=$node}"
    fi
  done
  if [ "$n" -eq 0 ]; then
    fail "No Promoted mail node found among: $MAIL_NODES"
    exit 3
  fi
  if [ "$n" -gt 1 ]; then
    fail "Split-brain? $n nodes report Promoted - refusing to backup"
    exit 3
  fi
  printf 'SELECTED=%s\n' "$found"
}

# Newest-first list of set directories under $1 that carry the marker written
# only after SHA256SUMS passed. Set names are UTC timestamps
# (YYYYmmddTHHMMSSZ) and ISO weeks, so sorting by name is sorting by time -
# and unlike mtime, a name cannot be changed by a later cp -al or touch.
verified_sets() {
  local root="$1" d
  for d in "$root"/*/; do
    [ -d "$d" ] || continue
    d=${d%/}
    [ -f "$d/.backup-ok" ] || continue
    printf '%s\n' "$d"
  done | sort -r
}

unverified_sets() {
  local root="$1" d
  for d in "$root"/*/; do
    [ -d "$d" ] || continue
    d=${d%/}
    [ -f "$d/.backup-ok" ] && continue
    printf '%s\n' "$d"
  done | sort -r
}

# Keep the newest $2 entries of the list on stdin; delete the rest.
prune_all_but() {
  local keep="$1" n=0 d
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    n=$((n + 1))
    [ "$n" -le "$keep" ] && continue
    rm -rf "$d"
    ok "pruned $(basename "$d")"
  done
}

apply_retention() {
  local daily="$BACKUP_ROOT/daily" weekly="$BACKUP_ROOT/weekly"
  mkdir -p "$daily" "$weekly"
  # Weekly layer: one hardlinked snapshot per ISO week (first daily of that week).
  local week
  week="$(date -u +%G-W%V)"
  if [ ! -e "$weekly/$week" ]; then
    cp -al "$1" "$weekly/$week"
    ok "weekly snapshot $week"
  fi

  # Only VERIFIED sets count towards the quota. Retention used to prune the
  # oldest by mtime regardless, so a run interrupted between the pull and the
  # checksum left a partial set in daily/ with a recent timestamp - and that
  # partial set then pushed a good backup out of the window. The thing that
  # made room was the thing that was not a backup.
  verified_sets "$daily" | prune_all_but "$DAILY_KEEP"
  verified_sets "$weekly" | prune_all_but "$WEEKLY_KEEP"

  # Partial sets are kept for inspection but bounded, and never counted above.
  unverified_sets "$daily" | prune_all_but "$UNVERIFIED_KEEP"
  local leftover
  leftover=$(unverified_sets "$daily" | wc -l | tr -d '[:space:]')
  if [ "${leftover:-0}" -gt 0 ]; then
    warn "${leftover} unverified set(s) in ${daily} - an interrupted run leaves these"
    warn "They are ignored by retention and by the backup-age check."
  fi
}

PROBE_ONLY=0
[ "${1:-}" = "--probe" ] && PROBE_ONLY=1

need_root
load_conf

if [ ! -f "$SSH_IDENTITY" ]; then
  fail "SSH identity $SSH_IDENTITY missing - generate a key and trust it on mail nodes"
  exit 2
fi
if [ ! -f "$REMOTE_SCRIPT_SRC" ]; then
  fail "Collector script not next to orchestrator: $REMOTE_SCRIPT_SRC"
  exit 2
fi

say "Installing collector on mail nodes"
for node in $MAIL_NODES; do
  install_remote "$node"
  ok "collector on $node"
done

say "Discovering Promoted node"
mapfile -t _probe < <(find_promoted)
printf '  %s\n' "${_probe[@]}"
PROMOTED=""
for line in "${_probe[@]}"; do
  case "$line" in
    SELECTED=*) PROMOTED="${line#SELECTED=}" ;;
  esac
done
[ -n "$PROMOTED" ] || { fail "internal: no SELECTED node"; exit 3; }
ok "backing up from $PROMOTED"

if [ "$PROBE_ONLY" = "1" ]; then
  exit 0
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST="$BACKUP_ROOT/daily/$STAMP"
mkdir -p "$BACKUP_ROOT/daily"
chmod 700 "$BACKUP_ROOT"

# Staging on the mail node is a full copy of the store, on that node's OS
# disk. Every exit path from here on has to clear it: without this trap, a
# collector that failed part-way left it there until the next night's run
# happened to reach its own `rm -rf` - and if the failure was a full disk,
# that is the copy keeping it full.
STAGING_CLEANED=0
cleanup_remote_staging() {
  [ "${STAGING_CLEANED:-0}" = "1" ] && return 0
  [ -n "${PROMOTED:-}" ] || return 0
  STAGING_CLEANED=1
  if backup_ssh "$PROMOTED" "rm -rf '$REMOTE_STAGING'" >/dev/null 2>&1; then
    warn "cleared leftover staging on $PROMOTED"
  else
    warn "could not clear $PROMOTED:$REMOTE_STAGING - remove it by hand"
  fi
}
trap cleanup_remote_staging EXIT

say "Collecting on $PROMOTED"
backup_ssh "$PROMOTED" "sudo $REMOTE_SCRIPT_DST --backup --staging '$REMOTE_STAGING'"

say "Pulling staging → $DEST"
mkdir -p "$DEST"
chmod 700 "$DEST"
rsync -aH --delete -e "ssh -i $SSH_IDENTITY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "${SSH_USER}@${PROMOTED}:${REMOTE_STAGING}/" "$DEST/"
chmod -R go-rwx "$DEST"

say "Verifying checksums"
checksum_rc=0
( cd "$DEST" && sha256sum -c SHA256SUMS ) >/dev/null 2>&1 || checksum_rc=$?

# Clean the remote staging either way - a corrupt local pull is not a reason
# to also leave this run's copy sitting on the production node's /var/tmp;
# repeated failures would otherwise fill that disk over time.
say "Removing remote staging"
STAGING_CLEANED=1
backup_ssh "$PROMOTED" "rm -rf '$REMOTE_STAGING'" || warn "remote staging cleanup failed, check $PROMOTED:$REMOTE_STAGING by hand"

if [ "$checksum_rc" -ne 0 ]; then
  fail "SHA256SUMS did not match - this set is not a valid backup"
  # Move out of daily/ (not delete - kept for forensics) so it can never be
  # mistaken for a good backup by retention or by the freshness monitor,
  # both of which only look at daily/.
  FAILED_DIR="$BACKUP_ROOT/failed"
  mkdir -p "$FAILED_DIR"
  mv "$DEST" "$FAILED_DIR/$STAMP"
  # Bound how many failed sets accumulate; each one is a full store+index
  # copy, so unbounded repeats would eventually fill this disk too.
  find "$FAILED_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | awk -v k=5 'NR>k {print $2}' \
    | while IFS= read -r d; do [ -n "$d" ] && rm -rf "$d"; done
  fail "Corrupt set kept at $FAILED_DIR/$STAMP for inspection"
  exit 1
fi
ok "SHA256SUMS match"
touch "$DEST/.backup-ok"

# Checksums prove the set arrived intact, not that it holds everything it was
# meant to. The collector records a shortfall in per-account exports; say so
# here rather than letting a partial set read as a clean one.
if grep -q '^complete=false' "$DEST/MANIFEST.txt" 2>/dev/null; then
  got=$(grep -m1 '^mailboxes_exported=' "$DEST/MANIFEST.txt" | cut -d= -f2)
  want=$(grep -m1 '^mailboxes_expected=' "$DEST/MANIFEST.txt" | cut -d= -f2)
  warn "This set is INCOMPLETE: ${got:-0} of ${want:-0} mailbox exports succeeded."
  warn "It is checksum-valid and restorable from store, MySQL and LDAP."
  warn "The accounts that failed are named in $DEST/MANIFEST.txt"
fi

say "Retention (daily keep=$DAILY_KEEP weekly keep=$WEEKLY_KEEP)"
apply_retention "$DEST"
ok "set $DEST"
cat "$DEST/MANIFEST.txt"
exit 0
