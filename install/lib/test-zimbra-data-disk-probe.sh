#!/usr/bin/env bash
# Decision helpers for zimbra-data-disk-probe.sh (no live disks).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# shellcheck source=zimbra-data-disk-probe.sh
. ./zimbra-data-disk-probe.sh

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/zimbra/bin" "$tmp/empty"
: >"$tmp/zimbra/bin/zmcontrol"
chmod +x "$tmp/zimbra/bin/zmcontrol"
echo x >"$tmp/empty/orphan"

if mount_has_zmcontrol "$tmp/zimbra"; then
  pass "mount_has_zmcontrol: real tree"
else
  bad "mount_has_zmcontrol missed bin/zmcontrol"
fi
if ! mount_has_zmcontrol "$tmp/empty"; then
  pass "mount_has_zmcontrol: leftover-only tree is false"
else
  bad "mount_has_zmcontrol true on leftover-only tree"
fi

# Stub sysfs: DRBD holder on sdb1 (Secondary / attached steady state).
sysfs=$(mktemp -d)
mkdir -p "$sysfs/class/block/sdb1/holders/drbd0"
export KIN_SYSFS_ROOT="$sysfs"
if data_disk_held_by_drbd "/dev/sdb1"; then
  pass "data_disk_held_by_drbd: true when holders/drbd0 exists"
else
  bad "data_disk_held_by_drbd missed stub holder"
fi
if zimbra_data_disk_has_real_install "/dev/sdb1"; then
  pass "has_real_install: DRBD holder alone proves real install (no mount)"
else
  bad "has_real_install false despite DRBD holder"
fi
act=$(zimbra_install_skip_action "0" "1" "1")
if [ "$act" = "skip_drbd_secondary" ]; then
  pass "skip_action: DRBD-held Secondary is skip_drbd_secondary"
else
  bad "skip_action drbd_secondary: [$act]"
fi
unset KIN_SYSFS_ROOT
rm -rf "$sysfs"

sysfs_empty=$(mktemp -d)
mkdir -p "$sysfs_empty/class/block/sdb1/holders"
export KIN_SYSFS_ROOT="$sysfs_empty"
if ! data_disk_held_by_drbd "/dev/sdb1"; then
  pass "data_disk_held_by_drbd: false without holders (mid-handoff / fresh)"
else
  bad "data_disk_held_by_drbd true with empty holders"
fi
unset KIN_SYSFS_ROOT
rm -rf "$sysfs_empty"

sysfs_luks=$(mktemp -d)
mkdir -p "$sysfs_luks/class/block/sdb1/holders/dm-2"
mkdir -p "$sysfs_luks/class/block/dm-2/holders/drbd0"
export KIN_SYSFS_ROOT="$sysfs_luks"
if data_disk_held_by_drbd "/dev/sdb1"; then
  pass "data_disk_held_by_drbd: true when LUKS dm holder has drbd0"
else
  bad "data_disk_held_by_drbd missed dm-crypt -> drbd0"
fi
unset KIN_SYSFS_ROOT
rm -rf "$sysfs_luks"

act=$(zimbra_install_skip_action "1" "0")
if [ "$act" = "skip_healthy" ]; then
  pass "skip_action: already healthy"
else
  bad "skip_action healthy: [$act]"
fi

act=$(zimbra_install_skip_action "0" "1" "0")
if [ "$act" = "skip_mid_handoff" ]; then
  pass "skip_action: mid-handoff with real data-disk install"
else
  bad "skip_action mid-handoff: [$act]"
fi

act=$(zimbra_install_skip_action "0" "0")
if [ "$act" = "fail_broken" ]; then
  pass "skip_action: unknown local tree (no 4th arg) still fails closed"
else
  bad "skip_action fail_broken: [$act]"
fi

act=$(zimbra_install_skip_action "0" "0" "0" "0")
if [ "$act" = "run_fresh" ]; then
  pass "skip_action: empty /opt/zimbra mount after 02 runs the installer"
else
  bad "skip_action run_fresh: [$act]"
fi

act=$(zimbra_install_skip_action "0" "0" "0" "1")
if [ "$act" = "fail_broken" ]; then
  pass "skip_action: local zmcontrol not healthy is fail_broken"
