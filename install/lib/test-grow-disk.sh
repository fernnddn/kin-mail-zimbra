#!/usr/bin/env bash
# Growing a filesystem is the most destructive button in this product.
#
# It moves a partition boundary on a live mail server. Done right it adds space
# nobody has to think about again; done wrong it eats whatever was after that
# boundary. So the tests that matter most here are the refusals, and every one
# of them is a case where the safe answer is an operator reading a sentence and
# the unsafe answer is a customer's mail.
#
# Every external tool is stubbed. Nothing here touches a real disk, and the
# stubs record what they were called with, so the tests assert on the exact
# commands that would have run.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

LIB="$(pwd)/grow-disk.sh"
[ -f "$LIB" ] || { printf 'FAIL grow-disk.sh not found\n'; exit 1; }

WORK="$(mktemp -d)" || exit 1
trap 'rm -rf "$WORK"' EXIT
BIN="${WORK}/bin"; mkdir -p "$BIN"
CALLS="${WORK}/calls"

# --- stubs -------------------------------------------------------------------
# Each reads a fixture from the environment and appends its argv to CALLS, so a
# test can assert both what was decided and what would have been executed.
make_stub() {
  local name="$1" body="$2"
  cat > "${BIN}/${name}" <<STUB
#!/usr/bin/env bash
printf '%s %s\n' "${name}" "\$*" >> "\$KIN_TEST_CALLS"
${body}
STUB
  chmod +x "${BIN}/${name}"
}

# The library asks about "/" as well as its target, to work out whether a
# directory is on the root filesystem or a volume of its own. A stub that
# answers identically for every target makes those two cases indistinguishable,
# so it answers per target.
make_stub findmnt '
target="${@: -1}"
case "$*" in
  *FSTYPE*) printf "%s\n" "${STUB_FSTYPE:-ext4}" ;;
  *SOURCE*)
    if [ "$target" = "/" ]; then
      printf "%s\n" "${STUB_ROOT_SOURCE:-${STUB_SOURCE:-/dev/sda2}}"
    else
      printf "%s\n" "${STUB_SOURCE:-/dev/sda2}"
    fi
    ;;
esac
exit 0'

make_stub lsblk 'printf "%s\n" "${STUB_CHAIN:-}"; exit 0'
make_stub parted 'printf "%s\n" "${STUB_PARTED:-}"; exit ${STUB_PARTED_RC:-0}'
make_stub growpart 'printf "%s\n" "${STUB_GROWPART_OUT:-CHANGED}"; exit ${STUB_GROWPART_RC:-0}'
make_stub partx 'exit 0'
make_stub resize2fs 'exit ${STUB_RESIZE2FS_RC:-0}'
make_stub xfs_growfs 'exit 0'
make_stub pvresize 'exit 0'
make_stub lvextend 'printf "%s\n" "${STUB_LVEXTEND_OUT:-Size of logical volume changed}"; exit ${STUB_LVEXTEND_RC:-0}'
make_stub cryptsetup 'exit 0'

export KIN_GROW_FINDMNT="${BIN}/findmnt"
export KIN_GROW_LSBLK="${BIN}/lsblk"
export KIN_GROW_PARTED="${BIN}/parted"
export KIN_GROW_GROWPART="${BIN}/growpart"
export KIN_GROW_PARTX="${BIN}/partx"
export KIN_GROW_RESIZE2FS="${BIN}/resize2fs"
export KIN_GROW_XFS_GROWFS="${BIN}/xfs_growfs"
export KIN_GROW_PVRESIZE="${BIN}/pvresize"
export KIN_GROW_LVEXTEND="${BIN}/lvextend"
export KIN_GROW_CRYPTSETUP="${BIN}/cryptsetup"
export KIN_GROW_ALLOW_NONROOT=1
export KIN_SYSFS_ROOT="${WORK}/sys"
export KIN_TEST_CALLS="$CALLS"

# A 100 GB disk whose single partition ends at 50 GB: 50 GB of free tail.
PARTED_ROOM='BYT;
/dev/sda:107374182400B:scsi:512:512:gpt:VMware Virtual disk;
1:1048576B:2097151B:1048576B::bios_grub;
2:2097152B:53687091200B:53686042624B:ext4::;'

# The same disk with a THIRD partition after the one being grown.
PARTED_TRAPPED='BYT;
/dev/sda:107374182400B:scsi:512:512:gpt:VMware Virtual disk;
1:1048576B:2097151B:1048576B::bios_grub;
2:2097152B:53687091200B:53686042624B:ext4::;
3:53687091201B:107374182399B:53686042624B:ext4::;'

# Nothing left after the partition.
PARTED_FULL='BYT;
/dev/sda:107374182400B:scsi:512:512:gpt:VMware Virtual disk;
2:2097152B:107374182399B:107372085248B:ext4::;'

# A 100 GB disk whose partition 3 is last and ends at 50 GB. Matches CHAIN_LVM,
# which names sda3: a fixture that disagrees with its chain tests the refusal
# path by accident and proves nothing about the path it claims to cover.
PARTED_ROOM_P3='BYT;
/dev/sda:107374182400B:scsi:512:512:gpt:VMware Virtual disk;
1:1048576B:2097151B:1048576B::bios_grub;
2:2097152B:2149580287B:2147483136B:ext4::;
3:2149580288B:53687091200B:51537510912B::;'

# Full device paths, as lsblk's PATH column reports them. The NAME column
# gives "ubuntu--vg-ubuntu--lv" for a device-mapper node, and /dev/ prefixed
# onto that is a path that does not exist; using it meant lvextend and pvresize
# were handed something unopenable on every LVM appliance.
CHAIN_PLAIN='/dev/sda2 part
/dev/sda disk'
CHAIN_LVM='/dev/mapper/ubuntu--vg-ubuntu--lv lvm
/dev/sda3 part
/dev/sda disk'
CHAIN_LUKS='/dev/mapper/kin--vg-data lvm
/dev/mapper/kin_crypt crypt
/dev/sdb1 part
/dev/sdb disk'
CHAIN_DRBD='/dev/drbd0 disk
/dev/sdb1 part
/dev/sdb disk'

