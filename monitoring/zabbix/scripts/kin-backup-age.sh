#!/bin/bash
# Age in seconds of the newest VERIFIED backup under /var/lib/kin-mail-backup/daily/
# (a set only counts once its .backup-ok marker exists, written by
# kin-mail-backup.sh after SHA256SUMS passes - a set that failed checksum
# verification is moved to daily/../failed/ and never has that marker, so it
# can't make a corrupt backup look "fresh" here).
# 999999999 if none (always older than the 26h trigger).
set -euo pipefail
dir="/var/lib/kin-mail-backup/daily"
if [[ ! -d "$dir" ]]; then
  echo 999999999
  exit 0
fi
newest="$(find "$dir" -mindepth 2 -maxdepth 2 -name '.backup-ok' -printf '%T@\n' 2>/dev/null | sort -n | tail -1 || true)"
if [[ -z "${newest:-}" ]]; then
  echo 999999999
  exit 0
fi
now="$(date +%s)"
newest_i="${newest%%.*}"
echo $(( now - newest_i ))
