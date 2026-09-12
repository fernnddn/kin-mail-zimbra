#!/usr/bin/env bash
# =============================================================================
# Grow a filesystem into space that was added to its disk by the hypervisor.
#
#   grow-disk.sh plan   <mountpoint>
#   grow-disk.sh apply  <mountpoint>
#   grow-disk.sh apply  <mountpoint> --reclaim-reserved
#
# The operator enlarges a virtual disk in VMware, Proxmox or wherever, and the
# guest keeps showing the old size, because a bigger disk is not a bigger
# partition and a bigger partition is not a bigger filesystem. Doing it by hand
# is three or four tools deep depending on whether the box uses LVM and LUKS,
# every one of them capable of destroying the data if given the wrong argument.
# This does the whole chain, or refuses and explains why.
#
# WHAT IT WILL DO
#   rescan the disk so the kernel sees the new size
#   grow the LAST partition into the free space that follows it
#   resize a LUKS container, if the stack has one
#   grow the LVM physical volume and extend the logical volume, if it has those
#   grow the filesystem (ext2/3/4 or xfs)
#
# WHAT IT WILL NOT DO, EVER
#   shrink anything
#   move or reorder a partition, or change where one starts
#   touch a partition that is not the last one on its disk
#   delete a partition - with exactly one consented exception, below
#   touch a device that DRBD or MD is using
#   act on any mountpoint other than the ones named in ALLOWED_MOUNTS
#
# Every one of those refusals is a case where the safe outcome is an operator
# reading an explanation, and the unsafe outcome is a mail server that has lost
# its data. There is no force flag on purpose.
#
# THE ONE EXCEPTION: --reclaim-reserved
#
# The installer lays the mail disk out as a big data partition followed by a
# small unformatted partition reserved for a future second server (DRBD
# metadata). On a single-server appliance - which is the product - that
# reserved partition is never used, and because it sits AFTER the data
# partition it makes the mail disk permanently ungrowable. Adding space in the
# hypervisor does nothing: the free space lands after the reserved partition,
# with the reserved partition in between (live QA Phase 17, 12 Sep 2026).
#
# --reclaim-reserved deletes that one partition so the data partition can
# extend. It is opt-in, it is never implied by plan or by a bare apply, and it
# refuses unless ALL of these hold:
#
#   the partition is the last one on the disk
#   it sits immediately after the partition being grown
#   it is smaller than RECLAIM_MAX_BYTES
#   blkid and wipefs see NOTHING on it: no filesystem, no LVM, no DRBD
#   it is not mounted, has no holders, and is not open by device-mapper
#   it is named in neither /etc/fstab nor /etc/crypttab
#   this host has no DRBD resource configured at all
#
# It does not recreate the partition afterwards. Building an HA pair later
# needs a small separate disk, and saying that plainly is better than leaving
# a hole nobody can explain.
#
# Injection points, all for tests. Nothing here should ever be set in
# production; the defaults are the real tools.
#   KIN_GROW_FINDMNT KIN_GROW_LSBLK KIN_GROW_PARTED KIN_GROW_GROWPART
#   KIN_GROW_PARTX KIN_GROW_RESIZE2FS KIN_GROW_XFS_GROWFS KIN_GROW_PVRESIZE
#   KIN_GROW_LVEXTEND KIN_GROW_CRYPTSETUP KIN_SYSFS_ROOT KIN_ZIMBRA_DIR
#   KIN_GROW_ALLOW_NONROOT   skip the euid check (tests only)
#   KIN_GROW_BLKID KIN_GROW_WIPEFS KIN_GROW_PROC_ROOT KIN_GROW_ETC_ROOT
# =============================================================================
set -uo pipefail

FINDMNT="${KIN_GROW_FINDMNT:-findmnt}"
LSBLK="${KIN_GROW_LSBLK:-lsblk}"
PARTED="${KIN_GROW_PARTED:-parted}"
GROWPART="${KIN_GROW_GROWPART:-growpart}"
PARTX="${KIN_GROW_PARTX:-partx}"
RESIZE2FS="${KIN_GROW_RESIZE2FS:-resize2fs}"
XFS_GROWFS="${KIN_GROW_XFS_GROWFS:-xfs_growfs}"
PVRESIZE="${KIN_GROW_PVRESIZE:-pvresize}"
LVEXTEND="${KIN_GROW_LVEXTEND:-lvextend}"
CRYPTSETUP="${KIN_GROW_CRYPTSETUP:-cryptsetup}"
BLKID="${KIN_GROW_BLKID:-blkid}"
WIPEFS="${KIN_GROW_WIPEFS:-wipefs}"
PROC_ROOT="${KIN_GROW_PROC_ROOT:-/proc}"
ETC_ROOT="${KIN_GROW_ETC_ROOT:-/etc}"
# DRBD metadata is 128 MiB per TiB of data; the installer reserves 256 MiB.
# Anything materially larger is not the partition this is meant to reclaim,
# and the safe response to "bigger than I expected" is to stop.
RECLAIM_MAX_BYTES="${KIN_GROW_RECLAIM_MAX_BYTES:-2147483648}"   # 2 GiB
SYSFS="${KIN_SYSFS_ROOT:-/sys}"
ZIMBRA_DIR="${KIN_ZIMBRA_DIR:-/opt/zimbra}"

