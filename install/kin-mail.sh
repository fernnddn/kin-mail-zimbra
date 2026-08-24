#!/usr/bin/env bash
# =============================================================================
# KIN Mail - bootstrap installer
#
# Single entry point for deploying KIN Mail. Lives under install/ with the
# stage scripts. Ensures stages are present (clone if needed), then offers a
# menu to run a full install or one stage at a time.
#
#   sudo ./install/kin-mail.sh
#
# Override defaults when needed:
#   KIN_MAIL_REPO_URL=…  KIN_MAIL_DEPLOY_DIR=…  sudo -E ./install/kin-mail.sh
#   KIN_MAIL_DRY_RUN=1 - print the planned pipeline without executing stages
#
# Console (non-interactive) full install - ONLY with explicit operator confirm:
#   sudo KIN_CONSOLE_CONFIRMED=1 ./install/kin-mail.sh --full-install
# Skips /dev/tty y/n prompts; does NOT skip the ufw dead-man's switch.
# Dead-man cancel is a separate operator step (CLI or console action).
# =============================================================================
set -u

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'

say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Run as root:  sudo $0"; exit 1; }
}

REPO_URL="${KIN_MAIL_REPO_URL:-https://github.com/fernnddn/kin-mail-zimbra.git}"
DEPLOY_DIR="${KIN_MAIL_DEPLOY_DIR:-/opt/kin-mail-deploy}"
DRY_RUN="${KIN_MAIL_DRY_RUN:-0}"
# Set only by admin console after an explicit UI confirm - never invent a default-yes.
CONSOLE_CONFIRMED="${KIN_CONSOLE_CONFIRMED:-0}"

REQUIRED_SCRIPTS=(
  00-config.sh
  01-preflight.sh
  02-prepare-os.sh
  03-install-zimbra.sh
  04-tls-dkim.sh
  05-healthcheck.sh
  06-hybrid-auth.sh
  07-zpush.sh
  08-create-mailbox.sh
  09-hardening.sh
  10-host-firewall.sh
  11-admin-path-lockdown.sh
  12-branding.sh
  check-zimbra-foss-update.sh
  kin-mail.sh
)

# Stages that need a live Zimbra tree after 03. Mid-handoff skips these in
# run_full_install (see full_install_zimbra_stage_action); 01-03 always run.
FULL_PIPELINE_ZIMBRA_STAGES=(
  04-tls-dkim.sh
  06-hybrid-auth.sh
)

hr() {
  printf '  %s\n' "${DIM}────────────────────────────────────────────────────────────${RST}"
}

banner() {
  clear 2>/dev/null || true
  printf '\n'
  printf '  %s%s┌──────────────────────────────────────────────────────────┐%s\n' "$BLD" "$BLU" "$RST"
  printf '  %s%s│%-58s│%s\n' "$BLD" "$BLU" "" "$RST"
  printf '  %s%s│  %-54s  │%s\n' "$BLD" "$BLU" "KIN Mail" "$RST"
  printf '  %s%s│  %-54s  │%s\n' "$DIM" "$BLU" "Managed email - PT Karya Informasi Nusantara" "$RST"
  printf '  %s%s│%-58s│%s\n' "$BLD" "$BLU" "" "$RST"
  printf '  %s%s└──────────────────────────────────────────────────────────┘%s\n' "$BLD" "$BLU" "$RST"
  printf '\n'
}

scripts_present() {
  local s
  for s in "${REQUIRED_SCRIPTS[@]}"; do
    [ -f "./$s" ] || return 1
  done
  return 0
}

# Resolve where stage scripts live inside a clone (install/ layout, or legacy root).
resolve_scripts_dir() {
  local base="$1"
  if [ -d "$base/install" ] && ( cd "$base/install" && scripts_present ); then
    printf '%s\n' "$base/install"
    return 0
  fi
  if ( cd "$base" && scripts_present ); then
    printf '%s\n' "$base"
    return 0
  fi
  return 1
}

