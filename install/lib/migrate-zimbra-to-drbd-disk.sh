#!/usr/bin/env bash
# =============================================================================
# KIN Mail — move a live /opt/zimbra tree from the OS volume onto the DRBD
# data partition (usually /dev/sdb1) BEFORE Build HA pair.
#
# Why: console ha_disk.py refuses HA while Zimbra still lives on the root
# volume (replicating an empty DRBD disk would drop mail). plan_auto_partition()
# GPT-partitions only — it does NOT mkfs the data slice (meta stays unformatted).
#
# This script is MANUAL and per-host. It is NOT called from orchestration.
# Do not run it on a node that already has Pacemaker/DRBD owning /opt/zimbra.
#
#   sudo ./install/lib/migrate-zimbra-to-drbd-disk.sh --dry-run
#   sudo ./install/lib/migrate-zimbra-to-drbd-disk.sh --i-understand-this-moves-live-mail
#
# Override the target with KIN_DRBD_DATA_DISK=/dev/nvme0n1p1 if needed.
# Take an independent backup of /opt/zimbra before the real run.
# =============================================================================
set -u

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

DATA_DISK="${KIN_DRBD_DATA_DISK:-/dev/sdb1}"
META_DISK="${KIN_DRBD_META_DISK:-/dev/sdb2}"
ZIMBRA_DIR="/opt/zimbra"
TMP_MNT="/mnt/kin-zimbra-data"
FSTAB="/etc/fstab"
DRY_RUN=0
CONFIRMED=0
STAGE=init
BACKUP_DIR=""
FSTAB_BAK=""
TMP_MOUNTED=0
OPT_MOUNTED_NEW=0
RESUME_RSYNC=0
RSYNC_FLAGS=(-aHAX --numeric-ids --sparse --exclude='/lost+found')

usage() {
  cat <<EOF
Usage: sudo $0 [--dry-run] [--i-understand-this-moves-live-mail]

  --dry-run
      Read-only checks. Prints whether mkfs is needed. Does not stop Zimbra.

  --i-understand-this-moves-live-mail
      Required for the real run. Stops Zimbra, rsyncs onto ${DATA_DISK},
      mounts by UUID at ${ZIMBRA_DIR}, starts Zimbra, leaves the old tree
      as a rename on the root volume (not deleted).

Environment:
  KIN_DRBD_DATA_DISK     data partition (default /dev/sdb1)
  KIN_DRBD_META_DISK     meta partition — never formatted (default /dev/sdb2)
  KIN_MIGRATE_CHECKSUM=1 extra rsync --checksum verify (slow; optional)
  KIN_MIGRATE_STOP_WAIT_SEC   max seconds to wait after zmcontrol stop (default 300)
  KIN_MIGRATE_START_WAIT_SEC  max seconds to wait after zmcontrol start (default 900)

See HA-RUNBOOK.md §13.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --i-understand-this-moves-live-mail) CONFIRMED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

norm_src() {
  local s="$1"
  s="${s%%[*}"
  s="${s%% *}"
  printf '%s' "$s"
}

die() {
  fail "$*"
  rollback
  exit 1
}

# zmcontrol status text. Tests set KIN_ZMCONTROL_STATUS_TEXT / _FILE.
zimbra_status_text() {
  if [ -n "${KIN_ZMCONTROL_STATUS_TEXT+x}" ]; then
    printf '%s\n' "$KIN_ZMCONTROL_STATUS_TEXT"
    return 0
  fi
  if [ -n "${KIN_ZMCONTROL_STATUS_FILE:-}" ]; then
    cat "$KIN_ZMCONTROL_STATUS_FILE" 2>/dev/null || true
    return 0
  fi
  su - zimbra -c "zmcontrol status" 2>&1 || true
}

# Service names whose last field is Running or Stopped (stdin = zmcontrol status).
# Ignores Host, blank, and Connect: noise. "service webapp" stays one name.
zimbra_status_names() {
  local want="$1"
  awk -v want="$want" '
    /^[[:space:]]*$/ { next }
    $1 == "Host" { next }
    /Connect:/ { next }
    /Enabled services read from cache/ { next }
    NF >= 2 && $NF == want {
      n = $1
      for (i = 2; i < NF; i++) n = n " " $i
      print n
    }
  '
}

