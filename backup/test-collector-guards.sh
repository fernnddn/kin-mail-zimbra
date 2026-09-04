#!/usr/bin/env bash
# Tests for the collector's staging guards.
#
# This script runs as root on a LIVE Promoted mail node and its first act is
# `rm -rf $STAGING`, where $STAGING arrives from a config file on another
# machine. It then copies the whole mail store onto the OS disk. Both of those
# are worth proving rather than reading.
set -u

SRC="$(cd "$(dirname "$0")" && pwd)/kin-mail-backup-remote.sh"
[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

t_pass=0
t_fail=0
t_ok()  { t_pass=$((t_pass + 1)); echo "ok  $1"; }
t_bad() { t_fail=$((t_fail + 1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; return 0; }

LIB=$(mktemp "${TMPDIR:-/tmp}/kin-collector.XXXXXX")
{
  echo 'say(){ :; }; ok(){ :; }; warn(){ echo "WARN: $*"; }; fail(){ echo "FAIL: $*"; }'
  awk '/^assert_safe_staging\(\) \{/,/^\}/'  "$SRC"
  awk '/^human\(\) \{/,/^\}/'                "$SRC"
  awk '/^assert_staging_space\(\) \{/,/^\}/' "$SRC"
} > "$LIB"
# shellcheck disable=SC1090
. "$LIB"
rm -f "$LIB"

for fn in assert_safe_staging assert_staging_space human; do
  declare -F "$fn" >/dev/null 2>&1 || { echo "FAILED  could not extract $fn" >&2; exit 1; }
done
t_ok "the staging guards were extracted and are callable"

# assert_safe_staging exits on refusal, so call it in a subshell - and under
# the same `set -euo pipefail` the collector itself runs with, or these would
# be testing the functions under flags they never see in production.
refuses() { ( set -euo pipefail; assert_safe_staging "$1" ) >/dev/null 2>&1; [ $? -ne 0 ]; }
allows()  { ( set -euo pipefail; assert_safe_staging "$1" ) >/dev/null 2>&1; }

# --- paths that must never reach `rm -rf` ----------------------------------
for bad in / /etc /var /usr /opt /root /home /boot /etc/kin-mail /usr/local \
           /opt/zimbra /opt/zimbra/store relative/path "" /var/tmp/.. /tmp//x; do
  if refuses "$bad"; then
    t_ok "refuses staging '$bad'"
  else
    t_bad "ACCEPTED dangerous staging path '$bad'"
  fi
done

# --- the real default, and other legitimate scratch paths -------------------
for good in /var/tmp/kin-mail-backup-staging /srv/backup-staging/kin \
            /mnt/scratch/kin-mail /data/staging; do
  if allows "$good"; then
    t_ok "allows staging '$good'"
  else
    t_bad "refused a legitimate staging path '$good'"
  fi
done

# The path the orchestrator actually sends must be one of the allowed ones.
default=$(grep -m1 'REMOTE_STAGING="' "$(dirname "$SRC")/kin-mail-backup.conf.example" | sed 's/.*"\(.*\)".*/\1/')
if [ -n "$default" ] && allows "$default"; then
  t_ok "the shipped REMOTE_STAGING default passes its own guard ($default)"
else
  t_bad "the shipped REMOTE_STAGING default would be refused" "$default"
fi

# --- free-space refusal -----------------------------------------------------
# Stub du/df so the decision runs for real without a mail store present.
STORE_BYTES=0
AVAIL_BYTES=0
du() { echo "$STORE_BYTES  stub"; }
df() { printf 'avail\n%s\n' "$AVAIL_BYTES"; }
# The function only sizes directories that exist; point it at real ones.
space_rc() {  # space_rc <store bytes> <avail bytes>
  STORE_BYTES="$1"; AVAIL_BYTES="$2"
  ( KIN_BACKUP_SPACE_FACTOR=150 assert_staging_space /var/tmp/kin-x ) >/dev/null 2>&1
  echo $?
}

if [ -d /opt/zimbra/store ]; then
  echo "note: /opt/zimbra/store exists here; space tests use the real tree"
else
  # Create the paths the function looks for, so the stubs are consulted.
  FAKEROOT=$(mktemp -d "${TMPDIR:-/tmp}/kin-fake.XXXXXX")
  t_ok "no live Zimbra tree here - exercising the sizing logic via stubs"
fi

# 100GB of mail, 40GB free: 150% of 100GB is 150GB > 40GB, must refuse.
# 100GB of mail, 400GB free: must allow.
if [ -d /opt/zimbra/store ]; then
  t_ok "skipping stubbed space assertions on a live node"
else
  # Re-extract with the source dirs pointed at a temp tree we control.
  probe() {  # probe <store bytes> <avail bytes> -> rc
    local sb="$1" ab="$2"
    (
      say(){ :; }; ok(){ :; }; warn(){ :; }; fail(){ :; }
      human() { printf '%sB' "${1:-0}"; }
      du() { echo "$sb  x"; }
      df() { printf 'avail\n%s\n' "$ab"; }
      # shellcheck disable=SC2317
      set -- /var/tmp/kin-x
      KIN_BACKUP_SPACE_FACTOR=150
      set -euo pipefail
      eval "$(awk '/^assert_staging_space\(\) \{/,/^\}/' "$SRC" \
              | sed 's#/opt/zimbra/store /opt/zimbra/index /opt/zimbra/redolog#'"$FAKEROOT"'/store#')"
      assert_staging_space /var/tmp/kin-x
    ) >/dev/null 2>&1
    echo $?
  }
  mkdir -p "$FAKEROOT/store"
  rc=$(probe $((100 * 1024 * 1024 * 1024)) $((40 * 1024 * 1024 * 1024)))
  [ "$rc" = "4" ] \
    && t_ok "refuses when the OS disk cannot hold the mail store (exit 4)" \
    || t_bad "did not refuse an impossible staging copy (rc=$rc)"
  rc=$(probe $((100 * 1024 * 1024 * 1024)) $((400 * 1024 * 1024 * 1024)))
  [ "$rc" = "0" ] \
    && t_ok "allows the copy when there is room" \
    || t_bad "refused a copy that fits (rc=$rc)"
  # Exactly 150% must pass, not fail on an off-by-one.
  rc=$(probe $((100 * 1024 * 1024 * 1024)) $((150 * 1024 * 1024 * 1024)))
  [ "$rc" = "0" ] \
    && t_ok "exactly the required size is allowed" \
    || t_bad "off-by-one at the threshold (rc=$rc)"
  # Unreadable sizes must not silently block backups forever.
  rc=$(probe 0 $((400 * 1024 * 1024 * 1024)))
  [ "$rc" = "0" ] \
    && t_ok "an unreadable store size warns and continues, it does not refuse" \
    || t_bad "an unsized store blocked the backup (rc=$rc)"
  rm -rf "$FAKEROOT"
fi

# --- the escape hatch and the ordering that makes the guard matter ----------
grep -q 'KIN_BACKUP_SKIP_SPACE_CHECK' "$SRC" \
  && t_ok "there is a documented override for an unusual layout" \
  || t_bad "no way to override the estimate"

# The guards are worthless if they run after the destructive line.
guard_line=$(grep -n 'assert_safe_staging "\$STAGING"' "$SRC" | head -1 | cut -d: -f1)
space_line=$(grep -n 'assert_staging_space "\$STAGING"' "$SRC" | head -1 | cut -d: -f1)
rm_line=$(grep -n '^rm -rf "\$STAGING"' "$SRC" | head -1 | cut -d: -f1)
if [ -n "$guard_line" ] && [ -n "$rm_line" ] && [ "$guard_line" -lt "$rm_line" ]; then
  t_ok "the path guard runs before rm -rf"
else
  t_bad "rm -rf runs before the path is validated" "guard=$guard_line rm=$rm_line"
fi
if [ -n "$space_line" ] && [ -n "$rm_line" ] && [ "$space_line" -lt "$rm_line" ]; then
  t_ok "the space check runs before anything is written"
else
  t_bad "the space check runs too late" "space=$space_line rm=$rm_line"
fi

# --- mailbox export accounting ---------------------------------------------
grep -q 'mailboxes_exported=' "$SRC" \
  && t_ok "the manifest records how many mailboxes were exported" \
  || t_bad "a set that exported no mailboxes looks like one that exported all"
grep -q 'MIN_TGZ_BYTES' "$SRC" \
  && t_ok "a truncated tgz is not counted as an export" \
  || t_bad "an empty getRestURL response counts as a successful export"
grep -q 'none of the \$expected mailboxes exported' "$SRC" \
  && t_ok "a total mailbox-export failure is reported loudly" \
  || t_bad "every mailbox export can fail and nothing says so"
# Deliberately NOT fatal: store, MySQL and LDAP are the restore path, so the
# set is still worth keeping - and aborting here would strand a full copy of
# the mail store on the production node's OS disk until the next run.
if awk '/mailboxes_exported=0|none of the \$expected/,/^ok "\$exported of/' "$SRC" | grep -qE '^\s*exit [0-9]'; then
  t_bad "a mailbox shortfall aborts the run and strands staging on the mail node"
else
  t_ok "a mailbox shortfall is recorded, not fatal (the set is still restorable)"
fi
grep -q 'complete=true' "$SRC" && grep -q 'complete=false' "$SRC" \
  && t_ok "the manifest states plainly whether the set is complete" \
  || t_bad "nothing in the set records whether it is complete"

echo
if [ "$t_fail" -eq 0 ]; then echo "ALL OK ($t_pass checks)"; exit 0; fi
echo "FAILED $t_fail test(s) ($t_pass ok)"; exit 1
