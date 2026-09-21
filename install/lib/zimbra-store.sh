#!/usr/bin/env bash
# =============================================================================
# KIN Mail - which machine runs mailboxd, and how to reach it with zmprov
#
# Source this after 00-config.sh (needs zimbra_cmd, and the topology helpers
# it already loads):
#
#   . ./00-config.sh
#   . ./lib/zimbra-store.sh
#
# WHY THIS EXISTS
#
# zmprov on a node without a local mailboxd falls back to LDAP-only mode and
# does not say so. Most subcommands are unaffected - ca, ms, mc, mcf, md, sp
# and every read were measured working that way on 21 Sep 2026. Three are not:
#
#   da, ra           refuse to act unattended. They print
#                    "Continue? [Y]es, [N]o", read end-of-file, print
#                    "aborted" - AND EXIT 0. A caller checking the exit status
#                    is told the mailbox was deleted while it is still there.
#   getQuotaUsage    refused outright: "can only be used with SOAP".
#   fc (flushCache)  same refusal, so a cache flush silently never happens.
#
# The fix in every case is to name the SOAP target with -s. That is one line,
# and it is exactly the kind of one line that gets written differently in two
# places and then drifts - which is why it lives here rather than in each
# stage that needs it.
#
# On a single appliance this resolves to the host itself and changes nothing.
# =============================================================================

# The machine holding the mail store, as a hostname zmprov can be aimed at.
# Empty output means "not known" - callers fall back rather than passing -s "".
kin_mailbox_server() {
  if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
    kin_mailbox_host
    return 0
  fi
  zimbra_cmd zmhostname 2>/dev/null | tr -d '\r'
}

# zmprov, aimed at the machine that can actually carry the request out.
kin_zmprov_on_store() {
  local host
  host=$(kin_mailbox_server | head -1)
  if [ -n "$host" ]; then
    zimbra_cmd zmprov -s "$host" "$@"
  else
    # No idea which node holds the store: better to try than to refuse. Every
    # caller checks the result rather than the exit status anyway.
    zimbra_cmd zmprov "$@"
  fi
}

# Does mailboxd run on THIS machine?
#
# The binary is present on every node - Zimbra ships zmmailboxdctl regardless -
# so its existence proves nothing. What decides it is whether this node was
# built with the mailbox service at all.
kin_mailboxd_is_local() {
  if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
    local role
    role=$(kin_node_role 2>/dev/null) || return 1
    [ "$role" = "mailbox" ]
    return $?
  fi
  return 0
}
