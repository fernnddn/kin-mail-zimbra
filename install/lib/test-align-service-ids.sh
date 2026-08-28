#!/usr/bin/env bash
# Tests for the peer service-id aligner.
#
# What this protects: /opt/zimbra is replicated over DRBD and its ownership is
# numeric, so if `zimbra` is a different uid on the peer, Zimbra can never run
# there. That is not a degraded state, it is a node that fails over into an
# outage - and it cost two QA rounds of a node fencing itself every three
# minutes before anyone thought to compare `id zimbra` on the two hosts.
set -u

LIB="$(cd "$(dirname "$0")" && pwd)/align-service-ids.sh"
[ -f "$LIB" ] || { echo "missing $LIB" >&2; exit 1; }

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

# A fake account database: "name:kind:id" lines that the stubs read and write.
DB="$TMP/db"; ACTIONS="$TMP/actions"

cat >"$TMP/bin/getent" <<'STUB'
#!/usr/bin/env bash
# getent passwd|group <name-or-id>
kind=$1; key=$2
while IFS=: read -r name k id; do
  [ "$k" = "$kind" ] || continue
  if [ "$name" = "$key" ] || [ "$id" = "$key" ]; then
    echo "${name}:x:${id}:"; exit 0
  fi
done <"$DB"
exit 2
STUB

cat >"$TMP/bin/id" <<'STUB'
#!/usr/bin/env bash
[ "$1" = "-u" ] || exit 1
while IFS=: read -r name k idv; do
  [ "$k" = passwd ] && [ "$name" = "$2" ] && { echo "$idv"; exit 0; }
done <"$DB"
exit 1
STUB

setid() {  # setid <name> <kind> <newid>
  awk -F: -v n="$1" -v k="$2" -v v="$3" 'BEGIN{OFS=":"} {if($1==n&&$2==k)$3=v; print}' "$DB" >"$DB.t"
  mv "$DB.t" "$DB"
}
export -f setid

for cmd in usermod groupmod groupadd; do
  cat >"$TMP/bin/$cmd" <<STUB
#!/usr/bin/env bash
echo "$cmd \$*" >>"\$ACTIONS"
kind=passwd; [ "$cmd" = usermod ] || kind=group
if [ "$cmd" = groupadd ]; then
  echo "\$3:group:\$2" >>"\$DB"; exit 0
fi
newid=\$2; name=\$3
awk -F: -v n="\$name" -v k="\$kind" -v v="\$newid" 'BEGIN{OFS=":"} {if(\$1==n&&\$2==k)\$3=v; print}' "\$DB" >"\$DB.t"
mv "\$DB.t" "\$DB"
STUB
done
# find must never be allowed to walk the real filesystem in a test.
cat >"$TMP/bin/find" <<'STUB'
#!/usr/bin/env bash
echo "find $*" >>"$ACTIONS"
exit 0
STUB
chmod +x "$TMP"/bin/*
export PATH="$TMP/bin:$PATH" DB ACTIONS
export KIN_ALIGN_IDS_SOURCE_ONLY=1
# shellcheck disable=SC1090
. "$LIB"

reset_db() { printf '%s\n' "$@" >"$DB"; : >"$ACTIONS"; }
idof() { awk -F: -v n="$1" -v k="$2" '$1==n&&$2==k{print $3}' "$DB"; }

# --- the real Phase 8 divergence ------------------------------------------
# primary: zimbra 997/999, postfix 996/997, postdrop gid 996
# peer:    zimbra 998/999, postfix 997/998, postdrop gid 997, console on 996
reset_db "lxd:passwd:999" "zimbra:passwd:998" "postfix:passwd:997" "kin-console:passwd:996" \
         "zimbra:group:999" "postfix:group:998" "postdrop:group:997" "kin-console:group:996"
KIN_PEER_ZIMBRA_UID=997 KIN_PEER_ZIMBRA_GID=999 \
KIN_PEER_POSTFIX_UID=996 KIN_PEER_POSTFIX_GID=997 \
KIN_PEER_POSTDROP_GID=996 \
  align_service_ids_from_env >/dev/null

[ "$(idof zimbra passwd)" = 997 ] && ok "zimbra uid now matches the primary" || bad "zimbra uid" "got $(idof zimbra passwd)"
[ "$(idof zimbra group)" = 999 ] && ok "zimbra gid already matched and was left alone" || bad "zimbra gid" "got $(idof zimbra group)"
[ "$(idof postfix passwd)" = 996 ] && ok "postfix uid now matches" || bad "postfix uid" "got $(idof postfix passwd)"
[ "$(idof postfix group)" = 997 ] && ok "postfix gid now matches" || bad "postfix gid" "got $(idof postfix group)"
[ "$(idof postdrop group)" = 996 ] && ok "postdrop gid now matches" || bad "postdrop gid" "got $(idof postdrop group)"

# Whoever was sitting on a wanted id must be moved, not overwritten.
kc_uid=$(idof kin-console passwd)
[ "$kc_uid" != 996 ] && [ "$kc_uid" -lt 990 ] && ok "the account holding a wanted uid was displaced below 990" \
  || bad "displaced account" "kin-console uid=$kc_uid"
grep -q "^usermod .* kin-console" "$ACTIONS" && ok "displacement went through usermod" || bad "displacement"

# Local ownership must be rewritten, and /opt/zimbra must never be touched:
# that is the replicated volume we are aligning TO.
grep -q -- "-uid 998" "$ACTIONS" && ok "local files owned by the old uid are rewritten" || bad "chown of old uid"
grep -q -- "-not -path /opt/zimbra/\*" "$ACTIONS" && ok "/opt/zimbra is excluded from the rewrite" \
  || bad "/opt/zimbra exclusion" "$(grep '^find' "$ACTIONS" | head -2)"

# --- already aligned: do nothing -------------------------------------------
reset_db "zimbra:passwd:997" "zimbra:group:999"
KIN_PEER_ZIMBRA_UID=997 KIN_PEER_ZIMBRA_GID=999 align_service_ids_from_env >/dev/null
[ -s "$ACTIONS" ] && bad "already aligned" "changed things anyway: $(cat "$ACTIONS")" \
  || ok "a peer that already matches is left completely alone"

# --- no ids supplied: refuse to guess --------------------------------------
reset_db "zimbra:passwd:998" "zimbra:group:999"
env -u KIN_PEER_ZIMBRA_UID -u KIN_PEER_ZIMBRA_GID -u KIN_PEER_POSTFIX_UID \
    -u KIN_PEER_POSTFIX_GID -u KIN_PEER_POSTDROP_GID \
    bash -c "KIN_ALIGN_IDS_SOURCE_ONLY=1; . '$LIB'; align_service_ids_from_env" >/dev/null
[ "$(idof zimbra passwd)" = 998 ] && ok "with no primary ids given, nothing is invented" || bad "no-ids case"

# --- a user that does not exist yet ----------------------------------------
# On a fresh peer Zimbra has not been installed, so its user is absent. Creating
# it here would fight the installer; the group is safe to pre-create.
reset_db "kin-console:passwd:996"
KIN_PEER_ZIMBRA_UID=997 KIN_PEER_ZIMBRA_GID=999 align_service_ids_from_env >/dev/null
[ -z "$(idof zimbra passwd)" ] && ok "an absent user is left for its own installer" || bad "absent user"
[ "$(idof zimbra group)" = 999 ] && ok "an absent group is created at the right gid" || bad "absent group" "got $(idof zimbra group)"

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
