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

# True when the kernel already handed the disk to a drbd* holder
# (Secondary or Primary after drbdadm up). Mid-handoff has no holders yet.
# Optional 2nd arg: sysfs root (tests inject a stub tree).
data_disk_held_by_drbd() {
  local disk="$1"
  local sys_root="${2:-${KIN_SYSFS_ROOT:-/sys}}"
  local base sysdev holder
  base=$(basename "$disk")
  [ -n "$base" ] || return 1
  sysdev=$(readlink -f "${sys_root}/class/block/${base}" 2>/dev/null || true)
  if [ -z "$sysdev" ] || [ ! -d "${sysdev}/holders" ]; then
    return 1
  fi
  for holder in "${sysdev}/holders"/*; do
    [ -e "$holder" ] || continue
    case "$(basename "$holder")" in
      drbd*) return 0 ;;
    esac
  done
  return 1
}

# Real install on the data path: either DRBD already holds the disk (do not
# mount it), or a brief RO mount finds bin/zmcontrol (mid-handoff / plain).
# Optional 2nd arg: sysfs root for the holders check.
# Returns 0 if a real install is present. Never leaves a mount behind.
zimbra_data_disk_has_real_install() {
  local disk="${1:-${KIN_DRBD_DATA_DISK:-/dev/sdb1}}"
  local sys_root="${2:-${KIN_SYSFS_ROOT:-/sys}}"
  local tmp rc=1
  if data_disk_held_by_drbd "$disk" "$sys_root"; then
    return 0
  fi
  [ -b "$disk" ] || return 1
  tmp=$(mktemp -d) || return 1
  if mount -o ro "$disk" "$tmp" 2>/dev/null; then
    if mount_has_zmcontrol "$tmp"; then
      rc=0
    fi
    umount "$tmp" 2>/dev/null || true
  fi
  rmdir "$tmp" 2>/dev/null || true
  return "$rc"
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
#       data_disk_held_by_drbd 0|1 (optional; distinguishes Secondary messaging)
# Prints: skip_healthy | skip_drbd_secondary | skip_mid_handoff | fail_broken
zimbra_install_skip_action() {
  local healthy="$1"
  local on_data="$2"
  local drbd_held="${3:-0}"
  if [ "$healthy" = "1" ]; then
    printf '%s\n' "skip_healthy"
    return 0
  fi
  if [ "$on_data" = "1" ]; then
    if [ "$drbd_held" = "1" ]; then
      printf '%s\n' "skip_drbd_secondary"
      return 0
    fi
    printf '%s\n' "skip_mid_handoff"
    return 0
  fi
  printf '%s\n' "fail_broken"
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