run() {  # run <action> <mountpoint>; output on stdout, rc in RC
  : > "$CALLS"
  OUT=$(bash "$LIB" "$1" "$2" 2>&1); RC=$?
}

has() { printf '%s' "$OUT" | grep -q "$1"; }
called() { grep -q "^$1" "$CALLS"; }
# called() anchors at the start of a recorded argv line. Some assertions care
# about an argument in the middle of one, which is a different question.
called_with() { grep -q -- "$1" "$CALLS"; }

# --- refusals ----------------------------------------------------------------

STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" run plan /home
if [ "$RC" -ne 0 ] && has "reason=not-allowed"; then
  pass "refuses a mountpoint that is not the system or mail disk"
else bad "grew an arbitrary mountpoint (rc=$RC)"; fi

STUB_FSTYPE=btrfs STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" run plan /
if [ "$RC" -ne 0 ] && has "reason=unsupported-fs"; then
  pass "refuses a filesystem it cannot grow"
else bad "did not refuse an unsupported filesystem"; fi

# THE guard. Partition 2 with partition 3 after it: growing 2 would run into 3.
STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_TRAPPED" run plan /
if [ "$RC" -ne 0 ] && has "reason=not-last-partition"; then
  pass "refuses to grow a partition that has another partition after it"
else bad "WOULD HAVE GROWN INTO THE NEXT PARTITION (rc=$RC): $OUT"; fi

STUB_SOURCE=/dev/drbd0 STUB_CHAIN="$CHAIN_DRBD" STUB_PARTED="$PARTED_ROOM" run plan /opt/zimbra
if [ "$RC" -ne 0 ] && has "reason=replicated"; then
  pass "refuses replicated storage, which is a cluster operation"
else bad "did not refuse DRBD-backed storage"; fi

STUB_CHAIN='/dev/sda2 part
/dev/sda disk
/dev/sdb1 part
/dev/sdb disk' STUB_PARTED="$PARTED_ROOM" run plan /
if [ "$RC" -ne 0 ] && (has "reason=many-disks" || has "reason=many-partitions"); then
  pass "refuses a filesystem spanning more than one disk"
else bad "did not refuse a multi-disk filesystem"; fi

STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_FULL" run plan /
if [ "$RC" -ne 0 ] && has "NOTHING_TO_DO=1"; then
  pass "reports nothing to do when the disk was never enlarged"
else bad "did not detect a full disk"; fi

STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED_RC=1 STUB_PARTED="" run plan /
if [ "$RC" -ne 0 ] && has "reason=unreadable-table"; then
  pass "refuses when the partition table cannot be read"
else bad "did not refuse an unreadable partition table"; fi

# --- the happy paths ---------------------------------------------------------

STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" run plan /
if [ "$RC" -eq 0 ] && has "KIN_GROW_FREE_BYTES=" && has "DISK=/dev/sda"; then
  pass "plans a plain ext4 partition with free space after it"
else bad "did not plan the simple case (rc=$RC): $OUT"; fi

if printf '%s' "$OUT" | grep -q "50176 MiB\|MiB of unused space"; then
  pass "says how much space it found, in words an operator can check"
else bad "did not report the free space in human terms"; fi

STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" run apply /
if [ "$RC" -eq 0 ] && called "growpart /dev/sda 2" && called "resize2fs /dev/sda2"; then
  pass "plain ext4: grows the partition then the filesystem"
else bad "plain apply did not run the expected commands (rc=$RC)"; fi

if [ "$(grep -n '^growpart' "$CALLS" | cut -d: -f1)" -lt "$(grep -n '^resize2fs' "$CALLS" | cut -d: -f1)" ]; then
  pass "the partition is grown before the filesystem, never the other way"
else bad "filesystem was grown before the partition"; fi

STUB_SOURCE=/dev/mapper/ubuntu--vg-ubuntu--lv STUB_CHAIN="$CHAIN_LVM" \
  STUB_PARTED="$PARTED_ROOM_P3" run apply /
if [ "$RC" -eq 0 ] && called "growpart /dev/sda 3" && called "pvresize /dev/sda3" \
   && called "lvextend -l +100%FREE" && called "resize2fs"; then
  pass "LVM: partition, physical volume, logical volume, then filesystem"
else bad "LVM apply missed a layer (rc=$RC)"; fi

STUB_SOURCE=/dev/mapper/kin--vg-data STUB_CHAIN="$CHAIN_LUKS" \
  STUB_PARTED='BYT;
/dev/sdb:107374182400B:scsi:512:512:gpt:disk;
1:1048576B:53687091200B:53686042624B::;' run apply /opt/zimbra
if [ "$RC" -eq 0 ] && called "cryptsetup resize" && called_with "kin_crypt" \
   && called "pvresize /dev/mapper/kin_crypt"; then
  pass "LUKS: the encrypted container is resized before the volume group"
else bad "LUKS apply did not resize the container (rc=$RC)"; fi

# cryptsetup PROMPTS for a key when it cannot find one, and a prompt inside a
# console stream is not an error the operator sees - it is a spinner that never
# stops until the helper times out. Found on a real LUKS device; stubs cannot
# show it, so the shape of the call is pinned instead.
if called_with "cryptsetup resize --batch-mode"; then
  pass "cryptsetup is run in batch mode, so it can never sit waiting for input"
else bad "cryptsetup resize is missing --batch-mode: $(grep cryptsetup "$CALLS" | head -1)"; fi

