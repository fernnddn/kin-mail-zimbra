#!/usr/bin/env bash
# Unit tests for the kin-zimbra OCF agent's mount-holder release.
#
# This path decides whether a node gets fenced. `stop` returning an error
# leaves Pacemaker one option - reset the node - so on 28 Aug 2026 a single
# leftover `sleep` owned by zimbra rebooted a mail node every three minutes.
# The rules that matter are: clear what is safe to clear, and never signal the
# cluster or system processes whose death would be worse than the fence.
set -u

AGENT="$(cd "$(dirname "$0")/../.." && pwd)/ansible/roles/pacemaker_agents/files/zimbra"
[ -f "$AGENT" ] || { echo "missing agent: $AGENT" >&2; exit 1; }

pass=0; fail=0
ok()   { pass=$((pass+1)); echo "ok  $1"; }
bad()  { fail=$((fail+1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A fake /proc where each pid has the comm we want to test against.
mkproc() {
  local root="$1"; shift
  rm -rf "$root"; mkdir -p "$root"
  while [ $# -gt 0 ]; do
    mkdir -p "$root/$1"; printf '%s\n' "$2" >"$root/$1/comm"
    shift 2
  done
}

# Source only the helpers, with ocf_log and kill stubbed out.
load_agent() {
  cat >"$TMP/harness.sh" <<'HARNESS'
OCF_SUCCESS=0; OCF_ERR_GENERIC=1; OCF_ERR_INSTALLED=5
ocf_log() { shift; printf '%s\n' "$*" >>"$LOGFILE"; }
kill() { printf 'kill %s\n' "$*" >>"$KILLFILE"; }
HARNESS
  # Take the two helper functions out of the agent without running the
  # dispatch at the bottom.
  sed -n '/^zimbra_holder_is_protected()/,/^}/p;/^zimbra_release_holders()/,/^}/p' \
    "$AGENT" >"$TMP/funcs.sh"
  . "$TMP/harness.sh"
  . "$TMP/funcs.sh"
}

LOGFILE="$TMP/log"; KILLFILE="$TMP/kills"
: >"$LOGFILE"; : >"$KILLFILE"
load_agent

# --- who must never be signalled -------------------------------------------
export KIN_OCF_PROC_ROOT="$TMP/proc"
mkproc "$KIN_OCF_PROC_ROOT" \
  100 sbd 101 corosync 102 pacemakerd 103 pacemaker-controld \
  104 sshd 105 systemd 106 systemd-udevd 107 iscsid 108 drbdsetup \
  200 sleep 201 java 202 zmmailboxdmgr

for pid in 100 101 102 103 104 105 106 107 108; do
  comm=$(cat "$KIN_OCF_PROC_ROOT/$pid/comm")
  if zimbra_holder_is_protected "$pid"; then ok "protected: $comm"
  else bad "protected: $comm" "would have been killed"; fi
done

# Killing sbd or pacemaker to avoid a fence is self-defeating, and killing
# sshd removes the only way to fix whatever went wrong.
for pid in 200 201 202; do
  comm=$(cat "$KIN_OCF_PROC_ROOT/$pid/comm")
  if zimbra_holder_is_protected "$pid"; then bad "clearable: $comm" "was treated as protected"
  else ok "clearable: $comm"; fi
done

if zimbra_holder_is_protected 1; then ok "protected: pid 1"; else bad "protected: pid 1"; fi
if zimbra_holder_is_protected "$$"; then ok "protected: our own pid"; else bad "protected: our own pid"; fi
if zimbra_holder_is_protected ""; then ok "protected: empty pid"; else bad "protected: empty pid"; fi
if zimbra_holder_is_protected "notanumber"; then ok "protected: non-numeric pid"; else bad "protected: non-numeric"; fi
# A pid that has already gone has no comm; it must not be signalled blindly.
if zimbra_holder_is_protected 999999; then bad "vanished pid" "treated as protected"; else ok "a vanished pid is still a kill candidate (kill is a no-op)"; fi

# --- releasing the mount ----------------------------------------------------
# fuser is stubbed: it reports holders until a marker file says they are gone.
mkfuser() {
  cat >"$TMP/bin/fuser" <<FUSER
#!/usr/bin/env bash
if [ -f "$TMP/released" ]; then exit 1; fi
case " \$* " in *" -v"*) echo "  /dev/drbd0: root kernel mount /opt/zimbra" >&2;; esac
echo "$1"
exit 0
FUSER
  chmod +x "$TMP/bin/fuser"
}
mkdir -p "$TMP/bin"; PATH="$TMP/bin:$PATH"

# The real case: one clearable straggler that goes away on TERM.
rm -f "$TMP/released"; : >"$KILLFILE"; : >"$LOGFILE"
mkfuser "200"
cat >"$TMP/bin/fuser" <<FUSER
#!/usr/bin/env bash
# Holds until a TERM has been recorded, then reports free.
if grep -q -- "-TERM 200" "$KILLFILE" 2>/dev/null; then exit 1; fi
echo "200"
exit 0
FUSER
chmod +x "$TMP/bin/fuser"
if zimbra_release_holders /dev/drbd0; then ok "a straggler that honours TERM frees the mount"
else bad "a straggler that honours TERM frees the mount" "$(cat "$LOGFILE")"; fi
grep -q -- "-TERM 200" "$KILLFILE" && ok "TERM was sent before KILL" || bad "TERM was sent before KILL"
grep -q -- "-KILL 200" "$KILLFILE" && bad "KILL not needed" "escalated unnecessarily" || ok "no KILL when TERM was enough"

# A holder that ignores TERM must be escalated rather than left to cause a fence.
: >"$KILLFILE"; : >"$LOGFILE"
cat >"$TMP/bin/fuser" <<FUSER
#!/usr/bin/env bash
echo "200"
exit 0
FUSER
chmod +x "$TMP/bin/fuser"
KIN_OCF_HOLDER_TERM_WAIT=1 zimbra_release_holders /dev/drbd0
grep -q -- "-KILL 200" "$KILLFILE" && ok "a holder that ignores TERM is escalated to KILL" \
  || bad "a holder that ignores TERM is escalated to KILL" "$(cat "$KILLFILE")"

# Nothing but protected processes: signal none of them, and report failure so
# the caller still reports a real error rather than claiming success.
: >"$KILLFILE"; : >"$LOGFILE"
cat >"$TMP/bin/fuser" <<FUSER
#!/usr/bin/env bash
echo "100 104"
exit 0
FUSER
chmod +x "$TMP/bin/fuser"
if KIN_OCF_HOLDER_TERM_WAIT=1 zimbra_release_holders /dev/drbd0; then
  bad "protected-only holders" "claimed the mount was released"
else ok "protected-only holders are not signalled and not claimed as released"; fi
[ -s "$KILLFILE" ] && bad "protected-only holders" "sent: $(cat "$KILLFILE")" || ok "no signal sent to sbd or sshd"

# No holders at all: nothing to do, and no signals.
: >"$KILLFILE"
cat >"$TMP/bin/fuser" <<'FUSER'
#!/usr/bin/env bash
exit 1
FUSER
chmod +x "$TMP/bin/fuser"
if zimbra_release_holders /dev/drbd0; then bad "no holders" "reported a release it did not perform"
else ok "no holders means nothing to release"; fi
[ -s "$KILLFILE" ] && bad "no holders" "sent signals anyway" || ok "no signals when there is nothing holding the mount"

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
