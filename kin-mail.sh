#!/usr/bin/env bash
# =============================================================================
# KIN Mail — bootstrap installer
#
# Single entry point for deploying KIN Mail. Ensures the stage scripts are
# present (clone if needed), then offers a menu to run a full install or one
# stage at a time.
#
#   sudo ./kin-mail.sh
#   curl -fsSL … | sudo bash          # only after the file is on the host
#
# Override defaults when needed:
#   KIN_MAIL_REPO_URL=…  KIN_MAIL_DEPLOY_DIR=…  sudo -E ./kin-mail.sh
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
  [ "$(id -u)" -eq 0 ] || { fail "Jalankan sebagai root:  sudo $0"; exit 1; }
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
  check-zimbra-foss-update.sh
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
  printf '  %s%s│  %-54s  │%s\n' "$DIM" "$BLU" "Managed email platform — PT Karya Informasi Nusantara" "$RST"
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

ensure_scripts() {
  local self_dir
  self_dir=$(cd "$(dirname "$0")" && pwd)
  cd "$self_dir" || exit 1

  if scripts_present; then
    ok "Script tahap ditemukan di ${self_dir}"
    return 0
  fi

  say "Script tahap tidak lengkap di direktori ini — menyiapkan salinan deploy"
  info "Sumber : ${REPO_URL}"
  info "Target : ${DEPLOY_DIR}"

  command -v git >/dev/null 2>&1 || { fail "git belum terpasang"; exit 1; }

  if [ -d "$DEPLOY_DIR/.git" ] && ( cd "$DEPLOY_DIR" && scripts_present ); then
    ok "Clone yang sudah ada dipakai ulang: ${DEPLOY_DIR}"
  elif [ -e "$DEPLOY_DIR" ] && [ ! -d "$DEPLOY_DIR/.git" ]; then
    fail "${DEPLOY_DIR} sudah ada tetapi bukan clone git KIN Mail"
    info "Pindahkan/hapus folder itu, atau set KIN_MAIL_DEPLOY_DIR ke path lain."
    exit 1
  elif [ -d "$DEPLOY_DIR/.git" ]; then
    warn "Clone ada tetapi script belum lengkap — mencoba git pull"
    git -C "$DEPLOY_DIR" pull --ff-only || {
      fail "git pull gagal di ${DEPLOY_DIR}"
      exit 1
    }
  else
    mkdir -p "$(dirname "$DEPLOY_DIR")"
    git clone --depth 1 "$REPO_URL" "$DEPLOY_DIR" || {
      fail "Clone gagal dari ${REPO_URL}"
      exit 1
    }
    ok "Repository di-clone ke ${DEPLOY_DIR}"
  fi

  cd "$DEPLOY_DIR" || exit 1
  if ! scripts_present; then
    fail "Setelah clone, script tahap masih belum lengkap di ${DEPLOY_DIR}"
    exit 1
  fi

  # Continue from the cloned bootstrap so relative paths stay correct.
  if [ "$(cd "$(dirname "$0")" && pwd)" != "$DEPLOY_DIR" ]; then
    info "Melanjutkan dari ${DEPLOY_DIR}/kin-mail.sh"
    exec "$DEPLOY_DIR/kin-mail.sh" "$@"
  fi
}

run_stage() {
  local script="$1"
  echo
  hr
  say "Menjalankan ${script}"
  hr
  echo
  if [ ! -x "./$script" ]; then
    chmod +x "./$script" 2>/dev/null || true
  fi
  "./$script"
  local rc=$?
  echo
  if [ "$rc" -eq 0 ]; then
    ok "${script} selesai (exit 0)"
  else
    fail "${script} gagal (exit ${rc})"
  fi
  return "$rc"
}