CODE_FOR_STDIN=$(sed 's/#.*$//' "$LIB")
if printf '%s' "$CODE_FOR_STDIN" | grep -q '\$CRYPTSETUP" resize.*</dev/null'; then
  pass "and its stdin is closed, so a prompt fails fast instead of hanging"
else bad "cryptsetup resize can still read from stdin"; fi

# The appliance keeps a keyfile; using it is what makes the mail disk growable
# at all when the volume key is not in the kernel keyring.
if printf '%s' "$CODE_FOR_STDIN" | grep -q 'luks-keyfile'; then
  pass "the appliance LUKS keyfile is used when the kernel keyring has no key"
else bad "the LUKS keyfile is never consulted"; fi

STUB_FSTYPE=xfs STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" \
  STUB_PARTED="$PARTED_ROOM" run apply /
if [ "$RC" -eq 0 ] && called "xfs_growfs /" && ! called "resize2fs"; then
  pass "xfs is grown with xfs_growfs, by mountpoint"
else bad "xfs apply used the wrong tool (rc=$RC)"; fi

# --- failure handling --------------------------------------------------------

STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  STUB_GROWPART_RC=1 STUB_GROWPART_OUT="NOCHANGE: partition 2 is size 1234. it cannot be grown" run apply /
if [ "$RC" -eq 0 ] && called "resize2fs"; then
  pass "growpart reporting NOCHANGE is not a failure and the filesystem still grows"
else bad "NOCHANGE aborted the run (rc=$RC)"; fi

STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  STUB_GROWPART_RC=1 STUB_GROWPART_OUT="FAILED: cannot read partition table" run apply /
if [ "$RC" -ne 0 ] && has "reason=growpart-failed" && ! called "resize2fs"; then
  pass "a real growpart failure stops before the filesystem is touched"
else bad "carried on to the filesystem after growpart failed (rc=$RC)"; fi

STUB_SOURCE=/dev/mapper/ubuntu--vg-ubuntu--lv STUB_CHAIN="$CHAIN_LVM" \
  STUB_PARTED="$PARTED_ROOM_P3" STUB_LVEXTEND_RC=5 \
  STUB_LVEXTEND_OUT="New size (25599 extents) matches existing size (25599 extents)" run apply /
if [ "$RC" -eq 0 ] && called "resize2fs"; then
  pass "lvextend saying the volume is already full size lets the filesystem grow"
else bad "a genuine no-op lvextend aborted the run (rc=$RC)"; fi

# The other half, and the one that made a wrong device path silent: lvextend
# failing for any OTHER reason must stop, not shrug and report success.
STUB_SOURCE=/dev/mapper/ubuntu--vg-ubuntu--lv STUB_CHAIN="$CHAIN_LVM" \
  STUB_PARTED="$PARTED_ROOM_P3" STUB_LVEXTEND_RC=5 \
  STUB_LVEXTEND_OUT="Failed to find logical volume" run apply /
if [ "$RC" -ne 0 ] && has "reason=lvextend-failed" && ! called "resize2fs"; then
  pass "an lvextend that really failed stops before the filesystem is touched"
else bad "a failed lvextend was swallowed and the run reported success (rc=$RC)"; fi

# The paths handed to the LVM tools must be the ones that exist on disk.
STUB_SOURCE=/dev/mapper/ubuntu--vg-ubuntu--lv STUB_CHAIN="$CHAIN_LVM" \
  STUB_PARTED="$PARTED_ROOM_P3" run apply /
if called "lvextend -l +100%FREE /dev/mapper/ubuntu--vg-ubuntu--lv"; then
  pass "lvextend is given the /dev/mapper path, not a pasted-together one"
else bad "lvextend got a bad path: $(grep '^lvextend' "$CALLS")"; fi

STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  STUB_RESIZE2FS_RC=1 run apply /
if [ "$RC" -ne 0 ] && has "reason=resize2fs-failed"; then
  pass "a filesystem that will not grow is reported, not swallowed"
else bad "resize2fs failure was not reported (rc=$RC)"; fi

# --- a missing tool is caught before anything is touched ---------------------
# 02-prepare-os installs cloud-guest-utils, but it only warns when a package
# fails, so an appliance can end up without growpart. Discovering that half-way
# through, with the partition table already rewritten, is the worst moment.
STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  KIN_GROW_GROWPART="${WORK}/definitely-not-installed" run plan /
if [ "$RC" -ne 0 ] && has "reason=missing-tools" && has "cloud-guest-utils"; then
  pass "a missing growpart is reported by name, with its package"
else bad "a missing growpart was not caught up front (rc=$RC)"; fi

STUB_FSTYPE=xfs STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  KIN_GROW_XFS_GROWFS="${WORK}/definitely-not-installed" run plan /
if [ "$RC" -ne 0 ] && has "xfsprogs"; then
  pass "an xfs appliance is told it needs xfsprogs"
else bad "a missing xfs_growfs was not caught (rc=$RC)"; fi

# LVM tools are only required when the stack actually has LVM in it.
STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  KIN_GROW_LVEXTEND="${WORK}/definitely-not-installed" run plan /
if [ "$RC" -eq 0 ]; then
  pass "a plain appliance is not asked to install lvm2 it does not need"
else bad "demanded LVM tools on a stack with no LVM (rc=$RC)"; fi

# --- the two-disk appliance layout -------------------------------------------
#
# prepare-zimbra-data-disk.sh partitions a spare disk as
#   mkpart zimbra-data 1MiB -256MiB
#   mkpart drbd-meta  -256MiB 100%
# so the mail data is partition 1 and a 256 MiB replication meta partition sits
# at the very END. New space from the hypervisor therefore lands behind the
# meta partition, not behind the data, and the data cannot be grown into it
# without moving the meta partition. Refusing is right. Refusing while naming
# what is in the way is the difference between an operator who knows what to do
# and one who thinks the feature is broken.
PARTED_TWO_DISK='BYT;
/dev/sdb:214748364800B:scsi:512:512:gpt:VMware Virtual disk;
1:1048576B:107105026047B:107103977472B:ext4:zimbra-data:;
2:107105026048B:107374182399B:268435456B::drbd-meta:;'

