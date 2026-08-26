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

# cryptsetup always holds the raw LUKS partition. Holder checks must target
# the mapper (the filesystem / DRBD backing), never DATA_DISK, when the mapper
# exists. On a retry the mount is already gone, so SRC cannot be used.
# Prefer KIN_DRBD_BACKING_DISK when Ansible already resolved the mapper path.
backing_fuser_dev() {
  local mapped attempt=0
  local retries="${KIN_RELEASE_BACKING_RETRIES:-5}"
  local delay="${KIN_RELEASE_BACKING_DELAY:-1}"

  # Never fall back to DATA_DISK once Ansible set KIN_DRBD_BACKING_DISK:
  # cryptsetup always holds the raw LUKS partition, so waiting there fails
  # closed with a misleading holders story under udev lag.
  if [ -n "${KIN_DRBD_BACKING_DISK:-}" ]; then
    while [ ! -b "${KIN_DRBD_BACKING_DISK}" ] && [ "$attempt" -lt "$retries" ]; do
      attempt=$((attempt + 1))
      sleep "$delay"
    done
    printf '%s\n' "${KIN_DRBD_BACKING_DISK}"
    return 0
  fi
  mapped=$(luks_mapper_path)
  if [ -b "$mapped" ]; then
    printf '%s\n' "$mapped"
    return 0
  fi
  printf '%s\n' "$DATA_DISK"
}

pacemaker_owns_drbd_clone() {
  local pcs_clone="${KIN_DRBD_PCS_CLONE:-kin-drbd-clone}"
  command -v pcs >/dev/null 2>&1 || return 1
  pcs resource config "$pcs_clone" >/dev/null 2>&1
}

drbd_resource_is_up() {
  local resource="${KIN_DRBD_RESOURCE_NAME:-kin-zimbra}"
  command -v drbdadm >/dev/null 2>&1 || return 1
  drbdadm status "$resource" >/dev/null 2>&1
}

# Unmounted resume: if Pacemaker owns the clone or this node already has the
# resource up, do not free-wait / tear down here. release runs before activate;
# tearing down a healthy Secondary then failing the peer leaves both nodes
# down. activate.yml early-down + wait+up (or pacemaker-owns skip) owns attach.
release_skip_free_device_wait() {
  if pacemaker_owns_drbd_clone; then
    ok "Pacemaker owns ${KIN_DRBD_PCS_CLONE:-kin-drbd-clone}; skip free-device wait"
    return 0
  fi
  if drbd_resource_is_up; then
    ok "DRBD ${KIN_DRBD_RESOURCE_NAME:-kin-zimbra} is already up; skip free-device wait (activate will re-attach)"
    return 0
  fi
  return 1
}

# ocf:kin:zimbra stop calls this before zmcontrol stop. The handoff must too:
# fail2ban jails zimbra-auth / zpush-auth tail ${ZIMBRA_DIR}/log/*, and those
# FDs keep the LUKS mapper busy after umount. Best-effort: missing helper must
# not abort the handoff.
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

# Resolve mapper symlink → /dev/dm-N. fuser/udisks often attach to the dm
# node while /dev/mapper/* looks quiet (live Host A, 26 Aug 2026 phase3).
backing_realpath() {
  local path="$1"
  readlink -f "$path" 2>/dev/null || printf '%s' "$path"
}

# PIDs with an open FD on this block device (maj:min). Catches holders that
# fuser misses (udisksd, udev workers, leftover java as root, etc.).
list_block_fd_holders() {
  local path="$1"
  python3 - "$path" <<'PY' 2>/dev/null || true
import os, stat, sys
path = sys.argv[1]
try:
    st = os.stat(path)
except OSError:
    sys.exit(0)
if not stat.S_ISBLK(st.st_mode):
    sys.exit(0)
maj, minu = os.major(st.st_rdev), os.minor(st.st_rdev)
me = os.getpid()
found = []
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    p = int(pid)
    if p in (1, me):
        continue
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        continue
    hit = False
    for fd in fds:
        fp = f"{fd_dir}/{fd}"
        try:
            target = os.readlink(fp)
        except OSError:
            continue
        if target == path or target.endswith("/" + os.path.basename(path)):
            hit = True
            break
        try:
            fst = os.stat(fp)
        except OSError:
            continue
        if stat.S_ISBLK(fst.st_mode) and os.major(fst.st_rdev) == maj and os.minor(fst.st_rdev) == minu:
            hit = True
            break
    if hit:
        cmd = ""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")[:120]
        except OSError:
            pass
        found.append(f"{pid}:{cmd}" if cmd else str(pid))
print("\n".join(found))
PY
}

