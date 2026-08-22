#!/usr/bin/env bash
# LUKS prepare-action + keyfile mode (no cryptsetup, no live disks).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

export KIN_LUKS_SOURCE_ONLY=1
# shellcheck source=zimbra-data-disk-luks.sh
. ./zimbra-data-disk-luks.sh
# shellcheck source=zimbra-data-disk-probe.sh
. ./zimbra-data-disk-probe.sh

act=$(luks_prepare_action "1" "")
if [ "$act" = "format_luks" ]; then
  pass "empty + encrypt: format_luks"
else
  bad "empty encrypt: [$act]"
fi

act=$(luks_prepare_action "1" "crypto_LUKS")
if [ "$act" = "open_luks" ]; then
  pass "already LUKS + encrypt: open_luks (never reformat)"
else
  bad "already LUKS encrypt: [$act]"
fi

act=$(luks_prepare_action "0" "crypto_LUKS")
if [ "$act" = "open_luks" ]; then
  pass "already LUKS + encrypt off: still open (do not destroy)"
else
  bad "already LUKS encrypt off: [$act]"
fi

act=$(luks_prepare_action "1" "ext4")
if [ "$act" = "skip_plain_existing" ]; then
  pass "existing ext4 + encrypt: skip_plain_existing (no live-pair retrofit)"
else
  bad "ext4 encrypt: [$act]"
fi

act=$(luks_prepare_action "0" "")
if [ "$act" = "mkfs_plain" ]; then
  pass "empty + encrypt off: mkfs_plain"
else
  bad "empty encrypt off: [$act]"
fi

act=$(luks_prepare_action "0" "ext4")
if [ "$act" = "skip_plain_existing" ]; then
  pass "ext4 + encrypt off: skip_plain_existing"
else
  bad "ext4 encrypt off: [$act]"
fi

act=$(luks_prepare_action "1" "xfs")
if [ "$act" = "die_unknown" ]; then
  pass "unknown fstype dies"
else
  bad "unknown fstype: [$act]"
fi

if [ "$(luks_format_blocked_reason /dev/sdb1 crypto_LUKS)" = "already_crypto_LUKS" ]; then
  pass "luks_format_blocked_reason: crypto_LUKS"
else
  bad "luks_format_blocked_reason crypto_LUKS"
fi
if luks_format_blocked_reason /dev/sdb1 ext4; then
  bad "luks_format_blocked_reason blocked ext4 without mapper"
else
  pass "luks_format_blocked_reason: ext4 not blocked"
fi
if grep -q 'cryptsetup isLuks' ./zimbra-data-disk-luks.sh \
  && grep -q 'mapper_already_open' ./zimbra-data-disk-luks.sh; then
  pass "luksFormat extra gates: isLuks + open mapper"
else
  bad "luksFormat extra gates missing"
fi

if data_disk_is_luks "/dev/sdb1" "crypto_LUKS"; then
  pass "data_disk_is_luks: crypto_LUKS"
else
  bad "data_disk_is_luks missed crypto_LUKS"
fi
if ! data_disk_is_luks "/dev/sdb1" "ext4"; then
  pass "data_disk_is_luks: ext4 is false"
else
  bad "data_disk_is_luks true on ext4"
fi

if same_as_data_backing "/dev/sdb1" "/dev/sdb1"; then
  pass "same_as_data_backing: raw partition"
else
  bad "same_as_data_backing raw"
fi
if same_as_data_backing "/dev/mapper/kin-zimbra-crypt" "/dev/sdb1"; then
  pass "same_as_data_backing: KIN mapper"
else
  bad "same_as_data_backing mapper"
fi
if same_as_data_backing $'/dev/mapper/kin-zimbra-crypt\n/dev/mapper/kin-zimbra-crypt' "/dev/sdb1"; then
  pass "same_as_data_backing: duplicate findmnt lines"
else
  bad "same_as_data_backing duplicate findmnt lines"
fi
if same_as_data_backing "/dev/mapper/kin-zimbra-crypt[/]" "/dev/sdb1"; then
  pass "same_as_data_backing: findmnt mapper[/] suffix"
