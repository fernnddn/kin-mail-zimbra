#!/bin/bash
# Count systemd units in a failed state. Alert-only — do not auto-restart
# Pacemaker/Corosync/SBD/DRBD from this wrapper.
#
# Exclude leftovers that are expected-failed on this stack:
#   zimbra.service     — Pacemaker ocf:kin:zimbra owns the MTA; the LSB unit is leftover
#   fwupd-refresh.service — Ubuntu firmware metadata, not mail runtime
#   openhpid.service   — leftover HP OpenHPI unit seen on the backup VM
set -euo pipefail
n="$(
  systemctl --failed --no-legend --plain --state=failed 2>/dev/null \
    | awk '{print $1}' \
    | grep -vE '^(zimbra|fwupd-refresh|openhpid)\.service$' \
    | grep -c . || true
)"
echo "${n:-0}"
