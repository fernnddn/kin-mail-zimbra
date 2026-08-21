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
  && grep -q 'python3 parted e2fsprogs' ../02-prepare-os.sh; then
  pass "02-prepare-os.sh calls the helper and installs python3/parted"
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
