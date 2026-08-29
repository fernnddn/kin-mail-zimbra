#!/usr/bin/env bash
# KIN Mail - make a peer's service account ids match the primary's.
#
# /opt/zimbra lives on DRBD and is replicated byte for byte, ownership
# included - and ownership on disk is numeric. If `zimbra` is uid 997 on the
# primary and 998 on the peer, then every one of those 8000 files belongs to
# somebody else as far as the peer is concerned, and Zimbra cannot start there
# at all. It fails on something as small as creating its own ldapi socket:
#
#   slap_open_listener: failed on ldapi:///   bind(8) failed errno=13
#
# which reads as a permission problem with no obvious cause, because the
# permissions are fine - it is the identity that moved.
#
# The ids diverge for a dull reason: Ubuntu allocates system uids downward from
# 999 in creation order, and the console user is created at a different point in
# the sequence on a peer than on a primary. Nothing warns about it, and the
# result is a node that can never take over.
#
# RESERVE, do not wait. The first version of this file created the groups but
# skipped any user that did not exist yet, on the theory that creating `zimbra`
# early would fight Zimbra's installer. That theory was wrong twice over. It is
# wrong because a peer is exactly the case where the user does NOT exist yet -
# so the aligner did nothing at all where it was needed most - and it is wrong
# because both installers create these accounts only `if` they are missing, so
# an account already sitting at the right number is simply used. The proof came
# from the same run that exposed the bug: the groups this file pre-created
# survived a full Zimbra install untouched, at exactly the gids it pinned.
# Deployment of 29 Aug 2026 stopped at step 12 of 14 for this.
#
# Run this on the peer BEFORE Zimbra is installed. It is also safe to re-run on
# a peer that is already installed - a resumed or retried build - where it
# renumbers in place instead, stopping anything still holding the old id.
#
# No `set -e` here: this file is SOURCED into installer stages that manage
# their own error handling, and turning on -e/pipefail underneath a caller that
# was not written for it changes how every later line in that stage behaves.
# Every function below reports failure through its return code instead.

align_log() { printf '%s\n' "$*"; }
align_warn() { printf '%s\n' "$*" >&2; }

# How each account is created when it does not exist yet. These match what the
# installers themselves would create, so the installer finds nothing to do.
align_user_home() {
  case "$1" in
  zimbra) printf '/opt/zimbra' ;;
  postfix) printf '/var/spool/postfix' ;;
  *) printf '/nonexistent' ;;
  esac
}

align_user_shell() {
  case "$1" in
  zimbra) printf '/bin/bash' ;;
  *) printf '/usr/sbin/nologin' ;;
  esac
}

# align_group <name> <wanted gid>
align_group() {
  local name="$1" want="$2" have
  case "$want" in '' | *[!0-9]*)
    align_log "  skip group ${name}: no id given"
    return 0
    ;;
  esac
  have=$(getent group "$name" 2>/dev/null | cut -d: -f3)
  if [ -z "$have" ]; then
    align_displace_group "$want" "$name" || return 1
    align_log "  create group ${name} gid=${want}"
    if ! groupadd -g "$want" "$name"; then
      align_warn "  could not create group ${name} at gid ${want}"
      return 1
    fi
    return 0
  fi
  if [ "$have" = "$want" ]; then
    align_log "  group ${name} already gid=${want}"
    return 0
  fi
  align_displace_group "$want" "$name" || return 1
  align_log "  group ${name} gid ${have} -> ${want}"
  if ! groupmod -g "$want" "$name"; then
    align_warn "  could not move group ${name} from gid ${have} to ${want}"
    return 1
  fi
  align_chown_gid "$have" "$want"
}

# align_user <name> <wanted uid> [primary group name]
align_user() {
  local name="$1" want="$2" group="${3:-$1}" have
  case "$want" in '' | *[!0-9]*)
    align_log "  skip user ${name}: no id given"
    return 0
    ;;
  esac
  have=$(id -u "$name" 2>/dev/null)
  if [ -z "$have" ]; then
    align_reserve_user "$name" "$want" "$group"
    return $?
  fi
  if [ "$have" = "$want" ]; then
    align_log "  user ${name} already uid=${want}"
    return 0
  fi
  align_displace_user "$want" "$name" || return 1
  align_log "  user ${name} uid ${have} -> ${want}"
  if ! align_usermod_uid "$name" "$want"; then
    align_warn "  could not move user ${name} from uid ${have} to ${want}"
    return 1
  fi
  align_chown_uid "$have" "$want"
}

