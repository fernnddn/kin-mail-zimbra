#!/usr/bin/env bash
# Parser and wait-gate tests for migrate-zimbra-to-drbd-disk.sh (no live zmcontrol).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

export KIN_MIGRATE_SOURCE_ONLY=1
# shellcheck source=migrate-zimbra-to-drbd-disk.sh
. ./migrate-zimbra-to-drbd-disk.sh

STOPPED_ALL='Host mail.nisaroti.my.id
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

STILL_RUNNING='Host mail.nisaroti.my.id
	amavis                  Stopped
	ldap                    Stopped
	mailbox                 Running
	onlyoffice              Running
	zmconfigd               Stopped
'

ALL_RUNNING='Host mail.nisaroti.my.id
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
CONNECT_NOISE='Host mail.nisaroti.my.id
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

if [ "$fails" -ne 0 ]; then
  printf '%s\n' "FAILED $fails test(s)"
  exit 1
fi
printf '%s\n' "All migrate-zimbra status tests passed"
exit 0
