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
# result is a node that can never take over. It cost two QA rounds of a mail
# node fencing itself every three minutes (Phase 7 and 8, Aug 2026).
#
# Run this on the peer BEFORE Zimbra is installed, so there is nothing running
# as these users and nothing on the local disk that belongs to them yet.
set -euo pipefail

align_log() { printf '%s\n' "$*"; }

# align_group <name> <wanted gid>
align_group() {
  local name="$1" want="$2" have
  case "$want" in '' | *[!0-9]*) align_log "  skip group ${name}: no id given"; return 0 ;; esac
  have=$(getent group "$name" 2>/dev/null | cut -d: -f3 || true)
  if [ -z "$have" ]; then
    align_log "  create group ${name} gid=${want}"
    groupadd -g "$want" "$name"
    return 0
  fi
  if [ "$have" = "$want" ]; then
    align_log "  group ${name} already gid=${want}"
    return 0
  fi
  # Whoever is sitting on the wanted id has to move first, or groupmod refuses.
  local squatter
  squatter=$(getent group "$want" 2>/dev/null | cut -d: -f1 || true)
  if [ -n "$squatter" ] && [ "$squatter" != "$name" ]; then
    align_log "  gid ${want} is held by ${squatter}; moving it out of the way"
    groupmod -g "$(align_free_id group)" "$squatter"
  fi
  align_log "  group ${name} gid ${have} -> ${want}"
  groupmod -g "$want" "$name"
  align_chown_gid "$have" "$want"
}

# align_user <name> <wanted uid>
align_user() {
  local name="$1" want="$2" have
  case "$want" in '' | *[!0-9]*) align_log "  skip user ${name}: no id given"; return 0 ;; esac
  have=$(id -u "$name" 2>/dev/null || true)
  if [ -z "$have" ]; then
    align_log "  user ${name} does not exist yet; leaving creation to its installer"
    return 0
  fi
  if [ "$have" = "$want" ]; then
    align_log "  user ${name} already uid=${want}"
    return 0
  fi
  local squatter
  squatter=$(getent passwd "$want" 2>/dev/null | cut -d: -f1 || true)
  if [ -n "$squatter" ] && [ "$squatter" != "$name" ]; then
    align_log "  uid ${want} is held by ${squatter}; moving it out of the way"
    usermod -u "$(align_free_id user)" "$squatter"
  fi
  align_log "  user ${name} uid ${have} -> ${want}"
  usermod -u "$want" "$name"
  align_chown_uid "$have" "$want"
}

# Lowest free id below 990, so displaced accounts land somewhere predictable
# and well clear of the range Ubuntu allocates from.
align_free_id() {
  local kind="$1" id=989
  while [ "$id" -gt 900 ]; do
    if [ "$kind" = user ]; then
      getent passwd "$id" >/dev/null 2>&1 || { printf '%s' "$id"; return 0; }
    else
      getent group "$id" >/dev/null 2>&1 || { printf '%s' "$id"; return 0; }
    fi
    id=$((id - 1))
  done
  align_log "no free id between 901 and 989" >&2
  return 1
}

# Local files only. /opt/zimbra is deliberately excluded: it is the replicated
# volume whose ownership we are aligning TO, and rewriting it here would break
# the primary.
align_chown_uid() {
  local from="$1" to="$2"
  [ "$from" = "$to" ] && return 0
  find / -xdev -uid "$from" -not -path '/opt/zimbra/*' -exec chown -h "$to" {} + 2>/dev/null || true
}

align_chown_gid() {
  local from="$1" to="$2"
  [ "$from" = "$to" ] && return 0
  find / -xdev -gid "$from" -not -path '/opt/zimbra/*' -exec chgrp -h "$to" {} + 2>/dev/null || true
}

# Ordering matters: groups before users, because usermod checks the primary
# group, and each name is aligned before the next one can be displaced onto it.
align_service_ids_from_env() {
  local any=0
  for spec in \
    "group:zimbra:${KIN_PEER_ZIMBRA_GID:-}" \
    "group:postfix:${KIN_PEER_POSTFIX_GID:-}" \
    "group:postdrop:${KIN_PEER_POSTDROP_GID:-}" \
    "user:zimbra:${KIN_PEER_ZIMBRA_UID:-}" \
    "user:postfix:${KIN_PEER_POSTFIX_UID:-}"; do
    local kind name want
    kind=${spec%%:*}
    name=${spec#*:}; name=${name%%:*}
    want=${spec##*:}
    [ -n "$want" ] || continue
    any=1
    if [ "$kind" = group ]; then align_group "$name" "$want"; else align_user "$name" "$want"; fi
  done
  [ "$any" = 1 ] || align_log "  no primary ids supplied; nothing to align"
  return 0
}

if [ "${KIN_ALIGN_IDS_SOURCE_ONLY:-0}" != "1" ]; then
  align_log "Aligning service account ids with the primary"
  align_service_ids_from_env
fi
