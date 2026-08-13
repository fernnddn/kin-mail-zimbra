#!/bin/bash
# Qdevice vote count from corosync-quorumtool (0 if qdevice missing).
set -euo pipefail
out="$(/usr/sbin/corosync-quorumtool -s 2>/dev/null || true)"
votes="$(echo "$out" | awk '/^[[:space:]]*0[[:space:]]+/ && /Qdevice/ { print $2; exit }')"
if [[ -z "${votes:-}" ]]; then
  echo 0
  exit 0
fi
echo "$votes"
