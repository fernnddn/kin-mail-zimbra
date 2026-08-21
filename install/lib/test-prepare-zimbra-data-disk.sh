#!/usr/bin/env bash
# Helpers for prepare-zimbra-data-disk.sh (no live disks, no parted).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

export KIN_PREPARE_SOURCE_ONLY=1
# shellcheck source=prepare-zimbra-data-disk.sh
. ./prepare-zimbra-data-disk.sh

sel=$(find_selector || true)
if [ -n "$sel" ] && [ -f "$sel" ]; then
  pass "find_selector locates select_drbd_disk.py"
else
  bad "find_selector: [$sel]"
fi

tmp=$(mktemp -d)
mkdir -p "$tmp/empty"
if leftover=$(first_leftover "$tmp/empty" || true); [ -z "$leftover" ]; then
  pass "first_leftover: empty dir"
else
  bad "first_leftover empty: [$leftover]"
fi
mkdir -p "$tmp/with/lost+found"
if leftover=$(first_leftover "$tmp/with" || true); [ -z "$leftover" ]; then
  pass "first_leftover: lost+found only"
else
  bad "first_leftover lost+found: [$leftover]"
fi
echo x >"$tmp/with/mailboxd"
got=$(first_leftover "$tmp/with" || true)
if [ "$got" = "mailboxd" ]; then
  pass "first_leftover: reports a real leftover"
else
  bad "first_leftover leftover: [$got]"
fi

# Mid-handoff: unmounted ext4 with a real Zimbra tree must not die_leftover.
mkdir -p "$tmp/zimbra/bin" "$tmp/zimbra/backup"
: >"$tmp/zimbra/bin/zmcontrol"
chmod +x "$tmp/zimbra/bin/zmcontrol"
if mount_has_zmcontrol "$tmp/zimbra"; then
  pass "mount_has_zmcontrol: real tree"
else
  bad "mount_has_zmcontrol missed bin/zmcontrol"
fi
if ! mount_has_zmcontrol "$tmp/with"; then
  pass "mount_has_zmcontrol: leftover-only tree is false"
else
  bad "mount_has_zmcontrol true on leftover-only tree"
fi

act=$(ext4_unmounted_action "backup" "1" "")
if [ "$act" = "accept_zimbra" ]; then
  pass "ext4_unmounted_action: zmcontrol present accepts mid-handoff"
else
  bad "ext4_unmounted_action mid-handoff: [$act]"
fi

act=$(ext4_unmounted_action "backup" "0" "")
if [ "$act" = "die_leftover" ]; then
  pass "ext4_unmounted_action: leftover without zmcontrol still dies"
else
  bad "ext4_unmounted_action leftover: [$act]"
fi

act=$(ext4_unmounted_action "" "0" "")
if [ "$act" = "continue" ]; then
  pass "ext4_unmounted_action: empty ext4 continues to fstab/mount"
else
  bad "ext4_unmounted_action empty: [$act]"
fi

act=$(ext4_unmounted_action "backup" "0" "$ZIMBRA_DIR")
if [ "$act" = "continue" ]; then
  pass "ext4_unmounted_action: leftover while already mounted at ZIMBRA_DIR continues"
else
  bad "ext4_unmounted_action mounted: [$act]"
fi

# DRBD-attached (third state): backing device has a drbd* holder.
sysfs=$(mktemp -d)
mkdir -p "$sysfs/class/block/sdb1/holders/drbd0"
if data_disk_held_by_drbd "/dev/sdb1" "$sysfs"; then
  pass "data_disk_held_by_drbd: true when holders/drbd0 exists"
else
  bad "data_disk_held_by_drbd missed stub holder"
fi
sysfs_empty=$(mktemp -d)
mkdir -p "$sysfs_empty/class/block/sdb1/holders"
if ! data_disk_held_by_drbd "/dev/sdb1" "$sysfs_empty"; then
  pass "data_disk_held_by_drbd: false without holders (mid-handoff / fresh)"
else
  bad "data_disk_held_by_drbd true with empty holders"
fi
rm -rf "$sysfs" "$sysfs_empty"

act=$(prepare_data_disk_drbd_action "1" "0")
if [ "$act" = "skip_drbd_attached" ]; then
  pass "prepare_data_disk_drbd_action: held by DRBD skips"
else
  bad "prepare_data_disk_drbd_action held: [$act]"
fi
act=$(prepare_data_disk_drbd_action "0" "1")
if [ "$act" = "skip_drbd_attached" ]; then
  pass "prepare_data_disk_drbd_action: mount on /dev/drbd* skips"
else
  bad "prepare_data_disk_drbd_action on_drbd: [$act]"
