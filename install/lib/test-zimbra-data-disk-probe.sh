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

act=$(zimbra_install_verify_action "mid_handoff")
if [ "$act" = "skip_mid_handoff" ]; then
  pass "verify_action: mid-handoff skips live zmcontrol"
else
  bad "verify_action mid-handoff: [$act]"
fi

act=$(zimbra_install_verify_action "healthy")
if [ "$act" = "verify_live" ]; then
  pass "verify_action: healthy still verifies live"
else
  bad "verify_action healthy: [$act]"
fi

act=$(zimbra_install_verify_action "none")
if [ "$act" = "verify_live" ]; then
  pass "verify_action: fresh install still verifies live"
else
  bad "verify_action none: [$act]"
fi

act=$(full_install_zimbra_stage_action "1")
if [ "$act" = "skip_mid_handoff" ]; then
  pass "full_install stage: mid-handoff skips zimbra stages"
else
  bad "full_install stage mid: [$act]"
fi

act=$(full_install_zimbra_stage_action "0")
if [ "$act" = "run" ]; then
  pass "full_install stage: normal path still runs"
else
  bad "full_install stage run: [$act]"
fi

act=$(full_install_hardening_mode "1" "1")
if [ "$act" = "os_only" ]; then
  pass "hardening_mode: mid-handoff forces os_only even if -d exists"
else
  bad "hardening_mode mid: [$act]"
fi

act=$(full_install_hardening_mode "0" "0")
if [ "$act" = "os_only" ]; then
  pass "hardening_mode: missing dir still os_only"
else
  bad "hardening_mode missing: [$act]"
fi

act=$(full_install_hardening_mode "0" "1")
if [ "$act" = "full" ]; then
  pass "hardening_mode: live tree still full"
else
  bad "hardening_mode full: [$act]"
fi

# zimbra_is_mid_handoff: live zmcontrol means not mid-handoff
export KIN_ZIMBRA_DIR="$tmp/zimbra"
export KIN_DRBD_DATA_DISK="/dev/null"
if ! zimbra_is_mid_handoff; then
  pass "zimbra_is_mid_handoff: false when bin/zmcontrol on tree"
else
  bad "zimbra_is_mid_handoff true despite live zmcontrol"
fi

# Empty mountpoint + non-block disk -> not mid-handoff (no false positive)
KIN_ZIMBRA_DIR="$tmp/empty"
if ! zimbra_is_mid_handoff; then
  pass "zimbra_is_mid_handoff: false without data-disk install"
else
  bad "zimbra_is_mid_handoff true without data disk"
fi

if grep -q 'zimbra_install_skip_action' ../03-install-zimbra.sh \
  && grep -q 'zimbra-data-disk-probe.sh' ../03-install-zimbra.sh \
  && grep -q 'skip_mid_handoff' ../03-install-zimbra.sh \
  && grep -q 'zimbra_install_verify_action' ../03-install-zimbra.sh \
  && grep -q 'SKIP_REASON' ../03-install-zimbra.sh; then
  pass "03-install-zimbra.sh uses shared probe, mid-handoff skip, and verify action"
else
  bad "03-install-zimbra.sh missing shared probe / verify wiring"
fi

if grep -q 'zimbra-data-disk-probe.sh' ./prepare-zimbra-data-disk.sh; then
  pass "prepare-zimbra-data-disk.sh sources the shared probe"
else
  bad "prepare-zimbra-data-disk.sh does not source the shared probe"
fi

if grep -q 'full_install_zimbra_stage_action' ../kin-mail.sh \
  && grep -q 'zimbra_is_mid_handoff' ../kin-mail.sh \
  && grep -q 'full_install_hardening_mode' ../kin-mail.sh; then
  pass "kin-mail.sh driver uses mid-handoff stage/hardening decisions"
else
  bad "kin-mail.sh missing mid-handoff driver wiring"
fi

for stage in 04-tls-dkim.sh 05-healthcheck.sh 06-hybrid-auth.sh 07-zpush.sh 09-hardening.sh 11-admin-path-lockdown.sh; do
  if grep -q 'zimbra_is_mid_handoff' "../${stage}"; then
    pass "${stage} has mid-handoff gate"
  else
    bad "${stage} missing mid-handoff gate"
  fi
done

if [ "$fails" -ne 0 ]; then
  printf 'FAILED %s checks\n' "$fails"
  exit 1
fi
printf 'All zimbra-data-disk-probe helper tests passed\n'
exit 0
