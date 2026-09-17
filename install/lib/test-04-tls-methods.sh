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

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
