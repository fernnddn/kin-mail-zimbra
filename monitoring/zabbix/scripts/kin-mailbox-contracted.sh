#!/bin/bash
# CONTRACTED_SEATS from /etc/kin-mail/config (0 if unset / PLACEHOLDER_UNSET).
# Read-only parse — does not source the file (it contains secrets and $(...)).
set -euo pipefail

conf="/etc/kin-mail/config"
if [[ ! -r "$conf" ]]; then
  echo 0
  exit 0
fi
seats="$(awk -F= '$1=="CONTRACTED_SEATS"{v=$2; gsub(/^[ \t"]+|[ \t"]+$/,"",v); print v; exit}' "$conf")"
if [[ -z "${seats:-}" || "$seats" == "PLACEHOLDER_UNSET" ]]; then
  echo 0
  exit 0
fi
case "$seats" in
  ''|*[!0-9]*)
    echo 0
    exit 0
    ;;
esac
echo "$seats"