STUB_SOURCE=/dev/sdb1 STUB_ROOT_SOURCE=/dev/sda2 STUB_CHAIN='/dev/sdb1 part
/dev/sdb disk' STUB_PARTED="$PARTED_TWO_DISK" run plan /opt/zimbra
if [ "$RC" -ne 0 ] && has "reason=meta-partition-in-the-way"; then
  pass "two-disk layout: refuses the mail disk and names the meta partition"
else bad "two-disk layout: gave a generic refusal or worse (rc=$RC): $OUT"; fi
if printf '%s' "$OUT" | grep -q "system disk instead"; then
  pass "two-disk layout: tells the operator what they can actually do"
else bad "two-disk layout: refusal offers no way forward"; fi

# ...and the reclaim path has to be REACHABLE from here, because this is the
# layout every appliance actually has.
#
# The console's "free the reserved partition" button is driven entirely by the
# KIN_GROW_RECLAIMABLE_RESERVED marker, and only report_reserved_blocker prints
# it. This branch - the one that recognises the partition by name - used to
# return before calling it. So the generic "some partition is after yours" case
# offered the operator a way out and the standard KIN Mail case did not: a
# feature that is built, tested, and wired to the UI, and that no real
# deployment could reach.
#
# Asserted against the source because the marker needs a real block device to
# name (partition_device refuses a path that is not one), which the stubs here
# cannot provide. The loop-device test below proves the behaviour itself.
META_BRANCH=$(awk '/tail_name" = "drbd-meta"/,/^    fi$/' "$LIB")
if printf '%s' "$META_BRANCH" | grep -q 'report_reserved_blocker'; then
  pass "two-disk layout: the recognised-blocker branch still offers the reclaim"
else bad "two-disk layout: recognising the partition by name skips the reclaim offer"; fi

# ...and the same layout BEFORE the hypervisor has given it anything.
#
# This is what both live mail nodes looked like on 19 Sep 2026: the disk is
# exactly the size it was installed at, trailing free space is the 1 MiB of GPT
# tail, and the reserved partition is still in the way. The refusal is the same
# and must still be printed - but the OFFER must not be, because freeing the
# partition would delete it and release nothing. The console spends that offer
# on a button labelled "Free the reserved partition and extend", on the machine
# that holds every message.
PARTED_TWO_DISK_NO_ROOM='BYT;
/dev/sdb:107374182400B:scsi:512:512:gpt:VMware Virtual disk;
1:1048576B:107105746943B:107104698368B:ext4:zimbra-data:;
2:107105746944B:107373133823B:267386880B::drbd-meta:;'

STUB_SOURCE=/dev/sdb1 STUB_ROOT_SOURCE=/dev/sda2 STUB_CHAIN='/dev/sdb1 part
/dev/sdb disk' STUB_PARTED="$PARTED_TWO_DISK_NO_ROOM" run plan /opt/zimbra
if [ "$RC" -ne 0 ] && has "reason=meta-partition-in-the-way"; then
  pass "no room yet: still refuses, and still names the meta partition"
else bad "no room yet: lost the refusal (rc=$RC): $OUT"; fi
if has "KIN_GROW_RECLAIMABLE_RESERVED"; then
  bad "no room yet: OFFERED TO DELETE A PARTITION THAT WOULD RELEASE NOTHING"
else
  pass "no room yet: does not offer to free the reserved partition"
fi
if printf '%s' "$OUT" | grep -q "Enlarge the disk in the hypervisor first"; then
  pass "no room yet: says which step is actually missing"
else bad "no room yet: refuses without naming the hypervisor step"; fi

# "Nothing to do" has two causes that call for opposite actions.
#
# A disk nobody enlarged needs the hypervisor. A disk that was enlarged and
# already collected needs nothing at all - and telling that operator to "enlarge
# the disk in the hypervisor first" sends them back to add space they added
# yesterday. Seen on the live edge: / was 150 GB, entirely in use, and the
# message still asked for more (20 Sep 2026).
STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_FULL" run plan /
if has "KIN_GROW_ALREADY_WHOLE_DISK=1" && printf '%s' "$OUT" | grep -q "already uses the whole disk"; then
  pass "a disk already taken in full says so, instead of asking for more"
else bad "a full disk still reads as never enlarged: $OUT"; fi
if printf '%s' "$OUT" | grep -q "enlarge /dev/sda in the hypervisor"; then
  pass "and still says how to make it bigger"
else bad "no way forward offered for a disk that is genuinely full"; fi

# A disk with a partition that stops well short of the end is the other case:
# space was never added, and the hypervisor really is the missing step.
PARTED_SHORT='BYT;
/dev/sda:107374182400B:scsi:512:512:gpt:VMware Virtual disk;
1:1048576B:2097151B:1048576B::bios_grub;
2:2097152B:53687091200B:53686042624B:ext4::;'
STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_SHORT" \
  KIN_GROW_MIN_BYTES=999999999999 run plan /
if has "KIN_GROW_NOTHING_TO_DO=1" && ! has "ALREADY_WHOLE_DISK"; then
  pass "a half-empty disk is not called full"
else bad "a disk with room was reported as already whole: $OUT"; fi

