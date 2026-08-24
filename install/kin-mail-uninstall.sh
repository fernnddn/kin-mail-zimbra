#!/usr/bin/env bash
# =============================================================================
# KIN Mail — host uninstaller (kin-mail-uninstall.sh)
#
# Wipes KIN Mail stack from the host it runs on. Follows
# docs/uninstaller_and_remove_host_design_brief.md (operator-approved):
#
#   --decommission   DEFAULT. Full wipe including cluster-bound network
#                    (VIP leftovers / kin netplan snippets). SSH may drop
#                    at the final network step — intentional when the VM
#                    is about to be destroyed.
#   --detach         Same wipe EXCEPT base management IP/SSH stay up.
#   --dry-run        Print the plan only; mutate nothing.
#
# Order: stop services cleanly BEFORE removing config. Network wipe is LAST
# so earlier stages still emit clear progress if the session dies mid-run.
#
#   sudo ./kin-mail-uninstall.sh                # decommission (default)
#   sudo ./kin-mail-uninstall.sh --detach
#   sudo ./kin-mail-uninstall.sh --dry-run
#
# NEVER run --decommission against a live production mail peer you still need.
# =============================================================================
set -u

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }
stage() {
  printf '\n%s\n' "${BLU}======== STAGE: $* ========${RST}"
  info "timestamp=$(date -Is)"
}

MODE=decommission
DRY_RUN=0

usage() {
  cat <<EOF
Usage: sudo $0 [--decommission|--detach] [--dry-run] [--help]

  --decommission  Full wipe including network (DEFAULT — VM usually destroyed after)
  --detach        Wipe stack but keep base management IP / SSH reachable
  --dry-run       Print planned actions only; do not change the system
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --decommission) MODE=decommission ;;
    --detach) MODE=detach ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  fail "Run as root: sudo $0"
  exit 1
fi

run() {
  # shellcheck disable=SC2145
  info "+ $*"
  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi
  "$@"
}

run_soft() {
  # shellcheck disable=SC2145
  info "+ $*  (ignore failure)"
  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi
  "$@" || true
}

CONF="${KIN_MAIL_CONFIG:-/etc/kin-mail/config}"
MAIL_DOMAIN=""
NET_IFACE=""
if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  set +u
  # Prefer sourcing over eval — config is KEY="value" shell form.
  # shellcheck disable=SC1090
  . "$CONF" 2>/dev/null || true
  set -u
  MAIL_DOMAIN="${MAIL_DOMAIN:-}"
  NET_IFACE="${NET_IFACE:-}"
fi

echo
say "KIN Mail uninstaller"
info "mode=${MODE} dry_run=${DRY_RUN} host=$(hostname -f 2>/dev/null || hostname)"
info "config=${CONF} domain=${MAIL_DOMAIN:-unset} iface=${NET_IFACE:-unset}"
if [ "$MODE" = "decommission" ]; then
  warn "DEFAULT mode --decommission: final stage may drop SSH/network reachability."
  warn "Use --detach if this host must stay reachable after wipe."
fi
if [ "$DRY_RUN" -eq 1 ]; then
  warn "DRY-RUN — no mutations will be performed."
fi

# -----------------------------------------------------------------------------
stage "1/8 Stop Pacemaker/Corosync on THIS node (before config delete)"
# Local stop only — never pcs cluster stop --all (would hit survivors).
if systemctl is-active --quiet pacemaker 2>/dev/null || systemctl is-active --quiet corosync 2>/dev/null; then
  run_soft pcs cluster stop
  run_soft systemctl stop pacemaker
  run_soft systemctl stop corosync
  run_soft systemctl stop pcsd
  ok "Cluster stack stop attempted on local node"
else
  info "Pacemaker/Corosync not active — skip"
fi
run_soft systemctl disable pacemaker corosync pcsd 2>/dev/null

# -----------------------------------------------------------------------------
stage "2/8 Stop Zimbra cleanly"
if [ -x /opt/zimbra/bin/zmcontrol ]; then
  if id zimbra >/dev/null 2>&1; then
    run_soft su - zimbra -c "zmcontrol stop"
  else
    run_soft /opt/zimbra/bin/zmcontrol stop
  fi
  ok "zmcontrol stop attempted"
