#!/usr/bin/env bash
# =============================================================================
# Grow a filesystem into space that was added to its disk by the hypervisor.
#
#   grow-disk.sh plan  <mountpoint>
#   grow-disk.sh apply <mountpoint>
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
#   create, delete, reorder or move a partition
#   touch a partition that is not the last one on its disk
#   touch a device that DRBD or MD is using
#   act on any mountpoint other than the ones named in ALLOWED_MOUNTS
#
# Every one of those refusals is a case where the safe outcome is an operator
# reading an explanation, and the unsafe outcome is a mail server that has lost
# its data. There is no force flag on purpose.
#
# Injection points, all for tests. Nothing here should ever be set in
# production; the defaults are the real tools.
#   KIN_GROW_FINDMNT KIN_GROW_LSBLK KIN_GROW_PARTED KIN_GROW_GROWPART
#   KIN_GROW_PARTX KIN_GROW_RESIZE2FS KIN_GROW_XFS_GROWFS KIN_GROW_PVRESIZE
#   KIN_GROW_LVEXTEND KIN_GROW_CRYPTSETUP KIN_SYSFS_ROOT KIN_ZIMBRA_DIR
#   KIN_GROW_ALLOW_NONROOT   skip the euid check (tests only)
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
#   ubuntu--vg-ubuntu--lv lvm
#   sda3                  part
#   sda                   disk
device_chain() { "$LSBLK" -nsro NAME,TYPE "$1" 2>/dev/null; }

chain_types() { awk '{print $2}'; }

# Last partition on a disk, and the bytes left after it.
# Prints "PARTNUM PART_END DISK_SIZE FREE_BYTES", or nothing if unreadable.
# Name of a numbered partition, from parted's machine output
# (num:start:end:size:fstype:name:flags). Empty when unnamed or unreadable.
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

grow_plan() {
  local mp="$1"
  PLAN_MOUNT="$mp"; PLAN_FS=""; PLAN_FSTYPE=""; PLAN_DISK=""; PLAN_PART=""
  PLAN_PARTNUM=""; PLAN_CRYPT=""; PLAN_LVM=""; PLAN_FREE=0

  if ! allowed_mount "$mp"; then
    fail not-allowed "Only / and ${ZIMBRA_DIR} can be grown from here."
    return 1
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
  if printf '%s\n' "$chain" | grep -qiE '(^| )(drbd[0-9]*|md[0-9]+|raid[0-9]*)( |$)'; then
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

  PLAN_DISK="/dev/$(printf '%s\n' "$chain" | awk '$2=="disk"||$2=="loop"{print $1}' | head -1)"
  if [ "$parts" -eq 1 ]; then
    PLAN_PART="/dev/$(printf '%s\n' "$chain" | awk '$2=="part"{print $1}' | head -1)"
  fi
  printf '%s\n' "$types" | grep -q '^crypt$' && \
    PLAN_CRYPT="/dev/$(printf '%s\n' "$chain" | awk '$2=="crypt"{print $1}' | head -1)"
  printf '%s\n' "$types" | grep -q '^lvm$' && \
    PLAN_LVM="/dev/$(printf '%s\n' "$chain" | awk '$2=="lvm"{print $1}' | head -1)"

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

grow_apply() {
  local mp="$1" rc=0
  grow_plan "$mp" || return 1

  # Best effort: a disk enlarged while the guest was running still reports its
  # old size until something asks the controller again. A failure here is not
  # fatal, because the hypervisor may already have notified the kernel.
  local base sysdev
  base=$(basename "$PLAN_DISK")
  sysdev="${SYSFS}/class/block/${base}/device/rescan"
  if [ -w "$sysdev" ]; then
    say "==> rescanning ${PLAN_DISK}"
    if [ "${KIN_GROW_DRY_RUN:-0}" = "1" ]; then
      # A dry run that pokes the kernel is not a dry run. This is harmless in
      # itself, but the whole value of the mode is being able to say that
      # nothing at all happened.
      say "    (dry run) echo 1 > ${sysdev}"
    else
      printf '1' > "$sysdev" 2>/dev/null || say "    (rescan not accepted, continuing)"
    fi
  fi

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
    run_step "resizing the encrypted container" "$CRYPTSETUP" resize "$(basename "$PLAN_CRYPT")" || {
      fail luks-failed "Could not resize the LUKS container on ${PLAN_CRYPT}."
      return 1
    }
  fi

  if [ -n "$PLAN_LVM" ]; then
    local pv="${PLAN_CRYPT:-${PLAN_PART:-$PLAN_DISK}}"
    run_step "growing the physical volume" "$PVRESIZE" "$pv" || {
      fail pvresize-failed "Could not grow the physical volume on ${pv}."
      return 1
    }
    run_step "extending the logical volume" "$LVEXTEND" -l +100%FREE "$PLAN_LVM" || {
      # lvextend also exits non-zero when there is nothing free to add, which
      # is not a failure worth stopping on: the filesystem step below is still
      # worth running.
      say "    (no free extents to add, continuing to the filesystem)"
    }
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
  local action="${1:-}" mp="${2:-}"
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
  grow_apply "$mp"
  return $?
}

# Sourced by the test suite; executed by the privhelper.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
  exit $?
fi
