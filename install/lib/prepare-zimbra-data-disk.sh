#!/usr/bin/env bash
# =============================================================================
# KIN Mail: mount a unique spare disk at /opt/zimbra BEFORE Zimbra installs.
#
# Called from 02-prepare-os.sh. Uses select_drbd_disk.py install_prepare_plan
# (the same plan_auto_partition GPT as ansible drbd_disk_prep). Does not
# reimplement disk detection.
#
#   unique blank spare matching KIN_DRBD_DATA_DISK parent
#       -> GPT data + 256 MiB unformatted meta, mkfs.ext4 data, fstab UUID, mount
#   proven GPT already present
#       -> mkfs data if empty, fstab UUID, mount (never rewrite GPT)
#   no spare / two spares / unique spare is not the expected parent
#       -> exit 0, Zimbra installs on the OS volume (today's behaviour)
#
# After GPT/mkfs/fstab/mount has started, errors fail the install (exit 1).
# Never mkfs the meta partition. Never mount over an existing Zimbra tree.
#
#   KIN_PREPARE_ZIMBRA_DATA_DISK=0  force OS-volume install (no disk work)
#   KIN_DRBD_DATA_DISK / KIN_DRBD_META_DISK  same defaults as disk_prep
# =============================================================================
set -u

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

ZIMBRA_DIR="/opt/zimbra"
FSTAB="${KIN_PREPARE_FSTAB:-/etc/fstab}"
FSTAB_MARK="# KIN Mail zimbra data partition (pre-cluster)"
DATA_DISK="${KIN_DRBD_DATA_DISK:-/dev/sdb1}"
META_DISK="${KIN_DRBD_META_DISK:-/dev/sdb2}"

