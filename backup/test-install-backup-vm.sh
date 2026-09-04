#!/usr/bin/env bash
# Tests for the backup VM installer.
#
# These run the installer for real against a staging root (DESTDIR), so they
# exercise the same code path a backup VM does rather than grepping the source.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="$HERE/install-backup-vm.sh"
[ -x "$INSTALLER" ] || { echo "missing $INSTALLER" >&2; exit 1; }

t_pass=0
t_fail=0
t_ok()  { t_pass=$((t_pass + 1)); echo "ok  $1"; }
t_bad() { t_fail=$((t_fail + 1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; return 0; }

stage() { D=$(mktemp -d "${TMPDIR:-/tmp}/kin-inst.XXXXXX"); }
run()   { DESTDIR="$D" bash "$INSTALLER" "$@" >"$D/.out" 2>&1; echo $?; }
out()   { cat "$D/.out"; }

# --- 1. a fresh install puts every piece in place ---------------------------
stage
rc=$(run)
[ "$rc" = "0" ] && t_ok "a fresh install exits 0" || t_bad "install exited $rc" "$(out)"
for f in usr/local/sbin/kin-mail-backup.sh \
         usr/local/sbin/kin-mail-backup-remote.sh \
         usr/local/sbin/kin-mail-restore.sh \
         etc/kin-mail-backup/config \
         etc/kin-mail-backup/id_ed25519 \
         etc/cron.d/kin-mail-backup \
         etc/logrotate.d/kin-mail-backup; do
  [ -f "$D/$f" ] && t_ok "installed /$f" || t_bad "missing /$f"
done
[ -x "$D/usr/local/sbin/kin-mail-backup.sh" ] \
  && t_ok "the orchestrator is executable" || t_bad "orchestrator is not executable"

# The orchestrator resolves the collector relative to its own directory. If
# they land apart, every run dies at "Collector script not next to orchestrator".
sbin_dir=$(dirname "$D/usr/local/sbin/kin-mail-backup.sh")
[ -f "$sbin_dir/kin-mail-backup-remote.sh" ] \
  && t_ok "the collector installs beside the orchestrator" \
  || t_bad "collector is not next to the orchestrator"

# --- 2. the cron entry actually runs the thing and keeps a log --------------
cron="$D/etc/cron.d/kin-mail-backup"
grep -q 'kin-mail-backup\.sh' "$cron" \
  && t_ok "cron invokes the orchestrator" || t_bad "cron does not invoke the orchestrator" "$(cat "$cron")"
grep -q '>>.*kin-mail-backup\.log' "$cron" \
  && t_ok "cron output is redirected to the log the runbook names" \
  || t_bad "cron discards its output - the runbook's log would always be empty"
grep -qE '^[0-9]+ [0-9]+ \* \* \* +root ' "$cron" \
  && t_ok "cron runs daily as root" || t_bad "cron schedule/user line is malformed" "$(cat "$cron")"
# A cron.d file with a bad mode is silently ignored by cron.
mode=$(stat -f '%Lp' "$cron" 2>/dev/null || stat -c '%a' "$cron" 2>/dev/null)
[ "$mode" = "644" ] && t_ok "cron.d file is 0644 (cron ignores other modes)" \
                    || t_bad "cron.d mode is $mode, want 644"

# --- 3. re-running must not rotate the SSH key or clobber the config --------
# This is the one that would be silent: a new key means every mail node still
# trusts the old one, and backups start failing at the next unattended run.
key_before=$(cat "$D/etc/kin-mail-backup/id_ed25519.pub")
printf 'MAIL_NODES="203.0.113.10 203.0.113.11"\nDAILY_KEEP=9\n' > "$D/etc/kin-mail-backup/config"
conf_before=$(cat "$D/etc/kin-mail-backup/config")
rc=$(run)
[ "$rc" = "0" ] && t_ok "re-running exits 0 (idempotent)" || t_bad "re-run exited $rc" "$(out)"
[ "$(cat "$D/etc/kin-mail-backup/id_ed25519.pub")" = "$key_before" ] \
  && t_ok "re-running keeps the existing SSH identity" \
  || t_bad "re-running generated a NEW SSH key - mail nodes would still trust the old one"
[ "$(cat "$D/etc/kin-mail-backup/config")" = "$conf_before" ] \
  && t_ok "re-running keeps the operator's config" \
  || t_bad "re-running overwrote the config"
out | grep -q 'MAIL_NODES is set' \
  && t_ok "a configured install reports itself ready" || t_bad "configured install did not report ready" "$(out)"
rm -rf "$D"

# --- 4. the example addresses are never mistaken for a real config ----------
stage
run >/dev/null
out | grep -q 'NOT READY' \
  && t_ok "an unconfigured install says NOT READY rather than looking installed" \
  || t_bad "the template config was treated as ready" "$(out)"
rc=$(run --check)
[ "$rc" != "0" ] \
  && t_ok "--check fails while MAIL_NODES holds the example addresses" \
  || t_bad "--check passed on a config that backs up nothing"
out | grep -q 'example addresses' \
  && t_ok "--check names the example addresses as the problem" || t_bad "--check does not explain why"
rm -rf "$D"

# --- 5. --check notices a missing schedule ----------------------------------
stage
run >/dev/null
printf 'MAIL_NODES="203.0.113.10"\n' > "$D/etc/kin-mail-backup/config"
rm -f "$D/etc/cron.d/kin-mail-backup"
rc=$(run --check)
[ "$rc" != "0" ] && t_ok "--check fails when nothing is scheduled" \
                 || t_bad "--check passed with no cron entry"
out | grep -q 'nothing is taking a nightly backup' \
  && t_ok "--check says plainly that no backup is scheduled" \
  || t_bad "--check did not name the missing schedule" "$(out)"

# --- 6. --uninstall drops the schedule and keeps the data -------------------
run >/dev/null                       # reinstall the cron entry
mkdir -p "$D/var/lib/kin-mail-backup/daily/20260101T020000Z"
touch "$D/var/lib/kin-mail-backup/daily/20260101T020000Z/.backup-ok"
rc=$(run --uninstall)
[ "$rc" = "0" ] && t_ok "--uninstall exits 0" || t_bad "--uninstall exited $rc" "$(out)"
[ ! -f "$D/etc/cron.d/kin-mail-backup" ] \
  && t_ok "--uninstall removes the schedule" || t_bad "--uninstall left the cron entry"
[ -d "$D/var/lib/kin-mail-backup/daily/20260101T020000Z" ] \
  && t_ok "--uninstall never deletes a backup" \
  || t_bad "--uninstall deleted backup data"
[ -f "$D/usr/local/sbin/kin-mail-restore.sh" ] \
  && t_ok "--uninstall leaves the restore script (you may still need it)" \
  || t_bad "--uninstall removed the restore script"
rm -rf "$D"

# --- 7. an unknown argument is refused, not ignored -------------------------
stage
rc=$(run --wat)
[ "$rc" = "2" ] && t_ok "an unknown argument exits 2 instead of installing" \
               || t_bad "unknown argument returned $rc"
[ ! -d "$D/usr/local/sbin" ] \
  && t_ok "a rejected argument installs nothing" || t_bad "a rejected run still wrote files"
rm -rf "$D"

# --- 8. the schedule and the runbook's freshness window agree ---------------
RUNBOOK="$HERE/../HA-RUNBOOK.md"
if [ -f "$RUNBOOK" ]; then
  if grep -q '15 2 \* \* \*' "$RUNBOOK"; then
    t_ok "the runbook documents the same schedule the installer writes"
  else
    t_bad "the runbook's cron schedule no longer matches the installer"
  fi
fi

echo
if [ "$t_fail" -eq 0 ]; then echo "ALL OK ($t_pass checks)"; exit 0; fi
echo "FAILED $t_fail test(s) ($t_pass ok)"; exit 1
