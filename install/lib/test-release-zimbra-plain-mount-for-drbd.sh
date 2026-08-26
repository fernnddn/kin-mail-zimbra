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
export KIN_RELEASE_POST_FREE_SLEEP=0
export KIN_RELEASE_DRBD_DOWN_SLEEP=0
export KIN_RELEASE_PKILL_SLEEP=0
export KIN_RELEASE_NUDGE_KILL_SLEEP=0
export KIN_RELEASE_NUDGE_EVERY=2
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
if [ "\${1:-}" = "-n" ] && [ "\${2:-}" = "-S" ]; then
  # Not mounted from this block device during tests unless STATE matches.
  exit 1
fi
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

cat >"$STUB/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB/systemctl"

cat >"$STUB/blockdev" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB/blockdev"

cat >"$STUB/pkill" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB/pkill"

# Default: resource not up. Resume / busy-DRBD tests override this stub.
cat >"$STUB/drbdadm" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/drbdadm.log"
exit 1
EOF
chmod +x "$STUB/drbdadm"

cat >"$STUB/kin-fail2ban-jails" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/fail2ban.log"
# Only the first drop must precede zmcontrol stop. Later nudge() re-drops are
# intentional after umount (holders can reopen the mapper).
if [ ! -f "${ROOT}/fail2ban-seen" ]; then
  touch "${ROOT}/fail2ban-seen"
  if [ -s "${ROOT}/su.log" ] && grep -q zmcontrol "${ROOT}/su.log"; then
    echo late >>"${ROOT}/fail2ban-late"
  fi
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
rm -f "$ROOT/fail2ban-late" "$ROOT/fail2ban-seen"
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
rm -f "$ROOT/fail2ban-seen"
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
[ "\${1:-}" = "-v" ] && exit 1
n=\$(wc -l <"$FUSER_COUNTER")
echo call >>"$FUSER_COUNTER"
# Each wait loop may call fuser twice (with and without -m). Stay busy for
# the first two loops, then go free.
[ "\$n" -lt 4 ] && exit 0
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

# phase2-3 / resume hardening: mail2 unmounted with DRBD already up must NOT
# tear down in release (peer may still fail the same task). Skip free-wait and
# only clean fstab; activate.yml early-down owns re-attach.
cat >"$STUB/drbdadm" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/drbdadm.log"
if [ "\$1" = "status" ]; then
  exit 0
fi
if [ "\$1" = "down" ]; then
  touch "${ROOT}/drbd_down"
  exit 0
fi
exit 0
EOF
chmod +x "$STUB/drbdadm"
cat >"$STUB/fuser" <<EOF
#!/usr/bin/env bash
# Would stay busy if release incorrectly waited without skipping.
exit 0
EOF
chmod +x "$STUB/fuser"
write_fstab
: >"$ROOT/mnt_state"
: >"$ROOT/drbdadm.log"
rm -f "$ROOT/drbd_down"
KIN_RELEASE_FUSER_RETRIES=2 KIN_RELEASE_FUSER_DELAY=0 "$SCRIPT" >/dev/null 2>&1
stale_drbd_rc=$?
if [ "$stale_drbd_rc" -eq 0 ] \
  && ! grep -q '^down ' "$ROOT/drbdadm.log" \
  && grep -q '^status ' "$ROOT/drbdadm.log" \
  && ! grep -Fq "$MARK" "$FSTAB"; then
  pass "unmounted resume skips free-wait when DRBD is already up"
else
  bad "unmounted resume must skip free-wait/down when DRBD is already up"
fi

# wait-backing-only (activate path) must still down an up resource before wait.
write_fstab
: >"$ROOT/mnt_state"
: >"$ROOT/drbdadm.log"
: >"$ROOT/fail2ban.log"
rm -f "$ROOT/drbd_down"
cat >"$STUB/fuser" <<EOF
#!/usr/bin/env bash
if [ -f "${ROOT}/drbd_down" ]; then exit 1; fi
exit 0
EOF
chmod +x "$STUB/fuser"
KIN_RELEASE_WAIT_BACKING_ONLY=1 KIN_RELEASE_FUSER_RETRIES=2 KIN_RELEASE_FUSER_DELAY=0 \
  "$SCRIPT" >/dev/null 2>&1