norm_src() {
  local s="$1"
  s="${s%%[*}"
  s="${s%% *}"
  printf '%s' "$s"
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

# Shared mount_has_zmcontrol / zimbra_data_disk_has_real_install.
# shellcheck source=zimbra-data-disk-probe.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/zimbra-data-disk-probe.sh"

# Decide what to do for an already-ext4 data disk.
# Args: leftover_name (may be empty), has_zmcontrol 0|1, current_mountpoint
# Prints: accept_zimbra | die_leftover | continue
ext4_unmounted_action() {
  local leftover="$1"
  local has_zm="$2"
  local data_mp="$3"
  if [ "$has_zm" = "1" ]; then
    printf '%s\n' "accept_zimbra"
    return 0
  fi
  if [ -n "$leftover" ] && [ "$data_mp" != "$ZIMBRA_DIR" ]; then
    printf '%s\n' "die_leftover"
    return 0
  fi
  printf '%s\n' "continue"
}

find_selector() {
  if [ -n "${KIN_SELECT_DRBD_DISK:-}" ] && [ -f "${KIN_SELECT_DRBD_DISK}" ]; then
    printf '%s\n' "${KIN_SELECT_DRBD_DISK}"
    return 0
  fi
  local here deploy
  here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  deploy="${KIN_MAIL_DEPLOY_DIR:-/opt/kin-mail-deploy}"
  local c
  for c in \
    "${here}/../../ansible/roles/drbd_disk_prep/files/select_drbd_disk.py" \
    "${deploy}/ansible/roles/drbd_disk_prep/files/select_drbd_disk.py" \
    "/opt/kin-mail-console/ansible/roles/drbd_disk_prep/files/select_drbd_disk.py"
  do
    if [ -f "$c" ]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

json_get() {
  local key="$1"
  python3 -c 'import json,sys; d=json.load(sys.stdin); k=sys.argv[1]
print("" if d.get(k) is None else d.get(k) if not isinstance(d.get(k), bool) else ("true" if d.get(k) else "false"))' "$key"
}

json_plan_argv0() {
  python3 -c 'import json,sys
d=json.load(sys.stdin)
argv=(d.get("parted_argv") or d.get("plan",{}).get("parted_argv") or [])
print(argv[0] if argv else "")'
}

fstab_has_uuid() {
  local uuid="$1"
  grep -q "UUID=${uuid} ${ZIMBRA_DIR} " "$FSTAB" 2>/dev/null
}

fstab_has_kin_mark() {
  grep -Fq "$FSTAB_MARK" "$FSTAB" 2>/dev/null
}

if [ "${KIN_PREPARE_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

fallback_os() {
  warn "$1"
  info "Zimbra will install on the OS volume (same as previous installs)."
  info "A later unique spare disk can still be GPT-partitioned by Build HA pair."
  exit 0
}

die() {
  fail "$1"
  info "Refusing to continue full-install after a data-disk mutation failed."
  exit 1
}

say "Optional Zimbra data disk (before 03-install-zimbra.sh)"

if [ "${KIN_PREPARE_ZIMBRA_DATA_DISK:-1}" = "0" ]; then
  fallback_os "KIN_PREPARE_ZIMBRA_DATA_DISK=0: skipping spare-disk prepare"
fi

if [ "$(id -u)" -ne 0 ]; then
  fail "Run as root"
  exit 1
fi

Z_MNTSRC=$(norm_src "$(findmnt -n -o SOURCE "$ZIMBRA_DIR" 2>/dev/null || true)")
if [ -x "${ZIMBRA_DIR}/bin/zmcontrol" ]; then
  if [ -n "$Z_MNTSRC" ] && [ "$Z_MNTSRC" = "$DATA_DISK" ]; then
    ok "${ZIMBRA_DIR} is already on ${DATA_DISK}"
    exit 0
  fi
  fallback_os "Zimbra is already installed under ${ZIMBRA_DIR}; not mounting a data disk over it (use install/lib/migrate-zimbra-to-drbd-disk.sh)"
fi

if [ -d "$ZIMBRA_DIR" ] && ! mountpoint -q "$ZIMBRA_DIR" 2>/dev/null; then
  leftover=$(first_leftover "$ZIMBRA_DIR" || true)
  if [ -n "$leftover" ]; then
    fallback_os "${ZIMBRA_DIR} already has files on the OS volume (e.g. ${leftover})"
  fi
fi

SELECTOR=$(find_selector || true)
if [ -z "$SELECTOR" ]; then
  fallback_os "select_drbd_disk.py not found next to this tree"
fi
if ! command -v python3 >/dev/null 2>&1; then
  fallback_os "python3 is not installed; cannot run the disk selector"
fi

for bin in lsblk findmnt blkid; do
  command -v "$bin" >/dev/null 2>&1 || fallback_os "Required command not found: ${bin}"
done

LSBLK_FILE=$(mktemp)
REQ_FILE=$(mktemp)
PLAN_FILE=$(mktemp)
cleanup_tmp() {
  rm -f "$LSBLK_FILE" "$REQ_FILE" "$PLAN_FILE"
}
trap cleanup_tmp EXIT

if ! lsblk -J -b -o NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,PKNAME,PTTYPE >"$LSBLK_FILE"; then
  fallback_os "lsblk JSON failed"
fi
ROOT_SRC=$(norm_src "$(findmnt -n -o SOURCE /)")
[ -n "$ROOT_SRC" ] || fallback_os "Could not read the backing device for /"

python3 -c '
import json, sys
lsblk = json.load(open(sys.argv[1], encoding="utf-8"))
json.dump({
    "mode": "install",
    "lsblk": lsblk,
    "root_source": sys.argv[2],
    "data_disk": sys.argv[3],
    "meta_disk": sys.argv[4],
}, sys.stdout)
' "$LSBLK_FILE" "$ROOT_SRC" "$DATA_DISK" "$META_DISK" >"$REQ_FILE" || fallback_os "Could not build selector request JSON"

if ! python3 "$SELECTOR" <"$REQ_FILE" >"$PLAN_FILE"; then
  fallback_os "disk selector exited non-zero"
fi

INSTALL_MODE=$(json_get install_mode <"$PLAN_FILE")
NEED_PART=$(json_get need_partition <"$PLAN_FILE")
REASON=$(json_get reason <"$PLAN_FILE")
PLAN_DATA=$(json_get data_disk <"$PLAN_FILE")
PLAN_META=$(json_get meta_disk <"$PLAN_FILE")
PLAN_DISK=$(json_get disk <"$PLAN_FILE")
[ -n "$PLAN_DATA" ] && DATA_DISK="$PLAN_DATA"
[ -n "$PLAN_META" ] && META_DISK="$PLAN_META"

if [ "$INSTALL_MODE" != "prepare_data" ]; then
  fallback_os "${REASON:-no unique spare disk}"
fi

info "$REASON"
info "data=${DATA_DISK} meta=${META_DISK} disk=${PLAN_DISK:-none}"

if [ "$NEED_PART" = "true" ]; then
  command -v parted >/dev/null 2>&1 || die "parted is not installed"
  command -v wipefs >/dev/null 2>&1 || die "wipefs is not installed"
  [ -n "$PLAN_DISK" ] || die "selector returned partition with an empty disk path"
  argv0=$(json_plan_argv0 <"$PLAN_FILE")
  [ "$argv0" = "parted" ] || die "refusing to run a non-parted argv (got '${argv0}')"
  wipe_out=$(wipefs -n "$PLAN_DISK" 2>/dev/null || true)
  if [ -n "$(printf '%s' "$wipe_out" | tr -d '[:space:]')" ]; then
    die "wipefs still sees a signature on ${PLAN_DISK}: ${wipe_out}"
  fi
  say "GPT-partition ${PLAN_DISK} (data + ${META_DISK} meta, no mkfs on meta)"
  python3 -c '
import json, os, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
argv = d.get("parted_argv") or d.get("plan", {}).get("parted_argv") or []
disk = sys.argv[2]
if not argv or str(argv[0]) != "parted":
    sys.stderr.write("refusing a non-parted argv\n")
    sys.exit(1)
if disk not in [str(x) for x in argv]:
    sys.stderr.write("parted argv does not name the selected disk\n")
    sys.exit(1)
os.execvp(str(argv[0]), [str(x) for x in argv])
' "$PLAN_FILE" "$PLAN_DISK" || die "parted failed on ${PLAN_DISK}"
  partprobe "$PLAN_DISK" 2>/dev/null || true
  udevadm settle || true
  [ -b "$DATA_DISK" ] || die "data partition ${DATA_DISK} did not appear after parted"
  [ -b "$META_DISK" ] || die "meta partition ${META_DISK} did not appear after parted"
  ok "GPT layout ${DATA_DISK} + ${META_DISK}"
fi

[ -b "$DATA_DISK" ] || die "data partition ${DATA_DISK} is not a block device"
[ -b "$META_DISK" ] || die "meta partition ${META_DISK} is not a block device"

META_TYPE=$(blkid -s TYPE -o value "$META_DISK" 2>/dev/null || true)
if [ -n "$META_TYPE" ]; then
  die "meta partition ${META_DISK} has TYPE=${META_TYPE}; refusing to use it (meta must stay unformatted)"
fi

DATA_MP=$(lsblk -no MOUNTPOINT "$DATA_DISK" 2>/dev/null | head -1 | tr -d ' ')
if [ -n "$DATA_MP" ] && [ "$DATA_MP" != "$ZIMBRA_DIR" ]; then
  die "${DATA_DISK} is already mounted on ${DATA_MP}"
fi

FSTYPE=$(blkid -s TYPE -o value "$DATA_DISK" 2>/dev/null || true)
case "$FSTYPE" in
  "")
    command -v mkfs.ext4 >/dev/null 2>&1 || die "mkfs.ext4 not found (install e2fsprogs)"
    say "Creating ext4 on ${DATA_DISK}"
    mkfs.ext4 -F -L zimbra-data "$DATA_DISK" || die "mkfs.ext4 failed"
    udevadm settle || true
    FSTYPE=$(blkid -s TYPE -o value "$DATA_DISK" 2>/dev/null || true)
    [ "$FSTYPE" = "ext4" ] || die "mkfs reported success but TYPE is '${FSTYPE}'"
    ok "ext4 ready on ${DATA_DISK}"
    ;;
  ext4)
    leftover=""
    has_zmcontrol=0
    tmp=$(mktemp -d)
    if mount -o ro "$DATA_DISK" "$tmp" 2>/dev/null; then
      leftover=$(first_leftover "$tmp" || true)
      if mount_has_zmcontrol "$tmp"; then
        has_zmcontrol=1
      fi
      umount "$tmp" 2>/dev/null || true
    fi
    rmdir "$tmp" 2>/dev/null || true
    case "$(ext4_unmounted_action "$leftover" "$has_zmcontrol" "$DATA_MP")" in
      accept_zimbra)
        # Mid-handoff (or any prior install on the data partition): real Zimbra
        # tree is on ${DATA_DISK}, /opt/zimbra is just empty. Do not mkfs, do not
        # remount here - release-zimbra-plain-mount-for-drbd.sh and Pacemaker
        # kin-fs own that lifecycle during Build HA pair.
        ok "${DATA_DISK} already holds a Zimbra install (bin/zmcontrol); leaving unmounted for DRBD/Pacemaker"
        exit 0
        ;;
      die_leftover)
        die "${DATA_DISK} already has files (e.g. ${leftover}) and is not mounted at ${ZIMBRA_DIR}"
        ;;
    esac
    ok "${DATA_DISK} is already ext4"
    ;;
  *)
    die "${DATA_DISK} has TYPE=${FSTYPE}; refusing to reuse or reformat it"
    ;;