ensure_scripts() {
  local self_dir scripts_dir
  self_dir=$(cd "$(dirname "$0")" && pwd)
  cd "$self_dir" || exit 1

  if scripts_present; then
    ok "Stage scripts found in ${self_dir}"
    return 0
  fi

  say "Stage scripts incomplete in this directory; preparing deploy copy"
  info "Source : ${REPO_URL}"
  info "Target : ${DEPLOY_DIR}"

  command -v git >/dev/null 2>&1 || { fail "git is not installed"; exit 1; }

  if [ -d "$DEPLOY_DIR/.git" ] && scripts_dir=$(resolve_scripts_dir "$DEPLOY_DIR"); then
    ok "Reusing existing clone: ${scripts_dir}"
  elif [ -e "$DEPLOY_DIR" ] && [ ! -d "$DEPLOY_DIR/.git" ]; then
    fail "${DEPLOY_DIR} already exists but is not a KIN Mail git clone"
    info "Move/remove that folder, or set KIN_MAIL_DEPLOY_DIR to another path."
    exit 1
  elif [ -d "$DEPLOY_DIR/.git" ]; then
    warn "Clone exists but scripts are incomplete; trying git pull"
    git -C "$DEPLOY_DIR" pull --ff-only || {
      fail "git pull failed in ${DEPLOY_DIR}"
      exit 1
    }
  else
    mkdir -p "$(dirname "$DEPLOY_DIR")"
    git clone --depth 1 "$REPO_URL" "$DEPLOY_DIR" || {
      fail "Clone failed from ${REPO_URL}"
      exit 1
    }
    ok "Repository cloned to ${DEPLOY_DIR}"
  fi

  scripts_dir=$(resolve_scripts_dir "$DEPLOY_DIR") || {
    fail "After clone/pull, stage scripts are still incomplete in ${DEPLOY_DIR}"
    info "Expected at ${DEPLOY_DIR}/install/ (or legacy layout at clone root)."
    exit 1
  }

  cd "$scripts_dir" || exit 1

  # Continue from the cloned bootstrap so relative paths stay correct.
  if [ "$(cd "$(dirname "$0")" && pwd)" != "$scripts_dir" ]; then
    info "Continuing from ${scripts_dir}/kin-mail.sh"
    exec "$scripts_dir/kin-mail.sh" "$@"
  fi
}

load_install_config() {
  # shellcheck disable=SC1091
  . ./00-config.sh
  # shellcheck disable=SC1091
  . ./lib/zimbra-data-disk-probe.sh
}

run_stage() {
  local script="$1"
  shift || true
  echo
  hr
  say "Running ${script}${*:+ ($*)}"
  hr
  echo
  if [ "$DRY_RUN" = "1" ]; then
    info "DRY_RUN=1 - would execute: ./${script} $*"
    ok "${script} skipped (dry-run)"
    return 0
  fi
  if [ ! -x "./$script" ]; then
    chmod +x "./$script" 2>/dev/null || true
  fi
  "./$script" "$@"
  local rc=$?
  echo
  if [ "$rc" -eq 0 ]; then
    ok "${script} finished (exit 0)"
  else
    fail "${script} failed (exit ${rc})"
  fi
  return "$rc"
}

ufw_is_active() {
  command -v ufw >/dev/null 2>&1 || return 1
  ufw status 2>/dev/null | grep -q '^Status: active'
}

