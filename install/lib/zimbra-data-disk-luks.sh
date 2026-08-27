#!/usr/bin/env bash
# =============================================================================
# KIN Mail: LUKS under the Zimbra data partition (fresh installs only).
#
# Layering: raw partition -> LUKS (/dev/mapper/kin-zimbra-crypt) -> ext4 / DRBD.
# Each node has its own keyfile. DRBD replicates already-decrypted blocks
# through the mapper, so mail and mail2 never share a key.
#
# Do not luksFormat a disk that is already crypto_LUKS (retry) or already
# ext4 (live unencrypted pair; retrofit is a separate migration).
#
#   KIN_ENCRYPT_ZIMBRA_DATA_DISK  default 1 (empty disk gets LUKS). 0 = plain.
#   KIN_LUKS_MAPPER_NAME          default kin-zimbra-crypt
#   KIN_LUKS_KEYFILE              default /etc/kin-mail/luks-keyfile
#   KIN_LUKS_CRYPTTAB             default /etc/crypttab
#
# Keyfile backup into the provisioning vault is a follow-up: the vault only
# stores host passwords today (ALLOWED_FIELDS) and lives on the console, not
# on each mail node.
# =============================================================================

luks_mapper_name() {
  printf '%s' "${KIN_LUKS_MAPPER_NAME:-kin-zimbra-crypt}"
}

luks_mapper_path() {
  printf '%s' "/dev/mapper/$(luks_mapper_name)"
}

luks_keyfile_path() {
  printf '%s' "${KIN_LUKS_KEYFILE:-/etc/kin-mail/luks-keyfile}"
}

luks_crypttab_path() {
  printf '%s' "${KIN_LUKS_CRYPTTAB:-/etc/crypttab}"
}

encrypt_zimbra_data_disk_enabled() {
  case "${KIN_ENCRYPT_ZIMBRA_DATA_DISK:-1}" in
    0|false|FALSE|no|NO|off|OFF) return 1 ;;
    *) return 0 ;;
  esac
}

# fstype of the raw partition (crypto_LUKS / ext4 / empty).
# Optional 2nd arg overrides blkid (tests).
data_disk_fstype() {
  local disk="$1"
  if [ -n "${2:-}" ]; then
    printf '%s' "$2"
    return 0
  fi
  blkid -s TYPE -o value "$disk" 2>/dev/null || true
}

data_disk_is_luks() {
  local t
  t=$(data_disk_fstype "$1" "${2:-}")
  [ "$t" = "crypto_LUKS" ]
}

# Decide LUKS vs plain. Never returns format_luks for crypto_LUKS or ext4.
# Args: encrypt 0|1, fstype
# Prints: format_luks | open_luks | mkfs_plain | skip_plain_existing | die_unknown
luks_prepare_action() {
  local encrypt="${1:-0}"
  local fstype="${2:-}"
  case "$fstype" in
    crypto_LUKS)
      printf '%s\n' "open_luks"
      ;;
    ext4)
      printf '%s\n' "skip_plain_existing"
      ;;
    "")
      if [ "$encrypt" = "1" ]; then
        printf '%s\n' "format_luks"
      else
        printf '%s\n' "mkfs_plain"
      fi
      ;;
    *)
      printf '%s\n' "die_unknown"
      ;;
  esac
}

# True when mount source is the raw data partition or the KIN LUKS mapper.
same_as_data_backing() {
  local src="$1"
  local data="${2:-${KIN_DRBD_DATA_DISK:-/dev/sdb1}}"
  local mapper
  src="${src%%$'\n'*}"
  src="${src%%[*}"
  src="${src%% *}"
  mapper=$(luks_mapper_path)
  [ -n "$src" ] || return 1
  [ "$src" = "$data" ] && return 0
  [ "$src" = "$mapper" ] && return 0
  return 1
}

ensure_luks_keyfile() {
  local key dir
  key=$(luks_keyfile_path)
  dir=$(dirname "$key")
  mkdir -p "$dir" || return 1
  # 0755: kin-console must traverse /etc/kin-mail to read 0644 markers.
  # The keyfile itself stays 0400, so directory execute does not leak the key.
  chmod 0755 "$dir" 2>/dev/null || true
  if [ ! -f "$key" ]; then
    umask 077
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -out "$key" 4096 || return 1
    else
      dd if=/dev/urandom of="$key" bs=4096 count=1 2>/dev/null || return 1
    fi
  fi
  chown root:root "$key" 2>/dev/null || true
  chmod 0400 "$key" || return 1
  return 0
}

# The line this deployment needs, given the CURRENT header on the disk.
luks_crypttab_line() {
  printf '%s UUID=%s %s luks,discard' "$1" "$2" "$3"
}

# True when crypttab already names this mapper with THIS disk's LUKS UUID.
luks_crypttab_matches() {
  local tab="$1" name="$2" uuid="$3"
  [ -f "$tab" ] || return 1
  awk -v n="$name" -v u="UUID=$uuid" '$1 == n && $2 == u { found = 1 } END { exit found ? 0 : 1 }' \
    "$tab" 2>/dev/null
}