zimbra_status_service_count() {
  awk '
    /^[[:space:]]*$/ { next }
    $1 == "Host" { next }
    /Connect:/ { next }
    /Enabled services read from cache/ { next }
    NF >= 2 && ($NF == "Running" || $NF == "Stopped") { n++ }
    END { print n + 0 }
  '
}

count_nonempty_lines() {
  awk 'NF { n++ } END { print n + 0 }'
}

names_on_one_line() {
  tr '\n' ' ' | sed 's/[[:space:]]*$//'
}

# Daemons that must be gone before rsync. Ignores shells and the status check.
# Antispam is not a dedicated spamd: zmantispamctl runs antispam-mysql.server
# (mysqld_safe) and zmamavisdctl (amavisd master / amavis-mc).
zimbra_lingering_daemons() {
  [ "${KIN_MIGRATE_SKIP_PS_CHECK:-0}" = "1" ] && return 0
  ps -u zimbra -ww -o comm=,args= 2>/dev/null | awk '
    $1 ~ /^(bash|sh|dash|su|ps|awk|sed|grep|cat|head|tr|wc)$/ { next }
    $0 ~ /zmcontrol/ { next }
    $1 ~ /^(java|mysqld|mysqld_safe|slapd|nginx|master|amavisd|amavis-mc|clamd|clamdscan|memcached|opendkim|httpd|node|beam\.smp|epmd|god)$/ { print; next }
    $0 ~ /zmconfigd/ { print; next }
    $0 ~ /onlyoffice/ { print; next }
    $0 ~ /soffice/ { print; next }
    $0 ~ /amavisd/ { print; next }
    $0 ~ /antispam-mysql/ { print; next }
  ' || true
}

# zmamavisdctl status walks the process table for amavisd (master). It does not
# need LDAP. KIN_ANTISPAM_BACKING=up|down mocks this for tests.
zimbra_antispam_backing_up() {
  case "${KIN_ANTISPAM_BACKING:-}" in
    up) return 0 ;;
    down) return 1 ;;
  esac
  su - zimbra -c "zmamavisdctl status" >/dev/null 2>&1
}

# zmcontrol maps *ctl status exit 0 to Running. zmantispamctl does:
#   zmprov -l gs $host zimbraServiceEnabled | grep -qw antispam
#   if that grep fails, ENABLED=0 and status exits 0 without checking processes.
# After ldap is Stopped, zmprov fails, so antispam stays "Running" forever even
# when amavisd and antispam-mysql are gone. Drop that cached line only then.
zimbra_filter_cached_antispam() {
  local text="$1"
  local running="$2"
  local ldap_stopped
  ldap_stopped=$(printf '%s\n' "$text" | zimbra_status_names Stopped | grep -cx 'ldap' || true)
  if [ "${ldap_stopped:-0}" -eq 0 ]; then
    printf '%s\n' "$running"
    return 0
  fi
  if ! printf '%s\n' "$running" | grep -qx 'antispam'; then
    printf '%s\n' "$running"
    return 0
  fi
  if zimbra_antispam_backing_up; then
    printf '%s\n' "$running"
    return 0
  fi
  # info writes stdout; this function's stdout is the remaining Running names.
  info "ldap is Stopped; zmcontrol antispam Running is zmantispamctl skipping process checks after zmprov failed. Backing processes are gone; treating antispam as Stopped." >&2
  printf '%s\n' "$running" | grep -vx 'antispam' || true
}

dump_zimbra_status() {
  info "zmcontrol status:"
  zimbra_status_text | sed 's/^/    /' || true
}

dump_zimbra_lingering() {
  local linger
  linger=$(zimbra_lingering_daemons || true)
  if [ -n "$linger" ]; then
    info "lingering zimbra daemons:"
    printf '%s\n' "$linger" | sed 's/^/    /'
  fi
}

