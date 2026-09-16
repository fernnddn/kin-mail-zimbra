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

# --- is this disk the right place to keep a customer's mail? -----------------
# The selector only asks whether a disk is blank and past a ~20 GiB floor. That
# is the right question for "can Zimbra install here" and the wrong one for
# "should the mail live here", and the gap between those two is where an estate
# ends up confined to whatever spare disk the OS installer left behind.
GB=1073741824
TB=1099511627776

if reason=$(data_disk_too_small $((60 * GB)) 1000); then
  case "$reason" in
    *"60 GB"*) pass "a 60 GB disk is refused when the site needs 1 TB" ;;
    *) bad "refused, but the message does not name the size: $reason" ;;
  esac
else
  bad "a 60 GB disk was accepted for a deployment needing 1 TB"
fi

data_disk_too_small $((1200 * GB)) 1000 >/dev/null \
  && bad "a 1.2 TB disk was refused for a 1 TB requirement" \
  || pass "a 1.2 TB disk satisfies a 1 TB requirement"

# No requirement stated means no opinion: a demo must not be blocked by a rule
# nobody set.
data_disk_too_small $((60 * GB)) "" >/dev/null \
  && bad "refused with no minimum configured" \
  || pass "no minimum configured means no refusal"
data_disk_too_small $((60 * GB)) 0 >/dev/null \
  && bad "refused when the minimum is zero" \
  || pass "a zero minimum means no refusal"
data_disk_too_small $((60 * GB)) "not-a-number" >/dev/null \
  && bad "a malformed minimum was treated as a limit" \
  || pass "a malformed minimum is ignored rather than guessed at"

# The inverted-layout fingerprint: OS on the big disk, mail on the leftover.
data_disk_looks_inverted $((60 * GB)) $((100 * GB)) \
  && pass "60 GB mail disk beside a 100 GB system disk reads as inverted" \
  || bad "did not notice an inverted layout"
data_disk_looks_inverted $((1200 * GB)) $((60 * GB)) \
  && bad "called a correct layout inverted" \
  || pass "a large mail disk beside a small system disk is not flagged"
data_disk_looks_inverted $((60 * GB)) 0 \
  && bad "flagged inversion without knowing the system disk size" \
  || pass "an unknown system disk size is not treated as inversion"

case "$(human_bytes $((1200 * GB)))" in
  *TB) pass "human_bytes reports a terabyte disk in TB" ;;
  *) bad "human_bytes: $(human_bytes $((1200 * GB)))" ;;
esac
case "$(human_bytes $((60 * GB)))" in
  "60 GB") pass "human_bytes reports 60 GB exactly" ;;
  *) bad "human_bytes: $(human_bytes $((60 * GB)))" ;;
esac

# The stage must actually consult these, and must say the size out loud - the
# device name alone is what let an inverted layout pass unnoticed.
STAGE="$(pwd)/prepare-zimbra-data-disk.sh"
grep -q 'data_disk_too_small' "$STAGE" \
  && pass "the stage refuses a disk below the configured minimum" \
  || bad "the stage never calls data_disk_too_small"
grep -q 'data_disk_looks_inverted' "$STAGE" \
  && pass "the stage warns about an inverted layout" \
  || bad "the stage never calls data_disk_looks_inverted"
grep -q 'Mail will be stored on' "$STAGE" \
  && pass "the stage prints which disk the mail lands on, with its size" \
  || bad "the stage still names a device without its size"

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
  && grep -q 'data_disk_held_by_drbd' ./prepare-zimbra-data-disk.sh \
  && grep -q 'zimbra_is_mid_handoff' ./prepare-zimbra-data-disk.sh; then
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