ensure_admin_ips_for_firewall() {
  if [ -n "${KIN_ADMIN_IPS:-}" ]; then
    return 0
  fi
  if [ "$CONSOLE_CONFIRMED" = "1" ]; then
    fail "KIN_ADMIN_IPS is empty - cannot apply firewall from console"
    info "Set KIN_ADMIN_IPS via wizard (apply_wizard_draft) before run_full_install."
    return 1
  fi
  say "KIN_ADMIN_IPS is empty - required for host firewall"
  info "Set once in the wizard, or enter sources now (also written to ${CONF_FILE})."
  ask KIN_ADMIN_IPS "Admin source IPs/CIDRs (space-separated)" ""
  KIN_ADMIN_IPS=$(printf '%s' "$KIN_ADMIN_IPS" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')
  if [ -z "$KIN_ADMIN_IPS" ]; then
    fail "Cannot apply firewall without KIN_ADMIN_IPS"
    return 1
  fi
  if [ -f "$CONF_FILE" ]; then
    if grep -q '^KIN_ADMIN_IPS=' "$CONF_FILE" 2>/dev/null; then
      # Replace existing assignment (simple line rewrite).
      local tmp
      tmp=$(mktemp)
      awk -v v="$KIN_ADMIN_IPS" '
        BEGIN { done=0 }
        /^KIN_ADMIN_IPS=/ { print "KIN_ADMIN_IPS=\"" v "\""; done=1; next }
        { print }
        END { if (!done) print "KIN_ADMIN_IPS=\"" v "\"" }
      ' "$CONF_FILE" >"$tmp" && mv "$tmp" "$CONF_FILE"
      chmod 600 "$CONF_FILE"
    else
      printf '\nKIN_ADMIN_IPS="%s"\n' "$KIN_ADMIN_IPS" >>"$CONF_FILE"
      chmod 600 "$CONF_FILE"
    fi
    ok "Persisted KIN_ADMIN_IPS into ${CONF_FILE}"
  fi
  export KIN_ADMIN_IPS
}

run_firewall_stage_interactive() {
  echo
  hr
  say "Host firewall (stage 10) - HIGH RISK"
  hr
  info "Applies ufw on THIS host only (dead-man's switch always armed)."
  info "For HA: finish + verify this host before running full install / stage 10 on the peer."
  info "Do NOT apply Host A and Host B in parallel."
  echo

  local default_ans=y admin_ips
  if ufw_is_active; then
    warn "ufw is already active on this host."
    info "Re-apply resets rules and re-arms the dead-man switch."
    default_ans=n
  fi

  if [ "$DRY_RUN" = "1" ]; then
    info "DRY_RUN=1 - would prompt: apply firewall now? (default ${default_ans})"
    ok "10-host-firewall.sh skipped (dry-run)"
    return 0
  fi

  if [ "$CONSOLE_CONFIRMED" = "1" ]; then
    # Wizard marks Admin IPs optional. Empty = skip stage 10 (same as CLI "no"),
    # then continue to 11 and 05. Do not fail the whole pipeline.
    admin_ips=$(printf '%s' "${KIN_ADMIN_IPS:-}" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')
    if [ -z "$admin_ips" ]; then
      warn "Host firewall (ufw) not applied - no admin IPs configured via wizard."
      warn "Perimeter firewall (FortiGate) remains your only protection at host level until you set Admin access IPs and re-run this stage."
      info "Console: run_script 10-host-firewall.sh apply"
      info "CLI:     sudo ./10-host-firewall.sh apply"
      return 0
    fi
    # Console UI button is the operator confirm - never invent a silent default-yes
    # for bare non-TTY runs without this flag.
    say "KIN_CONSOLE_CONFIRMED=1 - applying firewall (console operator confirmed)"
    warn "Dead-man will be armed. Console will NOT auto-cancel it."
    info "After verifying SSH + cluster + mail, cancel via console or:"
    info "  sudo ./10-host-firewall.sh cancel-deadman"
  else
    if ! ask_yn "Apply host firewall (ufw) on THIS host now?" "$default_ans"; then
      warn "Skipped 10-host-firewall.sh - host perimeter unchanged"
      info "Run later: sudo ./10-host-firewall.sh apply"
      return 0
    fi
  fi

  ensure_admin_ips_for_firewall || return 1
  export KIN_ADMIN_IPS
  run_stage 10-host-firewall.sh apply || return $?

  echo
  say "Dead-man is armed - verify SSH + cluster, then cancel"
  info "sudo ./10-host-firewall.sh cancel-deadman"

  if [ "$CONSOLE_CONFIRMED" = "1" ]; then
    # Dead-man stays armed in the background. Do not stall or fail Deploy
    # waiting for cancel - the operator cancels from the console after SSH
    # still works. If they never cancel, ufw reverts when the timer ends.
    warn "Dead-man stays armed. Verify SSH, then cancel from the console (or sudo ./10-host-firewall.sh cancel-deadman)."
    info "Continuing the install; ufw will auto-disable if you do not cancel in time."
    return 0
  fi

  if ask_yn "Cancel dead-man now (keep ufw enabled after your checks)?" y; then
    ./10-host-firewall.sh cancel-deadman || {
      fail "cancel-deadman failed - ufw may auto-disable when the timer ends"
      return 1
    }
  else
    warn "Dead-man still armed - cancel manually before the timer fires"
  fi
  return 0
}

run_full_install() {
  local s rc mid_handoff=0 zimbra_dir_exists=0 hardening_mode stage_action
  load_install_config

  say "Full install"
  info "Stops automatically if any stage exits non-zero."
  info "This host only - for HA, install/verify one node before the peer."
  if [ "$DRY_RUN" = "1" ]; then
    warn "KIN_MAIL_DRY_RUN=1 - no stage will modify the system"
  fi
  echo

  # 01-03 always run. Mid-handoff is only meaningful after 03 (data disk probe).
  for s in 01-preflight.sh 02-prepare-os.sh 03-install-zimbra.sh; do
    run_stage "$s" || {
      rc=$?
      echo
      fail "Pipeline stopped at ${s}"
      info "Fix the issue above, then re-run the menu or that stage only."
      return "$rc"
    }
  done

  if zimbra_is_mid_handoff; then
    mid_handoff=1
    if data_disk_held_by_drbd "${KIN_DRBD_DATA_DISK:-/dev/sdb1}"; then
      warn "DRBD already holds the data disk (Secondary or attached); skipping Zimbra-touching stages."
      info "Pacemaker owns kin-zimbra on the Promoted node. Still running 09 --os-only and 10 host firewall."
    else
      warn "Mid-handoff detected: real Zimbra is on the data disk; /opt/zimbra is unmounted."
      info "Skipping Zimbra-touching stages 04/05/06/07/11. Pacemaker/DRBD own bringing the tree live."
      info "Still running 09 --os-only (fail2ban/unattended) and 10 host firewall."
    fi
  fi

  # 04 + 06 need a live Zimbra tree (zmcertmgr / zmprov / auth probes).
  for s in "${FULL_PIPELINE_ZIMBRA_STAGES[@]}"; do
    stage_action="$(full_install_zimbra_stage_action "$mid_handoff")"
    if [ "$stage_action" = "skip_mid_handoff" ]; then
      warn "Skipping ${s} (mid-handoff; empty /opt/zimbra mountpoint)"
      continue
    fi
    run_stage "$s" || {
      rc=$?
      echo
      fail "Pipeline stopped at ${s}"
      info "Fix the issue above, then re-run the menu or that stage only."
      return "$rc"
    }
  done

  # 07 - optional (wizard ZPUSH_ENABLED); needs live /opt/zimbra
  if [ "${ZPUSH_ENABLED:-yes}" != "yes" ]; then
    warn "ZPUSH_ENABLED=${ZPUSH_ENABLED:-} - skipping 07-zpush.sh"
  elif [ "$(full_install_zimbra_stage_action "$mid_handoff")" = "skip_mid_handoff" ]; then
    warn "Skipping 07-zpush.sh (mid-handoff; empty /opt/zimbra mountpoint)"
  elif [ ! -d /opt/zimbra ]; then
    warn "/opt/zimbra missing - skipping 07-zpush.sh (expected on Secondary without mount)"
  else
    run_stage 07-zpush.sh || {
      warn "07-zpush.sh failed - mail install continues. Re-run later: sudo ./07-zpush.sh"
    }
  fi

  # 09 - full when live tree present; --os-only for Secondary or mid-handoff
  [ -d /opt/zimbra ] && zimbra_dir_exists=1
  hardening_mode="$(full_install_hardening_mode "$mid_handoff" "$zimbra_dir_exists")"
  if [ "$hardening_mode" = "full" ]; then
    run_stage 09-hardening.sh || {
      warn "09-hardening.sh failed - mail install continues. Re-run later: sudo ./09-hardening.sh"
    }
  else
    if [ "$mid_handoff" -eq 1 ]; then
      warn "Mid-handoff - running 09-hardening.sh --os-only (Zimbra attrs wait for Pacemaker)"
    else
      warn "/opt/zimbra missing - running 09-hardening.sh --os-only"
    fi
    run_stage 09-hardening.sh --os-only || {
      warn "09-hardening.sh --os-only failed - mail install continues. Re-run later: sudo ./09-hardening.sh --os-only"
    }
  fi

  # 10 - always interactive + dead-man (never silent auto-apply); safe mid-handoff
  run_firewall_stage_interactive || {
    rc=$?
    fail "Pipeline stopped at 10-host-firewall.sh"
    return "$rc"
  }

  # 11 - admin path lockdown on public 443 (Primary / mounted Zimbra only)
  if [ "$(full_install_zimbra_stage_action "$mid_handoff")" = "skip_mid_handoff" ]; then
    warn "Skipping 11-admin-path-lockdown.sh (mid-handoff; empty /opt/zimbra mountpoint)"
  elif [ -d /opt/zimbra ]; then
    run_stage 11-admin-path-lockdown.sh || {
      warn "11-admin-path-lockdown.sh failed - mail install continues. Re-run later: sudo ./11-admin-path-lockdown.sh"
    }
  else
    warn "/opt/zimbra missing - skipping 11-admin-path-lockdown.sh"
  fi

  if [ "$(full_install_zimbra_stage_action "$mid_handoff")" = "skip_mid_handoff" ]; then
    warn "Skipping 05-healthcheck.sh (mid-handoff; zmcontrol not reachable on mountpoint)"
  else
    # Operator is watching the same log that printed the DKIM TXT. Wait so a
    # paste during 06-11 plus this poll can pass. Customer TLS has no ACME
    # paste; do not stall Deploy for ten minutes. Standalone 05 keeps WAIT=0.
    if [ "${TLS_METHOD:-}" = "customer" ]; then
      KIN_DKIM_WAIT_SEC="${KIN_DKIM_WAIT_SEC:-0}"
    else
      KIN_DKIM_WAIT_SEC="${KIN_DKIM_WAIT_SEC:-600}"
    fi
    export KIN_DKIM_WAIT_SEC
    run_stage 05-healthcheck.sh || {
      rc=$?
      fail "Pipeline stopped at 05-healthcheck.sh"
      return "$rc"
    }
  fi

  echo
  if [ "$mid_handoff" -eq 1 ]; then
    say "Full install complete (mid-handoff path)"
    ok "OS stages finished; Zimbra-touching stages deferred to DRBD attach + Pacemaker"
  else
    say "Full install complete"
    ok "All selected pipeline stages exited 0"
  fi
  mkdir -p /etc/kin-mail
  chmod 755 /etc/kin-mail
  printf 'complete %s\n' "$(date -Is 2>/dev/null || date)" > /etc/kin-mail/setup-complete
  chmod 644 /etc/kin-mail/setup-complete
}

pick_one_stage() {
  local choice
  while true; do
    echo
    say "Pick one stage"
    hr
    printf '  %s%s%s  %s\n' "$BLD" "1)" "$RST" "01-preflight.sh          ${DIM}host readiness check${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "2)" "$RST" "02-prepare-os.sh         ${DIM}hostname, resolver, dependencies${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "3)" "$RST" "03-install-zimbra.sh     ${DIM}download & install Zimbra FOSS${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "4)" "$RST" "04-tls-dkim.sh           ${DIM}Let's Encrypt + DKIM${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "5)" "$RST" "06-hybrid-auth.sh        ${DIM}AD LDAP + local fallback${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "6)" "$RST" "07-zpush.sh              ${DIM}ActiveSync (Z-Push + Zimbra backend)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "7)" "$RST" "08-create-mailbox.sh     ${DIM}create mailbox (quota-gated)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "8)" "$RST" "09-hardening.sh          ${DIM}Part A + SMTP rate limits${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "9)" "$RST" "10-host-firewall.sh      ${DIM}ufw (dead-man; THIS host only)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "10)" "$RST" "11-admin-path-lockdown.sh ${DIM}block /zimbraAdmin on :443${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "11)" "$RST" "12-branding.sh          ${DIM}customer logo/theme (optional)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "12)" "$RST" "05-healthcheck.sh        ${DIM}acceptance tests${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "13)" "$RST" "00-config.sh --reset     ${DIM}re-run configuration wizard${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "0)" "$RST" "Back"
    hr
    printf '  %sChoice%s [0-13]: ' "$BLD" "$RST"
    read -r choice </dev/tty || return 0
    case "$choice" in
      1) run_stage 01-preflight.sh; return $? ;;
      2) run_stage 02-prepare-os.sh; return $? ;;
      3) run_stage 03-install-zimbra.sh; return $? ;;
      4) run_stage 04-tls-dkim.sh; return $? ;;
      5) run_stage 06-hybrid-auth.sh; return $? ;;
      6) run_stage 07-zpush.sh; return $? ;;
      7) run_stage 08-create-mailbox.sh; return $? ;;
      8) run_stage 09-hardening.sh; return $? ;;
      9)
        load_install_config
        run_firewall_stage_interactive
        return $?
        ;;
      10) run_stage 11-admin-path-lockdown.sh; return $? ;;
      11) run_stage 12-branding.sh; return $? ;;
      12) run_stage 05-healthcheck.sh; return $? ;;
      13)
        echo
        say "Running 00-config.sh --reset"
        ./00-config.sh --reset
        return $?
        ;;
      0) return 0 ;;
      *) warn "Unknown choice: ${choice}" ;;
    esac
  done
}

