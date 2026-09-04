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

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
