#!/usr/bin/env bash
# Parser, wait-gate, and rsync-verify tests for migrate-zimbra-to-drbd-disk.sh
# (no live zmcontrol; rsync fixtures use local temp dirs).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

export KIN_MIGRATE_SOURCE_ONLY=1
# shellcheck source=migrate-zimbra-to-drbd-disk.sh
. ./migrate-zimbra-to-drbd-disk.sh

STOPPED_ALL='Host mail.example.test
	amavis                  Stopped
	antivirus               Stopped
	ldap                    Stopped
	mailbox                 Stopped
	memcached               Stopped
	mta                     Stopped
	onlyoffice              Stopped
	proxy                   Stopped
	service webapp          Stopped
	zmconfigd               Stopped
'

STILL_RUNNING='Host mail.example.test
	amavis                  Stopped
	ldap                    Stopped
	mailbox                 Running
	onlyoffice              Running
	zmconfigd               Stopped
'

ALL_RUNNING='Host mail.example.test
	amavis                  Running
	antivirus               Running
	ldap                    Running
	mailbox                 Running
	memcached               Running
	mta                     Running
	onlyoffice              Running
	proxy                   Running
	service webapp          Running
	zimbra webapp           Running
	zmconfigd               Running
'

EMPTY=''
CONNECT_NOISE='Host mail.example.test
Connect: Connection refused
	ldap                    Running
	mailbox                 Stopped
'

names=$(printf '%s\n' "$STILL_RUNNING" | zimbra_status_names Running)
if [ "$(printf '%s\n' "$names" | count_nonempty_lines)" -eq 2 ] \
  && printf '%s\n' "$names" | grep -qx 'mailbox' \
  && printf '%s\n' "$names" | grep -qx 'onlyoffice'; then
  pass "parser: two Running names (mailbox, onlyoffice)"
else
  bad "parser Running names: [$names]"
fi

stopped=$(printf '%s\n' "$STOPPED_ALL" | zimbra_status_names Running)
if [ "$(printf '%s\n' "$stopped" | count_nonempty_lines)" -eq 0 ]; then
  pass "parser: all-Stopped has zero Running"
else
  bad "parser all-Stopped still Running: [$stopped]"
fi

web=$(printf '%s\n' "$ALL_RUNNING" | zimbra_status_names Running | grep -x 'service webapp' || true)
if [ "$web" = "service webapp" ]; then
  pass "parser: multi-word service name"
else
  bad "parser multi-word: [$web]"
fi

svc=$(printf '%s\n' "$STOPPED_ALL" | zimbra_status_service_count)
if [ "$svc" -eq 10 ]; then
  pass "parser: 10 Stopped service lines"
else
  bad "parser service count all-Stopped=$svc"
fi

# Unanchored grep -c Running would treat empty status as 0 Running (false stop success).
empty_run=$(printf '%s\n' "$EMPTY" | zimbra_status_names Running | count_nonempty_lines)
empty_svc=$(printf '%s\n' "$EMPTY" | zimbra_status_service_count)
if [ "$empty_run" -eq 0 ] && [ "$empty_svc" -eq 0 ]; then
  pass "parser: empty status is not a plausible service table"
else
  bad "parser empty: running=$empty_run svc=$empty_svc"
fi

noise_stopped=$(printf '%s\n' "$CONNECT_NOISE" | zimbra_status_names Stopped)
if printf '%s\n' "$noise_stopped" | grep -qx 'mailbox'; then
  pass "parser: Connect: noise ignored, mailbox still Stopped"
else
  bad "parser Connect noise: [$noise_stopped]"
fi

export KIN_MIGRATE_SKIP_PS_CHECK=1
export KIN_MIGRATE_STOP_WAIT_SEC=0
export KIN_MIGRATE_START_WAIT_SEC=0
export KIN_ZMCONTROL_STATUS_TEXT="$STOPPED_ALL"
if wait_none_running; then
  pass "wait_none_running: all Stopped succeeds on first poll"
else
  bad "wait_none_running should succeed on all-Stopped"
fi

export KIN_ZMCONTROL_STATUS_TEXT="$STILL_RUNNING"
if wait_none_running; then
  bad "wait_none_running should fail while mailbox/onlyoffice Running"
else
  pass "wait_none_running: mailbox+onlyoffice Running fails (tonight's shape)"
fi

