#!/usr/bin/env bash
# Tests for stage 04's TLS decisions.
#
# An HA pair serves ONE certificate, for the service hostname, and it lives on
# the replicated volume. A peer that issues its own certificate for its own
# name installs a renewal hook that writes into that same volume, so the first
# renewal after the peer becomes Primary replaces the service certificate with
# one issued for mail2 - and every client gets a name mismatch.
set -u

STAGE="$(cd "$(dirname "$0")/.." && pwd)/04-tls-dkim.sh"
[ -f "$STAGE" ] || { echo "missing $STAGE" >&2; exit 1; }

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; }

# --- a split mailbox must not issue the public certificate ------------------
if grep -q 'This is the mailbox node; skipping' "$STAGE" &&
   grep -q 'kin_topology_is_split' "$STAGE"; then
  ok "TLS/DKIM is skipped on the mailbox of a split"
else
  bad "stage 04 would issue a certificate on a node that does not serve :443"
fi

# --- the peer must not issue its own certificate ----------------------------
# Anchor on the dispatch, not on prose: the file explains this at length.
dispatch=$(awk '/^if kin_ha_peer_install; then/,/^fi$/' "$STAGE" | head -40)
if printf '%s' "$dispatch" | grep -q "tls_cloudflare" &&
   printf '%s' "$dispatch" | grep -q "not issuing a TLS certificate"; then
  ok "certificate issuance is inside a kin_ha_peer_install guard"
else
  bad "the TLS method dispatch is not guarded for an HA peer" "$dispatch"
fi

# The three methods must all still be reachable on a primary.
for m in tls_cloudflare tls_manual tls_customer; do
  if grep -qE "^\s+(cloudflare|manual|customer)\)\s+${m} ;;" "$STAGE"; then
    ok "method still dispatched: ${m}"
  else
    bad "method no longer dispatched: ${m}"
  fi
done

# --- the deploy hook must not fail on a node that is not serving ------------
if grep -q 'if \[ ! -x /opt/zimbra/bin/zmcertmgr \]; then' "$STAGE" &&
   grep -A6 'if \[ ! -x /opt/zimbra/bin/zmcertmgr \]; then' "$STAGE" | grep -q "exit 0"; then
  ok "the renewal hook exits 0 when this node is not serving Zimbra"
else
  bad "the hook would fail on a Secondary, where /opt/zimbra is not mounted"
fi

# It still has to refuse a lineage that is not ours.
grep -q 'RENEWED_LINEAGE' "$STAGE" \
  && ok "the hook only acts on its own certificate lineage" \
  || bad "no lineage guard in the deploy hook"

# And it must still restore Pacemaker management on every exit path.
grep -q 'trap remanage_kin_zimbra EXIT' "$STAGE" \
  && ok "kin-zimbra is remanaged even if the hook exits early" \
  || bad "no EXIT trap remanaging kin-zimbra"

# --- renewal has to be checked, not assumed ---------------------------------
if grep -q 'certbot renew --dry-run' "$STAGE" && grep -q 'systemctl is-active --quiet "\$timer"' "$STAGE"; then
  ok "renewal is both dry-run tested and confirmed to be scheduled"
else
  bad "nothing verifies that a timer will actually run the renewal"
fi
grep -q 'masked' "$STAGE" \
  && ok "a masked renewal timer is called out rather than ignored" \
  || bad "a masked timer would pass silently"

# --- no fragile word-splitting for optional flags ---------------------------
if grep -qE '\$\(\[ "\$\{KIN_TLS_FORCE_RENEW' "$STAGE"; then
  bad "optional certbot flags still rely on unquoted command substitution"
else
  ok "optional certbot flags use an array, not word splitting"
fi

# --- the ISRG root must be verified, not just downloaded --------------------
# `curl -s` without -f exits 0 on an HTTP error and writes the error page to
# the file. Let's Encrypt's 404 is 23KB of HTML, so a non-empty check passes
# and the deploy hook appends that HTML to chain.pem at every renewal.
if grep -q 'curl -fsS' "$STAGE"; then
  ok "the ISRG root download fails on an HTTP error status"
else
  bad "the ISRG root is fetched with a curl that ignores HTTP errors"
fi
if grep -q 'isrg_root_is_valid' "$STAGE"; then
  ok "the downloaded bytes are checked to be the expected certificate"
else
  bad "nothing verifies that the download is actually a certificate"
fi

# And the check itself has to work. Extract it and run it for real.
vfy=$(mktemp "${TMPDIR:-/tmp}/kin-isrg-vfy.XXXXXX")
awk '/^isrg_root_is_valid\(\) \{/,/^\}/' "$STAGE" > "$vfy"
# shellcheck disable=SC1090
. "$vfy"
rm -f "$vfy"

work=$(mktemp -d "${TMPDIR:-/tmp}/kin-isrg.XXXXXX")
: > "$work/empty.pem"
printf '<!DOCTYPE html>\n<html><body>Not Found</body></html>\n' > "$work/404.pem"
printf 'not a certificate at all\n' > "$work/junk.pem"
openssl req -x509 -newkey rsa:2048 -nodes -keyout /dev/null \
  -out "$work/other.pem" -days 1 -subj "/CN=not-the-root" >/dev/null 2>&1

