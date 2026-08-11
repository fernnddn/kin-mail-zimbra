#!/usr/bin/env bash
# =============================================================================
# KIN Mail Admin Console — bootstrap
#
# Installs the admin console (unprivileged) and kin-mail-privhelperd (root,
# local Unix socket only — Option B). Does not touch Zimbra/DRBD/Pacemaker
# config and does not run install stages.
#
#   sudo ./console/bootstrap.sh
#
# First boot prints a one-time admin password on stdout (not written to disk).
# Default HTTPS port: 9443 (override CONSOLE_PORT in /etc/kin-mail-console/console.env).
# =============================================================================
set -eu

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Run as root: sudo $0"; exit 1; }
}

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
OPT_ROOT="/opt/kin-mail-console"
ETC_ROOT="/etc/kin-mail-console"
DATA_ROOT="/var/lib/kin-mail-console"
LOG_ROOT="/var/log/kin-mail"
SVC_USER="kin-console"
ENV_FILE="${ETC_ROOT}/console.env"
UNIT_SRC="${SCRIPT_DIR}/deploy/kin-mail-console.service"
UNIT_DST="/etc/systemd/system/kin-mail-console.service"
PRIV_UNIT_SRC="${SCRIPT_DIR}/deploy/kin-mail-privhelperd.service"
PRIV_UNIT_DST="/etc/systemd/system/kin-mail-privhelperd.service"

need_root

CONSOLE_PORT=9443
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi
CONSOLE_PORT="${CONSOLE_PORT:-9443}"

say "KIN Mail console bootstrap"
info "Install root : ${OPT_ROOT}"
info "Data root    : ${DATA_ROOT}"
info "Listen port  : ${CONSOLE_PORT} (HTTPS)"

say "1. Base packages (skip apt when already present)"
export DEBIAN_FRONTEND=noninteractive
need_pkgs=()
for p in openssl ca-certificates rsync curl; do
  if ! dpkg -s "$p" >/dev/null 2>&1; then
    need_pkgs+=("$p")
  fi
done
if [ "${#need_pkgs[@]}" -gt 0 ]; then
  info "Installing: ${need_pkgs[*]}"
  apt-get -qq update
  apt-get -y install "${need_pkgs[@]}" >/dev/null
else
  ok "openssl / ca-certificates / rsync / curl present"
fi

say "2. Unprivileged service user"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home "$DATA_ROOT" --shell /usr/sbin/nologin "$SVC_USER"
  ok "Created user ${SVC_USER}"
else
  ok "User ${SVC_USER} already exists"
fi

say "3. Directories"
install -d -o root -g root -m 755 "$OPT_ROOT"
install -d -o root -g root -m 755 "$ETC_ROOT"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$DATA_ROOT"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "${DATA_ROOT}/tls"
install -d -o root -g root -m 755 "$LOG_ROOT"
# Root-owned audit log — kin-console must not be able to modify it.
touch "${LOG_ROOT}/privhelper.log"
chown root:root "${LOG_ROOT}/privhelper.log"
chmod 640 "${LOG_ROOT}/privhelper.log"
ok "Layout ready"

say "4. Install application files"
rsync -a --delete \
  --exclude 'frontend/node_modules' \
  --exclude 'frontend/dist' \
  --exclude 'backend/.venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "${SCRIPT_DIR}/" "${OPT_ROOT}/"
if [ -d "${SCRIPT_DIR}/frontend/dist" ]; then
  rsync -a --delete "${SCRIPT_DIR}/frontend/dist/" "${OPT_ROOT}/frontend/dist/"
  ok "Installed prebuilt frontend dist"
else
  fail "frontend/dist missing — build with: (cd console/frontend && npm ci && npm run build)"
  exit 1
fi
chown -R root:root "$OPT_ROOT"
chmod -R a+rX "$OPT_ROOT"
ok "Synced to ${OPT_ROOT}"