pause_return() {
  echo
  printf '  %sPress Enter to return to the menu…%s ' "$DIM" "$RST"
  read -r _ </dev/tty || true
}

main_menu() {
  local choice workdir
  workdir=$(pwd)
  while true; do
    banner
    info "Working directory : ${workdir}"
    info "Run as            : root"
    [ "$DRY_RUN" = "1" ] && warn "DRY_RUN mode is ON (KIN_MAIL_DRY_RUN=1)"
    echo
    say "Main menu"
    hr
    printf '  %s%s%s  %s\n' "$BLD" "1)" "$RST" "Full install from scratch"
    printf '      %s%s\n' "$DIM" "01→02→03→04→06→[07?]→09→[10?]→11→05  (firewall always prompted)${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "2)" "$RST" "Run a specific stage"
    printf '      %s%s\n' "$DIM" "submenu includes Z-Push, branding, hardening, firewall, admin lockdown${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "3)" "$RST" "Healthcheck only"
    printf '      %s%s\n' "$DIM" "05-healthcheck.sh${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "4)" "$RST" "Check Zimbra FOSS update"
    printf '      %s%s\n' "$DIM" "check-zimbra-foss-update.sh${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "0)" "$RST" "Exit"
    hr
    printf '  %sChoice%s [0-4]: ' "$BLD" "$RST"
    read -r choice </dev/tty || exit 0
    case "$choice" in
      1) run_full_install; pause_return ;;
      2) pick_one_stage; pause_return ;;
      3) run_stage 05-healthcheck.sh; pause_return ;;
      4) run_stage check-zimbra-foss-update.sh; pause_return ;;
      0)
        echo
        say "Done"
        info "Goodbye."
        echo
        exit 0
        ;;
      *)
        warn "Unknown choice: ${choice}"
        sleep 1
        ;;
    esac
  done
}

# --- entry -------------------------------------------------------------------
need_root

# Non-interactive full install for admin console only.
# Require confirm BEFORE ensure_scripts so a rejected call cannot trigger git pull.
if [ "${1:-}" = "--full-install" ]; then
  if [ "$CONSOLE_CONFIRMED" != "1" ]; then
    fail "--full-install requires KIN_CONSOLE_CONFIRMED=1"
    info "That flag is set by the admin console after an explicit UI confirm."
    info "For interactive CLI use, run without --full-install and pick menu option 1."
    exit 2
  fi
  ensure_scripts "$@"
  chmod +x ./*.sh 2>/dev/null || true
  say "Console full install (KIN_CONSOLE_CONFIRMED=1)"
  info "TTY prompts skipped; ufw dead-man's switch still applies on stage 10."
  run_full_install
  exit $?
fi

ensure_scripts "$@"
chmod +x ./*.sh 2>/dev/null || true
main_menu