wait_down_rc=$?
if [ "$wait_down_rc" -eq 0 ] \
  && grep -q '^down ' "$ROOT/drbdadm.log" \
  && grep -Fq "$MARK" "$FSTAB"; then
  pass "wait-backing-only downs up DRBD before free-device wait"
else
  bad "wait-backing-only must drbdadm down when resource is still up"
fi

# Pacemaker-owned clone + unmounted + busy holders: fstab only, no down/wait fail.
cat >"$STUB/pcs" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "resource" ] && [ "\$2" = "config" ]; then
  exit 0
fi
exit 1
EOF
chmod +x "$STUB/pcs"
cat >"$STUB/drbdadm" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/drbdadm.log"
exit 1
EOF
chmod +x "$STUB/drbdadm"
cat >"$STUB/fuser" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB/fuser"
write_fstab
: >"$ROOT/mnt_state"
: >"$ROOT/drbdadm.log"
KIN_RELEASE_FUSER_RETRIES=2 KIN_RELEASE_FUSER_DELAY=0 "$SCRIPT" >/dev/null 2>&1
pcs_owns_rc=$?
if [ "$pcs_owns_rc" -eq 0 ] \
  && ! grep -q '^down ' "$ROOT/drbdadm.log" \
  && ! grep -Fq "$MARK" "$FSTAB"; then
  pass "unmounted resume with Pacemaker-owned clone skips free-wait"
else
  bad "Pacemaker-owned unmounted resume must skip free-wait and clean fstab"
fi
rm -f "$STUB/pcs"


cat >"$STUB/drbdadm" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/drbdadm.log"
exit 1
EOF
chmod +x "$STUB/drbdadm"
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
  && grep -q 'KIN_DRBD_UP_RETRY' "$activate_yml" \
  && grep -q 'Tear down any existing DRBD resource before metadata or attach' "$activate_yml" \
  && grep -q 'drbd_resource_meta_disk_stat' "$activate_yml" \
  && grep -q 'Peer DRBD role is already Primary' "$activate_yml"; then
  pass "activate.yml downs first, then wait+up in one retry loop"
else
  bad "activate.yml must down before wait and re-wait inside up retries"
fi
release_yml="../../ansible/roles/drbd_resource/tasks/release_plain_mount.yml"
if [ -f "$release_yml" ] \
  && grep -q 'KIN_DRBD_BACKING_DISK' "$release_yml" \
  && grep -q 'KIN_DRBD_RESOURCE_NAME' "$release_yml" \
  && grep -q 'KIN_DRBD_PCS_CLONE' "$release_yml" \
  && grep -q 'KIN_DRBD_PCS_CLONE' "$activate_yml"; then
  pass "release/activate pass backing disk, resource name, and PCS clone"
else
  bad "release/activate must pass KIN_DRBD_BACKING_DISK, RESOURCE_NAME, and PCS_CLONE"
fi

# =============================================================================
# Regression: open-count-without-fuser (live Host A, 26 Aug 2026 phase3-1).
#
# A mount surviving in ANOTHER mount namespace holds the dm device at
# open count=1 while findmnt, fuser, /proc/*/fd and sysfs holders are all
# clean. Waiting can never clear it, so the old code looped until timeout and
# died with the unreadable "dmsetup open count=1".
# =============================================================================
PROCROOT="$ROOT/proc"
make_fake_proc() {
  # $1 = pid, $2 = comm, $3 = ns id, $4 = majmin ("" for no mount entry)
  rm -rf "$PROCROOT"
  mkdir -p "$PROCROOT/self/ns" "$PROCROOT/$1/ns"
  printf 'mnt:[4026531840]\n' >"$PROCROOT/self/ns/mnt"
  printf '%s\n' "$2" >"$PROCROOT/$1/comm"
  printf 'mnt:[%s]\n' "$3" >"$PROCROOT/$1/ns/mnt"
  if [ -n "${4:-}" ]; then
    printf '36 35 %s / %s rw,relatime - ext4 /dev/mapper/kin-zimbra-crypt rw\n' \
      "$4" "$ROOT/opt/zimbra" >"$PROCROOT/$1/mountinfo"
  fi
}