say "5. Python virtualenv + dependencies"
install_uv() {
  if command -v uv >/dev/null 2>&1 || [ -x /usr/local/bin/uv ]; then
    return 0
  fi
  info "Fetching uv for reliable venv+pip (avoids apt python3-venv stalls)"
  curl -fsSL "https://astral.sh/uv/install.sh" | UV_INSTALL_DIR=/usr/local/bin sh
  command -v uv >/dev/null 2>&1 || [ -x /usr/local/bin/uv ]
}
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
  UV_BIN=$(command -v uv)
elif [ -x /usr/local/bin/uv ]; then
  UV_BIN=/usr/local/bin/uv
fi

rm -rf "${OPT_ROOT}/venv"
if python3 -c "import ensurepip, venv" 2>/dev/null; then
  python3 -m venv "${OPT_ROOT}/venv"
  "${OPT_ROOT}/venv/bin/pip" -q install --upgrade pip
  "${OPT_ROOT}/venv/bin/pip" -q install -r "${OPT_ROOT}/backend/requirements.txt"
  ok "venv via python3 -m venv + pip"
else
  install_uv || { fail "Need python3-venv or uv"; exit 1; }
  UV_BIN=$(command -v uv || echo /usr/local/bin/uv)
  "$UV_BIN" venv "${OPT_ROOT}/venv"
  "$UV_BIN" pip install --python "${OPT_ROOT}/venv/bin/python" -r "${OPT_ROOT}/backend/requirements.txt"
  ok "venv via uv"
fi

say "6. Config (${ENV_FILE})"
if [ ! -f "$ENV_FILE" ]; then
  sed "s/^CONSOLE_PORT=.*/CONSOLE_PORT=${CONSOLE_PORT}/" \
    "${SCRIPT_DIR}/deploy/console.env.example" >"$ENV_FILE"
  ok "Wrote ${ENV_FILE}"
else
  ok "Keeping existing ${ENV_FILE}"
fi
chown root:"${SVC_USER}" "$ENV_FILE"
chmod 640 "$ENV_FILE"

say "7. TLS certificate (self-signed first-boot)"
CERT="${DATA_ROOT}/tls/cert.pem"
KEY="${DATA_ROOT}/tls/key.pem"
if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  ok "Existing TLS material retained"
else
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" -days 825 \
    -subj "/CN=kin-mail-console/O=KIN Mail/C=ID" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
  chmod 640 "$KEY" "$CERT"
  chown "${SVC_USER}:${SVC_USER}" "$KEY" "$CERT"
  ok "Generated self-signed cert (browser warning expected)"
fi

say "8. Admin password hash (first boot only)"
HASH_FILE="${DATA_ROOT}/admin.hash"
SECRET_FILE="${DATA_ROOT}/session.secret"
BOOT_PASS=""
if [ -f "$HASH_FILE" ]; then
  ok "admin.hash already present — password NOT re-printed"
else
  BOOT_PASS=$("${OPT_ROOT}/venv/bin/python" - <<'PY'
import secrets
print(secrets.token_urlsafe(18), end="")
PY
)
  KIN_BOOT_PASS="$BOOT_PASS" KIN_HASH_FILE="$HASH_FILE" "${OPT_ROOT}/venv/bin/python" - <<'PY'
import os
from pathlib import Path
import bcrypt
pwd = os.environ["KIN_BOOT_PASS"].encode("utf-8")
path = Path(os.environ["KIN_HASH_FILE"])
path.write_text(bcrypt.hashpw(pwd, bcrypt.gensalt(rounds=12)).decode("ascii") + "\n", encoding="utf-8")
path.chmod(0o600)
PY
  unset KIN_BOOT_PASS
  chown "${SVC_USER}:${SVC_USER}" "$HASH_FILE"
  ok "Wrote password hash (plaintext not stored)"
fi

if [ ! -f "$SECRET_FILE" ]; then
  "${OPT_ROOT}/venv/bin/python" - <<PY
