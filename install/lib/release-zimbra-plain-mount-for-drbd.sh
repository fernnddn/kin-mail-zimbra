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
# Idempotent:
#   * Already on /dev/drbd*: fstab cleanup only. Do not stop mail, do not drop
#     fail2ban jails (mailbox.log is live), do not fuser the backing device
#     (DRBD holds it).
#   * Unmounted (retry after a failed first handoff): drop fail2ban zimbra/
#     zpush jails, wait until the LUKS mapper has no open holders, then remove
#     the pre-cluster fstab block. Skipping the holder wait is how a retry
#     would hit "Can not open backing device" again (live 2vm, 25 Aug 2026).
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

# cryptsetup always holds the raw LUKS partition. fuser -m must target the
# mapper (the filesystem / DRBD backing), never DATA_DISK, when the mapper
# exists. On a retry the mount is already gone, so SRC cannot be used.
backing_fuser_dev() {
  if [ -b "$(luks_mapper_path)" ]; then
    luks_mapper_path
    return 0
  fi
  printf '%s\n' "$DATA_DISK"
}

# ocf:kin:zimbra stop calls this before zmcontrol stop. The handoff must too:
# fail2ban jails zimbra-auth / zpush-auth tail ${ZIMBRA_DIR}/log/*, and those
# FDs keep the LUKS mapper busy after umount. 5x1s fuser retries cannot outwait
# a still-running jail. Best-effort: missing helper must not abort the handoff.
drop_zimbra_log_holders() {
  local helper=""
  if [ -n "${KIN_FAIL2BAN_JAILS:-}" ] && [ -x "${KIN_FAIL2BAN_JAILS}" ]; then
    helper="${KIN_FAIL2BAN_JAILS}"
  elif [ -x /usr/local/sbin/kin-fail2ban-jails ]; then
    helper=/usr/local/sbin/kin-fail2ban-jails
  elif [ -x "${HERE}/kin-fail2ban-jails.sh" ]; then
    helper="${HERE}/kin-fail2ban-jails.sh"
  fi
  if [ -z "$helper" ]; then
    warn "kin-fail2ban-jails not found; log-tailers may keep the backing device busy after umount"
    return 0
  fi
  say "Drop fail2ban zimbra/zpush jails so they cannot hold ${ZIMBRA_DIR}/log after umount"
  KIN_FAIL2BAN_JAILS_BEST_EFFORT=1 "$helper" unmounted || true
}

wait_backing_device_free() {
  local fuser_dev="$1"
  local fuser_busy=1
  local fuser_attempt=0
  local fuser_retries="${KIN_RELEASE_FUSER_RETRIES:-20}"
  local fuser_delay="${KIN_RELEASE_FUSER_DELAY:-1}"

  if command -v udevadm >/dev/null 2>&1; then
    udevadm settle --timeout=10 >/dev/null 2>&1 || true
  fi

  if ! command -v fuser >/dev/null 2>&1; then
    warn "fuser not installed; skipping open-holder check on ${fuser_dev}"
    return 0
  fi
  [ -n "$fuser_dev" ] || return 0

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
    fail "${fuser_dev} still has open holders (checked ${fuser_attempt} times)"
    return 1
  fi
  ok "${fuser_dev} has no open holders"
  return 0
}

if [ "${KIN_RELEASE_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  die_release "Run as root"
fi

# activate.yml reuses the holder wait without stop/umount (retry / Diskless
# leftover). Mail is already down at that point; dropping jails is idempotent.
if [ "${KIN_RELEASE_WAIT_BACKING_ONLY:-0}" = "1" ]; then
  drop_zimbra_log_holders
  wait_backing_device_free "$(backing_fuser_dev)" \
    || die_release "backing device still has open holders; drbdadm up would fail"
  ok "Backing device is free for drbdadm up"
  exit 0
fi

SRC=$(current_src)

if [ -z "$SRC" ]; then
  ok "${ZIMBRA_DIR} is not mounted; skip stop/umount, still free the backing device"
  drop_zimbra_log_holders
  wait_backing_device_free "$(backing_fuser_dev)" \
    || die_release "backing device still has open holders after a prior umount"
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

# Same order as ocf:kin:zimbra stop: drop log-tailers, then zmcontrol stop.
drop_zimbra_log_holders

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

wait_backing_device_free "$(backing_fuser_dev)" \
  || die_release "backing device still has open holders after umount"

remove_precluster_fstab

SRC_AFTER=$(current_src)
if [ -n "$SRC_AFTER" ]; then
  die_release "${ZIMBRA_DIR} remounted from ${SRC_AFTER} during handoff"
fi

ok "Data partition ${DATA_DISK} is free for drbdadm up"
exit 0