# fuser and the fd scan must both be quiet so we reach the namespace check.
cat >"$STUB/fuser" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB/fuser"
cat >"$STUB/drbdadm" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/drbdadm.log"
exit 1
EOF
chmod +x "$STUB/drbdadm"
rm -f "$STUB/pcs"

# nsenter stub: a successful lazy umount inside the namespace removes that
# namespace's mount entry, exactly like the real one.
cat >"$STUB/nsenter" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/nsenter.log"
target=""
while [ \$# -gt 0 ]; do
  case "\$1" in
    -t) target="\$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "\$target" ] && rm -f "${PROCROOT}/\${target}/mountinfo"
exit 0
EOF
chmod +x "$STUB/nsenter"

make_fake_proc 900 fail2ban-server 4026532111 "253:0"
: >"$ROOT/nsenter.log"
: >"$ROOT/drbdadm.log"
write_fstab
: >"$ROOT/mnt_state"
KIN_RELEASE_WAIT_BACKING_ONLY=1 KIN_RELEASE_PROC_ROOT="$PROCROOT" \
  KIN_RELEASE_TEST_MAJMIN="253:0" \
  KIN_RELEASE_FUSER_RETRIES=6 KIN_RELEASE_FUSER_DELAY=0 \
  "$SCRIPT" >"$ROOT/ns_heal.out" 2>&1
ns_heal_rc=$?
if [ "$ns_heal_rc" -eq 0 ] \
  && grep -q 'umount' "$ROOT/nsenter.log" \
  && grep -q '\-t 900' "$ROOT/nsenter.log"; then
  pass "foreign mount-namespace holder is lazy-unmounted, handoff then succeeds"
else
  bad "namespace holder must be released via nsenter umount -l (rc=$ns_heal_rc)"
fi

# Same holder, but nsenter is unavailable: must fail closed AND name the
# culprit instead of printing the opaque dmsetup open count.
mv "$STUB/nsenter" "$ROOT/nsenter.disabled"
make_fake_proc 900 fail2ban-server 4026532111 "253:0"
write_fstab
: >"$ROOT/mnt_state"
KIN_RELEASE_WAIT_BACKING_ONLY=1 KIN_RELEASE_PROC_ROOT="$PROCROOT" \
  KIN_RELEASE_TEST_MAJMIN="253:0" \
  KIN_RELEASE_FUSER_RETRIES=2 KIN_RELEASE_FUSER_DELAY=0 \
  "$SCRIPT" >"$ROOT/ns_fail.out" 2>&1
ns_fail_rc=$?
if [ "$ns_fail_rc" -ne 0 ] \
  && grep -q 'mount-namespace holders' "$ROOT/ns_fail.out" \
  && grep -q 'fail2ban-server' "$ROOT/ns_fail.out"; then
  pass "unreleasable namespace holder fails closed and names the process"
else
  bad "namespace holder must fail closed naming the holder, not 'open count=1'"
fi
mv "$ROOT/nsenter.disabled" "$STUB/nsenter"

# A pid in OUR namespace must never be reported (that is the host mount,
# already covered by findmnt) - otherwise every run would false-positive.
make_fake_proc 901 fail2ban-server 4026531840 "253:0"
same_ns_out=$(
  KIN_RELEASE_SOURCE_ONLY=1 KIN_RELEASE_PROC_ROOT="$PROCROOT" \
    KIN_RELEASE_TEST_MAJMIN="253:0" \
    bash -c '. "$0" >/dev/null 2>&1; list_mountns_holders /dev/mapper/x' "$SCRIPT" 2>/dev/null
)
if [ -z "$same_ns_out" ]; then
  pass "a mount in our own namespace is not reported as a foreign holder"
else
  bad "own-namespace mount must not be treated as a foreign holder"
fi

