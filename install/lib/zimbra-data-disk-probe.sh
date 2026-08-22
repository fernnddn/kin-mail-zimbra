#!/usr/bin/env bash
# =============================================================================
# Shared probe: does the DRBD data partition already hold a real Zimbra tree?
#
# Covers mid-handoff (release-zimbra-plain-mount-for-drbd.sh unmounted
# /opt/zimbra; data still readable on the raw disk) and steady-state Secondary
# (DATA_DISK is held by a drbd* kernel device; do not mount it directly).
# Callers must not leave a temporary mount up or start Zimbra; DRBD/Pacemaker
# own that lifecycle.
#
#   KIN_DRBD_DATA_DISK   default /dev/sdb1
#   KIN_ZIMBRA_DIR       default /opt/zimbra
#   KIN_SYSFS_ROOT       default /sys (tests inject a stub tree)
# =============================================================================

# Positive signal that a mounted tree is a real Zimbra install (same idea as
# ha_disk.py zimbra_tree_present).
mount_has_zmcontrol() {
  [ -x "${1}/bin/zmcontrol" ]
}

# Probe-only: walks holders and also dm-crypt (LUKS under DRBD).
# Optional 2nd arg: sysfs root (tests inject a stub tree).
data_disk_held_by_drbd() {
  local disk="$1"
  local sys_root="${2:-${KIN_SYSFS_ROOT:-/sys}}"
  local depth="${3:-0}"
  local base sysdev holder hbase
  [ "$depth" -lt 6 ] || return 1
  base=$(basename "$disk")
  [ -n "$base" ] || return 1
  sysdev=$(readlink -f "${sys_root}/class/block/${base}" 2>/dev/null || true)
  if [ -z "$sysdev" ] || [ ! -d "${sysdev}/holders" ]; then
    return 1
  fi
  for holder in "${sysdev}/holders"/*; do
    [ -e "$holder" ] || continue
    hbase=$(basename "$holder")
    case "$hbase" in
      drbd*) return 0 ;;
      dm-*)
        # LUKS sits under DRBD: sdb1 -> dm-crypt -> drbd0.
        if data_disk_held_by_drbd "/dev/${hbase}" "$sys_root" $((depth + 1)); then
          return 0
        fi
        ;;
    esac
  done
  return 1
}

# Real install on the data path: either DRBD already holds the disk (do not
# mount it), or a brief RO mount finds bin/zmcontrol (mid-handoff / plain).
# After 02, the filesystem is often on the LUKS mapper, not the raw partition;
# mounting crypto_LUKS as ext4 fails, so also try the mapper when it is a
# block device and not already busy (already mounted at /opt/zimbra).
# Optional 2nd arg: sysfs root for the holders check.
# Returns 0 if a real install is present. Never leaves a mount behind.
zimbra_data_disk_has_real_install() {
  local disk="${1:-${KIN_DRBD_DATA_DISK:-/dev/sdb1}}"
  local sys_root="${2:-${KIN_SYSFS_ROOT:-/sys}}"
  local tmp rc=1 candidate mapper
  if data_disk_held_by_drbd "$disk" "$sys_root"; then
    return 0
  fi
  mapper="${KIN_LUKS_MAPPER_PATH:-/dev/mapper/${KIN_LUKS_MAPPER_NAME:-kin-zimbra-crypt}}"
  for candidate in "$disk" "$mapper"; do
    [ -b "$candidate" ] || continue
    tmp=$(mktemp -d) || continue
    if mount -o ro "$candidate" "$tmp" 2>/dev/null; then
      if mount_has_zmcontrol "$tmp"; then
        rc=0
      fi
      umount "$tmp" 2>/dev/null || true
    fi
    rmdir "$tmp" 2>/dev/null || true
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
  done
  return 1
}

# True when /opt/zimbra has no runnable zmcontrol but the data disk already
# holds a real install (mid-handoff OR DRBD Secondary/attached).
zimbra_is_mid_handoff() {
  local disk="${1:-${KIN_DRBD_DATA_DISK:-/dev/sdb1}}"
  local zimbra_dir="${KIN_ZIMBRA_DIR:-/opt/zimbra}"
  if [ -x "${zimbra_dir}/bin/zmcontrol" ]; then
    return 1
  fi
  zimbra_data_disk_has_real_install "$disk"
}

# Decide whether 03-install-zimbra.sh should skip the interactive installer
# when /opt/zimbra exists as a directory.
# Args: all_services_running 0|1, data_disk_has_real_install 0|1,
#       data_disk_held_by_drbd 0|1 (optional; distinguishes Secondary messaging),
#       local_zmcontrol 0|1 (optional; 1 = /opt/zimbra/bin/zmcontrol is executable).
#       Omit 4th arg to keep the old fail-closed behaviour for unknown trees.
# Prints: skip_healthy | skip_drbd_secondary | skip_mid_handoff | run_fresh | fail_broken
zimbra_install_skip_action() {
  local healthy="$1"
  local on_data="$2"
  local drbd_held="${3:-0}"
  local local_zm="${4:-}"
  if [ "$healthy" = "1" ]; then
    printf '%s\n' "skip_healthy"
    return 0
  fi
  if [ "$drbd_held" = "1" ]; then
    printf '%s\n' "skip_drbd_secondary"
    return 0
  fi
  # A runnable local tree that is not healthy is a broken install, not mid-handoff.
  if [ "$local_zm" = "1" ]; then
    printf '%s\n' "fail_broken"
    return 0
  fi
  if [ "$on_data" = "1" ]; then
    printf '%s\n' "skip_mid_handoff"
    return 0
  fi
  # 02-prepare-os mounts the data disk at /opt/zimbra before Zimbra exists.
  # That empty mount (lost+found only) must run the installer, not fail closed.
  if [ "$local_zm" = "0" ]; then
    printf '%s\n' "run_fresh"
    return 0
  fi
  printf '%s\n' "fail_broken"
}

# Wizard retry of a failed first 03: wipe the incomplete tree and re-run install.sh.
# Never wipe after setup-complete (mail is in production) or without console confirm.
# Args: console_confirmed 0|1, setup_complete_present 0|1
# Prints: wipe_and_run | refuse
zimbra_fail_broken_recover_action() {
  local console="${1:-0}"
  local setup_done="${2:-0}"
  if [ "$console" = "1" ] && [ "$setup_done" != "1" ]; then
    printf '%s\n' "wipe_and_run"
    return 0
  fi
  printf '%s\n' "refuse"
}

# Remove children of a Zimbra mount except lost+found. Does not unmount.
# Refuses obviously dangerous paths. Used only after wipe_and_run.
wipe_incomplete_zimbra_tree() {
  local dir="${1:-}"
  local child
  case "$dir" in
    ""|"/"|"/opt"|"/usr"|"/var"|"/home"|"/etc"|"/root") return 1 ;;
  esac
  [ -d "$dir" ] || return 1
  for child in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    [ -e "$child" ] || [ -L "$child" ] || continue
    [ "$(basename "$child")" = "lost+found" ] && continue
    rm -rf -- "$child"
  done
  return 0
}

# Decide whether a post-03 stage that needs a live Zimbra tree should run.
# Args: mid_handoff 0|1 (also true for DRBD Secondary; same skip behaviour)
# Prints: skip_mid_handoff | run
full_install_zimbra_stage_action() {
  if [ "${1:-0}" = "1" ]; then
    printf '%s\n' "skip_mid_handoff"
    return 0
  fi
  printf '%s\n' "run"
}

# Decide 09-hardening mode for full-install.
# Args: mid_handoff 0|1, zimbra_dir_exists 0|1 (legacy [ -d /opt/zimbra ])
# Prints: full | os_only
full_install_hardening_mode() {
  local mid="${1:-0}"
  local dir_exists="${2:-0}"
  if [ "$mid" = "1" ]; then
    printf '%s\n' "os_only"
    return 0
  fi
  if [ "$dir_exists" = "0" ]; then
    printf '%s\n' "os_only"
    return 0
  fi
  printf '%s\n' "full"
}

# Decide 03 verification behaviour after the installer-skip decision.
# Args: skip_reason none|healthy|mid_handoff|drbd_secondary
# Prints: skip_offline | verify_live
zimbra_install_verify_action() {
  case "${1:-none}" in
    mid_handoff|drbd_secondary) printf '%s\n' "skip_offline" ;;
    *) printf '%s\n' "verify_live" ;;
  esac
}