# Below this, growing is not worth a partition table write. GPT keeps a backup
# header in the last sectors, so a disk always has a little unusable tail and a
# few hundred kilobytes of "free space" means nothing was actually added.
MIN_GROW_BYTES="${KIN_GROW_MIN_BYTES:-16777216}"   # 16 MiB

say()  { printf '%s\n' "$*"; }
mark() { printf 'KIN_GROW_%s\n' "$*"; }
fail() { printf 'KIN_GROW_REFUSED reason=%s\n' "$1"; shift; printf '%s\n' "$*"; }

# --- the only mountpoints this is allowed to touch ---------------------------
# A console button that can grow an arbitrary path is a console button that can
# be aimed at the wrong disk. These two are the ones an appliance runs out of.
allowed_mount() {
  case "$1" in
    /) return 0 ;;
    "$ZIMBRA_DIR") return 0 ;;
    *) return 1 ;;
  esac
}

# --- stack discovery ---------------------------------------------------------

# Filesystem source device for a mountpoint, e.g. /dev/mapper/vg-lv.
fs_source() { "$FINDMNT" -no SOURCE --target "$1" 2>/dev/null | head -1; }
fs_type()   { "$FINDMNT" -no FSTYPE --target "$1" 2>/dev/null | head -1; }

# The device stack from the filesystem down to the physical disk, leaf first.
# Each line is "NAME TYPE", e.g.
#   /dev/mapper/ubuntu--vg-ubuntu--lv lvm
#   /dev/sda3                         part
#   /dev/sda                          disk
#
# PATH, not NAME. lsblk's NAME for a device-mapper node is the kernel name,
# "ubuntu--vg-ubuntu--lv", and /dev/ubuntu--vg-ubuntu--lv does not exist: the
# real nodes are under /dev/mapper. Building a path by pasting /dev/ in front
# of NAME therefore produced a path that is right for a plain partition and
# wrong for every LVM and LUKS layer, so lvextend and pvresize were being
# handed something that could not be opened. lvextend failing is treated as
# "nothing to add" and does not stop the run, so on an LVM appliance the resize
# reported success while the filesystem had not grown at all. Ubuntu Server's
# guided install uses LVM by default, so that is the common layout, not an
# exotic one.
device_chain() { "$LSBLK" -nsro PATH,TYPE "$1" 2>/dev/null; }

chain_types() { awk '{print $2}'; }