# The partition named in the message has to be a device that exists.
#
# /dev/sdb + "2" reads as /dev/sdb2 and looked right for years, because every
# appliance this has run on used sd*. On nvme and loop devices the same
# concatenation produces /dev/nvme0n12 and /dev/loop72 - paths that do not
# exist, sending an operator to look for a partition that is not there. The
# guards already resolved this properly; only the sentences did not.
PARTED_NVME_NO_ROOM='BYT;
/dev/nvme0n1:107374182400B:nvme:512:512:gpt:Samsung;
1:1048576B:107105746943B:107104698368B:ext4:zimbra-data:;
2:107105746944B:107373133823B:267386880B::drbd-meta:;'
STUB_SOURCE=/dev/nvme0n1p1 STUB_ROOT_SOURCE=/dev/sda2 STUB_CHAIN='/dev/nvme0n1p1 part
/dev/nvme0n1 disk' STUB_PARTED="$PARTED_NVME_NO_ROOM" run plan /opt/zimbra
if printf '%s' "$OUT" | grep -q "/dev/nvme0n12"; then
  bad "names a device that does not exist (/dev/nvme0n12)"
else
  pass "nvme: does not invent /dev/nvme0n12 in the refusal"
fi
if printf '%s' "$OUT" | grep -q "partition 2 on /dev/nvme0n1"; then
  pass "nvme: says which partition it means without guessing a path"
else
  bad "nvme: refusal does not identify the blocking partition at all: $OUT"
fi

# The generic blocker behaves the same way: a partition in the way and nothing
# to gain is not a reason to delete anything.
PARTED_TRAPPED_NO_ROOM='BYT;
/dev/sda:107374182400B:scsi:512:512:gpt:VMware Virtual disk;
2:1048576B:107105746943B:107104698368B:ext4::;
3:107105746944B:107373133823B:267386880B::spare:;'
STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_TRAPPED_NO_ROOM" run plan /
if [ "$RC" -ne 0 ] && has "reason=not-last-partition" && ! has "KIN_GROW_RECLAIMABLE_RESERVED"; then
  pass "generic blocker with no room: refuses and offers nothing"
else bad "generic blocker with no room: rc=$RC OUT=$OUT"; fi

# The system disk on that same appliance must still be growable, because that
# is where the space can safely go.
STUB_SOURCE=/dev/sda2 STUB_ROOT_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" \
  STUB_PARTED="$PARTED_ROOM" run plan /
if [ "$RC" -eq 0 ]; then
  pass "two-disk layout: the system disk is still growable"
else bad "two-disk layout: the system disk was refused too (rc=$RC)"; fi

# A data disk with NO meta partition (a plain second disk) grows normally.
STUB_SOURCE=/dev/sdb1 STUB_ROOT_SOURCE=/dev/sda2 STUB_CHAIN='/dev/sdb1 part
/dev/sdb disk' STUB_PARTED='BYT;
/dev/sdb:214748364800B:scsi:512:512:gpt:disk;
1:1048576B:107105026047B:107103977472B:ext4:zimbra-data:;' run plan /opt/zimbra
if [ "$RC" -eq 0 ] && has "FREE_BYTES="; then
  pass "a plain data disk with no meta partition grows normally"
else bad "a plain data disk was refused (rc=$RC)"; fi

# --- a dry run must genuinely change nothing ---------------------------------
# The mode exists so an operator, or this test suite, can see the exact command
# sequence without a disk being touched. The rescan wrote straight to sysfs and
# bypassed the guard, which made "dry run" not quite true.
: > "$CALLS"
FAKESYS="${WORK}/sys/class/block/sda/device"
mkdir -p "$FAKESYS"; : > "${FAKESYS}/rescan"
OUT=$(STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  KIN_GROW_DRY_RUN=1 bash "$LIB" apply / 2>&1); RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q "(dry run)"; then
  pass "a dry run reports the commands it would run"
else bad "dry run did not report its plan (rc=$RC)"; fi
if [ ! -s "${FAKESYS}/rescan" ]; then
  pass "a dry run does not even poke the kernel rescan"
else bad "dry run wrote to the sysfs rescan node"; fi
# It still reads: findmnt, lsblk and parted are how it works out what it would
# do, and a dry run that cannot plan is useless. What must not run is anything
# that changes the disk.
mutated=""
for tool in growpart resize2fs xfs_growfs pvresize lvextend cryptsetup partx; do
  grep -q "^${tool}" "$CALLS" && mutated="${mutated} ${tool}"
done
if [ -z "$mutated" ]; then
  pass "a dry run runs no tool that changes the disk"
else bad "dry run executed mutating tools:${mutated}"; fi

# --- a directory on the system disk is not a second disk ---------------------
# On a single-disk appliance /opt/zimbra lives on the root filesystem. Two rows
# in the console would then be one disk under two names, and an operator could
# extend one and be surprised the other moved. The plan has to say so.
STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" run plan /opt/zimbra
if [ "$RC" -eq 0 ] && has "SHARED_WITH_ROOT=1" && has "not a separate volume"; then
  pass "says when the mail directory shares the system disk"
else bad "did not notice that /opt/zimbra is on the root filesystem (rc=$RC)"; fi

# And must NOT say it when they really are different volumes: a data disk of
# its own is the layout this feature exists for.
STUB_SOURCE=/dev/sdb1 STUB_ROOT_SOURCE=/dev/sda2 STUB_CHAIN='/dev/sdb1 part
/dev/sdb disk' STUB_PARTED='BYT;
/dev/sdb:107374182400B:scsi:512:512:gpt:disk;
1:1048576B:53687091200B:53686042624B:ext4::;' run plan /opt/zimbra
if [ "$RC" -eq 0 ] && ! has "SHARED_WITH_ROOT=1"; then
  pass "does not claim a shared disk when the volumes are different"
else bad "wrongly reported a shared disk (rc=$RC)"; fi

