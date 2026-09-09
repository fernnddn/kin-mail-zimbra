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
make_stub lvextend 'exit ${STUB_LVEXTEND_RC:-0}'
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

CHAIN_PLAIN='sda2 part
sda disk'
CHAIN_LVM='ubuntu--vg-ubuntu--lv lvm
sda3 part
sda disk'
CHAIN_LUKS='kin--vg-data lvm
kin_crypt crypt
sdb1 part
sdb disk'
CHAIN_DRBD='drbd0 disk
sdb1 part
sdb disk'

run() {  # run <action> <mountpoint>; output on stdout, rc in RC
  : > "$CALLS"
  OUT=$(bash "$LIB" "$1" "$2" 2>&1); RC=$?
}

has() { printf '%s' "$OUT" | grep -q "$1"; }
called() { grep -q "^$1" "$CALLS"; }

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

STUB_CHAIN='sda2 part
sda disk
sdb1 part
sdb disk' STUB_PARTED="$PARTED_ROOM" run plan /
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
if [ "$RC" -eq 0 ] && called "cryptsetup resize" && called "pvresize /dev/kin_crypt"; then
  pass "LUKS: the encrypted container is resized before the volume group"
else bad "LUKS apply did not resize the container (rc=$RC)"; fi

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
  STUB_PARTED="$PARTED_ROOM_P3" STUB_LVEXTEND_RC=5 run apply /
if [ "$RC" -eq 0 ] && called "resize2fs"; then
  pass "lvextend finding nothing to add still lets the filesystem grow"
else bad "a no-op lvextend aborted the run (rc=$RC)"; fi

STUB_SOURCE=/dev/sda2 STUB_CHAIN="$CHAIN_PLAIN" STUB_PARTED="$PARTED_ROOM" \
  STUB_RESIZE2FS_RC=1 run apply /
if [ "$RC" -ne 0 ] && has "reason=resize2fs-failed"; then
  pass "a filesystem that will not grow is reported, not swallowed"
else bad "resize2fs failure was not reported (rc=$RC)"; fi

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
STUB_SOURCE=/dev/sdb1 STUB_ROOT_SOURCE=/dev/sda2 STUB_CHAIN='sdb1 part
sdb disk' STUB_PARTED='BYT;
/dev/sdb:107374182400B:scsi:512:512:gpt:disk;
1:1048576B:53687091200B:53686042624B:ext4::;' run plan /opt/zimbra
if [ "$RC" -eq 0 ] && ! has "SHARED_WITH_ROOT=1"; then
  pass "does not claim a shared disk when the volumes are different"
else bad "wrongly reported a shared disk (rc=$RC)"; fi

# --- it can never shrink -----------------------------------------------------

if grep -qE 'resize2fs[^|]*-M|--shrink|resizepart|mkfs|sfdisk|wipefs|dd ' "$LIB"; then
  bad "the library contains a shrinking or partition-writing command"
else
  pass "no shrink, mkfs, sfdisk or wipefs anywhere in the library"
fi

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

if [ "$fails" -eq 0 ]; then
  printf 'All grow-disk tests passed\n'
else
  printf '%s test(s) failed\n' "$fails"
fi
exit "$fails"
