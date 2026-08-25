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

cat >"$STUB/udevadm" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB/udevadm"

cat >"$STUB/kin-fail2ban-jails" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/fail2ban.log"
if [ -s "${ROOT}/su.log" ] && grep -q zmcontrol "${ROOT}/su.log"; then
  echo late >>"${ROOT}/fail2ban-late"
fi
exit 0
EOF
chmod +x "$STUB/kin-fail2ban-jails"
export KIN_FAIL2BAN_JAILS="$STUB/kin-fail2ban-jails"

cat >"$STUB/blkid" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB/blkid"

SCRIPT=./release-zimbra-plain-mount-for-drbd.sh

write_fstab
printf '%s\n' "/dev/drbd0" >"$ROOT/mnt_state"
: >"$ROOT/su.log"
: >"$ROOT/fail2ban.log"
rm -f "$ROOT/fail2ban-late"
if "$SCRIPT" >/dev/null \
  && ! grep -Fq "$MARK" "$FSTAB" \
  && ! grep -q zmcontrol "$ROOT/su.log" \
  && [ ! -s "$ROOT/fail2ban.log" ]; then
  pass "drbd mount: skips stop, skips fail2ban drop, removes fstab mark"
else
  bad "drbd mount should skip stop and fail2ban drop, and remove mark"
fi

write_fstab
: >"$ROOT/mnt_state"
: >"$ROOT/fail2ban.log"
: >"$ROOT/su.log"
if "$SCRIPT" >/dev/null \
  && ! grep -Fq "$MARK" "$FSTAB" \
  && grep -q unmounted "$ROOT/fail2ban.log" \
  && ! grep -q zmcontrol "$ROOT/su.log"; then
  pass "unmounted: drops fail2ban jails, waits holders, removes fstab mark without stop"
else
  bad "unmounted path should drop jails and remove mark without stop"
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
: >"$ROOT/fail2ban.log"
rm -f "$ROOT/fail2ban-late"
if "$SCRIPT" >/dev/null \
  && grep -q 'zmcontrol stop' "$ROOT/su.log" \
  && grep -q umount "$ROOT/umount.log" \
  && grep -q unmounted "$ROOT/fail2ban.log" \
  && [ ! -f "$ROOT/fail2ban-late" ] \
  && ! grep -Fq "$MARK" "$FSTAB" \
  && [ ! -s "$ROOT/mnt_state" ]; then
  pass "plain data mount: fail2ban drop before stop, umount, remove fstab mark"
else
  bad "plain data mount handoff failed"
  echo "--- su ---"; cat "$ROOT/su.log" || true
  echo "--- umount ---"; cat "$ROOT/umount.log" || true
  echo "--- fail2ban ---"; cat "$ROOT/fail2ban.log" || true
  echo "--- fstab ---"; cat "$FSTAB" || true
fi

write_fstab
printf '%s\n' "/dev/mapper/kin-zimbra-crypt" >"$ROOT/mnt_state"
: >"$ROOT/su.log"
: >"$ROOT/umount.log"
if "$SCRIPT" >/dev/null \
  && grep -q 'zmcontrol stop' "$ROOT/su.log" \
  && grep -q umount "$ROOT/umount.log" \
  && ! grep -Fq "$MARK" "$FSTAB"; then
  pass "LUKS mapper mount: stop, umount, remove fstab mark"
else
  bad "LUKS mapper mount handoff failed"
fi

# A log-tailer/udev probe can briefly reopen the just-freed device right
# after umount - fuser -m must be retried before treating that as a real
# stuck holder (live 2vm practice run, 25 Aug 2026: drbdadm up then also hit
# "Can not open backing device" on the exact same device, same race).
FUSER_COUNTER="$ROOT/fuser_calls"
: >"$FUSER_COUNTER"
cat >"$STUB/fuser" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" = "-vm" ] && exit 1
n=\$(wc -l <"$FUSER_COUNTER")
echo call >>"$FUSER_COUNTER"
[ "\$n" -lt 2 ] && exit 0
exit 1
EOF
chmod +x "$STUB/fuser"
write_fstab
printf '%s\n' "$DATA" >"$ROOT/mnt_state"
KIN_RELEASE_FUSER_RETRIES=5 KIN_RELEASE_FUSER_DELAY=0 "$SCRIPT" >/dev/null 2>&1
transient_rc=$?
if [ "$transient_rc" -eq 0 ]; then
  pass "transient fuser busy (holder already gone by the time we check) retries then succeeds"
else
  bad "transient fuser busy should recover within retries"
fi

cat >"$STUB/fuser" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB/fuser"
write_fstab
printf '%s\n' "$DATA" >"$ROOT/mnt_state"
KIN_RELEASE_FUSER_RETRIES=2 KIN_RELEASE_FUSER_DELAY=0 "$SCRIPT" >/dev/null 2>&1
persistent_rc=$?
if [ "$persistent_rc" -ne 0 ]; then
  pass "persistently busy device still fails closed after exhausting retries"
else
  bad "persistently busy device must fail closed, not silently proceed"
fi

cat >"$STUB/fuser" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB/fuser"

printf 'UUID=aaaa-bbbb /boot ext4 defaults 0 2\n' >"$FSTAB"
printf '%s\n' "/dev/sda1" >"$ROOT/mnt_state"
if "$SCRIPT" >/dev/null 2>&1; then
  bad "unexpected mount source must fail closed"