# A non-matching maj:min must not match either.
make_fake_proc 902 fail2ban-server 4026532111 "9:9"
other_dev_out=$(
  KIN_RELEASE_SOURCE_ONLY=1 KIN_RELEASE_PROC_ROOT="$PROCROOT" \
    KIN_RELEASE_TEST_MAJMIN="253:0" \
    bash -c '. "$0" >/dev/null 2>&1; list_mountns_holders /dev/mapper/x' "$SCRIPT" 2>/dev/null
)
if [ -z "$other_dev_out" ]; then
  pass "a mount of a different device is not reported as a holder"
else
  bad "maj:min match must be exact"
fi

# =============================================================================
# Regression: never SIGKILL a process whose death is worse than the failure.
# Killing corosync/pacemaker on a live node self-fences it (node reboots).
# =============================================================================
protected_ok=1
for proc_name in corosync pacemakerd sshd systemd-udevd python3 sbd; do
  make_fake_proc 950 "$proc_name" 4026532111 ""
  if ! KIN_RELEASE_SOURCE_ONLY=1 KIN_RELEASE_PROC_ROOT="$PROCROOT" \
      bash -c '. "$0" >/dev/null 2>&1; release_pid_is_protected 950' "$SCRIPT" 2>/dev/null; then
    protected_ok=0
    printf '    not protected: %s\n' "$proc_name"
  fi
done
make_fake_proc 951 java 4026532111 ""
if KIN_RELEASE_SOURCE_ONLY=1 KIN_RELEASE_PROC_ROOT="$PROCROOT" \
    bash -c '. "$0" >/dev/null 2>&1; release_pid_is_protected 951' "$SCRIPT" 2>/dev/null; then
  protected_ok=0
  printf '    wrongly protected: java\n'
fi
if [ "$protected_ok" -eq 1 ]; then
  pass "cluster/system daemons are never killed; a stray java still is"
else
  bad "kill guard must protect cluster daemons and still allow real holders"
fi

# =============================================================================
# Regression: a failed handoff must not leave udev's exec queue stopped.
# A permanently paused queue stops /dev/disk/by-id from being populated, which
# breaks iSCSI SBD discovery and DRBD device nodes later in the SAME run.
# =============================================================================
cat >"$STUB/udevadm" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/udevadm.log"
exit 0
EOF
chmod +x "$STUB/udevadm"
cat >"$STUB/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${ROOT}/systemctl.log"
[ "\$1" = "is-active" ] && exit 0
exit 0
EOF
chmod +x "$STUB/systemctl"
mv "$STUB/nsenter" "$ROOT/nsenter.disabled"
make_fake_proc 900 fail2ban-server 4026532111 "253:0"
: >"$ROOT/udevadm.log"
: >"$ROOT/systemctl.log"
write_fstab
: >"$ROOT/mnt_state"
KIN_RELEASE_WAIT_BACKING_ONLY=1 KIN_RELEASE_PROC_ROOT="$PROCROOT" \
  KIN_RELEASE_TEST_MAJMIN="253:0" \
  KIN_RELEASE_FUSER_RETRIES=2 KIN_RELEASE_FUSER_DELAY=0 \
  "$SCRIPT" >/dev/null 2>&1
mv "$ROOT/nsenter.disabled" "$STUB/nsenter"
if grep -q 'stop-exec-queue' "$ROOT/udevadm.log" \
  && grep -q 'start-exec-queue' "$ROOT/udevadm.log" \
  && grep -q 'start udisks2' "$ROOT/systemctl.log"; then
  pass "failed handoff still restarts the udev exec queue and udisks2"
else
  bad "udev exec queue / udisks2 must be restored even when the handoff fails"
fi

# settle must be issued BEFORE the queue is paused (it can never drain after).
if [ -n "$(awk '/settle/{s=NR} /stop-exec-queue/{q=NR} END{if (s && q && s<q) print "ok"}' "$ROOT/udevadm.log")" ]; then
  pass "udevadm settle runs before the exec queue is paused"
else
  bad "settle after stop-exec-queue can never drain; it must come first"
fi
rm -f "$STUB/udevadm" "$STUB/systemctl"

if [ "$fails" -eq 0 ]; then
  printf 'ALL OK\n'
  exit 0
fi
printf '%s failure(s)\n' "$fails"
exit 1
