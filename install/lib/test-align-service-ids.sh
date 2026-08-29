#!/usr/bin/env bash
# Tests for the peer service-id aligner.
#
# What this protects: /opt/zimbra is replicated over DRBD and its ownership is
# numeric, so if `zimbra` is a different uid on the peer, Zimbra can never run
# there. That is not a degraded state, it is a node that fails over into an
# outage - and it cost two QA rounds of a node fencing itself every three
# minutes before anyone thought to compare `id zimbra` on the two hosts.
#
# The case that matters most is the one a fresh peer is always in: the account
# does NOT exist yet. An earlier version of the aligner skipped that case and
# left the number to whatever order the installers happened to run in, which is
# precisely the thing this file exists to prevent. Tests below cover both the
# fresh peer and the retried build where the account is already there.
set -u

LIB="$(cd "$(dirname "$0")" && pwd)/align-service-ids.sh"
[ -f "$LIB" ] || { echo "missing $LIB" >&2; exit 1; }

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; [ -n "${2:-}" ] && echo "    $2"; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

# A fake account database: "name:kind:id" lines that the stubs read and write.
DB="$TMP/db"; ACTIONS="$TMP/actions"; BUSY="$TMP/busy"

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
  # Refuse a duplicate gid, exactly as the real groupadd does.
  while IFS=: read -r n k i; do
    [ "\$k" = group ] && [ "\$i" = "\$2" ] && exit 4
  done <"\$DB"
  echo "\$3:group:\$2" >>"\$DB"; exit 0
fi
newid=\$2; name=\$3
# The real usermod refuses while the account still has processes running.
if [ "$cmd" = usermod ] && [ -s "\$BUSY" ] && grep -qx "\$name" "\$BUSY"; then
  grep -vx "\$name" "\$BUSY" >"\$BUSY.t" 2>/dev/null; mv "\$BUSY.t" "\$BUSY"
  echo "usermod: user \$name is currently used by process 999" >&2
  exit 8
fi
awk -F: -v n="\$name" -v k="\$kind" -v v="\$newid" 'BEGIN{OFS=":"} {if(\$1==n&&\$2==k)\$3=v; print}' "\$DB" >"\$DB.t"
mv "\$DB.t" "\$DB"
STUB
done

cat >"$TMP/bin/useradd" <<'STUB'
#!/usr/bin/env bash
echo "useradd $*" >>"$ACTIONS"
uid=""; name=""
while [ $# -gt 0 ]; do
  case "$1" in
    -u) uid=$2; shift 2 ;;
    -g|-d|-s) shift 2 ;;
    -r|-M) shift ;;
    *) name=$1; shift ;;
  esac
done
while IFS=: read -r n k i; do
  [ "$k" = passwd ] && [ "$i" = "$uid" ] && exit 4
done <"$DB"
echo "${name}:passwd:${uid}" >>"$DB"
STUB

# find must never be allowed to walk the real filesystem in a test.
cat >"$TMP/bin/find" <<'STUB'
#!/usr/bin/env bash
echo "find $*" >>"$ACTIONS"
exit 0
STUB

for cmd in pkill systemctl timeout su; do
  cat >"$TMP/bin/$cmd" <<STUB
#!/usr/bin/env bash
echo "$cmd \$*" >>"\$ACTIONS"
exit 0
STUB
done
# Nothing is ever still running once we have signalled it, so the wait loop
# in align_release_account exits on its first pass.
cat >"$TMP/bin/pgrep" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$TMP"/bin/*
export PATH="$TMP/bin:$PATH" DB ACTIONS BUSY
export KIN_ALIGN_CHOWN_ROOTS="/"
export KIN_ALIGN_IDS_SOURCE_ONLY=1
# shellcheck disable=SC1090
. "$LIB"

reset_db() { printf '%s\n' "$@" >"$DB"; : >"$ACTIONS"; : >"$BUSY"; }
idof() { awk -F: -v n="$1" -v k="$2" '$1==n&&$2==k{print $3}' "$DB"; }

primary_env() {
  KIN_PEER_ZIMBRA_UID=997 KIN_PEER_ZIMBRA_GID=999 \
  KIN_PEER_POSTFIX_UID=996 KIN_PEER_POSTFIX_GID=997 \
  KIN_PEER_POSTDROP_GID=996 "$@"
}

# --- a fresh peer: none of the accounts exist yet ---------------------------
# This is the real deployment of 29 Aug 2026. Zimbra has not been installed, so
# both users are absent; the aligner has to RESERVE their numbers, because if it
# waits for the installers they allocate whatever is free and the pair is dead.
reset_db "lxd:passwd:999" "systemd-coredump:passwd:998"
primary_env align_service_ids_from_env >/dev/null
rc_fresh=$?

