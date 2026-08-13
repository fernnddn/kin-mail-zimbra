#!/bin/bash
# Count of successful pcs stonith history events completed AFTER the cutoff.
# Cutoff excludes the 2026-08-13 morning fence-test (last event 09:01:20 +07).
set -euo pipefail
CUTOFF='2026-08-13 09:05:00'
cutoff_epoch="$(date -d "$CUTOFF" +%s)"
hist="$(/usr/sbin/pcs stonith history 2>/dev/null || true)"
count=0
while IFS= read -r line; do
  ts="$(echo "$line" | sed -n "s/.*completed='\([^']*\)'.*/\1/p")"
  [[ -n "$ts" ]] || continue
  epoch="$(date -d "$ts" +%s 2>/dev/null || true)"
  [[ -n "${epoch:-}" ]] || continue
  if [[ "$epoch" -gt "$cutoff_epoch" ]]; then
    count=$((count + 1))
  fi
done <<< "$hist"
echo "$count"
