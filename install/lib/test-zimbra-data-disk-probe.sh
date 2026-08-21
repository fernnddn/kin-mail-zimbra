#!/usr/bin/env bash
# Decision helpers for zimbra-data-disk-probe.sh (no live disks).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# shellcheck source=zimbra-data-disk-probe.sh
. ./zimbra-data-disk-probe.sh

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/zimbra/bin" "$tmp/empty"
: >"$tmp/zimbra/bin/zmcontrol"
chmod +x "$tmp/zimbra/bin/zmcontrol"
echo x >"$tmp/empty/orphan"

if mount_has_zmcontrol "$tmp/zimbra"; then
  pass "mount_has_zmcontrol: real tree"
else
  bad "mount_has_zmcontrol missed bin/zmcontrol"
fi
if ! mount_has_zmcontrol "$tmp/empty"; then
  pass "mount_has_zmcontrol: leftover-only tree is false"
else
  bad "mount_has_zmcontrol true on leftover-only tree"
fi

act=$(zimbra_install_skip_action "1" "0")
if [ "$act" = "skip_healthy" ]; then
  pass "skip_action: already healthy"
else
  bad "skip_action healthy: [$act]"
fi

act=$(zimbra_install_skip_action "0" "1")
if [ "$act" = "skip_mid_handoff" ]; then
  pass "skip_action: mid-handoff with real data-disk install"
else
  bad "skip_action mid-handoff: [$act]"
fi

act=$(zimbra_install_skip_action "0" "0")
if [ "$act" = "fail_broken" ]; then
  pass "skip_action: unreadable/partial still fails closed"
else
  bad "skip_action fail_broken: [$act]"
fi

# Prefer healthy over mid-handoff when both signals are true.
act=$(zimbra_install_skip_action "1" "1")
if [ "$act" = "skip_healthy" ]; then
  pass "skip_action: healthy wins when both signals set"
else
  bad "skip_action both: [$act]"
fi

if grep -q 'zimbra_install_skip_action' ../03-install-zimbra.sh \
  && grep -q 'zimbra-data-disk-probe.sh' ../03-install-zimbra.sh \
  && grep -q 'skip_mid_handoff' ../03-install-zimbra.sh; then
  pass "03-install-zimbra.sh uses the shared probe and mid-handoff skip"
else
  bad "03-install-zimbra.sh missing shared probe wiring"
fi

if grep -q 'zimbra-data-disk-probe.sh' ./prepare-zimbra-data-disk.sh; then
  pass "prepare-zimbra-data-disk.sh sources the shared probe"
else
  bad "prepare-zimbra-data-disk.sh does not source the shared probe"
fi

if [ "$fails" -ne 0 ]; then
  printf 'FAILED %s checks\n' "$fails"
  exit 1
fi
printf 'All zimbra-data-disk-probe helper tests passed\n'
exit 0
