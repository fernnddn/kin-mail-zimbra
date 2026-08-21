#!/usr/bin/env bash
# =============================================================================
# KIN Mail: free the DRBD data partition for `drbdadm up` when /opt/zimbra is
# still mounted directly on that partition (pre-cluster fstab from
# prepare-zimbra-data-disk.sh).
#
# Called from ansible/roles/drbd_resource before activate.yml. Stops Zimbra
# cleanly, unmounts /opt/zimbra, and removes the pre-cluster fstab block so
# Pacemaker kin-fs can later mount /dev/drbd0 without fighting boot mounts.
#
# Mail is down from the stop until Pacemaker starts kin-zimbra again. That is
# intentional for Build HA pair.
#
# Idempotent: no-op when /opt/zimbra is unmounted or already on /dev/drbd*.
# Reuses wait_none_running / umount_retry from migrate-zimbra-to-drbd-disk.sh.
# =============================================================================
set -u

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MIGRATE="${KIN_MIGRATE_LIB:-${HERE}/migrate-zimbra-to-drbd-disk.sh}"

if [ ! -f "$MIGRATE" ]; then
  printf '%s\n' "release-zimbra-plain-mount-for-drbd: migrate helper not found at ${MIGRATE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
KIN_MIGRATE_SOURCE_ONLY=1
# Export before source so nested checks see it; migrate returns after helpers.
export KIN_MIGRATE_SOURCE_ONLY
# shellcheck source=migrate-zimbra-to-drbd-disk.sh
. "$MIGRATE"

DATA_DISK="${KIN_DRBD_DATA_DISK:-/dev/sdb1}"
ZIMBRA_DIR="${KIN_ZIMBRA_DIR:-/opt/zimbra}"
FSTAB="${KIN_RELEASE_FSTAB:-/etc/fstab}"
# Must match install/lib/prepare-zimbra-data-disk.sh
FSTAB_MARK="# KIN Mail zimbra data partition (pre-cluster)"

die_release() {
  fail "$*"
  exit 1
}

current_src() {
  norm_src "$(findmnt -n -o SOURCE "$ZIMBRA_DIR" 2>/dev/null || true)"
}

same_as_data_disk() {
  local src="$1"
  local data_uuid mnt_uuid
  [ -n "$src" ] || return 1
  [ "$src" = "$DATA_DISK" ] && return 0
  data_uuid=$(blkid -s UUID -o value "$DATA_DISK" 2>/dev/null || true)
  mnt_uuid=$(findmnt -n -o UUID "$ZIMBRA_DIR" 2>/dev/null || true)
  [ -n "$data_uuid" ] && [ -n "$mnt_uuid" ] && [ "$data_uuid" = "$mnt_uuid" ]
}

remove_precluster_fstab() {
  local ts bak
  if ! grep -Fq "$FSTAB_MARK" "$FSTAB" 2>/dev/null; then
    info "${FSTAB} has no ${FSTAB_MARK} block (already removed or never written)"
    return 0
  fi
  ts=$(date +%Y%m%dT%H%M%S)
  bak="${FSTAB}.kin-pre-drbd-handoff-${ts}"
  cp -a "$FSTAB" "$bak" || die_release "Could not backup ${FSTAB}"
  # Drop the mark line, the following Pacemaker reminder comment (if present),
  # and any /opt/zimbra UUID line that belongs to this pre-cluster mount.
  # index()==1 (starts-with), not ==, so an older mark line with extra
  # trailing text (migrate-zimbra-to-drbd-disk.sh's own historical
  # "... See HA-RUNBOOK §13." variant, already on disk on hosts migrated
  # before this marker text was standardized) still matches.
  awk -v mark="$FSTAB_MARK" -v zdir="$ZIMBRA_DIR" '
    index($0, mark) == 1 { skip = 1; next }
    skip && /^# Remove this UUID line when Pacemaker/ { next }
    skip && $0 ~ /^UUID=/ && index($0, zdir) { skip = 0; next }
    skip { skip = 0 }
    { print }
  ' "$bak" >"${FSTAB}.kin-new" || die_release "awk rewrite of ${FSTAB} failed"
  mv "${FSTAB}.kin-new" "$FSTAB" || die_release "Could not replace ${FSTAB}"
  if grep -Fq "$FSTAB_MARK" "$FSTAB" 2>/dev/null; then
    die_release "${FSTAB} still contains ${FSTAB_MARK} after rewrite (backup ${bak})"
  fi
  ok "Removed pre-cluster fstab block (backup ${bak})"
}

if [ "${KIN_RELEASE_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  die_release "Run as root"
fi

SRC=$(current_src)

if [ -z "$SRC" ]; then
  ok "${ZIMBRA_DIR} is not mounted; nothing to release for DRBD attach"
  remove_precluster_fstab
  exit 0
fi

case "$SRC" in
  /dev/drbd*|*/drbd/*)
    ok "${ZIMBRA_DIR} is already on DRBD (${SRC}); skipping stop/unmount"
    remove_precluster_fstab
    exit 0
    ;;
esac

if ! same_as_data_disk "$SRC"; then
  die_release \
    "${ZIMBRA_DIR} is mounted from ${SRC}, not ${DATA_DISK}. Refusing handoff (unexpected mount source)."
fi

say "Handoff: stop Zimbra and unmount ${ZIMBRA_DIR} from ${DATA_DISK}"
warn "Mail outage starts now on this node until Pacemaker mounts /dev/drbd0 and starts kin-zimbra."

if [ ! -x "${ZIMBRA_DIR}/bin/zmcontrol" ]; then
  die_release "zmcontrol not found under ${ZIMBRA_DIR} while it is still mounted from ${DATA_DISK}"
fi

su - zimbra -c "zmcontrol stop" || die_release "zmcontrol stop failed"
su - zimbra -c "zmconfigdctl stop" >/dev/null 2>&1 || true
if ! wait_none_running; then
  dump_zimbra_status
  dump_zimbra_lingering
  die_release "Zimbra still has Running services after stop"
fi
ok "Zimbra is stopped"

umount_retry "$ZIMBRA_DIR" || die_release "umount ${ZIMBRA_DIR} failed"
if findmnt -n "$ZIMBRA_DIR" >/dev/null 2>&1; then
  die_release "${ZIMBRA_DIR} is still mounted after umount"
fi
ok "Unmounted ${ZIMBRA_DIR}"

if command -v fuser >/dev/null 2>&1; then
  if fuser -m "$DATA_DISK" >/dev/null 2>&1; then
    fuser -vm "$DATA_DISK" 2>&1 | sed 's/^/    /' || true
    die_release "${DATA_DISK} still has open holders after umount"
  fi
fi

remove_precluster_fstab

SRC_AFTER=$(current_src)
if [ -n "$SRC_AFTER" ]; then
  die_release "${ZIMBRA_DIR} remounted from ${SRC_AFTER} during handoff"
fi

ok "Data partition ${DATA_DISK} is free for drbdadm up"
exit 0
