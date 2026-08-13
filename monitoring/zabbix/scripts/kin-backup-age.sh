#!/bin/bash
# Age in seconds of the newest directory under /var/lib/kin-mail-backup/daily/.
# 999999999 if none (always older than the 26h trigger).
set -euo pipefail
dir="/var/lib/kin-mail-backup/daily"
if [[ ! -d "$dir" ]]; then
  echo 999999999
  exit 0
fi
newest="$(find "$dir" -mindepth 1 -maxdepth 1 -type d -printf '%T@\n' 2>/dev/null | sort -n | tail -1 || true)"
if [[ -z "${newest:-}" ]]; then
  echo 999999999
  exit 0
fi
now="$(date +%s)"
newest_i="${newest%%.*}"
echo $(( now - newest_i ))