else
  info "Zimbra not installed — skip"
fi

# -----------------------------------------------------------------------------
stage "3/8 Stop DRBD / SBD / iSCSI initiator"
run_soft systemctl stop sbd
run_soft systemctl disable sbd
if command -v drbdadm >/dev/null 2>&1; then
  run_soft drbdadm down all
fi
run_soft systemctl stop drbd
run_soft systemctl disable drbd
run_soft systemctl stop kin-drbd-meta-loop.service
run_soft systemctl disable kin-drbd-meta-loop.service
run_soft iscsiadm -m node --logoutall=all
run_soft systemctl stop open-iscsi iscsid
ok "Storage / fencing clients stop attempted"

# -----------------------------------------------------------------------------
stage "4/8 Stop admin console + privhelper"
run_soft systemctl stop kin-mail-console.service
run_soft systemctl stop kin-mail-privhelperd.service
run_soft systemctl disable kin-mail-console.service
run_soft systemctl disable kin-mail-privhelperd.service
run_soft rm -f /etc/systemd/system/kin-mail-console.service \
  /etc/systemd/system/kin-mail-privhelperd.service
run_soft systemctl daemon-reload
ok "Console units stopped/removed"

# -----------------------------------------------------------------------------
stage "5/8 Disable host firewall (ufw)"
if command -v ufw >/dev/null 2>&1; then
  run_soft ufw --force disable
  run_soft ufw --force reset
  ok "ufw disabled/reset"
else
  info "ufw not present — skip"
fi

# fail2ban itself also guards plain SSH (install/09-hardening.sh), so don't
# stop/disable the service — just drop the KIN-specific jail (Zimbra/Z-Push
# rules pointing at log files that no longer exist) and reload.
FAIL2BAN_JAIL="${KIN_FAIL2BAN_JAIL_PATH:-/etc/fail2ban/jail.d/kin-mail.conf}"
if [ -f "$FAIL2BAN_JAIL" ]; then
  run_soft rm -f "$FAIL2BAN_JAIL"
  if command -v fail2ban-client >/dev/null 2>&1 && systemctl is-active --quiet fail2ban 2>/dev/null; then
    run_soft systemctl reload fail2ban
  fi
  ok "KIN fail2ban jail removed (fail2ban service left running for SSH)"
else
  info "No KIN fail2ban jail file — skip"
fi