ensure_luks_crypttab() {
  local disk="$1"
  local uuid tab name key tmp
  tab=$(luks_crypttab_path)
  name=$(luks_mapper_name)
  key=$(luks_keyfile_path)
  uuid=$(cryptsetup luksUUID "$disk" 2>/dev/null || true)
  [ -n "$uuid" ] || return 1
  touch "$tab" || return 1

  if luks_crypttab_matches "$tab" "$name" "$uuid"; then
    return 0
  fi

  # A line for this mapper naming a DIFFERENT UUID is worse than no line at
  # all, and this used to return early on the name alone and leave it. After
  # the disk is re-LUKS-formatted (rebuilt VM, wiped data disk) the old UUID
  # no longer exists, so at boot systemd-cryptsetup waits for a device that
  # will never appear - a very slow boot, then a passphrase prompt, then no
  # mapper. DRBD can then never attach its backing device and Pacemaker
  # retries it to INFINITY, which is exactly how a "successful" deploy came
  # back from a reboot with the whole stack dead (live Phase 6, 27 Aug 2026).
  tmp="${tab}.kin.$$"
  # Drop every existing entry for this mapper with awk, not grep -E "^name".
  # A line indented with spaces is a valid crypttab entry and awk's field
  # split sees it, but the anchored regex did not, so a stale indented line
  # survived next to the new one and systemd would still try the dead UUID
  # first. A commented line keeps its "#" in $1, so it is left alone.
  #
  # awk print also terminates every record, which normalises a crypttab whose
  # last line has no newline. Appending to such a file directly would glue our
  # entry onto the previous one and corrupt both.
  awk -v n="$name" '$1 != n { print }' "$tab" >"$tmp" 2>/dev/null || : >"$tmp"
  { luks_crypttab_line "$name" "$uuid" "$key"; printf '\n'; } >>"$tmp" \
    || { rm -f "$tmp"; return 1; }
  # Keep the original mode/owner; crypttab is read by early boot.
  chmod --reference="$tab" "$tmp" 2>/dev/null || chmod 0644 "$tmp" 2>/dev/null || true
  mv "$tmp" "$tab" || { rm -f "$tmp"; return 1; }
  return 0
}

# Would this host bring the mapper back on its own after a reboot?
# Prints one reason per problem and returns 1 when any is found.
luks_boot_ready_reason() {
  local disk="$1"
  local tab name key uuid
  tab=$(luks_crypttab_path)
  name=$(luks_mapper_name)
  key=$(luks_keyfile_path)
  uuid=$(cryptsetup luksUUID "$disk" 2>/dev/null || true)
  if [ -z "$uuid" ]; then
    printf '%s\n' "no LUKS header on ${disk}"
    return 1
  fi
  if [ ! -s "$key" ]; then
    printf '%s\n' "keyfile ${key} is missing or empty"
    return 1
  fi
  if ! luks_crypttab_matches "$tab" "$name" "$uuid"; then
    printf '%s\n' \
      "${tab} has no line for ${name} with UUID=${uuid} (a stale UUID here means the mapper never reappears after a reboot)"
    return 1
  fi
  if ! cryptsetup luksDump "$disk" >/dev/null 2>&1; then
    printf '%s\n' "cannot read the LUKS header on ${disk}"
    return 1
  fi
  return 0
}

open_luks_mapper() {
  local disk="$1"
  local name key mapper
  name=$(luks_mapper_name)
  key=$(luks_keyfile_path)
  mapper=$(luks_mapper_path)
  if [ -b "$mapper" ]; then
    return 0
  fi
  cryptsetup open --key-file "$key" "$disk" "$name"
}

# Format (once) and open. Caller must have already chosen format_luks.
# Extra gates: blkid can miss TYPE; never luksFormat if cryptsetup/mapper
# already show a LUKS volume (would destroy the header).
luks_format_blocked_reason() {
  local disk="$1"
  local fstype="${2:-}"
  local mapper
  mapper=$(luks_mapper_path)
  if [ -n "$fstype" ]; then
    if [ "$fstype" = "crypto_LUKS" ]; then
      printf '%s\n' "already_crypto_LUKS"
      return 0
    fi
  elif data_disk_is_luks "$disk"; then
    printf '%s\n' "already_crypto_LUKS"
    return 0
  fi
  if [ -b "$mapper" ]; then
    printf '%s\n' "mapper_already_open"
    return 0
  fi
  if command -v cryptsetup >/dev/null 2>&1 && cryptsetup isLuks "$disk" 2>/dev/null; then
    printf '%s\n' "cryptsetup_isLuks"
    return 0
  fi
  return 1
}

luks_format_and_open() {
  local disk="$1"
  local key reason
  key=$(luks_keyfile_path)
  reason=$(luks_format_blocked_reason "$disk" || true)
  if [ -n "$reason" ]; then
    printf '%s\n' "refusing luksFormat: ${disk} (${reason})" >&2
    return 1
  fi
  cryptsetup luksFormat --batch-mode --type luks2 --key-file "$key" "$disk" || return 1
  open_luks_mapper "$disk"
}

