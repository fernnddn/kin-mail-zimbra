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

# Run one install stage on the machine that holds the mail store.
#
# Several stages act on the directory or on mailboxd, and both live with the
# mailbox. Run from the edge they would do nothing useful while reporting
# success - so they are carried across the same SSH channel the orchestrator
# already uses, rather than left as a step an operator has to remember on the
# other machine.
#
# Prints nothing of its own: the stage's own output is what the operator reads.
kin_run_stage_on_store() {
  local stage="$1"
  shift
  if kin_mailboxd_is_local; then
    "${KIN_MAIL_INSTALL_DIR}/${stage}" "$@"
    return $?
  fi
  local mb user remote_root cmd
  mb=$(kin_mailbox_ip)
  user="${MAILBOX_SSH_USER:-${KIN_OS_USER:-kin}}"
  remote_root="${KIN_REMOTE_DEPLOY_ROOT:-/opt/kin-mail-deploy}"
  if [ -z "$mb" ] || ! command -v sshpass >/dev/null 2>&1; then
    return 3
  fi
  cmd="${remote_root}/install/${stage} $*"

  # Two passwords, in order, exactly as the orchestrator does it (and for the
  # same reason).
  #
  # MAILBOX_SSH_PASS is what the operator typed on the Topology step - the
  # password that machine had BEFORE anything was installed. 02-prepare-os.sh
  # then runs chpasswd on it and sets the account to KIN_USER_PASS from the
  # Default credentials step. By the time any stage after the build wants to
  # reach the mailbox, the first password is usually the stale one.
  #
  # Trying only the first is how the user synchronisation silently never ran:
  # ssh was refused, the stage warned once in the middle of a forty-minute
  # deploy log, and the operator was left with authentication working and an
  # empty admin console (QA, 25 Sep 2026).
  local pw
  for pw in "${MAILBOX_SSH_PASS:-}" "${KIN_USER_PASS:-}"; do
    [ -n "$pw" ] || continue
    if SSHPASS="$pw" sshpass -e ssh \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=15 -o NumberOfPasswordPrompts=1 \
      -o PreferredAuthentications=password -o LogLevel=ERROR \
      "${user}@${mb}" true >/dev/null 2>&1; then
      SSHPASS="$pw" sshpass -e ssh \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=20 -o LogLevel=ERROR \
        "${user}@${mb}" "echo '${pw}' | sudo -S -p '' ${cmd}"
      return $?
    fi
  done
  # Neither worked: say so as its own condition, not as the stage's failure.
  return 3
}