else
  bad "skip_action local broken: [$act]"
fi

act=$(zimbra_install_skip_action "0" "1" "0" "0")
if [ "$act" = "skip_mid_handoff" ]; then
  pass "skip_action: empty mount + real data-disk install is still mid-handoff"
else
  bad "skip_action mid with local_zm=0: [$act]"
fi

act=$(zimbra_fail_broken_recover_action "1" "0")
if [ "$act" = "wipe_and_run" ]; then
  pass "recover: console first-deploy may wipe incomplete tree"
else
  bad "recover console: [$act]"
fi

act=$(zimbra_fail_broken_recover_action "1" "1")
if [ "$act" = "refuse" ]; then
  pass "recover: setup-complete refuses wipe"
else
  bad "recover after complete: [$act]"
fi

act=$(zimbra_fail_broken_recover_action "0" "0")
if [ "$act" = "refuse" ]; then
  pass "recover: CLI without console confirm refuses wipe"
else
  bad "recover CLI: [$act]"
fi

wipe_root=$(mktemp -d)
mkdir -p "$wipe_root/bin" "$wipe_root/lost+found"
echo stub >"$wipe_root/bin/zmcontrol"
echo keep >"$wipe_root/lost+found/x"
if wipe_incomplete_zimbra_tree "$wipe_root" \
  && [ ! -e "$wipe_root/bin" ] \
  && [ -f "$wipe_root/lost+found/x" ]; then
  pass "wipe_incomplete_zimbra_tree: removes tree, keeps lost+found"
else
  bad "wipe_incomplete_zimbra_tree did not clear stub tree"
fi
rm -rf "$wipe_root"
if ! wipe_incomplete_zimbra_tree /opt; then
  pass "wipe_incomplete_zimbra_tree: refuses /opt"
else
  bad "wipe_incomplete_zimbra_tree allowed /opt"
fi

# Prefer healthy over mid-handoff when both signals are true.
act=$(zimbra_install_skip_action "1" "1" "1")
if [ "$act" = "skip_healthy" ]; then
  pass "skip_action: healthy wins when both signals set"
else
  bad "skip_action both: [$act]"
fi

act=$(zimbra_install_verify_action "mid_handoff")
if [ "$act" = "skip_offline" ]; then
  pass "verify_action: mid-handoff skips live zmcontrol"
else
  bad "verify_action mid-handoff: [$act]"
fi

act=$(zimbra_install_verify_action "drbd_secondary")
if [ "$act" = "skip_offline" ]; then
  pass "verify_action: DRBD Secondary skips live zmcontrol"
else
  bad "verify_action drbd_secondary: [$act]"
fi

act=$(zimbra_install_verify_action "healthy")
if [ "$act" = "verify_live" ]; then
  pass "verify_action: healthy still verifies live"
else
  bad "verify_action healthy: [$act]"
fi

act=$(zimbra_install_verify_action "none")
if [ "$act" = "verify_live" ]; then
  pass "verify_action: fresh install still verifies live"
else
  bad "verify_action none: [$act]"
fi

act=$(full_install_zimbra_stage_action "1")
if [ "$act" = "skip_mid_handoff" ]; then
  pass "full_install stage: mid-handoff skips zimbra stages"
else
  bad "full_install stage mid: [$act]"
fi

act=$(full_install_zimbra_stage_action "0")
if [ "$act" = "run" ]; then
  pass "full_install stage: normal path still runs"
else
  bad "full_install stage run: [$act]"
fi

act=$(full_install_hardening_mode "1" "1")
if [ "$act" = "os_only" ]; then
  pass "hardening_mode: mid-handoff forces os_only even if -d exists"
else
  bad "hardening_mode mid: [$act]"
fi

act=$(full_install_hardening_mode "0" "0")
if [ "$act" = "os_only" ]; then
  pass "hardening_mode: missing dir still os_only"
else
  bad "hardening_mode missing: [$act]"
fi

act=$(full_install_hardening_mode "0" "1")
if [ "$act" = "full" ]; then
  pass "hardening_mode: live tree still full"
else
  bad "hardening_mode full: [$act]"
fi

