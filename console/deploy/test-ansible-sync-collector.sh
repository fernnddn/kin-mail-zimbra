#!/usr/bin/env bash
# The mail flow collector must survive the sync onto the appliance.
#
# roles/monitoring_stack/files/kin-mail-flow-metrics.py is a symlink pointing
# OUTSIDE the ansible tree, at ../../../../monitoring/mailflow/. bootstrap.sh
# syncs only ansible/ to /opt/kin-mail-console/ansible, and monitoring/ is not
# copied to the appliance at all, so with a plain "rsync -a" (which preserves
# symlinks rather than following them) the link landed on the appliance
# dangling. The role's "Install the mail flow collector" task would then fail
# with "Could not find or access", and that collector is the only thing that
# fills the Reports tab.
#
# Nobody saw it in the field because the role failed to PARSE first, on
# ansible.builtin.systemd_service, so no task ran at all and the copy was never
# reached (live QA Phase 12, 9 Sep 2026). Fixing the module name alone would
# have moved the failure one task later and looked like the fix had not worked.
#
# This performs the real rsync into a scratch directory rather than grepping
# for a flag, because the flag is not the point: the file being readable is.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

REPO="$(cd ../.. && pwd)"
BOOTSTRAP="${REPO}/console/bootstrap.sh"
SRC="${REPO}/ansible"
COLLECTOR="roles/monitoring_stack/files/kin-mail-flow-metrics.py"

[ -f "$BOOTSTRAP" ] || { printf 'FAIL bootstrap.sh not found\n'; exit 1; }
[ -d "$SRC" ] || { printf 'FAIL ansible/ not found\n'; exit 1; }

command -v rsync >/dev/null 2>&1 || { printf 'ok  rsync not installed, skipping sync test\n'; exit 0; }

WORK="$(mktemp -d)" || exit 1
trap 'rm -rf "$WORK"' EXIT

# 1. A plain "rsync -a" must be shown to break it, so this test is known to be
#    testing something real rather than passing by construction.
rsync -a --delete "${SRC}/" "${WORK}/plain/" 2>/dev/null
if [ -r "${WORK}/plain/${COLLECTOR}" ]; then
  bad "control case: plain rsync -a unexpectedly resolved the collector (test is not proving anything)"
else
  pass "control: a plain rsync -a leaves the collector dangling, as it did in the field"
fi

# 2. The flags bootstrap.sh actually uses must land a readable collector.
rsync -a --copy-unsafe-links --delete \
  --exclude 'inventory/lab.yml' \
  --exclude 'inventory/*.local.yml' \
  --exclude '*.retry' \
  "${SRC}/" "${WORK}/real/" 2>/dev/null
if [ -r "${WORK}/real/${COLLECTOR}" ] && [ -s "${WORK}/real/${COLLECTOR}" ]; then
  pass "the collector is a readable file after the appliance sync"
else
  bad "the collector is missing or empty after the appliance sync"
fi

if [ -x "${WORK}/real/${COLLECTOR}" ]; then
  pass "the collector keeps its executable bit"
else
  bad "the collector is not executable after sync"
fi

if head -1 "${WORK}/real/${COLLECTOR}" 2>/dev/null | grep -q '^#!'; then
  pass "the collector still starts with an interpreter line"
else
  bad "the collector does not look like the real script"
fi

# 3. Symlinks that stay INSIDE the tree must remain symlinks: they resolve
#    after the sync and materialising them would duplicate shared templates.
intree_checked=0
for link in $(find "$SRC" -type l); do
  target="$(readlink "$link")"
  case "$target" in
    ../../../../*|/*) continue ;;
  esac
  rel="${link#"${SRC}/"}"
  intree_checked=$((intree_checked + 1))
  if [ -r "${WORK}/real/${rel}" ]; then
    :
  else
    bad "in-tree link does not resolve after sync: ${rel}"
  fi
done
if [ "$intree_checked" -lt 3 ]; then
  bad "expected several in-tree symlinks to check, found ${intree_checked}"
else
  pass "all ${intree_checked} in-tree symlinks still resolve after sync"
fi

# 4. bootstrap.sh must carry the flag and must refuse to finish without the
#    collector, so a future change to the layout fails loudly here instead of
#    silently shipping an appliance whose Reports tab can never fill.
if grep -q -- '--copy-unsafe-links' "$BOOTSTRAP"; then
  pass "bootstrap.sh syncs ansible with --copy-unsafe-links"
else
  bad "bootstrap.sh no longer dereferences the escaping symlink"
fi

if grep -q 'Mail flow collector missing' "$BOOTSTRAP"; then
  pass "bootstrap.sh fails loudly when the collector did not land"
else
  bad "bootstrap.sh does not verify the collector after syncing"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All ansible-sync collector tests passed\n'
else
  printf '%s test(s) failed\n' "$fails"
fi
exit "$fails"