# Last partition on a disk, and the bytes left after it.
# Prints "PARTNUM PART_END DISK_SIZE FREE_BYTES", or nothing if unreadable.
# Name of a numbered partition, from parted's machine output
# (num:start:end:size:fstype:name:flags). Empty when unnamed or unreadable.
# The DEVICE NODE for a partition number, e.g. 2 -> /dev/sdb2, /dev/loop0p2,
# /dev/nvme0n1p2.
#
# Not partition_name(), which returns parted's LABEL for the partition - "data",
# "meta". Using that as a path made every safety check run against a device that
# does not exist: blkid found no signature because there was no device, wipefs
# found nothing, findmnt found nothing, and the guards all passed vacuously on
# the way to deleting a real partition. Caught by the loop-device test, which is
# exactly why that test exists.
partition_device() {
  local disk="$1" want="$2" base entry num path
  base=$(basename "$disk")

  # sysfs, because it is the kernel's own answer and it carries the partition
  # NUMBER explicitly. lsblk cannot be asked for the number on util-linux 2.37,
  # which is what Ubuntu 22.04 ships and what the appliance runs.
  if [ -d "${SYSFS}/class/block/${base}" ]; then
    for entry in "${SYSFS}/class/block/${base}"/*/partition; do
      [ -r "$entry" ] || continue
      num=$(cat "$entry" 2>/dev/null | tr -d '[:space:]')
      [ "$num" = "$want" ] || continue
      path="/dev/$(basename "$(dirname "$entry")")"
      [ -b "$path" ] || continue
      printf '%s' "$path"
      return 0
    done
  fi

  # Fall back to the usual naming, and only accept it if it really is a block
  # device. Returning a path that does not exist is how every safety check
  # below ends up passing on nothing at all.
  case "$disk" in
    *[0-9]) path="${disk}p${want}" ;;
    *)      path="${disk}${want}" ;;
  esac
  [ -b "$path" ] || return 1
  printf '%s' "$path"
  return 0
}

partition_name() {
  local disk="$1" want="$2" out line
  out=$("$PARTED" -sm "$disk" unit B print 2>/dev/null) || return 0
  while IFS= read -r line; do
    case "$line" in
      "${want}:"*) printf '%s' "$line" | cut -d: -f6; return 0 ;;
    esac
  done <<< "$out"
  return 0
}

# Returns 2 specifically when the table could not be read because of
# permissions, which is a different problem from a table that is corrupt and
# deserves a different sentence.
disk_tail() {
  local disk="$1" out last_num=0 last_end=0 disk_size=0
  # parted exits 0 while printing "Error: Error opening /dev/sda: Permission
  # denied", so its exit status cannot be trusted to mean the table was read.
  # The output has to be inspected.
  out=$("$PARTED" -sm "$disk" unit B print 2>&1)
  case "$out" in
    *"Permission denied"*) return 2 ;;
  esac
  [ -n "$out" ] || return 1
  local line num end
  while IFS= read -r line; do
    case "$line" in
      BYT*|"") continue ;;
      "${disk}:"*)
        disk_size=$(printf '%s' "$line" | cut -d: -f2 | tr -d 'B')
        continue
        ;;
    esac
    num=$(printf '%s' "$line" | cut -d: -f1)
    end=$(printf '%s' "$line" | cut -d: -f3 | tr -d 'B')
    case "$num" in
      ''|*[!0-9]*) continue ;;
    esac
    case "$end" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "$end" -gt "$last_end" ]; then
      last_end="$end"
      last_num="$num"
    fi
  done <<< "$out"
  [ "$disk_size" -gt 0 ] || return 1
  [ "$last_num" -gt 0 ] || return 1
  local free=$(( disk_size - last_end - 1 ))
  [ "$free" -ge 0 ] || free=0
  printf '%s %s %s %s\n' "$last_num" "$last_end" "$disk_size" "$free"
}

# --- the plan ----------------------------------------------------------------
#
# Sets the globals the apply path uses, and prints a machine-readable summary.
# Returns 0 when there is something safe to do, 1 when there is not.

PLAN_MOUNT=""; PLAN_FS=""; PLAN_FSTYPE=""; PLAN_DISK=""; PLAN_PART=""
PLAN_PARTNUM=""; PLAN_CRYPT=""; PLAN_LVM=""; PLAN_FREE=0

# Ask the controller how big the disk is NOW.
#
# This has to happen BEFORE anything is measured, and it has to happen in plan
# as well as apply. It used to sit inside grow_apply, after grow_plan had
# already read the size - so the kernel was still reporting the capacity it
# saw at boot, plan said "no unused space", and apply then skipped growpart
# because plan had already decided there was nothing to do. An operator who
# enlarged the disk in the hypervisor and pressed Check saw nothing, twice,
# with no hint that a rescan was the missing step (live QA Phase 17,
# 12 Sep 2026).
#
# It writes to a sysfs node, not to the disk. No sector is touched, no table
# is written; the kernel re-reads the capacity the hypervisor is already
# advertising. That is why it is safe in a plan that promises to change
# nothing.
rescan_disk() {
  local disk="$1" base sysdev
  [ -n "$disk" ] || return 0
  base=$(basename "$disk")
  sysdev="${SYSFS}/class/block/${base}/device/rescan"
  if [ ! -w "$sysdev" ]; then
    # Loop devices and some virtio setups have no rescan node. The hypervisor
    # may already have notified the kernel, so this is not a failure.
    return 0
  fi
  printf '1' > "$sysdev" 2>/dev/null || {
    say "    (the kernel did not accept a rescan of ${disk}; continuing with the size it reports)"
    return 0
  }
  # The capacity update is not instantaneous on every controller.
  sleep 1
  return 0
}

# The disk a mountpoint sits on, without doing any of the rest of the planning.
# Needed because the rescan has to happen before the measuring, and the
# measuring is what normally discovers the disk.
disk_under() {
  local mp="$1" fs chain
  fs=$(fs_source "$mp") || return 1
  [ -n "$fs" ] || return 1
  chain=$(device_chain "$fs") || return 1
  printf '%s\n' "$chain" | awk '$2=="disk"||$2=="loop" {print $1; exit}'
}

grow_plan() {
  local mp="$1"
  PLAN_MOUNT="$mp"; PLAN_FS=""; PLAN_FSTYPE=""; PLAN_DISK=""; PLAN_PART=""
  PLAN_PARTNUM=""; PLAN_CRYPT=""; PLAN_LVM=""; PLAN_FREE=0

  if ! allowed_mount "$mp"; then
    fail not-allowed "Only / and ${ZIMBRA_DIR} can be grown from here."
    return 1
  fi

  # Before measuring anything. See rescan_disk.
  #
  # Not in a dry run. "plan" promises to change nothing ON DISK, and a rescan
  # honours that - it writes to a sysfs node, not to a sector. KIN_GROW_DRY_RUN
  # makes a stronger promise, that nothing at all happens anywhere, and that
  # mode exists so the refusals can be proved without a machine. The two
  # promises are different and both worth keeping.
  if [ "${KIN_GROW_SKIP_RESCAN:-0}" != "1" ] && [ "${KIN_GROW_DRY_RUN:-0}" != "1" ]; then
    local probe_disk
    probe_disk=$(disk_under "$mp" 2>/dev/null) || probe_disk=""
    [ -n "$probe_disk" ] && rescan_disk "$probe_disk"
  fi

  PLAN_FS=$(fs_source "$mp")
  PLAN_FSTYPE=$(fs_type "$mp")
  if [ -z "$PLAN_FS" ] || [ -z "$PLAN_FSTYPE" ]; then
    fail no-filesystem "Could not read what is mounted at ${mp}."
    return 1
  fi

  case "$PLAN_FSTYPE" in
    ext2|ext3|ext4|xfs) ;;
    *)
      fail unsupported-fs \
        "${mp} is ${PLAN_FSTYPE}. Only ext2, ext3, ext4 and xfs can be grown here."
      return 1
      ;;
  esac

  local chain types
  chain=$(device_chain "$PLAN_FS")
  if [ -z "$chain" ]; then
    fail no-device-chain "Could not follow ${PLAN_FS} down to a disk."
    return 1
  fi
  types=$(printf '%s\n' "$chain" | chain_types)

  # Replicated or RAID storage is somebody else's lifecycle. Growing a DRBD
  # backing device under a running cluster is how both replicas disagree about
  # how big they are.
  # drbd0, md0, md127: the number is part of the name, so the pattern has to
  # allow it. Matching bare "drbd" let a real DRBD device through, which is the
  # one device this must never touch.
  # The boundary has to allow a leading slash as well as a space, because the
  # chain carries full paths: /dev/drbd0 is preceded by "/", not by a space, so
  # a space-only boundary stopped this guard firing on the exact device it
  # exists to protect. md and raid keep their digit requirement so that
  # /dev/mapper/... cannot match on the word "mapper".
  if printf '%s\n' "$chain" | grep -qiE '(^|[ /])(drbd[0-9]*|md[0-9]+|raid[0-9]*)( |$)'; then
    fail replicated \
      "${mp} sits on replicated or RAID storage. Growing that is a cluster operation, not a disk one."
    return 1
  fi

  # The bottom of the stack is a "disk" on a normal appliance and a "loop" on
  # loop-backed storage. Insisting on the literal word "disk" refused loop
  # devices outright, which is both a real layout somebody could be running and
  # the only way to prove this whole chain end to end without a spare machine.
  local parts disks
  parts=$(printf '%s\n' "$types" | grep -c '^part$')
  disks=$(printf '%s\n' "$types" | grep -cE '^(disk|loop)$')
  if [ "$disks" -ne 1 ]; then
    fail many-disks \
      "${mp} spans ${disks} disks. Growing one of several is not something to do unattended."
    return 1
  fi
  if [ "$parts" -gt 1 ]; then
    fail many-partitions \
      "${mp} is built from ${parts} partitions. Which one to grow is not obvious enough to guess."
    return 1
  fi

  # The chain already carries full device paths, so nothing is pasted together
  # here. See device_chain for why that matters.
  PLAN_DISK="$(printf '%s\n' "$chain" | awk '$2=="disk"||$2=="loop"{print $1}' | head -1)"
  if [ "$parts" -eq 1 ]; then
    PLAN_PART="$(printf '%s\n' "$chain" | awk '$2=="part"{print $1}' | head -1)"
  fi
  printf '%s\n' "$types" | grep -q '^crypt$' && \
    PLAN_CRYPT="$(printf '%s\n' "$chain" | awk '$2=="crypt"{print $1}' | head -1)"
  printf '%s\n' "$types" | grep -q '^lvm$' && \
    PLAN_LVM="$(printf '%s\n' "$chain" | awk '$2=="lvm"{print $1}' | head -1)"

  # On a single-disk appliance /opt/zimbra is not a separate volume, it is a
  # directory on the root filesystem. Both rows in the console would then be
  # the same disk under two names, and an operator could reasonably extend one
  # and wonder why the other changed too. Say it rather than let them find out.
  if [ "$mp" != "/" ]; then
    local root_src
    root_src=$(fs_source /)
    if [ -n "$root_src" ] && [ "$root_src" = "$PLAN_FS" ]; then
      mark "SHARED_WITH_ROOT=1"
      say "${mp} is a directory on the system disk, not a separate volume. Growing either grows both."
    fi
  fi

  mark "MOUNT=${mp}"
  mark "FS=${PLAN_FS} type=${PLAN_FSTYPE}"
  mark "DISK=${PLAN_DISK}"
  [ -n "$PLAN_PART" ]  && mark "PART=${PLAN_PART}"
  [ -n "$PLAN_CRYPT" ] && mark "LUKS=${PLAN_CRYPT}"
  [ -n "$PLAN_LVM" ]   && mark "LVM=${PLAN_LVM}"

  if [ -z "$PLAN_PART" ]; then
    # Whole-disk filesystem or whole-disk PV: nothing to repartition, the
    # layers above just need to be told the disk is bigger.
    mark "PARTITION=whole-disk"
    PLAN_FREE=0
    say "The disk carries no partition table; only the layers above it need growing."
    return 0
  fi

  PLAN_PARTNUM="${PLAN_PART##*[!0-9]}"
  if [ -z "$PLAN_PARTNUM" ]; then
    fail no-partition-number "Could not read a partition number from ${PLAN_PART}."
    return 1
  fi

  # Every tool this stack will need, checked now rather than discovered
  # half-way through. 02-prepare-os installs cloud-guest-utils, but it only
  # warns when a package fails, so an appliance can end up without growpart and
  # nothing would say so until a resize stopped in the middle with "command not
  # found" and a partition table already rewritten.
  local missing=""
  command -v "$GROWPART" >/dev/null 2>&1 || missing="${missing} growpart (cloud-guest-utils)"
  case "$PLAN_FSTYPE" in
    ext2|ext3|ext4) command -v "$RESIZE2FS" >/dev/null 2>&1 || missing="${missing} resize2fs (e2fsprogs)" ;;
    xfs)            command -v "$XFS_GROWFS" >/dev/null 2>&1 || missing="${missing} xfs_growfs (xfsprogs)" ;;
  esac
  if [ -n "$PLAN_LVM" ]; then
    command -v "$PVRESIZE" >/dev/null 2>&1 || missing="${missing} pvresize (lvm2)"
    command -v "$LVEXTEND" >/dev/null 2>&1 || missing="${missing} lvextend (lvm2)"
  fi
  if [ -n "$PLAN_CRYPT" ]; then
    command -v "$CRYPTSETUP" >/dev/null 2>&1 || missing="${missing} cryptsetup (cryptsetup)"
  fi
  if [ -n "$missing" ]; then
    fail missing-tools \
      "This appliance is missing the tools needed to grow ${mp}:${missing}. Install the packages named in brackets, then try again. Nothing has been changed."
    return 1
  fi

  local tail last_num free trc
  tail=$(disk_tail "$PLAN_DISK"); trc=$?
  if [ "$trc" -eq 2 ]; then
    fail not-root "Reading the partition table on ${PLAN_DISK} needs root."
    return 1
  fi
  if [ "$trc" -ne 0 ]; then
    fail unreadable-table "Could not read the partition table on ${PLAN_DISK}."
    return 1
  fi
  last_num=$(printf '%s' "$tail" | awk '{print $1}')
  free=$(printf '%s' "$tail" | awk '{print $4}')

  # The one guard that matters most. growpart moves the END of a partition
  # forward; if anything lives after it, that data is inside the new boundary.
  if [ "$PLAN_PARTNUM" != "$last_num" ]; then
    # The two-disk layout puts a 256 MiB replication meta partition at the very
    # END of the data disk (mkpart drbd-meta -256MiB 100%), so the mail data
    # partition is never the last one there and new space from the hypervisor
    # lands after the meta partition rather than after the data. Saying only
    # "something is after it" would leave the operator guessing at their own
    # appliance's layout, so name it.
    local tail_name
    tail_name=$(partition_name "$PLAN_DISK" "$last_num")
    if [ "$tail_name" = "drbd-meta" ]; then
      fail meta-partition-in-the-way \
        "${PLAN_PART} holds the mail data, and the replication meta partition (${PLAN_DISK}${last_num}) sits at the end of this disk behind it. New space added to this disk lands after that meta partition, so it cannot be given to the mail data without moving it, which is not something to do unattended on a live mail server. Add the space to the system disk instead, or ask KIN to restructure this disk during a maintenance window."
      return 1
    fi
    fail not-last-partition \
      "${PLAN_PART} is partition ${PLAN_PARTNUM} and partition ${last_num} sits after it. Growing it would run into that partition."
    # A generic refusal here is true and useless. On the standard mail-disk
    # layout the blocker is always the same 256 MiB partition reserved for a
    # second server, and an operator who is told which partition and why can
    # act on it.
    report_reserved_blocker "$PLAN_DISK" "$PLAN_PARTNUM" "$last_num"
    return 1
  fi

  PLAN_FREE="$free"
  mark "FREE_BYTES=${PLAN_FREE}"
  if [ "$PLAN_FREE" -lt "$MIN_GROW_BYTES" ]; then
    mark "NOTHING_TO_DO=1"
    say "There is no unused space after ${PLAN_PART}. Enlarge the disk in the hypervisor first, then run this again."
    return 1
  fi
  say "About $(( PLAN_FREE / 1024 / 1024 )) MiB of unused space follows ${PLAN_PART} and can be added to ${mp}."
  return 0
}

# --- apply -------------------------------------------------------------------

run_step() {
  local what="$1"; shift
  say "==> ${what}"
  if [ "${KIN_GROW_DRY_RUN:-0}" = "1" ]; then
    say "    (dry run) $*"
    return 0
  fi
  "$@"
}

# --- the reserved partition --------------------------------------------------

# Byte size and end offset of one partition, from the table we already read.
part_field() {
  local disk="$1" num="$2" field="$3" out line
  out=$("$PARTED" -sm "$disk" unit B print 2>/dev/null) || return 1
  while IFS= read -r line; do
    case "$line" in "${num}:"*) ;; *) continue ;; esac
    printf '%s' "$line" | cut -d: -f"$field" | tr -d 'B'
    return 0
  done <<EOF
$out
EOF
  return 1
}

# Is <part> the small, empty, never-used partition the installer reserved for a
# second server? Prints a reason on stdout when it is NOT, so the caller can
# say precisely what stopped it.
#
# Every check here answers "could this partition possibly contain something
# somebody needs?" and the answer has to be no, on every one of them.
reclaimable_reserved() {
  local part="$1" disk="$2" num="$3" size sig holders base
  base=$(basename "$part")

  size=$(part_field "$disk" "$num" 4) || { printf 'its size could not be read'; return 1; }
  case "$size" in ''|*[!0-9]*) printf 'its size could not be read'; return 1 ;; esac
  if [ "$size" -gt "$RECLAIM_MAX_BYTES" ]; then
    printf 'it is %s MiB, far larger than a reserved metadata partition' "$((size / 1024 / 1024))"
    return 1
  fi

  # A filesystem, an LVM member, a DRBD superblock: anything blkid recognises
  # means somebody is using it.
  sig=$("$BLKID" -p -o value -s TYPE "$part" 2>/dev/null || true)
  if [ -n "$(printf '%s' "$sig" | tr -d '[:space:]')" ]; then
    printf 'it carries a %s signature' "$sig"
    return 1
  fi
  sig=$("$WIPEFS" -n "$part" 2>/dev/null || true)
  # wipefs prints a header even when it finds nothing; a data line names an
  # offset, so look for one.
  if printf '%s\n' "$sig" | grep -qE '^0x[0-9a-fA-F]+'; then
    printf 'wipefs still sees a signature on it'
    return 1
  fi

  if "$FINDMNT" -rno TARGET --source "$part" >/dev/null 2>&1; then
    printf 'it is mounted'
    return 1
  fi

  holders=$(ls "${SYSFS}/class/block/${base}/holders" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$holders" ]; then
    printf 'something is stacked on top of it'
    return 1
  fi

  if grep -qs -- "$part" "${ETC_ROOT}/fstab" 2>/dev/null; then
    printf 'it is named in /etc/fstab'
    return 1
  fi
  if grep -qs -- "$part" "${ETC_ROOT}/crypttab" 2>/dev/null; then
    printf 'it is named in /etc/crypttab'
    return 1
  fi

  # And this host must have no DRBD at all. A reserved partition on a machine
  # that is actually replicating is not reserved, it is in use.
  if [ -e "${PROC_ROOT}/drbd" ]; then
    printf 'this host has DRBD loaded'
    return 1
  fi
  if [ -d "${ETC_ROOT}/drbd.d" ] && ls "${ETC_ROOT}/drbd.d"/*.res >/dev/null 2>&1; then
    printf 'this host has a DRBD resource configured'
    return 1
  fi

  return 0
}

# Called from grow_plan when the only thing in the way is that reserved
# partition. Says so in words, and marks it for the console.
report_reserved_blocker() {
  local disk="$1" datanum="$2" lastnum="$3"
  local part reason
  part=$(partition_device "$disk" "$lastnum") || part=""
  [ -n "$part" ] || return 0
  # Only the partition immediately after ours can be reclaimed; anything
  # further out means the layout is not the one this understands.
  if [ "$lastnum" -ne $((datanum + 1)) ]; then
    return 0
  fi
  if reason=$(reclaimable_reserved "$part" "$disk" "$lastnum"); then
    mark "RECLAIMABLE_RESERVED=${part}"
    say ""
    say "${part} is the small partition the installer reserved for a future second"
    say "server. It has never been used, and because it sits after ${PLAN_PART} it is"
    say "what stops this disk from growing. Freeing it would let ${PLAN_MOUNT} take the"
    say "space you added."
    say ""
    say "Building an HA pair later would then need a small separate disk."
  else
    mark "RESERVED_NOT_RECLAIMABLE=${part}"
    say ""
    say "${part} sits after ${PLAN_PART} and cannot be freed automatically, because"
    say "${reason}."
  fi
  return 0
}

# The one deletion this script will perform, and only when asked in words.
reclaim_reserved() {
  local disk="$1" datanum="$2" lastnum="$3" part reason
  part=$(partition_device "$disk" "$lastnum") || part=""
  if [ -z "$part" ]; then
    fail reclaim-no-partition "Could not name the partition to free on ${disk}."
    return 1
  fi
  if [ "$lastnum" -ne $((datanum + 1)) ]; then
    fail reclaim-not-adjacent       "Partition ${lastnum} is not immediately after ${datanum}; this layout is not one to change unattended."
    return 1
  fi
  if ! reason=$(reclaimable_reserved "$part" "$disk" "$lastnum"); then
    fail reclaim-in-use "Refusing to free ${part}: ${reason}."
    return 1
  fi
  say "==> freeing the reserved partition ${part} (${lastnum})"
  if [ "${KIN_GROW_DRY_RUN:-0}" = "1" ]; then
    say "    (dry run) ${PARTED} -s ${disk} rm ${lastnum}"
    return 0
  fi
  if ! "$PARTED" -s "$disk" rm "$lastnum" >/dev/null 2>&1; then
    fail reclaim-failed "parted would not remove partition ${lastnum} from ${disk}."
    return 1
  fi
  "$PARTX" -u "$disk" >/dev/null 2>&1 || true
  ok_line "reserved partition freed; ${PLAN_PART} can now extend"
  return 0
}

ok_line() { printf 'KIN_GROW_OK %s\n' "$*"; }

# Plan first so PLAN_DISK and PLAN_PARTNUM are known, then free the blocker.
# grow_plan returns non-zero when there is nothing to do, which is exactly the
# state we are here to change, so its exit code is not the decision.
reclaim_preflight() {
  local mp="$1" tail last_num
  grow_plan "$mp" >/dev/null 2>&1 || true
  if [ -z "${PLAN_DISK:-}" ] || [ -z "${PLAN_PARTNUM:-}" ]; then
    fail reclaim-no-plan "Could not work out the layout of ${mp} well enough to free anything."
    return 1
  fi
  tail=$(disk_tail "$PLAN_DISK") || {
    fail reclaim-no-table "Could not read the partition table on ${PLAN_DISK}."
    return 1
  }
  last_num=$(printf '%s' "$tail" | cut -d' ' -f1)
  case "$last_num" in ''|*[!0-9]*) fail reclaim-no-table "Could not read the partition table on ${PLAN_DISK}."; return 1 ;; esac
  if [ "$last_num" = "$PLAN_PARTNUM" ]; then
    say "Nothing is in the way: ${PLAN_PART} is already the last partition."
    return 0
  fi
  reclaim_reserved "$PLAN_DISK" "$PLAN_PARTNUM" "$last_num" || return 1
  return 0
}

grow_apply() {
  local mp="$1" want_reclaim="${2:-0}" rc=0

  # Reclaiming has to happen BEFORE planning, because planning is what decides
  # there is no room, and the reserved partition is the reason there is none.
  if [ "$want_reclaim" = "1" ]; then
    if ! reclaim_preflight "$mp"; then
      return 1
    fi
  fi

  grow_plan "$mp" || return 1

  # The rescan already happened, inside grow_plan, before the measuring. It
  # used to be here - after the measuring - which made it useless: PLAN_FREE
  # was computed from the pre-rescan size and the growpart below was then
  # skipped for having nothing to do.

  if [ -n "$PLAN_PART" ] && [ "$PLAN_FREE" -ge "$MIN_GROW_BYTES" ]; then
    # growpart exits 1 and prints NOCHANGE when there is nothing to do, which
    # is success for our purposes and must not abort the run.
    local out
    out=$(run_step "growing ${PLAN_PART}" "$GROWPART" "$PLAN_DISK" "$PLAN_PARTNUM" 2>&1)
    rc=$?
    say "$out"
    if [ "$rc" -ne 0 ] && ! printf '%s' "$out" | grep -qi 'NOCHANGE'; then
      fail growpart-failed "Could not grow ${PLAN_PART} (exit ${rc}). Nothing after this step ran."
      return 1
    fi
    run_step "re-reading the partition table" "$PARTX" -u "$PLAN_DISK" >/dev/null 2>&1 || true
  fi

  if [ -n "$PLAN_CRYPT" ]; then
    # cryptsetup needs the volume key to resize, and where it gets one decides
    # whether this works or hangs.
    #
    # With the key in the kernel keyring - the usual state after an unlock -
    # `cryptsetup resize <name>` needs nothing further. Without it, cryptsetup
    # PROMPTS, and a prompt inside a console stream is not a failure the
    # operator sees: it is a spinner that never stops until the helper times
    # out. That is what the loop-device test found.
    #
    # So: use the appliance's own keyfile when it is there, and pin stdin to
    # /dev/null either way, so a prompt becomes an immediate, readable failure
    # instead of a hang.
    local crypt_name luks_key
    crypt_name=$(basename "$PLAN_CRYPT")
    luks_key="${KIN_LUKS_KEYFILE:-${ETC_ROOT}/kin-mail/luks-keyfile}"
    if [ -r "$luks_key" ]; then
      say "==> resizing the encrypted container (using the appliance keyfile)"
      if [ "${KIN_GROW_DRY_RUN:-0}" = "1" ]; then
        say "    (dry run) ${CRYPTSETUP} resize --key-file ${luks_key} ${crypt_name}"
      elif ! "$CRYPTSETUP" resize --batch-mode --key-file "$luks_key" "$crypt_name" </dev/null; then
        fail luks-failed \
          "Could not resize the LUKS container on ${PLAN_CRYPT}, even with the appliance keyfile. The partition is already bigger; the filesystem was not touched."
        return 1
      fi
    else
      say "==> resizing the encrypted container"
      if [ "${KIN_GROW_DRY_RUN:-0}" = "1" ]; then
        say "    (dry run) ${CRYPTSETUP} resize ${crypt_name}"
      elif ! "$CRYPTSETUP" resize --batch-mode "$crypt_name" </dev/null; then
        fail luks-failed \
          "Could not resize the LUKS container on ${PLAN_CRYPT}. cryptsetup wanted a key that is not in the kernel keyring and there is no keyfile at ${luks_key}. The partition is already bigger; the filesystem was not touched."
        return 1
      fi
    fi
  fi

  if [ -n "$PLAN_LVM" ]; then
    local pv="${PLAN_CRYPT:-${PLAN_PART:-$PLAN_DISK}}"
    run_step "growing the physical volume" "$PVRESIZE" "$pv" || {
      fail pvresize-failed "Could not grow the physical volume on ${pv}."
      return 1
    }
    # lvextend exits non-zero both when there is genuinely nothing to add and
    # when it could not run at all. Treating every failure as "nothing to add"
    # is what made a wrong device path silent: the extend never happened, the
    # filesystem step found no new space, and the whole run still reported
    # success. Only the specific "already this size" case is tolerated.
    local lv_out lv_rc
    lv_out=$(run_step "extending the logical volume" "$LVEXTEND" -l +100%FREE "$PLAN_LVM" 2>&1)
    lv_rc=$?
    say "$lv_out"
    if [ "$lv_rc" -ne 0 ]; then
      if printf '%s' "$lv_out" | grep -qiE 'matches existing size|already .* size|New size .* matches'; then
        say "    (the volume is already using every free extent)"
      else
        fail lvextend-failed \
          "Could not extend the logical volume ${PLAN_LVM} (exit ${lv_rc}). The filesystem was left alone rather than resized against a volume that did not grow."
        return 1
      fi
    fi
  fi

  case "$PLAN_FSTYPE" in
    ext2|ext3|ext4)
      run_step "growing the ${PLAN_FSTYPE} filesystem" "$RESIZE2FS" "$PLAN_FS" || {
        fail resize2fs-failed "Could not grow the filesystem on ${PLAN_FS}."
        return 1
      }
      ;;
    xfs)
      run_step "growing the xfs filesystem" "$XFS_GROWFS" "$mp" || {
        fail xfs-failed "Could not grow the filesystem mounted at ${mp}."
        return 1
      }
      ;;
  esac

  mark "DONE mount=${mp}"
  say "${mp} now uses the whole disk."
  return 0
}

# --- entry point -------------------------------------------------------------

main() {
  local action="${1:-}" mp="${2:-}" flag="${3:-}" reclaim=0
  case "$flag" in
    ''|--reclaim-reserved) ;;
    *)
      fail unknown-option "No such option '${flag}'. The only one is --reclaim-reserved."
      return 2
      ;;
  esac
  [ "$flag" = "--reclaim-reserved" ] && reclaim=1
  if [ "$reclaim" = "1" ] && [ "$action" != "apply" ]; then
    fail reclaim-needs-apply "--reclaim-reserved only makes sense with apply."
    return 2
  fi
  case "$action" in
    plan|apply) ;;
    *)
      say "usage: $(basename "$0") plan|apply <mountpoint>"
      return 2
      ;;
  esac
  if [ -z "$mp" ]; then
    say "usage: $(basename "$0") ${action} <mountpoint>"
    return 2
  fi
  # Both actions need root: reading a partition table is as privileged as
  # writing one. Saying so here beats failing later with a reason that sounds
  # like the disk is broken.
  if [ "${KIN_GROW_ALLOW_NONROOT:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    fail not-root "Reading and growing a disk both need root."
    return 1
  fi
  if [ "$action" = "plan" ]; then
    grow_plan "$mp"
    return $?
  fi
  grow_apply "$mp" "$reclaim"
  return $?
}

# Sourced by the test suite; executed by the privhelper.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
  exit $?
fi
