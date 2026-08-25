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
# Idempotent: when /opt/zimbra is unmounted or already on /dev/drbd*, skips
# stop/umount but still removes any stale pre-cluster fstab block (safe while
# mail is live on DRBD; fstab-only, no mount change).
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

# LUKS mapper is the DRBD/mkfs backing when encryption is on.
# shellcheck source=zimbra-data-disk-luks.sh
KIN_LUKS_SOURCE_ONLY=1
. "${HERE}/zimbra-data-disk-luks.sh"

DATA_DISK="${KIN_DRBD_DATA_DISK:-/dev/sdb1}"
ZIMBRA_DIR="${KIN_ZIMBRA_DIR:-/opt/zimbra}"
FSTAB="${KIN_RELEASE_FSTAB:-/etc/fstab}"
# Must match install/lib/prepare-zimbra-data-disk.sh
FSTAB_MARK="# KIN Mail zimbra data partition (pre-cluster)"
KIN_PRECLUSTER_FSTAB_BAK_PREFIX="${KIN_PRECLUSTER_FSTAB_BAK_PREFIX:-kin-pre-drbd-handoff}"
# Used by precluster-zimbra-fstab.sh (sourced below).
export FSTAB FSTAB_MARK ZIMBRA_DIR KIN_PRECLUSTER_FSTAB_BAK_PREFIX

# shellcheck source=precluster-zimbra-fstab.sh
. "${HERE}/precluster-zimbra-fstab.sh"

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
  if same_as_data_backing "$src" "$DATA_DISK"; then
    return 0
  fi
  data_uuid=$(blkid -s UUID -o value "$DATA_DISK" 2>/dev/null || true)
  mnt_uuid=$(findmnt -n -o UUID "$ZIMBRA_DIR" 2>/dev/null || true)
  [ -n "$data_uuid" ] && [ -n "$mnt_uuid" ] && [ "$data_uuid" = "$mnt_uuid" ] && return 0
  if [ -b "$(luks_mapper_path)" ]; then
    data_uuid=$(blkid -s UUID -o value "$(luks_mapper_path)" 2>/dev/null || true)
    [ -n "$data_uuid" ] && [ -n "$mnt_uuid" ] && [ "$data_uuid" = "$mnt_uuid" ] && return 0
  fi
  return 1
}

remove_precluster_fstab() {
  remove_precluster_zimbra_fstab || die_release "pre-cluster fstab cleanup failed"
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
  fuser_dev="$DATA_DISK"
  if [ "$SRC" = "$(luks_mapper_path)" ]; then
    # cryptsetup holds the raw LUKS partition; only the mapper is the FS.
    fuser_dev=$(luks_mapper_path)
  fi
  # A log-tailer (fail2ban, journald) or a udev probe can briefly reopen the
  # just-freed device right after umount and let go a moment later - retry
  # before treating this as a real stuck holder. Live 2vm practice run
  # (25 Aug 2026) hit exactly this: fuser -m reported busy, but the follow-up
  # verbose fuser -vm found nothing, meaning the holder was already gone.
  fuser_busy=1
  fuser_attempt=0
  fuser_retries="${KIN_RELEASE_FUSER_RETRIES:-5}"
  fuser_delay="${KIN_RELEASE_FUSER_DELAY:-1}"
  while [ "$fuser_attempt" -lt "$fuser_retries" ]; do
    if ! fuser -m "$fuser_dev" >/dev/null 2>&1; then
      fuser_busy=0
      break
    fi
    fuser_attempt=$((fuser_attempt + 1))
    sleep "$fuser_delay"
  done
  if [ "$fuser_busy" -eq 1 ]; then
    fuser -vm "$fuser_dev" 2>&1 | sed 's/^/    /' || true
    die_release "${fuser_dev} still has open holders after umount (checked ${fuser_attempt} times)"
  fi
fi

remove_precluster_fstab

SRC_AFTER=$(current_src)
if [ -n "$SRC_AFTER" ]; then
  die_release "${ZIMBRA_DIR} remounted from ${SRC_AFTER} during handoff"
fi

ok "Data partition ${DATA_DISK} is free for drbdadm up"
exit 0
