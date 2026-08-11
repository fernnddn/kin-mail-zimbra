#!/usr/bin/env bash
# =============================================================================
# KIN Mail — bootstrap installer
#
# Single entry point for deploying KIN Mail. Lives under install/ with the
# stage scripts. Ensures stages are present (clone if needed), then offers a
# menu to run a full install or one stage at a time.
#
#   sudo ./install/kin-mail.sh
#
# Override defaults when needed:
#   KIN_MAIL_REPO_URL=…  KIN_MAIL_DEPLOY_DIR=…  sudo -E ./install/kin-mail.sh
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
  check-zimbra-foss-update.sh
  kin-mail.sh
)

FULL_PIPELINE=(
  01-preflight.sh
  02-prepare-os.sh
  03-install-zimbra.sh
  04-tls-dkim.sh
  06-hybrid-auth.sh
  05-healthcheck.sh
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
  printf '  %s%s│  %-54s  │%s\n' "$DIM" "$BLU" "Managed email — PT Karya Informasi Nusantara" "$RST"
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

run_stage() {
  local script="$1"
  echo
  hr
  say "Running ${script}"
  hr
  echo
  if [ ! -x "./$script" ]; then
    chmod +x "./$script" 2>/dev/null || true
  fi
  "./$script"
  local rc=$?
  echo
  if [ "$rc" -eq 0 ]; then
    ok "${script} finished (exit 0)"
  else
    fail "${script} failed (exit ${rc})"
  fi
  return "$rc"
}

run_full_install() {
  local s rc
  say "Full install — order: ${FULL_PIPELINE[*]}"
  info "Stops automatically if any stage exits non-zero."
  echo
  for s in "${FULL_PIPELINE[@]}"; do
    run_stage "$s" || {
      rc=$?
      echo
      fail "Pipeline stopped at ${s}"
      info "Fix the issue above, then re-run the menu or that stage only."
      return "$rc"
    }
  done
  echo
  say "Full install complete"
  ok "All pipeline stages exited 0"
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
    printf '  %s%s%s  %s\n' "$BLD" "8)" "$RST" "09-hardening.sh          ${DIM}Part A hardening (fail2ban/TLS/…)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "9)" "$RST" "05-healthcheck.sh        ${DIM}acceptance tests${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "10)" "$RST" "00-config.sh --reset     ${DIM}re-run configuration wizard${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "0)" "$RST" "Back"
    hr
    printf '  %sChoice%s [0-10]: ' "$BLD" "$RST"
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
      9) run_stage 05-healthcheck.sh; return $? ;;
      10)
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
    echo
    say "Main menu"
    hr
    printf '  %s%s%s  %s\n' "$BLD" "1)" "$RST" "Full install from scratch"
    printf '      %s%s\n' "$DIM" "01 → 02 → 03 → 04 → 06 → 05 (stops on failure)${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "2)" "$RST" "Run a specific stage"
    printf '      %s%s\n' "$DIM" "submenu: preflight, OS, install, TLS/DKIM, hybrid auth, healthcheck${RST}"
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
ensure_scripts "$@"
chmod +x ./*.sh 2>/dev/null || true
main_menu