# Create the account now, at the primary's number, so that whichever installer
# would have created it later finds it already present and leaves it alone.
align_reserve_user() {
  local name="$1" want="$2" group="$3" gopt home shell
  align_displace_user "$want" "$name" || return 1
  if getent group "$group" >/dev/null 2>&1; then
    gopt="$group"
  else
    align_warn "  group ${group} is absent; letting useradd pick a gid for ${name}"
    gopt=""
  fi
  home=$(align_user_home "$name")
  shell=$(align_user_shell "$name")
  align_log "  reserve user ${name} uid=${want} (its installer will find it already here)"
  if [ -n "$gopt" ]; then
    useradd -r -u "$want" -g "$gopt" -d "$home" -s "$shell" -M "$name"
  else
    useradd -r -u "$want" -d "$home" -s "$shell" -M "$name"
  fi || {
    align_warn "  could not create user ${name} at uid ${want}"
    return 1
  }
  return 0
}

# usermod refuses to renumber an account that still has processes running. On a
# retried build Zimbra is already installed here, so make room first - and only
# then, because killing is never the opening move.
align_usermod_uid() {
  local name="$1" want="$2"
  usermod -u "$want" "$name" 2>/dev/null && return 0
  align_log "  user ${name} is still in use; stopping what holds it"
  align_release_account "$name"
  usermod -u "$want" "$name"
}

# Stop the services that run as this account, then signal whatever is left.
# Only processes owned by this account are touched, and never uid 0.
align_release_account() {
  local name="$1" uid
  uid=$(id -u "$name" 2>/dev/null) || return 0
  [ -n "$uid" ] && [ "$uid" != 0 ] || return 0
  if [ "$name" = zimbra ] && [ -x /opt/zimbra/bin/zmcontrol ]; then
    timeout 300 su - zimbra -c '/opt/zimbra/bin/zmcontrol stop' >/dev/null 2>&1
  fi
  case "$name" in
  postfix) systemctl stop postfix >/dev/null 2>&1 ;;
  kin-console) systemctl stop kin-mail-console kin-mail-privhelper >/dev/null 2>&1 ;;
  esac
  # Without both of these there is no way to see what is left, and escalating
  # to KILL on no information is not something this should ever do.
  command -v pkill >/dev/null 2>&1 && command -v pgrep >/dev/null 2>&1 || return 0
  pkill -TERM -u "$uid" >/dev/null 2>&1
  local waited=0
  while [ "$waited" -lt "${KIN_ALIGN_STOP_WAIT:-20}" ]; do
    pgrep -u "$uid" >/dev/null 2>&1 || return 0
    sleep 1
    waited=$((waited + 1))
  done
  align_warn "  processes still running as ${name} (uid ${uid}); killing them"
  pkill -KILL -u "$uid" >/dev/null 2>&1
  sleep 1
  return 0
}

# Move whoever is already sitting on a wanted id out of the way. groupmod and
# usermod both refuse to duplicate an id, so this has to happen first.
align_displace_user() {
  local want="$1" keep="$2" squatter free
  squatter=$(getent passwd "$want" 2>/dev/null | cut -d: -f1)
  [ -n "$squatter" ] && [ "$squatter" != "$keep" ] || return 0
  free=$(align_free_id user) || return 1
  align_log "  uid ${want} is held by ${squatter}; moving it to ${free}"
  if ! align_usermod_uid "$squatter" "$free"; then
    align_warn "  could not move ${squatter} off uid ${want}"
    return 1
  fi
  align_chown_uid "$want" "$free"
}

align_displace_group() {
  local want="$1" keep="$2" squatter free
  squatter=$(getent group "$want" 2>/dev/null | cut -d: -f1)
  [ -n "$squatter" ] && [ "$squatter" != "$keep" ] || return 0
  free=$(align_free_id group) || return 1
  align_log "  gid ${want} is held by ${squatter}; moving it to ${free}"
  if ! groupmod -g "$free" "$squatter"; then
    align_warn "  could not move ${squatter} off gid ${want}"
    return 1
  fi
  align_chown_gid "$want" "$free"
}

# Lowest free id below 990, so displaced accounts land somewhere predictable
# and well clear of the range Ubuntu allocates from.
align_free_id() {
  local kind="$1" id=989
  while [ "$id" -gt 900 ]; do
    if [ "$kind" = user ]; then
      getent passwd "$id" >/dev/null 2>&1 || {
        printf '%s' "$id"
        return 0
      }
    else
      getent group "$id" >/dev/null 2>&1 || {
        printf '%s' "$id"
        return 0
      }
    fi
    id=$((id - 1))
  done
  align_warn "no free id between 901 and 989"
  return 1
}