# --- every refusal has to be readable ----------------------------------------
# The console prints whatever comes back, with the KIN_GROW_ marker lines
# stripped out. A refusal that emits only a machine reason therefore shows the
# operator an empty panel, which is indistinguishable from the feature being
# broken. Each one must carry a sentence a person can act on.
short=""
while IFS= read -r reason; do
  # The sentence that follows this reason in the source, on the same call.
  sentence=$(awk -v r="fail ${reason} " '
    index($0, r) { found=1 }
    found { line = line $0; if ($0 ~ /"/ && length(line) > length(r) + 5) { print line; exit } }
  ' "$LIB" | grep -oE '"[^"]{2,}"' | head -1)
  if [ "${#sentence}" -lt 32 ]; then
    short="${short} ${reason}"
  fi
done < <(grep -oE 'fail [a-z-]+ ' "$LIB" | awk '{print $2}' | sort -u)
if [ -z "$short" ]; then
  pass "every refusal carries a sentence, not just a machine reason"
else bad "refusals with no readable explanation:${short}"; fi

reason_count=$(grep -oE 'fail [a-z-]+ ' "$LIB" | awk '{print $2}' | sort -u | wc -l)
if [ "$reason_count" -ge 15 ]; then
  pass "the library still distinguishes ${reason_count} separate refusal reasons"
else bad "refusal reasons collapsed to ${reason_count}; specific beats generic here"; fi

# --- it can never shrink -----------------------------------------------------

# Comments and double-quoted strings removed first, so this reads CODE. The
# refusal messages are long English sentences that explain partition layouts,
# and matching those made the guard fail on its own explanation rather than on
# anything the library would ever run.
CODE_ONLY="${WORK}/code-only.sh"
sed 's/#.*$//; s/"[^"]*"//g' "$LIB" > "$CODE_ONLY"
if grep -qE 'resize2fs[^|]*-M|--shrink|resizepart|mkfs|sfdisk|\bdd\b' "$CODE_ONLY"; then
  bad "the library contains a shrinking or partition-writing command"
else
  pass "no shrink, mkfs, sfdisk or dd in the executable code"
fi

# wipefs is allowed, and only in its no-act form. `wipefs -n` LISTS signatures
# and erases nothing; without -n the same command destroys them. That one
# character is the whole difference, so it is checked rather than trusted.
wipefs_calls=$(grep -oE '\$WIPEFS[^)|;&]*' "$CODE_ONLY" | sed 's/^ *//')
if [ -z "$wipefs_calls" ]; then
  pass "wipefs is not called at all"
elif printf '%s\n' "$wipefs_calls" | grep -qv -- '-n'; then
  bad "wipefs is called without -n, which ERASES signatures: ${wipefs_calls}"
else
  pass "wipefs is only ever called with -n, which lists and erases nothing"
fi

# parted may delete exactly one partition, and only through the guarded
# reclaim. Anything else - mkpart, resizepart, a second rm - is a layout change
# this script does not make.
parted_writes=$(grep -oE '\$PARTED[^)|;&]*' "$CODE_ONLY" | grep -vE '\-sm .* print' | sed 's/^ *//')
rm_count=$(printf '%s\n' "$parted_writes" | grep -c 'rm ' || true)
if printf '%s\n' "$parted_writes" | grep -qE 'mkpart|resizepart|mklabel|set |toggle'; then
  bad "parted is used to create or alter a partition: ${parted_writes}"
elif [ "${rm_count:-0}" -gt 1 ]; then
  bad "parted deletes a partition in more than one place (${rm_count}); there is meant to be exactly one"
else
  pass "parted is used to read, and to delete exactly one reserved partition"
fi

# ...and that single deletion has to be inside reclaim_reserved, which is the
# function carrying the guard chain. Anywhere else it would be unguarded.
RECLAIM_FN=$(awk '/^reclaim_reserved\(\) \{/,/^\}/' "$LIB")
if printf '%s' "$RECLAIM_FN" | grep -q 'rm "\$lastnum"'; then
  pass "the deletion lives inside reclaim_reserved, behind its guards"
else
  bad "the parted deletion is not inside reclaim_reserved"
fi

# Every guard that must hold before a partition is deleted.
for guard in \
  'RECLAIM_MAX_BYTES' \
  'BLKID' \
  'WIPEFS' \
  'holders' \
  'fstab' \
  'crypttab' \
  'drbd'
do
  if printf '%s' "$RECLAIM_FN$(awk '/^reclaimable_reserved\(\) \{/,/^\}/' "$LIB")" | grep -q "$guard"; then
    pass "deleting a partition is guarded on: ${guard}"
  else
    bad "no ${guard} guard before a partition is deleted"
  fi
done

# It is opt-in. A bare apply must never delete anything.
if grep -qE 'want_reclaim=.\$\{2:-0\}' "$LIB"; then
  pass "reclaiming defaults to off; a bare apply never deletes a partition"
else
  bad "reclaim is not opt-in"
fi

# Stronger than a keyword search: every external tool the library can run is
# declared as a variable at the top, so the set of tools is enumerable. If a
# destructive one is ever added, it has to appear here.
# [A-Z_0-9], not [A-Z_]: KIN_GROW_RESIZE2FS has a digit in it, and the first
# version of this pattern silently skipped that line. A guard that enumerates
# tools while missing one is worse than none, because it reads as complete.
TOOLS=$(grep -oE '^[A-Z_0-9]+="\$\{KIN_GROW_[A-Z_0-9]+:-[a-z_0-9.]+\}"' "$LIB" \
        | sed 's/.*:-//; s/}"//' | grep -vE '^[0-9]+$' | sort -u | tr '\n' ' ')
# Every tool the library documents as injectable must appear, or the
# enumeration is not enumerating.
for expect in findmnt lsblk parted growpart partx resize2fs xfs_growfs pvresize lvextend cryptsetup; do
  case " $TOOLS " in
    *" $expect "*) ;;
    *) bad "tool enumeration missed ${expect}" ;;
  esac
