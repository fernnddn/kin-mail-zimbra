#!/usr/bin/env bash
# =============================================================================
# KIN Mail — backup orchestrator (runs on the backup repository VM)
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

load_conf() {
  if [ ! -f "$CONF" ]; then
    fail "Missing $CONF — copy kin-mail-backup.conf.example and set MAIL_NODES"
    exit 2
  fi
  # shellcheck disable=SC1090
  . "$CONF"
  if [ -z "${MAIL_NODES:-}" ]; then
    fail "MAIL_NODES is empty in $CONF"
    exit 2
  fi
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
    fail "Split-brain? $n nodes report Promoted — refusing to backup"
    exit 3
  fi
  printf 'SELECTED=%s\n' "$found"
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
  # Prune by directory mtime. Keep newest DAILY_KEEP / WEEKLY_KEEP names.
  find "$daily" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | awk -v k="$DAILY_KEEP" 'NR>k {print $2}' \
    | while IFS= read -r d; do [ -n "$d" ] && rm -rf "$d"; done
  find "$weekly" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | awk -v k="$WEEKLY_KEEP" 'NR>k {print $2}' \
    | while IFS= read -r d; do [ -n "$d" ] && rm -rf "$d"; done
}

PROBE_ONLY=0
[ "${1:-}" = "--probe" ] && PROBE_ONLY=1

need_root
load_conf

if [ ! -f "$SSH_IDENTITY" ]; then
  fail "SSH identity $SSH_IDENTITY missing — generate a key and trust it on mail nodes"
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

say "Collecting on $PROMOTED"
backup_ssh "$PROMOTED" "sudo $REMOTE_SCRIPT_DST --backup --staging '$REMOTE_STAGING'"

say "Pulling staging → $DEST"
mkdir -p "$DEST"
chmod 700 "$DEST"
rsync -aH --delete -e "ssh -i $SSH_IDENTITY -o IdentitiesOnly=yes -o BatchMode=yes" \
  "${SSH_USER}@${PROMOTED}:${REMOTE_STAGING}/" "$DEST/"
chmod -R go-rwx "$DEST"

say "Verifying checksums"
( cd "$DEST" && sha256sum -c SHA256SUMS ) >/dev/null
ok "SHA256SUMS match"

say "Removing remote staging"
backup_ssh "$PROMOTED" "rm -rf '$REMOTE_STAGING'"

say "Retention (daily keep=$DAILY_KEEP weekly keep=$WEEKLY_KEEP)"
apply_retention "$DEST"
ok "set $DEST"
cat "$DEST/MANIFEST.txt"
exit 0