# -----------------------------------------------------------------------------
stage "6/8 Remove KIN Mail config, packages, and data paths"
# Config AFTER services stopped (design: never rm while services run).
# Includes /etc/kin-mail/brand/ (customer logo/theme; not inside /opt/zimbra).
run_soft rm -rf /etc/kin-mail
run_soft rm -rf /etc/kin-mail-console
run_soft rm -rf /var/lib/kin-mail-console
run_soft rm -f /var/log/kin-mail/deploy-last.log /var/log/kin-mail-install.log
run_soft rm -rf /opt/kin-mail-console
run_soft rm -rf /opt/kin-mail-deploy
run_soft rm -f /run/kin-mail/privhelper.sock
run_soft rm -rf /run/kin-mail
run_soft rm -f /etc/drbd.d/kin-zimbra.res
run_soft rm -f /etc/drbd.d/*.res
run_soft rm -rf /var/lib/kin-mail
run_soft rm -f /etc/systemd/system/kin-drbd-meta-loop.service
run_soft rm -f /usr/local/sbin/kin-drbd-meta-loop.sh
run_soft rm -f /usr/lib/ocf/resource.d/kin/zimbra
# Corosync/Pacemaker local config (node already stopped)
run_soft rm -rf /etc/corosync/corosync.conf /etc/corosync/uidgid.d
run_soft rm -rf /var/lib/pacemaker /var/lib/corosync
run_soft rm -rf /etc/corosync/qdevice
# SBD
run_soft rm -f /etc/default/sbd /etc/sysconfig/sbd
# Let's Encrypt state — includes the Cloudflare API token (CF_CREDS) and the
# zimbra-deploy renewal hook, not just certs. Remove before the cert can be
# picked up by a stray renewal run.
run_soft rm -rf /etc/letsencrypt
# OS user created by the Zimbra installer; its home (/opt/zimbra) is handled
# separately below via the mounted volume, so don't pass -r here.
if id zimbra >/dev/null 2>&1; then
  run_soft userdel zimbra
fi
# Optional package purge — best-effort; leave OS base packages if purge fails.
if [ "$DRY_RUN" -eq 0 ]; then
  if command -v apt-get >/dev/null 2>&1; then
    info "+ apt-get purge -y zimbra-* pacemaker corosync pcs drbd-utils sbd  (best-effort)"
    DEBIAN_FRONTEND=noninteractive apt-get purge -y \
      'zimbra-*' pacemaker corosync pcs drbd-utils sbd 2>/dev/null || true
    DEBIAN_FRONTEND=noninteractive apt-get autoremove -y 2>/dev/null || true
  fi
fi
# Zimbra tree last among filesystems (large)
if [ -d /opt/zimbra ]; then
  warn "Removing /opt/zimbra (may take several minutes)…"
  run_soft rm -rf /opt/zimbra
fi
ok "Config / data removal attempted"

# -----------------------------------------------------------------------------
stage "7/8 Clear leftover VIP / cluster addresses from interfaces (pre-network)"
# Safe on both modes: VIP is Pacemaker-managed and should not persist after stop.
if [ "$DRY_RUN" -eq 0 ]; then
  while read -r addr; do
    [ -z "$addr" ] && continue
    # Skip obvious primary /24 management addresses ending in common host IPs? We
    # only strip secondary addresses that look like VIPs (label or known pattern).
    info "consider address: $addr"
  done < <(ip -o -4 addr show 2>/dev/null | awk '{print $2,$4}')
  # Explicit: remove addresses with label *:vip or known kin-vip leftovers.
  ip -o -4 addr show 2>/dev/null | while read -r _ dev _ cidr rest; do
    case "$rest" in
      # "*vip*" already covers "*kin-vip*" (a substring match), no separate arm needed.
      *vip*)
        info "+ ip addr del $cidr dev $dev"
        ip addr del "$cidr" dev "$dev" 2>/dev/null || true
        ;;
    esac
  done
fi
ok "VIP leftovers cleared (best-effort)"

# -----------------------------------------------------------------------------
stage "8/8 Network wipe (decommission only) — LAST; SSH may drop after this"
if [ "$MODE" = "detach" ]; then
  info "--detach: leaving base management IP / netplan unchanged"
  ok "Detach complete — host should remain SSH-reachable"
elif [ "$MODE" = "decommission" ]; then
  warn "About to remove KIN-specific netplan snippets and flush non-primary extras."
  warn "If this drops your SSH session, that is expected for --decommission."
  # Remove any kin-* or secondary netplan files; keep cloud-init primary if present.
  if [ "$DRY_RUN" -eq 0 ]; then
    shopt -s nullglob
    for f in /etc/netplan/*kin* /etc/netplan/*drbd* /etc/netplan/*corosync* /etc/netplan/*vip*; do
      info "+ rm -f $f"
      rm -f "$f"
    done
    shopt -u nullglob
    # Flush routes that are clearly cluster-only (best-effort; do not delete default).
    info "+ netplan apply (may interrupt SSH)"
    netplan apply 2>/dev/null || true
  else
    info "+ would remove /etc/netplan/*kin* *drbd* *corosync* *vip* and netplan apply"
  fi
  ok "Decommission network stage finished (or session dropped — check local console)"
fi

echo
say "Uninstaller finished (mode=${MODE} dry_run=${DRY_RUN})"
info "On the SURVIVOR, run ansible playbooks/mail-remove-host.yml for this node identity"
info "so cluster membership / DRBD peer / constraints / qnetd leftovers are cleared."
exit 0