done
# blkid and wipefs are inspectors here: blkid only reads, and wipefs is called
# exclusively with -n, which is checked above. mkfs, sfdisk and dd have no
# read-only form worth having and stay forbidden outright.
case " $TOOLS " in
  *" mkfs"*|*" sfdisk"*|*" dd "*)
    bad "a destructive tool is wired into the library: $TOOLS" ;;
  *)
    pass "the only tools it can run are: ${TOOLS}" ;;
esac

# Comments stripped first: the header explains that there is deliberately no
# force flag, and a guard that matches its own rationale is checking prose.
if sed 's/#.*$//' "$LIB" | grep -qiE '(--force|force=1|FORCE_GROW)'; then
  bad "the library has a force path, which is how guards get bypassed"
else
  pass "there is no force flag to bypass the refusals"
fi

# --- root ---------------------------------------------------------------------
( unset KIN_GROW_ALLOW_NONROOT
  OUT=$(bash "$LIB" apply / 2>&1); RC=$?
  if [ "$(id -u)" -eq 0 ]; then exit 0; fi
  printf '%s' "$OUT" | grep -q "reason=not-root" && [ "$RC" -ne 0 ]
) && pass "apply refuses to run without root" || bad "apply did not require root"

# --- the real thing, when the machine allows it ------------------------------
#
# Everything above is stubbed, which proves the decisions and proves nothing
# about growpart and resize2fs actually working. This builds a real disk out of
# a file, puts a real ext4 filesystem on it that does not fill it, writes a
# file, enlarges the backing image the way a hypervisor would, and runs the
# real apply.
#
# It needs root and loop devices, so it skips politely rather than failing on a
# machine that has neither. Skipping is honest; pretending is not.
if [ "$(id -u)" -eq 0 ] && command -v losetup >/dev/null 2>&1    && command -v mkfs.ext4 >/dev/null 2>&1 && [ -e /dev/loop-control ]; then
  RWORK="$(mktemp -d)"
  RIMG="${RWORK}/disk.img"; RMNT="${RWORK}/mnt"; mkdir -p "$RMNT"
  RLOOP=""
  real_cleanup() {
    umount "$RMNT" 2>/dev/null || true
    [ -n "$RLOOP" ] && losetup -d "$RLOOP" 2>/dev/null || true
    rm -rf "$RWORK"
  }
  if truncate -s 200M "$RIMG" 2>/dev/null      && RLOOP=$(losetup --find --show "$RIMG" 2>/dev/null)      && parted -s "$RLOOP" mklabel gpt >/dev/null 2>&1      && parted -s "$RLOOP" mkpart primary ext4 1MiB 100MiB >/dev/null 2>&1; then
    partx -a "$RLOOP" >/dev/null 2>&1 || true
    if mkfs.ext4 -q -F "${RLOOP}p1" >/dev/null 2>&1 && mount "${RLOOP}p1" "$RMNT" 2>/dev/null; then
      before=$(df -BM --output=size "$RMNT" | tail -1 | tr -d ' M')
      printf 'important mail
' > "${RMNT}/mailbox.txt"
      truncate -s 400M "$RIMG"; losetup -c "$RLOOP"
      ( unset KIN_GROW_FINDMNT KIN_GROW_LSBLK KIN_GROW_PARTED KIN_GROW_GROWPART               KIN_GROW_PARTX KIN_GROW_RESIZE2FS KIN_GROW_XFS_GROWFS KIN_GROW_PVRESIZE               KIN_GROW_LVEXTEND KIN_GROW_CRYPTSETUP KIN_GROW_ALLOW_NONROOT
        KIN_ZIMBRA_DIR="$RMNT" bash "$LIB" apply "$RMNT" >/dev/null 2>&1 )
      after=$(df -BM --output=size "$RMNT" | tail -1 | tr -d ' M')
      if [ "${after:-0}" -gt "${before:-0}" ]; then
        pass "real disk: the filesystem grew from ${before}M to ${after}M"
      else bad "real disk: the filesystem did not grow (${before}M to ${after}M)"; fi
      if [ "$(cat "${RMNT}/mailbox.txt" 2>/dev/null)" = "important mail" ]; then
        pass "real disk: data written before the resize is still there"
      else bad "real disk: DATA WAS LOST across the resize"; fi
      if touch "${RMNT}/after-probe" 2>/dev/null; then
        pass "real disk: the filesystem stayed mounted and writable throughout"
      else bad "real disk: filesystem is no longer writable"; fi
    else
      printf 'ok  real disk test skipped (could not make a test filesystem)
'
    fi
  else
    printf 'ok  real disk test skipped (no loop device available)