# Print the block device DRBD / mkfs should use (mapper or raw).
# Does not luksFormat. For format_luks/open_luks the mapper must already be open.
luks_backing_device() {
  local disk="${1:-${KIN_DRBD_DATA_DISK:-/dev/sdb1}}"
  local encrypt=0
  local fstype action
  encrypt_zimbra_data_disk_enabled && encrypt=1
  fstype=$(data_disk_fstype "$disk")
  action=$(luks_prepare_action "$encrypt" "$fstype")
  case "$action" in
    format_luks|open_luks) luks_mapper_path ;;
    *) printf '%s' "$disk" ;;
  esac
}

if [ "${KIN_LUKS_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

# Standalone: ensure LUKS/open for ansible (fresh empty or already-LUKS).
# Never prints the keyfile. Last line is KIN_LUKS_BACKING=/dev/...
if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "ensure-zimbra-data-luks: run as root" >&2
  exit 1
fi

DATA_DISK="${KIN_DRBD_DATA_DISK:-/dev/sdb1}"
ENCRYPT=0
encrypt_zimbra_data_disk_enabled && ENCRYPT=1
FSTYPE=$(data_disk_fstype "$DATA_DISK")
ACTION=$(luks_prepare_action "$ENCRYPT" "$FSTYPE")
BACKING="$DATA_DISK"

# blkid TYPE="" on a live LUKS partition would choose format_luks. If the
# mapper is already open or cryptsetup isLuks, open instead of destroying.
if [ "$ACTION" = "format_luks" ]; then
  _blocked=$(luks_format_blocked_reason "$DATA_DISK" || true)
  if [ -n "$_blocked" ]; then
    printf '%s\n' "ensure-zimbra-data-luks: not formatting (${_blocked}); opening existing LUKS" >&2
    ACTION="open_luks"
  fi
  unset _blocked
fi

case "$ACTION" in
  die_unknown)
    printf '%s\n' "ensure-zimbra-data-luks: ${DATA_DISK} has TYPE=${FSTYPE}; refusing" >&2
    exit 1
    ;;
  skip_plain_existing|mkfs_plain)
    BACKING="$DATA_DISK"
    ;;
  format_luks)
    command -v cryptsetup >/dev/null 2>&1 || {
      printf '%s\n' "ensure-zimbra-data-luks: cryptsetup is not installed" >&2
      exit 1
    }
    ensure_luks_keyfile || exit 1
    luks_format_and_open "$DATA_DISK" || exit 1
    ensure_luks_crypttab "$DATA_DISK" || exit 1
    BACKING=$(luks_mapper_path)
    ;;
  open_luks)
    command -v cryptsetup >/dev/null 2>&1 || {
      printf '%s\n' "ensure-zimbra-data-luks: cryptsetup is not installed" >&2
      exit 1
    }
    ensure_luks_keyfile || exit 1
    open_luks_mapper "$DATA_DISK" || exit 1
    ensure_luks_crypttab "$DATA_DISK" || exit 1
    BACKING=$(luks_mapper_path)
    ;;
esac

# /dev/mapper/<name> is a udev symlink to the real dm-crypt device node,
# not something cryptsetup open/luksFormat creates directly - cryptsetup
# normally blocks internally until udev has processed that, but that wait
# can be skipped (no udev running, --disable-locks, some chroot/container
# install environments). Retry briefly before failing closed, same fix as
# the DRBD "Can not open backing device" race hit on a live 2vm practice
# run right after an adjacent unmount (25 Aug 2026) - belt-and-suspenders
# here since format_luks/open_luks just did the equivalent device-state
# change.
_backing_attempt=0
_backing_retries="${KIN_LUKS_BACKING_RETRIES:-5}"
_backing_delay="${KIN_LUKS_BACKING_DELAY:-1}"
while [ ! -b "$BACKING" ] && [ "$_backing_attempt" -lt "$_backing_retries" ]; do
  _backing_attempt=$((_backing_attempt + 1))
  sleep "$_backing_delay"
done
if [ ! -b "$BACKING" ]; then
  printf '%s\n' "ensure-zimbra-data-luks: backing ${BACKING} is not a block device (checked ${_backing_attempt} times)" >&2
  exit 1
fi
# Nothing so far proves this host can bring the mapper back BY ITSELF. The
# whole cluster hangs off that: without the mapper, DRBD cannot attach its
# backing device and Pacemaker retries the resource to INFINITY. Prove it at
# deploy time instead of discovering it on the next reboot.
if [ "$BACKING" != "$DATA_DISK" ]; then
  if ! _boot_reason=$(luks_boot_ready_reason "$DATA_DISK"); then
    printf '%s\n' "ensure-zimbra-data-luks: this node would NOT reopen ${BACKING} after a reboot: ${_boot_reason}" >&2
    exit 1
  fi
  printf 'KIN_LUKS_BOOT_READY=1\n'
fi
printf 'KIN_LUKS_BACKING=%s\n' "$BACKING"
exit 0
