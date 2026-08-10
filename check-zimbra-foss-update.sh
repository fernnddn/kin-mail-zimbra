#!/usr/bin/env bash
# =============================================================================
# KIN Mail - optional Zimbra FOSS patch monitor
#
# Compares the configured Maldua FOSS build tag (from /etc/kin-mail/config)
# against the newest matching Ubuntu artefact on GitHub. Read-only; safe for cron.
#
#   ./check-zimbra-foss-update.sh
#
# Exit codes: 0 = up to date (or no local version to compare),
#             1 = update available, 2 = could not query GitHub.
# =============================================================================
set -u
cd "$(dirname "$0")"

CONF_FILE="/etc/kin-mail/config"
ZCS_VERSION=""
if [ -f "$CONF_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONF_FILE"
fi
: "${ZCS_VERSION:=}"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
case "${VERSION_ID:-22.04}" in
  24.04) PLAT=UBUNTU24_64 ;;
  *)     PLAT=UBUNTU22_64 ;;
esac

latest_url() {
  curl -sL -m 25 "https://api.github.com/repos/maldua/zimbra-foss/releases?per_page=60" 2>/dev/null \
    | grep -oE "\"browser_download_url\": *\"[^\"]*${PLAT}[^\"]*\\.tgz\"" \
    | sed -E 's/.*: *"//; s/"$//' | sort -V | tail -1
}

echo "==> KIN Mail — Zimbra FOSS patch check (${PLAT})"

if [ -n "$ZCS_VERSION" ]; then
  echo "    configured build:  ${ZCS_VERSION}"
elif [ -d /opt/zimbra ]; then
  echo "    installed hint:    $(su - zimbra -c 'zmcontrol -v' 2>/dev/null | head -1 || echo unknown)"
else
  echo "    configured build:  (none — /etc/kin-mail/config belum ada)"
fi

URL=$(latest_url)
if [ -z "$URL" ]; then
  echo "    ERROR: tidak bisa membaca rilis GitHub maldua/zimbra-foss (${PLAT})" >&2
  exit 2
fi

LATEST_FILE="${URL##*/}"
LATEST_VER=$(printf '%s' "$URL" | awk -F/ '{print $(NF-1)}')
echo "    latest on GitHub:  ${LATEST_VER}"
echo "    artefact:          ${LATEST_FILE}"

if [ -n "$ZCS_VERSION" ] && [ "$ZCS_VERSION" = "$LATEST_VER" ]; then
  echo "    status: UP TO DATE"
  exit 0
fi

if [ -n "$ZCS_VERSION" ]; then
  echo "    status: UPDATE AVAILABLE (${ZCS_VERSION} -> ${LATEST_VER})"
  exit 1
fi

echo "    status: GitHub latest reported; bandingkan manual dengan build terpasang"
exit 0