else
  pass "unexpected mount source fails closed"
fi

# Shared helper (used by prepare + release + ansible staging).
if grep -q 'precluster-zimbra-fstab.sh' ./release-zimbra-plain-mount-for-drbd.sh \
  && grep -q 'precluster-zimbra-fstab.sh' ./prepare-zimbra-data-disk.sh \
  && [ -f ./precluster-zimbra-fstab.sh ]; then
  pass "release and prepare share precluster-zimbra-fstab.sh"
else
  bad "shared precluster-zimbra-fstab.sh wiring missing"
fi

release_yml="../../ansible/roles/drbd_resource/tasks/release_plain_mount.yml"
if [ -f "$release_yml" ] && python3 - "$release_yml" <<'PY'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
marker = "Release plain mount if needed, and always drop stale pre-cluster fstab"
idx = text.find(marker)
if idx < 0:
    raise SystemExit("release command task missing")
# Stop before the next Confirm task so we only inspect this task's when:.
chunk = text[idx : text.find("Confirm /opt/zimbra", idx)]
if "needs_plain_mount_release" in chunk:
    raise SystemExit("release script still gated on needs_plain_mount_release")
if "when: not ansible_check_mode" not in chunk:
    raise SystemExit("expected when: not ansible_check_mode")
if "precluster-zimbra-fstab.sh" not in text:
    raise SystemExit("precluster helper not staged")
if "zimbra-data-disk-luks.sh" not in text:
    raise SystemExit("LUKS helper not staged")
if "kin-fail2ban-jails.sh" not in text:
    raise SystemExit("fail2ban jail helper not staged")
if "KIN_FAIL2BAN_JAILS" not in chunk:
    raise SystemExit("release command must pass KIN_FAIL2BAN_JAILS")
PY
then
  pass "ansible release_plain_mount runs fstab cleanup on every mail node"
else
  bad "ansible release_plain_mount still gates the release script on plain-mount-only"
fi

# Persistently busy on the already-unmounted retry path (tonight's leftover:
# mail2 unmounted, fstab dirty, fail2ban still holding the mapper).
cat >"$STUB/fuser" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB/fuser"
write_fstab
: >"$ROOT/mnt_state"
: >"$ROOT/fail2ban.log"
KIN_RELEASE_FUSER_RETRIES=2 KIN_RELEASE_FUSER_DELAY=0 "$SCRIPT" >/dev/null 2>&1
unmounted_busy_rc=$?
if [ "$unmounted_busy_rc" -ne 0 ] && grep -Fq "$MARK" "$FSTAB"; then
  pass "unmounted retry still fails closed when the backing device is busy"
else
  bad "unmounted retry must not skip the holder wait or remove fstab while busy"
fi

cat >"$STUB/fuser" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB/fuser"

write_fstab
: >"$ROOT/mnt_state"
: >"$ROOT/su.log"
: >"$ROOT/fail2ban.log"
KIN_RELEASE_WAIT_BACKING_ONLY=1 "$SCRIPT" >/dev/null 2>&1
wait_only_rc=$?
if [ "$wait_only_rc" -eq 0 ] \
  && grep -q unmounted "$ROOT/fail2ban.log" \
  && ! grep -q zmcontrol "$ROOT/su.log" \
  && grep -Fq "$MARK" "$FSTAB"; then
  pass "wait-backing-only drops jails, does not stop mail, leaves fstab to release"
else
  bad "wait-backing-only path is wrong"
fi

playbook="../../ansible/playbooks/mail-drbd.yml"
activate_yml="../../ansible/roles/drbd_resource/tasks/activate.yml"
if [ -f "$playbook" ] && grep -q 'any_errors_fatal: true' "$playbook"; then
  pass "mail-drbd.yml aborts the pair when one node fails the handoff"
else
  bad "mail-drbd.yml must set any_errors_fatal so one node cannot create-md alone"
fi
pair_fatal_ok=1
for p in ../../ansible/playbooks/mail-cluster-setup.yml \
         ../../ansible/playbooks/mail-pacemaker.yml \
         ../../ansible/playbooks/mail-fencing.yml \
         ../../ansible/playbooks/mail-qdevice.yml; do
  if [ ! -f "$p" ] || ! grep -q 'any_errors_fatal: true' "$p"; then
    pair_fatal_ok=0
  fi
done
if [ "$pair_fatal_ok" -eq 1 ]; then
  pass "pair playbooks abort when one node fails"
else
  bad "mail-cluster-setup/pacemaker/fencing/qdevice must set any_errors_fatal"
fi
if [ -f "$activate_yml" ] \
  && grep -q 'KIN_RELEASE_WAIT_BACKING_ONLY' "$activate_yml" \
  && grep -q 'drbd_resource_meta_disk_stat' "$activate_yml" \
  && grep -q 'Peer DRBD role is already Primary' "$activate_yml"; then
  pass "activate.yml waits for a free backing device before drbdadm up"
else
  bad "activate.yml must reuse the holder wait before drbdadm up"
fi

if [ "$fails" -eq 0 ]; then
  printf 'ALL OK\n'
  exit 0
fi
printf '%s failure(s)\n' "$fails"
exit 1