# zimbra_is_mid_handoff: live zmcontrol means not mid-handoff
export KIN_ZIMBRA_DIR="$tmp/zimbra"
export KIN_DRBD_DATA_DISK="/dev/null"
if ! zimbra_is_mid_handoff; then
  pass "zimbra_is_mid_handoff: false when bin/zmcontrol on tree"
else
  bad "zimbra_is_mid_handoff true despite live zmcontrol"
fi

# Empty mountpoint + non-block disk -> not mid-handoff (no false positive)
KIN_ZIMBRA_DIR="$tmp/empty"
if ! zimbra_is_mid_handoff; then
  pass "zimbra_is_mid_handoff: false without data-disk install"
else
  bad "zimbra_is_mid_handoff true without data disk"
fi

# Empty mountpoint + DRBD holder -> treated like mid-handoff for stage skips
sysfs2=$(mktemp -d)
mkdir -p "$sysfs2/class/block/sdb1/holders/drbd0"
export KIN_SYSFS_ROOT="$sysfs2"
KIN_DRBD_DATA_DISK="/dev/sdb1"
KIN_ZIMBRA_DIR="$tmp/empty"
if zimbra_is_mid_handoff; then
  pass "zimbra_is_mid_handoff: true for DRBD Secondary (empty /opt/zimbra)"
else
  bad "zimbra_is_mid_handoff false for DRBD Secondary"
fi
unset KIN_SYSFS_ROOT
rm -rf "$sysfs2"

if grep -q 'zimbra_install_skip_action' ../03-install-zimbra.sh \
  && grep -q 'zimbra-data-disk-probe.sh' ../03-install-zimbra.sh \
  && grep -q 'skip_drbd_secondary' ../03-install-zimbra.sh \
  && grep -q 'run_fresh' ../03-install-zimbra.sh \
  && grep -q 'wipe_incomplete_zimbra_tree' ../03-install-zimbra.sh \
  && grep -q 'skip_offline' ../03-install-zimbra.sh \
  && grep -q 'SKIP_REASON' ../03-install-zimbra.sh; then
  pass "03-install-zimbra.sh uses shared probe, Secondary skip, run_fresh, and offline verify"
else
  bad "03-install-zimbra.sh missing shared probe / Secondary / run_fresh / verify wiring"
fi

if grep -q 'zimbra-data-disk-probe.sh' ./prepare-zimbra-data-disk.sh \
  && ! grep -q '^data_disk_held_by_drbd()' ./prepare-zimbra-data-disk.sh; then
  pass "prepare-zimbra-data-disk.sh uses shared data_disk_held_by_drbd (no local copy)"
else
  bad "prepare-zimbra-data-disk.sh still defines a local data_disk_held_by_drbd"
fi

if grep -q 'data_disk_held_by_drbd' ./migrate-zimbra-to-drbd-disk.sh \
  && grep -q 'zimbra-data-disk-probe.sh' ./migrate-zimbra-to-drbd-disk.sh \
  && grep -q 'already held by a drbd' ./migrate-zimbra-to-drbd-disk.sh; then
  pass "migrate-zimbra-to-drbd-disk.sh fail-closes on DRBD-held disk"
else
  bad "migrate-zimbra-to-drbd-disk.sh missing DRBD-held fail-closed guard"
fi

if grep -q 'full_install_zimbra_stage_action' ../kin-mail.sh \
  && grep -q 'zimbra_is_mid_handoff' ../kin-mail.sh \
  && grep -q 'data_disk_held_by_drbd' ../kin-mail.sh \
  && grep -q 'full_install_hardening_mode' ../kin-mail.sh; then
  pass "kin-mail.sh driver uses mid-handoff/DRBD stage/hardening decisions"
else
  bad "kin-mail.sh missing mid-handoff driver wiring"
fi

for stage in 04-tls-dkim.sh 05-healthcheck.sh 06-hybrid-auth.sh 07-zpush.sh 09-hardening.sh 11-admin-path-lockdown.sh; do
  if grep -q 'zimbra_is_mid_handoff' "../${stage}"; then
    pass "${stage} has mid-handoff gate"
  else
    bad "${stage} missing mid-handoff gate"
  fi
done

if [ "$fails" -ne 0 ]; then
  printf 'FAILED %s checks\n' "$fails"
  exit 1
fi
printf 'All zimbra-data-disk-probe helper tests passed\n'
exit 0