# zmcontrol stop prints "Stopping X...Done" when each ctl script returns, which
# is before mailboxd/onlyoffice/java have always fully exited. Poll status plus
# the process table, with backoff, until it is actually down.
wait_none_running() {
  local budget="${KIN_MIGRATE_STOP_WAIT_SEC:-300}"
  local slept=0 delay=2 max_delay=8
  local text running linger svc nrunning
  while [ "$slept" -le "$budget" ]; do
    text=$(zimbra_status_text)
    running=$(printf '%s\n' "$text" | zimbra_status_names Running)
    running=$(zimbra_filter_cached_antispam "$text" "$running")
    linger=$(zimbra_lingering_daemons || true)
    svc=$(printf '%s\n' "$text" | zimbra_status_service_count)
    nrunning=$(printf '%s\n' "$running" | count_nonempty_lines)
    if [ "${svc:-0}" -ge 1 ] && [ "${nrunning:-0}" -eq 0 ] && [ -z "$linger" ]; then
      return 0
    fi
    if [ -n "$running" ]; then
      info "stop wait ${slept}s/${budget}s: still Running: $(printf '%s\n' "$running" | names_on_one_line)"
    elif [ "${svc:-0}" -eq 0 ]; then
      info "stop wait ${slept}s/${budget}s: zmcontrol status has no service lines yet"
    fi
    if [ -n "$linger" ]; then
      info "stop wait ${slept}s/${budget}s: lingering daemons:"
      printf '%s\n' "$linger" | sed 's/^/    /'
    fi
    [ "$slept" -ge "$budget" ] && break
    sleep "$delay"
    slept=$((slept + delay))
    if [ "$delay" -lt "$max_delay" ]; then
      delay=$((delay + 1))
    fi
  done
  return 1
}

wait_all_running() {
  local budget="${KIN_MIGRATE_START_WAIT_SEC:-900}"
  local slept=0 delay=2 max_delay=8
  local text running stopped svc nrunning nstopped
  while [ "$slept" -le "$budget" ]; do
    text=$(zimbra_status_text)
    running=$(printf '%s\n' "$text" | zimbra_status_names Running)
    stopped=$(printf '%s\n' "$text" | zimbra_status_names Stopped)
    svc=$(printf '%s\n' "$text" | zimbra_status_service_count)
    nrunning=$(printf '%s\n' "$running" | count_nonempty_lines)
    nstopped=$(printf '%s\n' "$stopped" | count_nonempty_lines)
    if [ "${svc:-0}" -ge 1 ] && [ "${nstopped:-0}" -eq 0 ] && [ "${nrunning:-0}" -ge 1 ]; then
      return 0
    fi
    if [ -n "$stopped" ]; then
      info "start wait ${slept}s/${budget}s: still Stopped: $(printf '%s\n' "$stopped" | names_on_one_line)"
    elif [ "${svc:-0}" -eq 0 ]; then
      info "start wait ${slept}s/${budget}s: zmcontrol status has no service lines yet"
    fi
    [ "$slept" -ge "$budget" ] && break
    sleep "$delay"
    slept=$((slept + delay))
    if [ "$delay" -lt "$max_delay" ]; then
      delay=$((delay + 1))
    fi
  done
  return 1
}

rollback() {
  trap - INT TERM HUP
  [ "$DRY_RUN" -eq 1 ] && return 0
  case "$STAGE" in
    init|preflight|done) return 0 ;;
  esac
  warn "Attempting rollback from stage=${STAGE}"
  if [ "$OPT_MOUNTED_NEW" -eq 1 ]; then
    su - zimbra -c "zmcontrol stop" >/dev/null 2>&1 || true
    umount_retry "$ZIMBRA_DIR" 2>/dev/null || umount "$ZIMBRA_DIR" 2>/dev/null || true
    OPT_MOUNTED_NEW=0
  fi
  if [ -n "$FSTAB_BAK" ] && [ -f "$FSTAB_BAK" ]; then
    cp -a "$FSTAB_BAK" "$FSTAB"
    info "Restored ${FSTAB} from ${FSTAB_BAK}"
  fi
  if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] && [ ! -d "$ZIMBRA_DIR/bin" ]; then
    rmdir "$ZIMBRA_DIR" 2>/dev/null || true
    mv "$BACKUP_DIR" "$ZIMBRA_DIR"
    info "Restored ${ZIMBRA_DIR} from ${BACKUP_DIR}"
  fi
  if [ "$TMP_MOUNTED" -eq 1 ]; then
    umount_retry "$TMP_MNT" 2>/dev/null || umount "$TMP_MNT" 2>/dev/null || true
    TMP_MOUNTED=0
  fi
  if [ ! -d "$ZIMBRA_DIR/bin" ]; then
    fail "Rollback: ${ZIMBRA_DIR}/bin missing, cannot start Zimbra. Mail may be down."
    return 1
  fi
  say "Rollback: starting Zimbra"
  if ! su - zimbra -c "zmcontrol start"; then
    fail "Rollback zmcontrol start failed. Mail may be down."
    dump_zimbra_status
    return 1
  fi
  if ! wait_all_running; then
    fail "Rollback started Zimbra but not all services are Running. Mail may be down."
    dump_zimbra_status
    return 1
  fi
  ok "Rollback: all Zimbra services Running"
}