[ "$rc_fresh" = 0 ] && ok "a fresh peer aligns cleanly" || bad "fresh peer rc" "rc=$rc_fresh"
[ "$(idof zimbra passwd)" = 997 ] && ok "absent zimbra user is reserved at the primary's uid" \
  || bad "zimbra uid on fresh peer" "got '$(idof zimbra passwd)' (the Phase 8 bug: left absent)"
[ "$(idof postfix passwd)" = 996 ] && ok "absent postfix user is reserved at the primary's uid" \
  || bad "postfix uid on fresh peer" "got '$(idof postfix passwd)'"
[ "$(idof zimbra group)" = 999 ] && ok "absent zimbra group is created at the primary's gid" || bad "zimbra gid"
[ "$(idof postfix group)" = 997 ] && ok "absent postfix group is created at the primary's gid" || bad "postfix gid"
[ "$(idof postdrop group)" = 996 ] && ok "absent postdrop group is created at the primary's gid" || bad "postdrop gid"
grep -q -- "useradd .*-g zimbra .*-d /opt/zimbra .*-s /bin/bash" "$ACTIONS" \
  && ok "reserved zimbra user gets its real group, home and shell" \
  || bad "useradd attributes" "$(grep '^useradd' "$ACTIONS")"
primary_env align_verify_ids_from_env && ok "verify agrees a fresh peer is aligned" || bad "verify on fresh peer"

# --- the same divergence, but on a retried build ---------------------------
# Here Zimbra is already installed at the wrong number and has to be renumbered.
reset_db "lxd:passwd:999" "zimbra:passwd:998" "postfix:passwd:997" "kin-console:passwd:996" \
         "zimbra:group:999" "postfix:group:998" "postdrop:group:997" "kin-console:group:996"
primary_env align_service_ids_from_env >/dev/null

[ "$(idof zimbra passwd)" = 997 ] && ok "an existing zimbra user is renumbered to match" || bad "zimbra uid" "got $(idof zimbra passwd)"
[ "$(idof zimbra group)" = 999 ] && ok "a gid that already matched is left alone" || bad "zimbra gid" "got $(idof zimbra group)"
[ "$(idof postfix passwd)" = 996 ] && ok "an existing postfix user is renumbered" || bad "postfix uid" "got $(idof postfix passwd)"
[ "$(idof postfix group)" = 997 ] && ok "an existing postfix group is renumbered" || bad "postfix gid" "got $(idof postfix group)"
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

# --- a wanted uid held by an account that is still running ------------------
# usermod refuses while processes are alive. Stop them, then renumber - and the
# stopping has to be attempted before anything is killed.
reset_db "zimbra:passwd:998" "zimbra:group:999"
printf 'zimbra\n' >"$BUSY"
KIN_PEER_ZIMBRA_UID=997 KIN_PEER_ZIMBRA_GID=999 align_service_ids_from_env >/dev/null 2>&1
[ "$(idof zimbra passwd)" = 997 ] && ok "a busy account is released and then renumbered" \
  || bad "busy account" "got $(idof zimbra passwd)"
grep -q "^pkill -TERM" "$ACTIONS" && ok "release asks processes to stop before killing them" || bad "TERM before KILL"
grep -q "^pkill -KILL" "$ACTIONS" && bad "KILL used when nothing was left running" || ok "nothing is killed once the account is idle"

# --- a wanted gid already held by another group -----------------------------
# groupadd refuses a duplicate gid, so the squatter has to move first.
reset_db "docker:group:999"
KIN_PEER_ZIMBRA_GID=999 align_service_ids_from_env >/dev/null 2>&1
[ "$(idof zimbra group)" = 999 ] && ok "a squatter on a wanted gid is moved before the group is created" \
  || bad "gid squatter on create" "zimbra gid='$(idof zimbra group)' docker gid='$(idof docker group)'"
dk=$(idof docker group)
[ -n "$dk" ] && [ "$dk" -lt 990 ] && ok "the displaced group landed below 990" || bad "displaced group" "docker gid=$dk"

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

# --- verify reports a divergence instead of hiding it -----------------------
reset_db "zimbra:passwd:998" "zimbra:group:999"
if KIN_PEER_ZIMBRA_UID=997 KIN_PEER_ZIMBRA_GID=999 align_verify_ids_from_env 2>/dev/null; then
  bad "verify" "reported a mismatched uid as aligned"
else
  ok "verify fails when a uid does not match the primary"
fi
reset_db "zimbra:group:999"
if KIN_PEER_ZIMBRA_UID=997 align_verify_ids_from_env 2>/dev/null; then
  bad "verify" "reported a missing account as aligned"
else
  ok "verify fails when the account is missing entirely"
fi

# --- the aligner must not change the caller's shell options -----------------
# It is sourced into installer stages that were not written for `set -e`.
if grep -qE '^[[:space:]]*set[[:space:]]+-[a-z]*e' "$LIB"; then
  bad "shell options" "the sourced aligner turns on -e for its caller"
else
  ok "sourcing the aligner leaves the caller's shell options alone"
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