run_full_install() {
  local s rc
  say "Install lengkap — urutan: ${FULL_PIPELINE[*]}"
  info "Berhenti otomatis jika satu tahap exit non-zero."
  echo
  for s in "${FULL_PIPELINE[@]}"; do
    run_stage "$s" || {
      rc=$?
      echo
      fail "Pipeline dihentikan pada ${s}"
      info "Perbaiki masalah di atas, lalu jalankan ulang menu atau tahap itu saja."
      return "$rc"
    }
  done
  echo
  say "Install lengkap selesai"
  ok "Semua tahap dalam pipeline exit 0"
}

pick_one_stage() {
  local choice
  while true; do
    echo
    say "Pilih satu tahap"
    hr
    printf '  %s%s%s  %s\n' "$BLD" "1)" "$RST" "01-preflight.sh          ${DIM}cek kelayakan host${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "2)" "$RST" "02-prepare-os.sh         ${DIM}hostname, resolver, dependensi${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "3)" "$RST" "03-install-zimbra.sh     ${DIM}unduh & install Zimbra FOSS${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "4)" "$RST" "04-tls-dkim.sh           ${DIM}Let's Encrypt + DKIM${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "5)" "$RST" "06-hybrid-auth.sh        ${DIM}AD LDAP + fallback lokal${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "6)" "$RST" "05-healthcheck.sh        ${DIM}acceptance tests${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "7)" "$RST" "00-config.sh --reset     ${DIM}ulang wizard konfigurasi${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "0)" "$RST" "Kembali"
    hr
    printf '  %sPilihan%s [0-7]: ' "$BLD" "$RST"
    read -r choice </dev/tty || return 0
    case "$choice" in
      1) run_stage 01-preflight.sh; return $? ;;
      2) run_stage 02-prepare-os.sh; return $? ;;
      3) run_stage 03-install-zimbra.sh; return $? ;;
      4) run_stage 04-tls-dkim.sh; return $? ;;
      5) run_stage 06-hybrid-auth.sh; return $? ;;
      6) run_stage 05-healthcheck.sh; return $? ;;
      7)
        echo
        say "Menjalankan 00-config.sh --reset"
        ./00-config.sh --reset
        return $?
        ;;
      0) return 0 ;;
      *) warn "Pilihan tidak dikenal: ${choice}" ;;
    esac
  done
}

pause_return() {
  echo
  printf '  %sTekan Enter untuk kembali ke menu…%s ' "$DIM" "$RST"
  read -r _ </dev/tty || true
}

main_menu() {
  local choice workdir
  workdir=$(pwd)
  while true; do
    banner
    info "Direktori kerja : ${workdir}"
    info "Jalankan sebagai: root"
    echo
    say "Menu utama"
    hr
    printf '  %s%s%s  %s\n' "$BLD" "1)" "$RST" "Install lengkap dari awal"
    printf '      %s%s\n' "$DIM" "01 → 02 → 03 → 04 → 06 → 05 (berhenti jika ada yang gagal)${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "2)" "$RST" "Jalankan satu tahap tertentu"
    printf '      %s%s\n' "$DIM" "submenu: preflight, OS, install, TLS/DKIM, hybrid auth, healthcheck${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "3)" "$RST" "Healthcheck saja"
    printf '      %s%s\n' "$DIM" "05-healthcheck.sh${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "4)" "$RST" "Cek update Zimbra FOSS"
    printf '      %s%s\n' "$DIM" "check-zimbra-foss-update.sh${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "0)" "$RST" "Keluar"
    hr
    printf '  %sPilihan%s [0-4]: ' "$BLD" "$RST"
    read -r choice </dev/tty || exit 0
    case "$choice" in
      1) run_full_install; pause_return ;;
      2) pick_one_stage; pause_return ;;
      3) run_stage 05-healthcheck.sh; pause_return ;;
      4) run_stage check-zimbra-foss-update.sh; pause_return ;;
      0)
        echo
        say "Selesai"
        info "Sampai jumpa."
        echo
        exit 0
        ;;
      *)
        warn "Pilihan tidak dikenal: ${choice}"
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