parent_name() {
  lsblk -no PKNAME "$1" 2>/dev/null | head -1 | tr -d ' '
}

first_leftover() {
  local p
  for p in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    [ -e "$p" ] || [ -L "$p" ] || continue
    [ "$(basename "$p")" = "lost+found" ] && continue
    basename "$p"
    return 0
  done
  return 1
}

tree_entry_count() {
  find "$1" -mindepth 1 ! -path '*/lost+found' ! -path '*/lost+found/*' | wc -l | tr -d ' '
}

umount_retry() {
  local target="$1" i=0
  while [ "$i" -lt 10 ]; do
    umount "$target" 2>/dev/null && return 0
    i=$((i + 1))
    sleep 1
  done
  umount "$target"
}

if [ "${KIN_MIGRATE_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  fail "Run as root: sudo $0"
  exit 1
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$CONFIRMED" -eq 0 ]; then
  fail "Refusing to move live mail without --i-understand-this-moves-live-mail"
  info "Take an independent backup of ${ZIMBRA_DIR} first, then --dry-run, then the real flag."
  usage
  exit 2
fi

if [ "$DRY_RUN" -eq 0 ]; then
  trap 'die "interrupted"' INT TERM HUP
fi

# --- preflight (read-only) ---------------------------------------------------
STAGE=preflight
say "Preflight"

if [ ! -d "$ZIMBRA_DIR" ]; then
  fail "${ZIMBRA_DIR} is missing — nothing to migrate"
  exit 1
fi
if [ ! -x "$ZIMBRA_DIR/bin/zmcontrol" ] && [ ! -x /opt/zimbra/bin/zmcontrol ]; then
  fail "zmcontrol not found under ${ZIMBRA_DIR} — refusing"
  exit 1
fi

for bin in findmnt lsblk blkid blockdev mount umount rmdir mv mkdir cp awk grep find basename du date head sed sync su ps; do
  command -v "$bin" >/dev/null 2>&1 || { fail "Required command not found: ${bin}"; exit 1; }
done

if [ ! -b "$DATA_DISK" ]; then
  fail "Data partition ${DATA_DISK} is not a block device. Attach/partition the spare disk first (Build HA pair GPT: data + ~256 MiB meta)."
  exit 1
fi
if [ -b "$META_DISK" ] && [ "$DATA_DISK" = "$META_DISK" ]; then
  fail "Data and meta disks are the same path — refusing"
  exit 1
fi

ROOT_SRC=$(norm_src "$(findmnt -n -o SOURCE /)")
[ -n "$ROOT_SRC" ] || { fail "Could not read the backing device for /"; exit 1; }
ROOT_PARENT=$(parent_name "$ROOT_SRC")
DATA_PARENT=$(parent_name "$DATA_DISK")
[ -n "$DATA_PARENT" ] || { fail "Could not read PKNAME for ${DATA_DISK}"; exit 1; }
if [ "$DATA_PARENT" = "$ROOT_PARENT" ]; then
  fail "${DATA_DISK} is on the OS disk (${ROOT_PARENT}) — refusing to put Zimbra there"
  exit 1
fi

Z_MNTSRC=$(norm_src "$(findmnt -n -o SOURCE "$ZIMBRA_DIR" 2>/dev/null || true)")
Z_UUID=$(findmnt -n -o UUID "$ZIMBRA_DIR" 2>/dev/null || true)
DATA_UUID=$(blkid -s UUID -o value "$DATA_DISK" 2>/dev/null || true)
if [ -n "$Z_UUID" ] && [ -n "$DATA_UUID" ] && [ "$Z_UUID" = "$DATA_UUID" ]; then
  ok "${ZIMBRA_DIR} is already mounted from ${DATA_DISK} (UUID=${DATA_UUID})"
  exit 0
fi
if printf '%s' "$Z_MNTSRC" | grep -q '^/dev/drbd'; then
  ok "${ZIMBRA_DIR} is already on DRBD (${Z_MNTSRC}) — no migrate"
  exit 0
fi
if [ -n "$Z_MNTSRC" ] && [ "$Z_MNTSRC" = "$DATA_DISK" ]; then
  ok "${ZIMBRA_DIR} is already mounted from ${DATA_DISK}"
  exit 0
fi

if command -v pcs >/dev/null 2>&1 && pcs status >/dev/null 2>&1; then
  if pcs status 2>/dev/null | grep -qE 'kin-drbd|kin-fs|kin-mail-svc'; then
    fail "Pacemaker already owns KIN Mail resources — do not use this script on a formed cluster"
    exit 1
  fi
fi
if command -v drbdadm >/dev/null 2>&1; then
  if drbdadm status kin-zimbra >/dev/null 2>&1; then
    fail "DRBD resource kin-zimbra is already configured — refusing to mkfs/mount the backing disk"
    exit 1
  fi
fi

EXTRA_MOUNTS=$(findmnt -nr -o TARGET | awk '$0 ~ /^\/opt\/zimbra\// {print}' || true)
if [ -n "$EXTRA_MOUNTS" ]; then
  fail "Extra mounts under ${ZIMBRA_DIR} — refusing a blind rsync:"
  printf '%s\n' "$EXTRA_MOUNTS" | sed 's/^/    /'
  exit 1
fi

command -v rsync >/dev/null 2>&1 || {
  say "Installing rsync"
  if [ "$DRY_RUN" -eq 1 ]; then
    info "dry-run: would apt-get install rsync"
  else
    export DEBIAN_FRONTEND=noninteractive
    apt-get -y install rsync >/dev/null || die "apt-get install rsync failed"
  fi
}

NEED_MKFS=0
FSTYPE=$(blkid -s TYPE -o value "$DATA_DISK" 2>/dev/null || true)
DATA_MP=$(lsblk -no MOUNTPOINT "$DATA_DISK" 2>/dev/null | head -1 | tr -d ' ')
if [ -n "$DATA_MP" ]; then
  fail "${DATA_DISK} is already mounted on ${DATA_MP} — unmount it and re-run"
  exit 1
fi

case "$FSTYPE" in
  "")
    NEED_MKFS=1
    command -v mkfs.ext4 >/dev/null 2>&1 || { fail "mkfs.ext4 not found (install e2fsprogs)"; exit 1; }
    info "${DATA_DISK} has no filesystem — will mkfs.ext4 -L zimbra-data (plan_auto_partition does not mkfs data)"
    ;;
  ext4)
    ok "${DATA_DISK} is already ext4 (will not mkfs again)"
    ;;
  *)
    fail "${DATA_DISK} has TYPE=${FSTYPE} — refusing to reuse/reformat an unexpected filesystem"
    exit 1
    ;;