'
  fi
  real_cleanup

  # --- the mail-disk layout, which is the one that could never grow ---------
  #
  # The installer lays the mail disk out as LUKS-on-p1 followed by a small
  # unformatted p2 reserved for a future second server, and p2 being in the way
  # is what made the mail disk permanently ungrowable. This builds that exact
  # layout and proves the whole chain: refuse, name the blocker, refuse again on
  # a bare apply, then free it and grow - with the data still there.
  #
  # Stubs cannot prove this. Three real defects only showed up here: a
  # partition LABEL being used where a device path was needed (which made every
  # safety check pass on a device that did not exist), lsblk having no PARTN
  # column on the util-linux the appliance ships, and cryptsetup PROMPTING for
  # a key - which inside a console stream is not an error, it is a spinner that
  # never stops.
  if command -v cryptsetup >/dev/null 2>&1; then
    CWORK="$(mktemp -d)"
    CIMG="${CWORK}/disk.img"; CMNT="${CWORK}/mnt"; mkdir -p "$CMNT"
    CLOOP=""; CNAME="kin-growtest-crypt"
    crypt_cleanup() {
      umount "$CMNT" 2>/dev/null || true
      cryptsetup close "$CNAME" 2>/dev/null || true
      [ -n "$CLOOP" ] && losetup -d "$CLOOP" 2>/dev/null || true
      rm -rf "$CWORK"
    }
    printf 'growtestkey' > "${CWORK}/key"
    mkdir -p "${CWORK}/etc/kin-mail" "${CWORK}/proc"
    : > "${CWORK}/etc/fstab"; : > "${CWORK}/etc/crypttab"
    cp "${CWORK}/key" "${CWORK}/etc/kin-mail/luks-keyfile"
    chmod 0400 "${CWORK}/etc/kin-mail/luks-keyfile"

    if truncate -s 260M "$CIMG" 2>/dev/null \
       && CLOOP=$(losetup --find --show "$CIMG" 2>/dev/null) \
       && parted -s "$CLOOP" mklabel gpt >/dev/null 2>&1 \
       && parted -s "$CLOOP" mkpart zimbra-data 1MiB 200MiB >/dev/null 2>&1 \
       && parted -s "$CLOOP" mkpart drbd-meta 200MiB 256MiB >/dev/null 2>&1; then
      partx -a "$CLOOP" >/dev/null 2>&1 || true
      sleep 1
      if cryptsetup luksFormat --batch-mode --pbkdf pbkdf2 \
           --pbkdf-force-iterations 1000 "${CLOOP}p1" "${CWORK}/key" >/dev/null 2>&1 \
         && cryptsetup open --key-file "${CWORK}/key" "${CLOOP}p1" "$CNAME" >/dev/null 2>&1 \
         && mkfs.ext4 -q -F "/dev/mapper/${CNAME}" >/dev/null 2>&1 \
         && mount "/dev/mapper/${CNAME}" "$CMNT" 2>/dev/null; then

        printf 'customer mail\n' > "${CMNT}/mailbox.txt"
        cbefore=$(df --output=size -B1 "$CMNT" | tail -1 | tr -d ' ')

        crun() {
          ( unset KIN_GROW_FINDMNT KIN_GROW_LSBLK KIN_GROW_PARTED KIN_GROW_GROWPART \
                  KIN_GROW_PARTX KIN_GROW_RESIZE2FS KIN_GROW_XFS_GROWFS \
                  KIN_GROW_PVRESIZE KIN_GROW_LVEXTEND KIN_GROW_CRYPTSETUP \
                  KIN_GROW_ALLOW_NONROOT
            KIN_ZIMBRA_DIR="$CMNT" KIN_GROW_PROC_ROOT="${CWORK}/proc" \
            KIN_GROW_ETC_ROOT="${CWORK}/etc" bash "$LIB" "$@" 2>&1 )
        }

        # The partition names here are the ones prepare-zimbra-data-disk.sh
        # actually writes. They used to be "data" and "meta", which meant this
        # whole end-to-end test exercised the generic not-last-partition path
        # and never the drbd-meta one - so it proved the reclaim worked on a
        # layout this product does not build, while the layout it does build
        # had lost the reclaim entirely. A fixture that does not match the
        # installer is a test that passes for the wrong deployment.
        out=$(crun plan "$CMNT")
        case "$out" in *"reason=meta-partition-in-the-way"*)
          pass "mail layout: refuses while the reserved partition is in the way" ;;
          *) bad "mail layout: should refuse, got: ${out}" ;; esac
        case "$out" in *"KIN_GROW_RECLAIMABLE_RESERVED=${CLOOP}p2"*)
          pass "mail layout: names the reserved partition by device, not by label" ;;
          *) bad "mail layout: should name ${CLOOP}p2 as reclaimable" ;; esac

        truncate -s 420M "$CIMG"; losetup -c "$CLOOP"; sleep 1

        crun apply "$CMNT" >/dev/null 2>&1
        if parted -sm "$CLOOP" unit B print 2>/dev/null | grep -q "^2:"; then
          pass "mail layout: a bare apply never deletes the reserved partition"
        else bad "mail layout: a bare apply DELETED a partition"; fi

        out=$(crun apply "$CMNT" --reclaim-reserved)
        if parted -sm "$CLOOP" unit B print 2>/dev/null | grep -q "^2:"; then
          bad "mail layout: --reclaim-reserved did not free the partition"
        else pass "mail layout: --reclaim-reserved freed the reserved partition"; fi

        cafter=$(df --output=size -B1 "$CMNT" | tail -1 | tr -d ' ')
        if [ "${cafter:-0}" -gt "${cbefore:-0}" ]; then
          pass "mail layout: the encrypted filesystem grew (${cbefore} to ${cafter} bytes)"
        else bad "mail layout: the filesystem did not grow (${cbefore} to ${cafter}); ${out}"; fi
        if [ "$(cat "${CMNT}/mailbox.txt" 2>/dev/null)" = "customer mail" ]; then
          pass "mail layout: data written before the resize is still there"
        else bad "mail layout: DATA WAS LOST across the resize"; fi
        if touch "${CMNT}/after-probe" 2>/dev/null; then
          pass "mail layout: still mounted and writable afterwards"
        else bad "mail layout: filesystem is no longer writable"; fi
      else
        printf 'ok  encrypted mail-disk test skipped (could not build the LUKS layout)\n'
      fi
    else
      printf 'ok  encrypted mail-disk test skipped (no loop device available)\n'
    fi
    crypt_cleanup
  else
    printf 'ok  encrypted mail-disk test skipped (cryptsetup not installed)\n'
  fi
else
  printf 'ok  real disk test skipped (needs root and loop devices)
'
fi

if [ "$fails" -eq 0 ]; then
  printf 'All grow-disk tests passed\n'
else
  printf '%s test(s) failed\n' "$fails"
fi
exit "$fails"