fi
act=$(prepare_data_disk_drbd_action "0" "0")
if [ "$act" = "continue" ]; then
  pass "prepare_data_disk_drbd_action: mid-handoff/fresh still continues"
else
  bad "prepare_data_disk_drbd_action continue: [$act]"
fi

# Stale pre-cluster fstab cleanup (used on DRBD-attached skip).
fstab_drbd=$(mktemp)
export FSTAB="$fstab_drbd"
printf 'UUID=root / ext4 defaults 0 1\n\n%s. See HA-RUNBOOK §13.\n# Remove this UUID line when Pacemaker kin-fs mounts /dev/drbd0 (Build HA pair).\nUUID=stale-uuid %s ext4 defaults 0 2\n' \
  "$FSTAB_MARK" "$ZIMBRA_DIR" >"$fstab_drbd"
if remove_precluster_zimbra_fstab \
  && ! grep -Fq "$FSTAB_MARK" "$fstab_drbd" \
  && ! grep -q "$ZIMBRA_DIR" "$fstab_drbd"; then
  pass "remove_precluster_zimbra_fstab: drops legacy mark + UUID line"
else
  bad "remove_precluster_zimbra_fstab left mark or zimbra line"
fi
rm -f "$fstab_drbd"

# Already-on-data success path still lives at the top of the script.
if grep -q 'is already on \${Z_MNTSRC}' ./prepare-zimbra-data-disk.sh \
  && grep -q 'accept_zimbra' ./prepare-zimbra-data-disk.sh \
  && grep -q 'leaving unmounted for DRBD/Pacemaker' ./prepare-zimbra-data-disk.sh \
  && grep -q 'skip_drbd_attached' ./prepare-zimbra-data-disk.sh \
  && grep -q 'data_disk_held_by_drbd' ./prepare-zimbra-data-disk.sh; then
  pass "helper keeps already-on-data, mid-handoff accept_zimbra, and DRBD-attached skip"
else
  bad "helper missing already-on-data, mid-handoff, or DRBD-attached skip path"
fi

fstab=$(mktemp)
export FSTAB="$fstab"
printf 'UUID=root / ext4 defaults 0 1\n' >"$fstab"
if ! fstab_has_kin_mark; then
  pass "fstab_has_kin_mark: absent"
else
  bad "fstab_has_kin_mark saw a mark on a stock fstab"
fi
uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
printf '\n%s\nUUID=%s /opt/zimbra ext4 defaults 0 2\n' "$FSTAB_MARK" "$uuid" >>"$fstab"
if fstab_has_kin_mark && fstab_has_uuid "$uuid"; then
  pass "fstab_has_uuid matches the KIN Mail line"
else
  bad "fstab_has_uuid missed the written line"
fi
if ! fstab_has_uuid "ffffffff-ffff-ffff-ffff-ffffffffffff"; then
  pass "fstab_has_uuid: other UUID is not a match"
else
  bad "fstab_has_uuid matched a UUID that is not in the file"
fi

mode=$(printf '%s\n' '{"install_mode":"os_root","need_partition":false}' | json_get install_mode)
part=$(printf '%s\n' '{"install_mode":"prepare_data","need_partition":true}' | json_get need_partition)
if [ "$mode" = "os_root" ] && [ "$part" = "true" ]; then
  pass "json_get install_mode and need_partition"
else
  bad "json_get: mode=[$mode] part=[$part]"
fi

argv0=$(printf '%s\n' '{"parted_argv":["parted","-s","--","/dev/sdb"]}' | json_plan_argv0)
if [ "$argv0" = "parted" ]; then
  pass "json_plan_argv0 reads parted"
else
  bad "json_plan_argv0: [$argv0]"
fi

if grep -q 'prepare-zimbra-data-disk.sh' ../02-prepare-os.sh \
  && grep -q 'python3 parted e2fsprogs cryptsetup' ../02-prepare-os.sh; then
  pass "02-prepare-os.sh calls the helper and installs python3/parted/cryptsetup"
else
  bad "02-prepare-os.sh is missing the helper hook or packages"
fi

if grep -q 'os.execvp' ./prepare-zimbra-data-disk.sh \
  && grep -q 'Never mkfs the meta' ./prepare-zimbra-data-disk.sh; then
  pass "helper runs parted via execvp and documents no meta mkfs"
else
  bad "helper missing execvp parted or meta-mkfs guard"
fi

rm -rf "$tmp" "$fstab"

if [ "$fails" -ne 0 ]; then
  printf 'FAILED %s checks\n' "$fails"
  exit 1
fi
printf 'All prepare-zimbra-data-disk helper tests passed\n'
exit 0