esac

Z_BYTES=$(du -sb "$ZIMBRA_DIR" 2>/dev/null | awk '{print $1}')
[ -n "$Z_BYTES" ] || { fail "Could not measure ${ZIMBRA_DIR}"; exit 1; }
PART_BYTES=$(blockdev --getsize64 "$DATA_DISK")
# Leave ~10% headroom on the data partition.
NEED_BYTES=$((Z_BYTES + Z_BYTES / 10 + 1073741824))
if [ "$PART_BYTES" -lt "$NEED_BYTES" ]; then
  fail "${DATA_DISK} is ${PART_BYTES} bytes; ${ZIMBRA_DIR} is ${Z_BYTES} bytes (need ~10% headroom + 1GiB)"
  exit 1
fi
ok "Size: zimbra ${Z_BYTES} bytes, partition ${PART_BYTES} bytes"

if [ "$NEED_MKFS" -eq 0 ]; then
  mkdir -p "$TMP_MNT"
  if mount -o ro "$DATA_DISK" "$TMP_MNT" 2>/dev/null; then
    leftover=$(first_leftover "$TMP_MNT" || true)
    umount "$TMP_MNT" || true
    if [ -n "$leftover" ]; then
      DATA_LABEL=$(blkid -s LABEL -o value "$DATA_DISK" 2>/dev/null || true)
      if [ "$DATA_LABEL" = "zimbra-data" ] && [ -z "$Z_MNTSRC" ]; then
        RESUME_RSYNC=1
        warn "${DATA_DISK} already has files (e.g. ${leftover}) — treating as a resumed copy (label zimbra-data, source still on the root volume)"
      else
        fail "${DATA_DISK} already has files (e.g. ${leftover}) and is not a resumable zimbra-data slice. Inspect it before migrating."
        exit 1
      fi
    else
      ok "${DATA_DISK} ext4 is empty (only lost+found allowed)"
    fi
  else
    fail "Could not mount ${DATA_DISK} read-only to confirm it is empty"
    exit 1
  fi
