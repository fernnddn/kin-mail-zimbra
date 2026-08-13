#!/bin/bash
# Returns 1 if DRBD resource is Established with both disks UpToDate, else 0.
set -euo pipefail
out="$(/usr/sbin/drbdadm status 2>/dev/null || true)"
echo "$out" | grep -q 'replication:Established' || { echo 0; exit 0; }
echo "$out" | grep -q 'disk:UpToDate' || { echo 0; exit 0; }
echo "$out" | grep -q 'peer-disk:UpToDate' || { echo 0; exit 0; }
echo 1
