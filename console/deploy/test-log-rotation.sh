#!/usr/bin/env bash
# The appliance's own logs must not grow for ever.
#
# privhelperd writes the audit trail through a WatchedFileHandler, a handler
# whose entire purpose is to cooperate with logrotate by reopening the file
# after it is replaced. No rotation was ever installed for it, so on an
# appliance expected to run for years the audit log and the cluster operations
# log grew without limit. That ends as a full disk, and a full disk on a mail
# node takes mail down, which is a much worse outcome than losing old history.
#
# The backup VM has shipped a logrotate config since the beginning. The mail
# node had not.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

REPO="$(cd ../.. && pwd)"
BOOTSTRAP="${REPO}/console/bootstrap.sh"
[ -f "$BOOTSTRAP" ] || { printf 'FAIL bootstrap.sh not found\n'; exit 1; }

WORK="$(mktemp -d)" || exit 1
trap 'rm -rf "$WORK"' EXIT
CONF="${WORK}/kin-mail-console"

# Take the config out of bootstrap.sh itself rather than restating it here, so
# this tests what actually ships.
sed -n "/^cat > \/etc\/logrotate.d\/kin-mail-console <<'ROTATE'$/,/^ROTATE$/p" \
  "$BOOTSTRAP" | sed '1d;$d' > "$CONF"

if [ -s "$CONF" ]; then
  pass "bootstrap.sh ships a logrotate config for the console logs"
else
  bad "bootstrap.sh no longer installs /etc/logrotate.d/kin-mail-console"
  printf '%s test(s) failed\n' "$fails"
  exit "$fails"
fi

for log in /var/log/kin-mail/privhelper.log /var/log/kin-mail/cluster-ops.log; do
  if grep -qF "$log" "$CONF"; then
    pass "rotates ${log}"
  else
    bad "${log} is not rotated"
  fi
done

# The audit log is root-owned and kin-console must never be able to write it.
# Rotation recreating it with looser ownership would quietly undo that.
if grep -q 'create 0640 root root' "$CONF"; then
  pass "rotated logs are recreated root-owned and not group-writable"
else
  bad "rotation does not preserve root:root 0640 on the audit log"
fi

# deploy-last.log is truncated at the start of every deploy, so it is already
# bounded; rotating it mid-install would pull the transcript out from under the
# console's log viewer.
if grep -q 'deploy-last.log' "$CONF"; then
  bad "deploy-last.log must not be rotated: it is truncated per deploy and the console reads it live"
else
  pass "deploy-last.log is deliberately left alone"
fi

if command -v logrotate >/dev/null 2>&1; then
  if out=$(logrotate --debug --state "${WORK}/state" "$CONF" 2>&1); then
    pass "logrotate parses the shipped config"
  else
    bad "logrotate rejected the config: $(printf '%s' "$out" | tail -3)"
  fi
  if printf '%s' "$out" | grep -q "Handling 2 logs"; then
    pass "logrotate sees both stanzas"
  else
    bad "logrotate did not read the expected number of stanzas"
  fi
else
  pass "logrotate not installed here, config checked by inspection only"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All log rotation tests passed\n'
else
  printf '%s test(s) failed\n' "$fails"
fi
exit "$fails"
