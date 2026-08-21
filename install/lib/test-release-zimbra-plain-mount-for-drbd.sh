#!/usr/bin/env bash
# Gating tests for release-zimbra-plain-mount-for-drbd.sh (no live zmcontrol).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
STUB="$ROOT/bin"
mkdir -p "$STUB" "$ROOT/opt/zimbra/bin"
FSTAB="$ROOT/fstab"
DATA="$ROOT/fake-sdb1"
: >"$DATA"
: >"$ROOT/opt/zimbra/bin/zmcontrol"
chmod +x "$ROOT/opt/zimbra/bin/zmcontrol"

export PATH="$STUB:$PATH"
export KIN_RELEASE_FSTAB="$FSTAB"
export KIN_DRBD_DATA_DISK="$DATA"
export KIN_ZIMBRA_DIR="$ROOT/opt/zimbra"
export KIN_MIGRATE_LIB="$PWD/migrate-zimbra-to-drbd-disk.sh"
export KIN_MIGRATE_SKIP_PS_CHECK=1
export KIN_MIGRATE_STOP_WAIT_SEC=0
export KIN_ZMCONTROL_STATUS_TEXT='Host mail.example.test
	ldap                    Stopped
	mailbox                 Stopped
	zmconfigd               Stopped
'

# Must match prepare-zimbra-data-disk.sh
MARK='# KIN Mail zimbra data partition (pre-cluster)'
write_fstab() {
  printf 'UUID=aaaa-bbbb /boot ext4 defaults 0 2\n\n%s\n# Remove this UUID line when Pacemaker kin-fs mounts /dev/drbd0 (Build HA pair).\nUUID=cccc-dddd %s ext4 defaults 0 2\n' \
    "$MARK" "$ROOT/opt/zimbra" >"$FSTAB"
}

# migrate-zimbra-to-drbd-disk.sh's own historical mark line has extra
# trailing text after "(pre-cluster)". A host migrated by that script
# before this marker text was standardized still has this exact line on
# disk tonight; the awk match must still find and remove it.
write_fstab_legacy_mark() {
  printf 'UUID=aaaa-bbbb /boot ext4 defaults 0 2\n\n%s. See HA-RUNBOOK §13.\n# Remove this UUID line when Pacemaker kin-fs mounts /dev/drbd0 (Build HA pair).\nUUID=cccc-dddd %s ext4 defaults 0 2\n' \
    "$MARK" "$ROOT/opt/zimbra" >"$FSTAB"
}

cat >"$STUB/id" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = "-u" ] && { echo 0; exit 0; }
exit 0
EOF
chmod +x "$STUB/id"

cat >"$STUB/findmnt" <<EOF
#!/usr/bin/env bash
STATE="${ROOT}/mnt_state"
src=""
[ -f "\$STATE" ] && src=\$(cat "\$STATE")
[ -n "\$src" ] || exit 1
if [ "\${1:-}" = "-n" ] && [ "\${2:-}" = "-o" ] && [ "\${3:-}" = "SOURCE" ]; then
  printf '%s\n' "\$src"; exit 0
fi
if [ "\${1:-}" = "-n" ] && [ "\${2:-}" = "-o" ] && [ "\${3:-}" = "UUID" ]; then
  printf '%s\n' "\${KIN_TEST_FINDMNT_UUID:-}"; exit 0
fi
if [ "\${1:-}" = "-n" ]; then
  printf '%s\n' "\$src"; exit 0
fi
exit 1
EOF
chmod +x "$STUB/findmnt"

cat >"$STUB/umount" <<EOF
#!/usr/bin/env bash
echo "umount \$*" >>"${ROOT}/umount.log"
: >"${ROOT}/mnt_state"
exit 0
EOF
chmod +x "$STUB/umount"

cat >"$STUB/su" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/su.log"
exit 0
EOF
chmod +x "$STUB/su"

cat >"$STUB/fuser" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB/fuser"

cat >"$STUB/blkid" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB/blkid"

SCRIPT=./release-zimbra-plain-mount-for-drbd.sh

write_fstab
printf '%s\n' "/dev/drbd0" >"$ROOT/mnt_state"
: >"$ROOT/su.log"
if "$SCRIPT" >/dev/null \
  && ! grep -Fq "$MARK" "$FSTAB" \
  && ! grep -q zmcontrol "$ROOT/su.log"; then
  pass "drbd mount: skips stop and removes fstab mark"
else
  bad "drbd mount should skip stop and remove mark"
fi

write_fstab
: >"$ROOT/mnt_state"
if "$SCRIPT" >/dev/null && ! grep -Fq "$MARK" "$FSTAB"; then
  pass "unmounted: removes fstab mark without stop"
else
  bad "unmounted path should remove mark"
fi

write_fstab_legacy_mark
: >"$ROOT/mnt_state"
if "$SCRIPT" >/dev/null 2>&1 && ! grep -Fq "$MARK" "$FSTAB"; then
  pass "legacy mark line (extra trailing text) is still matched and removed"
else
  bad "legacy mark line should still be matched and removed"
  cat "$FSTAB" || true
fi

write_fstab
printf '%s\n' "$DATA" >"$ROOT/mnt_state"
: >"$ROOT/su.log"
: >"$ROOT/umount.log"
if "$SCRIPT" >/dev/null \
  && grep -q 'zmcontrol stop' "$ROOT/su.log" \
  && grep -q umount "$ROOT/umount.log" \
  && ! grep -Fq "$MARK" "$FSTAB" \
  && [ ! -s "$ROOT/mnt_state" ]; then
  pass "plain data mount: stop, umount, remove fstab mark"
else
  bad "plain data mount handoff failed"
  echo "--- su ---"; cat "$ROOT/su.log" || true
  echo "--- umount ---"; cat "$ROOT/umount.log" || true
  echo "--- fstab ---"; cat "$FSTAB" || true
fi

printf 'UUID=aaaa-bbbb /boot ext4 defaults 0 2\n' >"$FSTAB"
printf '%s\n' "/dev/sda1" >"$ROOT/mnt_state"
if "$SCRIPT" >/dev/null 2>&1; then
  bad "unexpected mount source must fail closed"
else
  pass "unexpected mount source fails closed"
fi

if [ "$fails" -eq 0 ]; then
  printf 'ALL OK\n'
  exit 0
fi
printf '%s failure(s)\n' "$fails"
exit 1