import secrets
from pathlib import Path
p = Path("${SECRET_FILE}")
p.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
p.chmod(0o600)
PY
  chown "${SVC_USER}:${SVC_USER}" "$SECRET_FILE"
  ok "Generated session secret"
else
  chown "${SVC_USER}:${SVC_USER}" "$SECRET_FILE" 2>/dev/null || true
  ok "Session secret present"
fi
chown -R "${SVC_USER}:${SVC_USER}" "$DATA_ROOT"

say "9. systemd — privhelperd (root, Unix socket only)"
install -m 644 "$PRIV_UNIT_SRC" "$PRIV_UNIT_DST"
systemctl daemon-reload
systemctl enable kin-mail-privhelperd.service >/dev/null
systemctl restart kin-mail-privhelperd.service
sleep 1
if systemctl is-active --quiet kin-mail-privhelperd.service; then
  ok "kin-mail-privhelperd.service active"
else
  fail "privhelperd failed — journalctl -u kin-mail-privhelperd -n 80"
  journalctl -u kin-mail-privhelperd -n 40 --no-pager | sed 's/^/    /' || true
  exit 1
fi
if [ -S /run/kin-mail/privhelper.sock ]; then
  sock_mode=$(stat -c '%a %U:%G' /run/kin-mail/privhelper.sock)
  ok "Socket /run/kin-mail/privhelper.sock (${sock_mode})"
else
  fail "privhelper.sock missing"
  exit 1
fi
# Confirm audit log not writable by kin-console
if su -s /bin/bash -c "test -w ${LOG_ROOT}/privhelper.log" "$SVC_USER" 2>/dev/null; then
  fail "privhelper.log is writable by ${SVC_USER} — abort"
  exit 1
fi
ok "privhelper.log not writable by ${SVC_USER}"

say "10. systemd — console (unprivileged)"
install -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable kin-mail-console.service >/dev/null
systemctl restart kin-mail-console.service
sleep 2
if systemctl is-active --quiet kin-mail-console.service; then
  ok "kin-mail-console.service active"
else
  fail "Service failed to start — journalctl -u kin-mail-console -n 80"
  journalctl -u kin-mail-console -n 40 --no-pager | sed 's/^/    /' || true
  exit 1
fi

main_pid=$(systemctl show -p MainPID --value kin-mail-console.service)
if [ -n "$main_pid" ] && [ "$main_pid" != "0" ]; then
  proc_user=$(ps -o user= -p "$main_pid" | tr -d ' ')
  if [ "$proc_user" = "root" ]; then
    fail "Console process runs as root — abort"
    exit 1
  fi
  ok "Console process user: ${proc_user} (pid ${main_pid})"
fi

say "11. Local HTTPS smoke"
code=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:${CONSOLE_PORT}/api/health" || true)
if [ "$code" != "200" ]; then
  fail "health returned ${code}"
  exit 1
fi
ok "https://127.0.0.1:${CONSOLE_PORT}/api/health → 200"
me=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:${CONSOLE_PORT}/api/me" || true)
if [ "$me" != "401" ]; then
  fail "/api/me expected 401 without session, got ${me}"
  exit 1
fi
ok "/api/me → 401 without session (auth required)"

echo
say "Bootstrap complete"
info "URL : https://<this-host>:${CONSOLE_PORT}/"
info "User: admin"
if [ -n "$BOOT_PASS" ]; then
  echo
  printf '%s\n' "${YLW}${BLD}ONE-TIME ADMIN PASSWORD (save now — will not be shown again):${RST}"
  printf '%s\n' "${BLD}${BOOT_PASS}${RST}"
  echo
  warn "This password is not written to any file or log."
else
  info "Password unchanged (existing install)."
fi
info "privhelperd: unix:/run/kin-mail/privhelper.sock (never a network port)"
info "Open ufw for ${CONSOLE_PORT} via install/10-host-firewall.sh (admin IPs + LAN only)."