else
  bad "same_as_data_backing mapper suffix"
fi
if ! same_as_data_backing "/dev/sda1" "/dev/sdb1"; then
  pass "same_as_data_backing: other disk is false"
else
  bad "same_as_data_backing accepted sda1"
fi

tmp=$(mktemp -d)
export KIN_LUKS_KEYFILE="$tmp/luks-keyfile"
if ensure_luks_keyfile \
  && [ -f "$KIN_LUKS_KEYFILE" ] \
  && [ "$(stat -c '%a' "$KIN_LUKS_KEYFILE" 2>/dev/null || stat -f '%OLp' "$KIN_LUKS_KEYFILE")" = "400" ]; then
  pass "ensure_luks_keyfile: creates mode 0400"
else
  bad "ensure_luks_keyfile mode: $(stat -c '%a' "$KIN_LUKS_KEYFILE" 2>/dev/null || true)"
fi
dirmode=$(stat -c '%a' "$tmp" 2>/dev/null || stat -f '%OLp' "$tmp")
if [ "$dirmode" = "755" ]; then
  pass "ensure_luks_keyfile: parent dir mode 0755 (console can traverse)"
else
  bad "ensure_luks_keyfile parent dir mode: [$dirmode] (need 755)"
fi
# Retry must not replace an existing keyfile.
sum1=$(cksum "$KIN_LUKS_KEYFILE" | awk '{print $1}')
ensure_luks_keyfile
sum2=$(cksum "$KIN_LUKS_KEYFILE" | awk '{print $1}')
if [ "$sum1" = "$sum2" ]; then
  pass "ensure_luks_keyfile: retry keeps the same file"
else
  bad "ensure_luks_keyfile rewrote an existing keyfile"
fi
rm -rf "$tmp"

# DRBD holder behind LUKS (sdb1 -> dm-2 -> drbd0).
sysfs=$(mktemp -d)
mkdir -p "$sysfs/class/block/sdb1/holders/dm-2"
mkdir -p "$sysfs/class/block/dm-2/holders/drbd0"
if data_disk_held_by_drbd "/dev/sdb1" "$sysfs"; then
  pass "data_disk_held_by_drbd: walks dm-crypt holder to drbd0"
else
  bad "data_disk_held_by_drbd missed LUKS-under-DRBD stub"
fi
rm -rf "$sysfs"

if grep -q 'luks_prepare_action' ./prepare-zimbra-data-disk.sh \
  && grep -q 'zimbra-data-disk-luks.sh' ./prepare-zimbra-data-disk.sh; then
  pass "prepare-zimbra-data-disk.sh sources LUKS helper and uses luks_prepare_action"
else
  bad "prepare-zimbra-data-disk.sh missing LUKS helper wiring"
fi

if grep -q 'same_as_data_backing\|luks_mapper_path' ./release-zimbra-plain-mount-for-drbd.sh; then
  pass "release script treats the LUKS mapper as the data backing"
else
  bad "release script missing mapper backing check"
fi

res_j2="../../ansible/roles/drbd_resource/templates/kin-zimbra.res.j2"
luks_yml="../../ansible/roles/drbd_resource/tasks/luks.yml"
dropin="../../ansible/roles/pacemaker_agents/templates/kin-luks-cryptsetup.conf.j2"
if [ -f "$res_j2" ] && grep -q 'drbd_resource_backing_disk' "$res_j2" \
  && [ -f "$luks_yml" ] \
  && grep -q 'zimbra-data-disk-luks.sh' "$luks_yml" \
  && [ -f "$dropin" ] \
  && grep -q 'After=cryptsetup.target' "$dropin"; then
  pass "ansible DRBD resource uses mapper backing + pacemaker waits for cryptsetup"
else
  bad "ansible LUKS wiring missing (res template, luks.yml, or pacemaker drop-in)"
fi

if [ "$fails" -ne 0 ]; then
  printf 'FAILED %s checks\n' "$fails"
  exit 1
fi
printf 'All zimbra-data-disk-luks helper tests passed\n'
exit 0
