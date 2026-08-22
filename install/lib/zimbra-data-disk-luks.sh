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

ensure_luks_crypttab() {
  local disk="$1"
  local uuid tab name key
  tab=$(luks_crypttab_path)
  name=$(luks_mapper_name)
  key=$(luks_keyfile_path)
  uuid=$(cryptsetup luksUUID "$disk" 2>/dev/null || true)
  [ -n "$uuid" ] || return 1
  touch "$tab" || return 1
  if grep -qE "^${name}[[:space:]]" "$tab" 2>/dev/null; then
    return 0
  fi
  printf '%s UUID=%s %s luks,discard\n' "$name" "$uuid" "$key" >>"$tab" || return 1
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
luks_format_and_open() {
  local disk="$1"
  local key
  key=$(luks_keyfile_path)
  if data_disk_is_luks "$disk"; then
    printf '%s\n' "refusing luksFormat: ${disk} is already crypto_LUKS" >&2
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

if [ ! -b "$BACKING" ]; then
  printf '%s\n' "ensure-zimbra-data-luks: backing ${BACKING} is not a block device" >&2
  exit 1
fi
printf 'KIN_LUKS_BACKING=%s\n' "$BACKING"
exit 0
