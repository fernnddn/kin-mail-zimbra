#!/usr/bin/env bash
# =============================================================================
# Shared: remove the pre-cluster /opt/zimbra fstab block.
#
# Used after DRBD owns the data disk (plain-mount release, prepare skip on
# attached Secondary, and any node whose fstab was never cleaned by peer_os_prep).
# Safe while mail is live on /dev/drbd*: this only edits fstab; it does not
# umount or stop Zimbra.
#
# Callers must set (or accept defaults for):
#   FSTAB, ZIMBRA_DIR, FSTAB_MARK
#   KIN_PRECLUSTER_FSTAB_BAK_PREFIX  backup filename stem (optional)
# And provide ok / info / fail helpers (install scripts already define these).
# =============================================================================

# Default mark must match prepare-zimbra-data-disk.sh and release-*.sh.
: "${FSTAB_MARK:=# KIN Mail zimbra data partition (pre-cluster)}"

remove_precluster_zimbra_fstab() {
  local ts bak mark fstab zdir bak_prefix
  mark="${FSTAB_MARK}"
  fstab="${FSTAB:-/etc/fstab}"
  zdir="${ZIMBRA_DIR:-/opt/zimbra}"
  bak_prefix="${KIN_PRECLUSTER_FSTAB_BAK_PREFIX:-kin-pre-drbd-fstab}"

  if ! grep -Fq "$mark" "$fstab" 2>/dev/null; then
    info "${fstab} has no ${mark} block (already removed or never written)"
    return 0
  fi
  ts=$(date +%Y%m%dT%H%M%S)
  bak="${fstab}.${bak_prefix}-${ts}"
  cp -a "$fstab" "$bak" || {
    fail "Could not backup ${fstab}"
    return 1
  }
  # index()==1 (starts-with), not ==, so an older mark line with extra
  # trailing text (migrate-zimbra-to-drbd-disk.sh's historical
  # "... See HA-RUNBOOK" variant) still matches.
  awk -v mark="$mark" -v zdir="$zdir" '
    index($0, mark) == 1 { skip = 1; next }
    skip && /^# Remove this UUID line when Pacemaker/ { next }
    skip && $0 ~ /^UUID=/ && index($0, zdir) { skip = 0; next }
    skip { skip = 0 }
    { print }
  ' "$bak" >"${fstab}.kin-new" || {
    fail "awk rewrite of ${fstab} failed"
    return 1
  }
  mv "${fstab}.kin-new" "$fstab" || {
    fail "Could not replace ${fstab}"
    return 1
  }
  if grep -Fq "$mark" "$fstab" 2>/dev/null; then
    fail "${fstab} still contains ${mark} after rewrite (backup ${bak})"
    return 1
  fi
  ok "Removed pre-cluster fstab block (backup ${bak})"
  return 0
}