export KIN_ZMCONTROL_STATUS_TEXT="$EMPTY"
if wait_none_running; then
  bad "wait_none_running must not treat empty status as stopped"
else
  pass "wait_none_running: empty status is not success"
fi

export KIN_ZMCONTROL_STATUS_TEXT="$ALL_RUNNING"
if wait_all_running; then
  pass "wait_all_running: all Running succeeds on first poll"
else
  bad "wait_all_running should succeed on all-Running"
fi

export KIN_ZMCONTROL_STATUS_TEXT="$STILL_RUNNING"
if wait_all_running; then
  bad "wait_all_running should fail while some Stopped"
else
  pass "wait_all_running: mixed Running/Stopped fails"
fi

export KIN_ZMCONTROL_STATUS_TEXT="$EMPTY"
if wait_all_running; then
  bad "wait_all_running must not treat empty status as all Running"
else
  pass "wait_all_running: empty status is not success"
fi

# Tonight: zmcontrol printed Done, then status still had Running.
# A 0s budget with STILL_RUNNING fails (above). With STOPPED_ALL it would
# have printed [ OK ] instead of die() after stop.
export KIN_ZMCONTROL_STATUS_TEXT="$STOPPED_ALL"
if wait_none_running; then
  pass "tonight replay: once status is all Stopped, stop gate passes"
else
  bad "tonight replay should pass on all-Stopped"
fi

# Live failure after 450160f: ldap already Stopped, zmcontrol fell back to
# cache, antispam stayed Running on every poll, every other service Stopped.
# zmantispamctl does not check processes when zmprov cannot list antispam.
TONIGHT_ANTISPAM_CACHE='Connect: Unable to determine enabled services from ldap.
Enabled services read from cache. Service list may be inaccurate.
Host mail.example.test
	amavis                  Stopped
	antispam                Running
	antivirus               Stopped
	ldap                    Stopped
	logger                  Stopped
	mailbox                 Stopped
	memcached               Stopped
	mta                     Stopped
	opendkim                Stopped
	proxy                   Stopped
	service webapp          Stopped
	snmp                    Stopped
	spell                   Stopped
	stats                   Stopped
	zimbra webapp           Stopped
	zimbraAdmin webapp      Stopped
	zimlet webapp           Stopped
	zmconfigd               Stopped
'

as_run=$(printf '%s\n' "$TONIGHT_ANTISPAM_CACHE" | zimbra_status_names Running)
as_svc=$(printf '%s\n' "$TONIGHT_ANTISPAM_CACHE" | zimbra_status_service_count)
if [ "$(printf '%s\n' "$as_run" | count_nonempty_lines)" -eq 1 ] \
  && printf '%s\n' "$as_run" | grep -qx 'antispam' \
  && [ "$as_svc" -eq 18 ]; then
  pass "parser: tonight ldap-cache dump is antispam Running, 18 services"
else
  bad "parser tonight cache dump: running=[$as_run] svc=$as_svc"
fi

export KIN_ANTISPAM_BACKING=down
filtered=$(zimbra_filter_cached_antispam "$TONIGHT_ANTISPAM_CACHE" "$as_run" 2>/dev/null)
if [ "$(printf '%s\n' "$filtered" | count_nonempty_lines)" -eq 0 ] \
  && ! printf '%s\n' "$filtered" | grep -q 'zmantispamctl'; then
  pass "filter: ldap Stopped + backing down drops cached antispam Running"
else
  bad "filter should drop antispam: [$filtered]"
fi

export KIN_ANTISPAM_BACKING=up
filtered_up=$(zimbra_filter_cached_antispam "$TONIGHT_ANTISPAM_CACHE" "$as_run" 2>/dev/null)
if printf '%s\n' "$filtered_up" | grep -qx 'antispam'; then
  pass "filter: backing up keeps antispam Running even when ldap is Stopped"
else
  bad "filter must not drop a live antispam process: [$filtered_up]"
fi

LDAP_UP_ANTISPAM='Host mail.example.test
	amavis                  Stopped
	antispam                Running
	ldap                    Running
	mailbox                 Stopped
'
export KIN_ANTISPAM_BACKING=down
filtered_ldap_up=$(zimbra_filter_cached_antispam "$LDAP_UP_ANTISPAM" $'antispam' 2>/dev/null)
if printf '%s\n' "$filtered_ldap_up" | grep -qx 'antispam'; then
  pass "filter: ldap Running keeps antispam (zmantispamctl zmprov still works)"
