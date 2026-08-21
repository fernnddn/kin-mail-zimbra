#!/usr/bin/env bash
# =============================================================================
# Shared probe: does the DRBD data partition already hold a real Zimbra tree?
#
# Used when /opt/zimbra is an empty mountpoint mid-handoff (release-zimbra-
# plain-mount-for-drbd.sh unmounted it so drbdadm can attach). Callers must
# not leave this mount up or start Zimbra; DRBD/Pacemaker own that lifecycle.
#
#   KIN_DRBD_DATA_DISK   default /dev/sdb1
#   KIN_ZIMBRA_DIR       default /opt/zimbra (only for docs/consistency)
# =============================================================================

# Positive signal that a mounted tree is a real Zimbra install (same idea as
# ha_disk.py zimbra_tree_present).
mount_has_zmcontrol() {
  [ -x "${1}/bin/zmcontrol" ]
}

# Briefly mount the data partition read-only and look for bin/zmcontrol.
# Returns 0 if a real install is present. Never leaves the mount behind.
zimbra_data_disk_has_real_install() {
  local disk="${1:-${KIN_DRBD_DATA_DISK:-/dev/sdb1}}"
  local tmp rc=1
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

# Decide whether 03-install-zimbra.sh should skip the interactive installer
# when /opt/zimbra exists as a directory.
# Args: all_services_running 0|1, data_disk_has_real_install 0|1
# Prints: skip_healthy | skip_mid_handoff | fail_broken
zimbra_install_skip_action() {
  local healthy="$1"
  local on_data="$2"
  if [ "$healthy" = "1" ]; then
    printf '%s\n' "skip_healthy"
    return 0
  fi
  if [ "$on_data" = "1" ]; then
    printf '%s\n' "skip_mid_handoff"
    return 0
  fi
  printf '%s\n' "fail_broken"
}