fi

info "Target : ${DATA_DISK} (parent /dev/${DATA_PARENT})"
info "Meta   : ${META_DISK} (must stay unformatted — this script never mkfs meta)"
info "Source : ${ZIMBRA_DIR} currently on ${Z_MNTSRC:-root volume (${ROOT_SRC})}"
if [ "$DRY_RUN" -eq 1 ]; then
  say "DRY-RUN complete — no changes"
  if [ "$NEED_MKFS" -eq 1 ]; then
    info "Real run would: mkfs.ext4, zmcontrol stop, rsync -aHAX, fstab UUID, mount ${ZIMBRA_DIR}, zmcontrol start"
  else
    info "Real run would: zmcontrol stop, rsync -aHAX, fstab UUID, mount ${ZIMBRA_DIR}, zmcontrol start"
  fi
  info "Independent backup of ${ZIMBRA_DIR} is still recommended before the real run."
  exit 0
fi

# --- mutate ------------------------------------------------------------------
if [ "$NEED_MKFS" -eq 1 ]; then
  say "Creating ext4 on ${DATA_DISK}"
  mkfs.ext4 -F -L zimbra-data "$DATA_DISK" || die "mkfs.ext4 failed"
  udevadm settle || true
  FSTYPE=$(blkid -s TYPE -o value "$DATA_DISK" 2>/dev/null || true)
  [ "$FSTYPE" = "ext4" ] || die "mkfs reported success but TYPE is '${FSTYPE}'"
  ok "ext4 ready on ${DATA_DISK}"
fi

UUID=$(blkid -s UUID -o value "$DATA_DISK" 2>/dev/null || true)
[ -n "$UUID" ] || die "No UUID for ${DATA_DISK} after mkfs/settle"

STAGE=stopped
say "Stopping Zimbra"
su - zimbra -c "zmcontrol stop" || die "zmcontrol stop failed"
# zmcontrol stop can print Done while zmconfigd's watcher is still up.
su - zimbra -c "zmconfigdctl stop" >/dev/null 2>&1 || true
if ! wait_none_running; then
  dump_zimbra_status
  dump_zimbra_lingering
  die "Zimbra still has Running services after stop"
fi
ok "Zimbra is stopped"

STAGE=rsync
say "Rsync ${ZIMBRA_DIR}/ → ${TMP_MNT}/"
mkdir -p "$TMP_MNT"
mount "$DATA_DISK" "$TMP_MNT" || die "mount ${DATA_DISK} → ${TMP_MNT} failed"
TMP_MOUNTED=1
if [ "$RESUME_RSYNC" -eq 1 ]; then
  RSYNC_FLAGS+=(--delete)
  warn "Resume: rsync --delete so ${DATA_DISK} matches ${ZIMBRA_DIR} (original tree still untouched)"
fi
rsync "${RSYNC_FLAGS[@]}" "$ZIMBRA_DIR"/ "$TMP_MNT"/ \
  || die "rsync failed (exit $?) — original tree is untouched at ${ZIMBRA_DIR}"
# Second pass must not want to copy or delete anything (timestamp-only lines are ok).
VERIFY_FLAGS=("${RSYNC_FLAGS[@]}")
if [ "${KIN_MIGRATE_CHECKSUM:-0}" = "1" ]; then
  VERIFY_FLAGS+=(--checksum)
  info "KIN_MIGRATE_CHECKSUM=1 — verify pass uses --checksum (slow on large stores)"