else
  bad "filter must not override antispam while ldap is Running: [$filtered_ldap_up]"
fi

export KIN_ANTISPAM_BACKING=down
export KIN_ZMCONTROL_STATUS_TEXT="$TONIGHT_ANTISPAM_CACHE"
if wait_none_running; then
  pass "wait_none_running: tonight antispam cache false Running succeeds when backing is down"
else
  bad "wait_none_running should pass tonight's dump once antispam backing is gone"
fi

export KIN_ANTISPAM_BACKING=up
export KIN_ZMCONTROL_STATUS_TEXT="$TONIGHT_ANTISPAM_CACHE"
if wait_none_running; then
  bad "wait_none_running must still fail when antispam backing is up"
else
  pass "wait_none_running: tonight dump still fails if antispam backing is up"
fi

unset KIN_ANTISPAM_BACKING

# Same grep the live verify gate uses: copy/delete lines count, GNU timestamp-only
# (leading '.') does not. Fixture text, no rsync required.
pending_keep=$(printf '%s\n' '.f..t...... keep.txt' 'cd+++++++++ empty-dir/' | rsync_itemize_pending)
if [ "$(printf '%s\n' "$pending_keep" | count_nonempty_lines)" -eq 0 ]; then
  pass "itemize: timestamp-only and dir-create lines are not pending"
else
  bad "itemize should ignore timestamp-only: [$pending_keep]"
fi

pending_one=$(printf '%s\n' \
  '.f..t...... keep.txt' \
  '>f.st...... log/zimbra.log' \
  '*deleting stale.pid' | rsync_itemize_pending)
if [ "$(printf '%s\n' "$pending_one" | count_nonempty_lines)" -eq 2 ] \
  && printf '%s\n' "$pending_one" | grep -q 'log/zimbra.log' \
  && printf '%s\n' "$pending_one" | grep -q 'stale.pid'; then
  pass "itemize: keeps copy and delete lines with their paths"
else
  bad "itemize pending paths: [$pending_one]"
fi

dump_out=$(dump_rsync_verify_pending $'>f.st...... log/zimbra.log')
if printf '%s\n' "$dump_out" | grep -q 'rsync verify pending (1):' \
  && printf '%s\n' "$dump_out" | grep -q 'log/zimbra.log'; then
  pass "dump: failure output includes the real itemize path"
else
  bad "dump should show path: [$dump_out]"
fi

if command -v rsync >/dev/null 2>&1; then
  rsync_fix=$(mktemp -d)
  mkdir -p "$rsync_fix/src" "$rsync_fix/dst"
  printf '%s\n' 'same' > "$rsync_fix/src/a.txt"
  printf '%s\n' 'same' > "$rsync_fix/dst/a.txt"
  ident=$(rsync_verify_pending_lines "$rsync_fix/src"/ "$rsync_fix/dst"/ -a)
  if [ "$(printf '%s\n' "$ident" | count_nonempty_lines)" -eq 0 ]; then
    pass "rsync fixture: identical trees have zero pending copy/delete lines"
  else
    bad "rsync fixture identical pending: [$ident]"
  fi
  printf '%s\n' 'only on source' > "$rsync_fix/src/only-src.txt"
  one=$(rsync_verify_pending_lines "$rsync_fix/src"/ "$rsync_fix/dst"/ -a)
  n_one=$(printf '%s\n' "$one" | count_nonempty_lines)
  if [ "$n_one" -eq 1 ] && printf '%s\n' "$one" | grep -q 'only-src.txt'; then
    pass "rsync fixture: single new file is the one pending path"
  else
    bad "rsync fixture one-file pending (n=$n_one): [$one]"
  fi
  dump_live=$(dump_rsync_verify_pending "$one")
  if printf '%s\n' "$dump_live" | grep -q 'only-src.txt'; then
    pass "dump: manufactured single-file diff prints the real path"
  else
    bad "dump of live itemize missing path: [$dump_live]"
  fi
  rm -rf "$rsync_fix"
else
  bad "rsync not installed; cannot run tree fixture tests"
fi

if [ "$fails" -ne 0 ]; then
  printf '%s\n' "FAILED $fails test(s)"
  exit 1
fi
printf '%s\n' "All migrate-zimbra tests passed"
exit 0