# Every locally-backed filesystem, so a separate /var or /home is rewritten too.
# /opt/zimbra is deliberately excluded: it is the replicated volume whose
# ownership we are aligning TO, and rewriting it here would break the primary.
align_chown_roots() {
  if [ -n "${KIN_ALIGN_CHOWN_ROOTS:-}" ]; then
    # Deliberately unquoted: a space-separated list of roots, used by the tests.
    # shellcheck disable=SC2086
    printf '%s\n' ${KIN_ALIGN_CHOWN_ROOTS}
    return 0
  fi
  if ! command -v findmnt >/dev/null 2>&1; then
    printf '/\n'
    return 0
  fi
  findmnt -rn -o TARGET,FSTYPE 2>/dev/null | while read -r target fstype; do
    case "$fstype" in
    ext2 | ext3 | ext4 | xfs | btrfs | f2fs | jfs | reiserfs) ;;
    *) continue ;;
    esac
    case "$target" in
    /opt/zimbra | /opt/zimbra/*) continue ;;
    esac
    printf '%s\n' "$target"
  done
}

align_chown_uid() {
  local from="$1" to="$2"
  [ "$from" = "$to" ] && return 0
  align_chown_roots | while IFS= read -r root; do
    [ -n "$root" ] || continue
    find "$root" -xdev -uid "$from" -not -path '/opt/zimbra/*' \
      -exec chown -h "$to" {} + 2>/dev/null
  done
  return 0
}

align_chown_gid() {
  local from="$1" to="$2"
  [ "$from" = "$to" ] && return 0
  align_chown_roots | while IFS= read -r root; do
    [ -n "$root" ] || continue
    find "$root" -xdev -gid "$from" -not -path '/opt/zimbra/*' \
      -exec chgrp -h "$to" {} + 2>/dev/null
  done
  return 0
}

# The accounts that own files on the replicated volume, and the env var each
# one's id arrives in. kin-console is deliberately absent: it owns nothing on
# /opt/zimbra, so its number is free to differ between the nodes.
align_specs() {
  cat <<'SPECS'
group zimbra KIN_PEER_ZIMBRA_GID
group postfix KIN_PEER_POSTFIX_GID
group postdrop KIN_PEER_POSTDROP_GID
user zimbra KIN_PEER_ZIMBRA_UID zimbra
user postfix KIN_PEER_POSTFIX_UID postfix
SPECS
}

# Ordering matters: groups before users, because a reserved user needs its
# primary group to exist at the right gid before it is created.
align_service_ids_from_env() {
  local any=0 rc=0 kind name var group want
  while read -r kind name var group; do
    [ -n "$kind" ] || continue
    eval "want=\${$var:-}"
    [ -n "$want" ] || continue
    any=1
    if [ "$kind" = group ]; then
      align_group "$name" "$want" || rc=1
    else
      align_user "$name" "$want" "${group:-$name}" || rc=1
    fi
  done <<EOF
$(align_specs)
EOF
  if [ "$any" != 1 ]; then
    align_log "  no primary ids supplied; nothing to align"
    return 0
  fi
  [ "$rc" = 0 ] || align_warn "  one or more ids could not be aligned"
  return "$rc"
}

# Read-only check that the pinned ids actually took. Used after Zimbra installs
# on a peer, so a divergence is caught on the node where it happened instead of
# eleven steps later when the cluster refuses to build.
align_verify_ids_from_env() {
  local rc=0 kind name var group want have
  while read -r kind name var group; do
    [ -n "$kind" ] || continue
    eval "want=\${$var:-}"
    [ -n "$want" ] || continue
    if [ "$kind" = group ]; then
      have=$(getent group "$name" 2>/dev/null | cut -d: -f3)
    else
      have=$(id -u "$name" 2>/dev/null)
    fi
    [ -n "$have" ] || have=absent
    if [ "$have" != "$want" ]; then
      align_warn "  ${kind} ${name} is ${have}, the primary has ${want}"
      rc=1
    fi
  done <<EOF
$(align_specs)
EOF
  return "$rc"
}

# Run directly - `bash align-service-ids.sh` with the KIN_PEER_* ids in the
# environment - and this is a complete program: align, then prove it took. That
# is how the console repairs a peer whose Zimbra was installed outside the HA
# sequence, where nothing pinned the numbers in the first place.
if [ "${KIN_ALIGN_IDS_SOURCE_ONLY:-0}" != "1" ]; then
  align_log "Aligning service account ids with the primary"
  if ! align_service_ids_from_env; then
    return 1 2>/dev/null || exit 1
  fi
  if ! align_verify_ids_from_env; then
    align_warn "service account ids still disagree with the primary"
    return 1 2>/dev/null || exit 1
  fi
  align_log "Service account ids match the primary"
fi