fi
pending=$(rsync "${VERIFY_FLAGS[@]}" --dry-run --itemize-changes \
  "$ZIMBRA_DIR"/ "$TMP_MNT"/ | grep -E '^[<>]|^\*deleting' | wc -l | tr -d ' ')
if [ "${pending:-0}" -ne 0 ]; then
  die "rsync verify still lists ${pending} change(s) — refusing cutover"
fi
src_n=$(tree_entry_count "$ZIMBRA_DIR")
dst_n=$(tree_entry_count "$TMP_MNT")
if [ "$src_n" != "$dst_n" ]; then
  die "entry count mismatch after rsync (source=${src_n} dest=${dst_n}) — refusing cutover"
fi
sync
ok "rsync complete (exit 0, verify dry-run empty, ${src_n} entries)"

umount_retry "$TMP_MNT" || die "umount ${TMP_MNT} failed after rsync"
TMP_MOUNTED=0

STAGE=cutover
say "Cutover: fstab UUID + rename old tree"
TS=$(date +%Y%m%dT%H%M%S)
BACKUP_DIR="${ZIMBRA_DIR}.root-${TS}"
FSTAB_BAK="${FSTAB}.kin-pre-zimbra-migrate-${TS}"
cp -a "$FSTAB" "$FSTAB_BAK" || die "Could not backup ${FSTAB}"

if grep -q "KIN Mail zimbra data partition" "$FSTAB" 2>/dev/null; then
  die "${FSTAB} already has a KIN Mail zimbra data entry — inspect by hand"
fi
cat >> "$FSTAB" <<EOF

# KIN Mail zimbra data partition (pre-cluster). See HA-RUNBOOK §13.
# Remove this UUID line when Pacemaker kin-fs mounts /dev/drbd0 (Build HA pair).
UUID=${UUID} ${ZIMBRA_DIR} ext4 defaults 0 2
EOF
grep -Fq "UUID=${UUID} ${ZIMBRA_DIR} ext4 defaults 0 2" "$FSTAB" \
  || die "fstab write did not stick — refusing cutover"

mv "$ZIMBRA_DIR" "$BACKUP_DIR" || die "mv ${ZIMBRA_DIR} → ${BACKUP_DIR} failed"
mkdir -p "$ZIMBRA_DIR"
if ! mount "$ZIMBRA_DIR"; then
  die "mount ${ZIMBRA_DIR} from fstab failed — restoring the original directory"
fi
OPT_MOUNTED_NEW=1

MOUNTED_SRC=$(norm_src "$(findmnt -n -o SOURCE "$ZIMBRA_DIR")")
UUID_SRC=$(findmnt -n -o UUID "$ZIMBRA_DIR" 2>/dev/null || true)
if [ "$UUID_SRC" != "$UUID" ] && [ "$MOUNTED_SRC" != "$DATA_DISK" ]; then
  die "${ZIMBRA_DIR} mounted from ${MOUNTED_SRC} (uuid=${UUID_SRC}), expected UUID=${UUID}"
fi
[ -x "${ZIMBRA_DIR}/bin/zmcontrol" ] || die "${ZIMBRA_DIR}/bin/zmcontrol missing after mount"
ok "Mounted ${ZIMBRA_DIR} from UUID=${UUID} (${DATA_DISK})"

STAGE=started
say "Starting Zimbra on the data partition"
su - zimbra -c "zmcontrol start" || die "zmcontrol start failed"
if ! wait_all_running; then
  dump_zimbra_status
  die "Zimbra did not reach all-services Running"
fi
dump_zimbra_status
ok "All services running"

STAGE='done'
echo
say "DONE — ${ZIMBRA_DIR} is on ${DATA_DISK} (UUID=${UUID})"
warn "Old tree kept at ${BACKUP_DIR} (same root filesystem, rename only)."
warn "Do NOT delete it until mail has been healthy AND you are ready for Build HA pair."
info "fstab backup: ${FSTAB_BAK}"
info "When Pacemaker mounts /dev/drbd0, comment/remove the UUID=${UUID} line in ${FSTAB}"
info "  (otherwise boot and kin-fs will fight over ${ZIMBRA_DIR})."
info "Build HA pair create-md may ask about the existing ext4 signature — that is expected"
info "  with external meta on ${META_DISK}; unmount ${ZIMBRA_DIR} first if DRBD refuses a busy disk."
exit 0