esac

UUID=$(blkid -s UUID -o value "$DATA_DISK" 2>/dev/null || true)
[ -n "$UUID" ] || die "No UUID for ${DATA_DISK}"

if fstab_has_kin_mark && ! fstab_has_uuid "$UUID"; then
  die "${FSTAB} already has a KIN Mail zimbra data entry for a different UUID"
fi
if ! fstab_has_uuid "$UUID"; then
  printf '\n%s\n# Remove this UUID line when Pacemaker kin-fs mounts /dev/drbd0 (Build HA pair).\nUUID=%s %s ext4 defaults 0 2\n' \
    "$FSTAB_MARK" "$UUID" "$ZIMBRA_DIR" >>"$FSTAB" \
    || die "Could not append ${FSTAB}"
  grep -Fq "UUID=${UUID} ${ZIMBRA_DIR} ext4 defaults 0 2" "$FSTAB" \
    || die "fstab write did not stick"
  ok "fstab UUID=${UUID} -> ${ZIMBRA_DIR}"
else
  ok "fstab already names UUID=${UUID} at ${ZIMBRA_DIR}"
fi

if mountpoint -q "$ZIMBRA_DIR" 2>/dev/null; then
  now=$(norm_src "$(findmnt -n -o SOURCE "$ZIMBRA_DIR")")
  uuid_now=$(findmnt -n -o UUID "$ZIMBRA_DIR" 2>/dev/null || true)
  if [ "$uuid_now" = "$UUID" ] || [ "$now" = "$DATA_DISK" ]; then
    ok "${ZIMBRA_DIR} already mounted from ${DATA_DISK}"
    exit 0
  fi
  die "${ZIMBRA_DIR} is mounted from ${now}, not ${DATA_DISK}"
fi

mkdir -p "$ZIMBRA_DIR" || die "mkdir ${ZIMBRA_DIR} failed"
if ! mount "$ZIMBRA_DIR"; then
  die "mount ${ZIMBRA_DIR} from fstab failed"
fi
now=$(norm_src "$(findmnt -n -o SOURCE "$ZIMBRA_DIR")")
uuid_now=$(findmnt -n -o UUID "$ZIMBRA_DIR" 2>/dev/null || true)
if [ "$uuid_now" != "$UUID" ] && [ "$now" != "$DATA_DISK" ]; then
  umount "$ZIMBRA_DIR" 2>/dev/null || true
  die "${ZIMBRA_DIR} mounted from ${now} (uuid=${uuid_now}), expected UUID=${UUID}"
fi
ok "Mounted ${ZIMBRA_DIR} from UUID=${UUID} (${DATA_DISK})"
info "Meta ${META_DISK} stays unformatted for a later Build HA pair."
info "When Pacemaker mounts /dev/drbd0, remove the UUID=${UUID} line from ${FSTAB}."
exit 0