isrg_root_is_valid "$work/empty.pem" && bad "an empty file is accepted" || ok "an empty file is rejected"
isrg_root_is_valid "$work/404.pem"   && bad "a 404 HTML page is accepted" || ok "a 404 HTML page is rejected"
isrg_root_is_valid "$work/junk.pem"  && bad "junk is accepted" || ok "junk is rejected"
isrg_root_is_valid "$work/other.pem" && bad "the wrong certificate is accepted" || ok "a different certificate is rejected"
isrg_root_is_valid "$work/missing.pem" && bad "a missing file is accepted" || ok "a missing file is rejected"

# A real ISRG Root X1 must be accepted, or every install fails at this step.
# Built locally so the test needs no network.
cat > "$work/subject-probe.pem" <<'PEM'
placeholder
PEM
if openssl req -x509 -newkey rsa:2048 -nodes -keyout /dev/null \
     -out "$work/looks-right.pem" -days 1 \
     -subj "/C=US/O=Internet Security Research Group/CN=ISRG Root X1" >/dev/null 2>&1; then
  isrg_root_is_valid "$work/looks-right.pem" \
    && ok "a certificate whose subject is ISRG Root X1 is accepted" \
    || bad "the real root would be rejected, failing every install"
else
  ok "openssl cannot build a probe certificate here; skipped"
fi
rm -rf "$work"

# --- a certificate is not worth an unfirewalled server -----------------------
# Every failure inside a TLS method calls exit 1. That ended the stage, and
# because 04 sat in the hard-failing part of the pipeline it ended the install:
# no DKIM key, no hardening, no host firewall, no admin-path lockdown, no
# healthcheck. On a split that is the edge - the machine facing the internet -
# left running with none of them, because a CA could not verify a DNS record
# that a human had 25 minutes to publish.
STAGE04="$(cd "$(dirname "$0")/.." && pwd)/04-tls-dkim.sh"
MAIN="$(cd "$(dirname "$0")/.." && pwd)/kin-mail.sh"

if grep -q 'TLS_PHASE_OK' "$STAGE04"; then
  ok "a failed TLS phase is recorded rather than ending the stage"
else
  bad "the TLS methods still take the whole stage down with them"
fi
# The isolation has to be a subshell, because the failures are exit calls
# scattered through three methods and their helpers.
if awk '/^TLS_PHASE_OK=1$/,/TLS_PHASE_OK=0/' "$STAGE04" | grep -q '^  ($'; then
  ok "the TLS methods run in a subshell, so their exits end only that phase"
else
  bad "the TLS methods are not isolated; an exit inside one still ends the stage"
fi
# DKIM is independent of TLS and must still be created.
dkim_line=$(grep -n 'say "DKIM"' "$STAGE04" | head -1 | cut -d: -f1)
tls_line=$(grep -n 'TLS_PHASE_OK" -eq 0' "$STAGE04" | head -1 | cut -d: -f1)
exit_line=$(grep -n 'TLS_PHASE_OK" -eq 0' "$STAGE04" | tail -1 | cut -d: -f1)
if [ -n "$dkim_line" ] && [ -n "$tls_line" ] && [ -n "$exit_line" ] \
   && [ "$tls_line" -lt "$dkim_line" ] && [ "$dkim_line" -lt "$exit_line" ]; then
  ok "DKIM is created even when the certificate was not, and the stage still exits non-zero"
else
  bad "DKIM sits on the wrong side of the TLS failure (tls=$tls_line dkim=$dkim_line exit=$exit_line)"
fi
# ...and the pipeline carries on to the stages that secure the host.
if grep -q 'soft_failed_stages+=("04-tls-dkim.sh")' "$MAIN"; then
  ok "the pipeline continues past a failed certificate and records it"
else
  bad "a failed certificate still stops the install before hardening and firewall"
fi
# It must stay honest about it: silent success here would be worse than the
# original bug, because nobody would ever look.
if grep -q 'Full install complete, with warnings' "$MAIN"; then
  ok "the transcript says the install finished with warnings"
else
  bad "a soft-failed stage would be reported as a clean install"
fi

# --- an empty admin list must not mean an unfirewalled host ------------------
# The wizard calls Admin IPs optional and says they can be narrowed later from
# the console. While blank meant the firewall stage refused, that sentence was
# false in the most expensive possible way: ufw never ran at all, so every port
# stayed open - SSH and the console included - on a host that had just been
# told it was finished.
if grep -q 'KIN_ADMIN_IPS is empty - applying the firewall' "$MAIN"; then
  ok "a console install with no admin IPs still applies the firewall"
else
  bad "an empty admin list still stops the firewall stage from running"
fi
FW="$(cd "$(dirname "$0")/.." && pwd)/10-host-firewall.sh"
if grep -q 'No admin IPs configured. This host will still be firewalled' "$FW"; then
  ok "the firewall stage itself no longer refuses without an admin list"
else
  bad "10-host-firewall still exits 2 on a non-mailbox node with no admin IPs"
fi
if grep -qE 'exit 2' "$FW" && grep -q 'MAILBOX_IP is not set' "$FW"; then
  ok "it still refuses the things it genuinely cannot guess (a missing peer address)"
else
  bad "the refusals that protect against guessing were removed along with the rest"
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