# fuser -m alone is not enough after umount: kernel/journal holders and some
# userspace FDs can leave the mapper EBUSY for drbdadm while fuser -m is quiet
# (live Build HA, 26 Aug 2026: mail1 "Can not open backing device" on
# /dev/mapper/kin-zimbra-crypt after a "successful" fuser wait).
backing_device_busy_reason() {
  local fuser_dev="$1"
  local real holders open_count mounts path fd_holders

  if [ -z "$fuser_dev" ] || [ ! -e "$fuser_dev" ]; then
    printf '%s\n' "missing backing path ${fuser_dev:-"(empty)"}"
    return 0
  fi

  real=$(backing_realpath "$fuser_dev")

  mounts=$(findmnt -n -S "$fuser_dev" 2>/dev/null || true)
  if [ -z "$mounts" ] && [ "$real" != "$fuser_dev" ]; then
    mounts=$(findmnt -n -S "$real" 2>/dev/null || true)
  fi
  if [ -n "$mounts" ]; then
    printf '%s\n' "still mounted: ${mounts}"
    return 0
  fi

  if command -v fuser >/dev/null 2>&1; then
    for path in "$fuser_dev" "$real"; do
      [ -n "$path" ] || continue
      if fuser "$path" >/dev/null 2>&1; then
        printf '%s\n' "fuser holders on ${path}"
        return 0
      fi
      if fuser -m "$path" >/dev/null 2>&1; then
        printf '%s\n' "fuser -m holders on ${path}"
        return 0
      fi
    done
  fi

  fd_holders=$(list_block_fd_holders "$fuser_dev")
  if [ -z "$fd_holders" ] && [ "$real" != "$fuser_dev" ]; then
    fd_holders=$(list_block_fd_holders "$real")
  fi
  if [ -n "$fd_holders" ]; then
    printf '%s\n' "proc-fd holders: $(printf '%s' "$fd_holders" | tr '\n' ' ')"
    return 0
  fi

  if [ -e "$real" ]; then
    holders=$(ls -A "/sys/class/block/$(basename "$real")/holders" 2>/dev/null || true)
    if [ -n "$holders" ]; then
      printf '%s\n' "sysfs holders: ${holders}"
      return 0
    fi
  fi

  if command -v dmsetup >/dev/null 2>&1 && [[ "$fuser_dev" == /dev/mapper/* ]]; then
    open_count=$(dmsetup info -c --noheadings -o open -- "$(basename "$fuser_dev")" 2>/dev/null | tr -d ' ' || true)
    # Open count 0 = free. Non-numeric / empty = skip. Any positive = busy.
    if [ -n "$open_count" ] && [ "$open_count" -gt 0 ] 2>/dev/null; then
      printf '%s\n' "dmsetup open count=${open_count}"
      return 0
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    if ! python3 - "$fuser_dev" <<'PY'
import errno, os, sys
path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDWR)
    os.close(fd)
except OSError as exc:
    if exc.errno in (errno.EBUSY, errno.EAGAIN):
        sys.exit(1)
    # Other errors (EPERM, etc.) are not treated as "busy holders".
    sys.exit(0)
sys.exit(0)
PY
    then
      printf '%s\n' "open(O_RDWR) EBUSY on ${fuser_dev}"
      return 0
    fi
  fi

  return 1
}

# activate.yml reuses the holder wait without stop/umount (retry / Diskless
# leftover). Mail is already down at that point; dropping jails is idempotent.
drop_leftover_zimbra_procs() {
  # Best-effort: JVM/mailboxd under uid zimbra can hold mapper FDs after a
  # "Stopped" status or a prior botched handoff. Hard gate remains
  # wait_backing_device_free.
  pkill -u zimbra 2>/dev/null || true
  sleep "${KIN_RELEASE_PKILL_SLEEP:-1}"
}

# Best-effort: drop known re-openers that leave dmsetup open count=1 with no
# fuser line (live Host A mail.gits-it.site, 26 Aug 2026 phase3-1).
nudge_backing_holders() {
  local fuser_dev="$1"
  local real line pid
  real=$(backing_realpath "$fuser_dev")

  drop_zimbra_log_holders
  drop_leftover_zimbra_procs

  # udisksd / udev blkid probes reopen the just-unmounted LUKS mapper.
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet udisks2.service 2>/dev/null; then
      systemctl stop udisks2.service >/dev/null 2>&1 || true
    fi
  fi
  if command -v udevadm >/dev/null 2>&1; then
    udevadm control --stop-exec-queue >/dev/null 2>&1 || true
    udevadm settle --timeout=5 >/dev/null 2>&1 || true
  fi

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pid=${line%%:*}
    case "$pid" in
      ''|*[!0-9]*) continue ;;
    esac
    [ "$pid" -eq 1 ] && continue
    [ "$pid" -eq "$$" ] && continue
    kill -TERM "$pid" 2>/dev/null || true
  done <<EOF
$(list_block_fd_holders "$fuser_dev")
$( [ "$real" != "$fuser_dev" ] && list_block_fd_holders "$real" )
EOF
  sleep "${KIN_RELEASE_NUDGE_KILL_SLEEP:-1}"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pid=${line%%:*}
    case "$pid" in
      ''|*[!0-9]*) continue ;;
    esac
    [ "$pid" -eq 1 ] && continue
    [ "$pid" -eq "$$" ] && continue
    kill -KILL "$pid" 2>/dev/null || true
  done <<EOF
$(list_block_fd_holders "$fuser_dev")
$( [ "$real" != "$fuser_dev" ] && list_block_fd_holders "$real" )
EOF

  sync 2>/dev/null || true
  if command -v blockdev >/dev/null 2>&1; then
    blockdev --flushbufs "$fuser_dev" 2>/dev/null || true
    [ "$real" != "$fuser_dev" ] && blockdev --flushbufs "$real" 2>/dev/null || true
  fi
}

# release_plain_mount.yml runs before activate.yml's early drbdadm down.
# On Build HA resume after a peer already reached drbdadm up, /opt/zimbra is
# unmounted but drbd0 still holds the LUKS mapper; waiting first fails closed
# with "sysfs holders: drbd0" (live Build HA, 26 Aug 2026 phase2-3).
ensure_backing_not_held_by_drbd() {
  local resource="${KIN_DRBD_RESOURCE_NAME:-kin-zimbra}"
  local fuser_dev reason

  if [ "${KIN_RELEASE_SKIP_DRBD_DOWN:-0}" = "1" ]; then
    return 0
  fi

  fuser_dev=$(backing_fuser_dev)
  if pacemaker_owns_drbd_clone; then
    warn "Pacemaker owns ${KIN_DRBD_PCS_CLONE:-kin-drbd-clone}; not running drbdadm down"
    return 0
  fi

  if ! command -v drbdadm >/dev/null 2>&1; then
    return 0
  fi

  # status rc=0 => resource is up. Also force down when status is down but
  # sysfs still shows orphan drbd* holders after a brutal kill / module leftover.
  reason=""
  if ! drbdadm status "$resource" >/dev/null 2>&1; then
    reason=$(backing_device_busy_reason "$fuser_dev" 2>/dev/null || true)
    case "$reason" in
      *drbd*)
        say "Backing busy (${reason}) while DRBD status is down; forcing drbdadm down ${resource}"
        ;;
      *)
        return 0
        ;;
    esac
  else
    say "DRBD ${resource} still holds ${fuser_dev}; bringing it down before free-device wait"
  fi

  if ! drbdadm down "$resource"; then
    warn "drbdadm down ${resource} failed; free-device wait may still fail"
    return 0
  fi
  sleep "${KIN_RELEASE_DRBD_DOWN_SLEEP:-2}"
  return 0
}

wait_backing_device_free() {
  local fuser_dev="$1"
  local fuser_busy=1
  local fuser_attempt=0
  local fuser_retries="${KIN_RELEASE_FUSER_RETRIES:-90}"
  local fuser_delay="${KIN_RELEASE_FUSER_DELAY:-1}"
  local nudge_every="${KIN_RELEASE_NUDGE_EVERY:-5}"
  local reason=""
  local real=""

  if command -v udevadm >/dev/null 2>&1; then
    udevadm settle --timeout=10 >/dev/null 2>&1 || true
  fi
  sync 2>/dev/null || true

  [ -n "$fuser_dev" ] || return 0
  real=$(backing_realpath "$fuser_dev")

  # First nudge immediately after umount - udisks/udev race is most common.
  nudge_backing_holders "$fuser_dev"

  while [ "$fuser_attempt" -lt "$fuser_retries" ]; do
    if ! reason=$(backing_device_busy_reason "$fuser_dev"); then
      fuser_busy=0
      break
    fi
    fuser_attempt=$((fuser_attempt + 1))
    if [ $((fuser_attempt % nudge_every)) -eq 0 ]; then
      warn "backing still busy (${reason}); nudging holders (attempt ${fuser_attempt}/${fuser_retries})"
      nudge_backing_holders "$fuser_dev"
    fi
    sleep "$fuser_delay"
  done
  if command -v udevadm >/dev/null 2>&1; then
    udevadm control --start-exec-queue >/dev/null 2>&1 || true
  fi
  if [ "$fuser_busy" -eq 1 ]; then
    warn "backing still busy after ${fuser_attempt} checks: ${reason}"
    if command -v fuser >/dev/null 2>&1; then
      fuser -vm "$fuser_dev" 2>&1 | sed 's/^/    /' || true
      fuser -v "$fuser_dev" 2>&1 | sed 's/^/    /' || true
      if [ -n "$real" ] && [ "$real" != "$fuser_dev" ]; then
        fuser -vm "$real" 2>&1 | sed 's/^/    /' || true
        fuser -v "$real" 2>&1 | sed 's/^/    /' || true
      fi
    fi
    list_block_fd_holders "$fuser_dev" 2>/dev/null | sed 's/^/    proc-fd /' || true
    if [ -n "$real" ] && [ "$real" != "$fuser_dev" ]; then
      list_block_fd_holders "$real" 2>/dev/null | sed 's/^/    proc-fd /' || true
    fi
    findmnt -S "$fuser_dev" 2>&1 | sed 's/^/    /' || true
    lsblk -o NAME,TYPE,MOUNTPOINT,FSTYPE "$fuser_dev" 2>&1 | sed 's/^/    /' || true
    if command -v dmsetup >/dev/null 2>&1 && [[ "$fuser_dev" == /dev/mapper/* ]]; then
      dmsetup info "$(basename "$fuser_dev")" 2>&1 | sed 's/^/    /' || true
    fi
    fail "${fuser_dev} still has open holders (checked ${fuser_attempt} times)"
    return 1
  fi
  # Brief settle so udev/journal flush cannot race the immediate drbdadm up.
  sleep "${KIN_RELEASE_POST_FREE_SLEEP:-2}"
  ok "${fuser_dev} has no open holders"
  return 0
}

if [ "${KIN_RELEASE_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  die_release "Run as root"
fi

if [ "${KIN_RELEASE_WAIT_BACKING_ONLY:-0}" = "1" ]; then
  drop_zimbra_log_holders
  drop_leftover_zimbra_procs
  if pacemaker_owns_drbd_clone; then
    ok "Pacemaker owns ${KIN_DRBD_PCS_CLONE:-kin-drbd-clone}; wait-backing-only is a no-op"
    exit 0
  fi
  ensure_backing_not_held_by_drbd
  wait_backing_device_free "$(backing_fuser_dev)" \
    || die_release "backing device still has open holders; drbdadm up would fail"
  ok "Backing device is free for drbdadm up"
  exit 0
fi

SRC=$(current_src)

if [ -z "$SRC" ]; then
  ok "${ZIMBRA_DIR} is not mounted; skip stop/umount, still free the backing device"
  drop_zimbra_log_holders
  drop_leftover_zimbra_procs
  if release_skip_free_device_wait; then
    remove_precluster_fstab
    exit 0
  fi
  ensure_backing_not_held_by_drbd
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
# Leftover java/mailboxd under uid zimbra can hold mapper FDs after a "Stopped"
# status line. Best-effort; wait_backing_device_free is the hard gate.
drop_leftover_zimbra_procs
ok "Zimbra is stopped"

umount_retry "$ZIMBRA_DIR" || die_release "umount ${ZIMBRA_DIR} failed"
if findmnt -n "$ZIMBRA_DIR" >/dev/null 2>&1; then
  die_release "${ZIMBRA_DIR} is still mounted after umount"
fi
ok "Unmounted ${ZIMBRA_DIR}"

ensure_backing_not_held_by_drbd
wait_backing_device_free "$(backing_fuser_dev)" \
  || die_release "backing device still has open holders after umount"

remove_precluster_fstab

SRC_AFTER=$(current_src)
if [ -n "$SRC_AFTER" ]; then
  die_release "${ZIMBRA_DIR} remounted from ${SRC_AFTER} during handoff"
fi

ok "Data partition ${DATA_DISK} is free for drbdadm up"
exit 0
