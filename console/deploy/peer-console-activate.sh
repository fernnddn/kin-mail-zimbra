#!/usr/bin/env bash
# =============================================================================
# KIN Mail: activate a transferred console tree on a peer (Mail B).
#
# Host A Build HA / Add Second Server scp's /opt/kin-mail-console here, then
# runs this script as root. Does NOT mint a new admin password - Host A pushes
# users.json / session.secret next.
#
#   sudo KIN_PEER_CONSOLE_ACTIVATE=1 ./peer-console-activate.sh
#   # optional: KIN_PEER_CONSOLE_IP / KIN_PEER_CONSOLE_NAME for TLS SAN
# =============================================================================
set -eu

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

OPT_ROOT="/opt/kin-mail-console"
ETC_ROOT="/etc/kin-mail-console"
DATA_ROOT="/var/lib/kin-mail-console"
LOG_ROOT="/var/log/kin-mail"
SVC_USER="kin-console"
ENV_FILE="${ETC_ROOT}/console.env"
UNIT_SRC="${OPT_ROOT}/deploy/kin-mail-console.service"
PRIV_UNIT_SRC="${OPT_ROOT}/deploy/kin-mail-privhelperd.service"
UNIT_DST="/etc/systemd/system/kin-mail-console.service"
PRIV_UNIT_DST="/etc/systemd/system/kin-mail-privhelperd.service"
DEPLOY_ROOT="${KIN_MAIL_DEPLOY_DIR:-/opt/kin-mail-deploy}"

if [ "$(id -u)" -ne 0 ]; then
  fail "Run as root"
  exit 1
fi

if [ "${KIN_PEER_CONSOLE_ACTIVATE:-0}" != "1" ]; then
  fail "Refusing: set KIN_PEER_CONSOLE_ACTIVATE=1 (peer activate only; no admin mint)"
  exit 2
fi

CONSOLE_PORT=9443
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi
CONSOLE_PORT="${CONSOLE_PORT:-9443}"

say "Peer console activate"
info "Install root : ${OPT_ROOT}"
info "Listen port  : ${CONSOLE_PORT}"

if [ ! -x "${OPT_ROOT}/venv/bin/python" ] && [ ! -x "${OPT_ROOT}/venv/bin/python3" ]; then
  fail "Missing ${OPT_ROOT}/venv/bin/python{,3} - transfer /opt/kin-mail-console from Host A first"
  exit 1
fi
PEER_PY="${OPT_ROOT}/venv/bin/python"
if [ ! -x "$PEER_PY" ]; then
  PEER_PY="${OPT_ROOT}/venv/bin/python3"
fi
if [ ! -d "${OPT_ROOT}/frontend/dist" ]; then
  fail "Missing ${OPT_ROOT}/frontend/dist"
  exit 1
fi
if [ ! -f "$UNIT_SRC" ] || [ ! -f "$PRIV_UNIT_SRC" ]; then
  fail "Missing systemd unit templates under ${OPT_ROOT}/deploy"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
need_pkgs=()
for p in openssl ca-certificates curl; do
  if ! dpkg -s "$p" >/dev/null 2>&1; then
    need_pkgs+=("$p")
  fi
done
if [ "${#need_pkgs[@]}" -gt 0 ]; then
  info "Installing: ${need_pkgs[*]}"
  apt-get -qq update
  apt-get -y install "${need_pkgs[@]}" >/dev/null
fi

if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home "$DATA_ROOT" --shell /usr/sbin/nologin "$SVC_USER"
  ok "Created user ${SVC_USER}"
else
  ok "User ${SVC_USER} already exists"
fi

install -d -o root -g root -m 755 "$OPT_ROOT"
install -d -o root -g root -m 755 "$ETC_ROOT"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$DATA_ROOT"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "${DATA_ROOT}/tls"
install -d -o root -g root -m 755 "$LOG_ROOT"
touch "${LOG_ROOT}/privhelper.log"
chown root:root "${LOG_ROOT}/privhelper.log"
chmod 640 "${LOG_ROOT}/privhelper.log"
chown -R root:root "$OPT_ROOT"
chmod -R a+rX "$OPT_ROOT"
# venv shebangs stay executable
find "${OPT_ROOT}/venv/bin" -type f -exec chmod a+rx {} + 2>/dev/null || true

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "${OPT_ROOT}/deploy/console.env.example" ]; then
    sed "s/^CONSOLE_PORT=.*/CONSOLE_PORT=${CONSOLE_PORT}/" \
      "${OPT_ROOT}/deploy/console.env.example" >"$ENV_FILE"
  else
    printf 'CONSOLE_PORT=%s\nCONSOLE_BIND=0.0.0.0\nCONSOLE_USER=admin\nPRIVHELPER_SOCKET=/run/kin-mail/privhelper.sock\nPRIVHELPER_LOG=/var/log/kin-mail/privhelper.log\nKIN_MAIL_DEPLOY_DIR=%s\n' \
      "$CONSOLE_PORT" "$DEPLOY_ROOT" >"$ENV_FILE"
  fi
  ok "Wrote ${ENV_FILE}"
else
  ok "Keeping existing ${ENV_FILE}"
fi
chown root:"${SVC_USER}" "$ENV_FILE"
chmod 640 "$ENV_FILE"

CERT="${DATA_ROOT}/tls/cert.pem"
KEY="${DATA_ROOT}/tls/key.pem"
peer_name="${KIN_PEER_CONSOLE_NAME:-$(hostname -f 2>/dev/null || hostname || true)}"
peer_ip="${KIN_PEER_CONSOLE_IP:-}"
need_tls=0
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  need_tls=1
elif [ -n "$peer_ip" ] && command -v openssl >/dev/null 2>&1; then
  # Self-heal: re-mint when management IP is missing from SAN (exact IP token).
  if ! openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null \
    | grep -Eoq "(^|[,[:space:]])IP( Address)?:${peer_ip}([,[:space:]]|$)"; then
    need_tls=1
    warn "TLS SAN missing ${peer_ip}; regenerating cert"
  fi
fi
if [ "$need_tls" -eq 0 ]; then
  ok "Existing TLS material retained"
else
  san="DNS:localhost,IP:127.0.0.1"
  if [ -n "$peer_name" ]; then
    san="${san},DNS:${peer_name}"
  fi
  if [ -n "$peer_ip" ]; then
    san="${san},IP:${peer_ip}"
  fi
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" -days 825 \
    -subj "/CN=kin-mail-console/O=KIN Mail/C=ID" \
    -addext "subjectAltName=${san}" >/dev/null 2>&1
  chmod 640 "$KEY" "$CERT"
  chown "${SVC_USER}:${SVC_USER}" "$KEY" "$CERT"
  ok "Generated self-signed cert (SAN=${san})"
fi

# Peer activate: never mint an admin password / initial-admin-password.
# Prefer Host A staged users/secret under /tmp (scp'd before activate) so the
# service never listens on LAN with an empty store + anonymous wizard.
USERS_FILE="${DATA_ROOT}/users.json"
STAGED_USERS="/tmp/kin-mail-peer-users.json"
STAGED_SECRET="/tmp/kin-mail-peer-session.secret"
if [ -f "$STAGED_USERS" ]; then
  [ ! -L "$STAGED_USERS" ] || { fail "refusing: ${STAGED_USERS} is a symlink"; exit 1; }
  install -m 600 -o "${SVC_USER}" -g "${SVC_USER}" "$STAGED_USERS" "$USERS_FILE"
  rm -f "$STAGED_USERS"
  ok "Installed staged users.json from Host A (before listen)"
elif [ -f "$USERS_FILE" ]; then
  ok "users.json present (will be overwritten by Host A sync if needed)"
else
  printf '%s\n' '{"version":1,"users":[]}' >"$USERS_FILE"
  chmod 600 "$USERS_FILE"
  chown "${SVC_USER}:${SVC_USER}" "$USERS_FILE"
  ok "Empty users.json placeholder (Host A pushes admin next; no local mint)"
fi
SECRET_FILE="${DATA_ROOT}/session.secret"
if [ -f "$STAGED_SECRET" ]; then
  [ ! -L "$STAGED_SECRET" ] || { fail "refusing: ${STAGED_SECRET} is a symlink"; exit 1; }
  install -m 600 -o "${SVC_USER}" -g "${SVC_USER}" "$STAGED_SECRET" "$SECRET_FILE"
  rm -f "$STAGED_SECRET"
  ok "Installed staged session.secret from Host A"
elif [ ! -f "$SECRET_FILE" ]; then
  # Placeholder so the service can start; Host A overwrites with the shared secret.
  "$PEER_PY" - <<PY
import secrets
from pathlib import Path
p = Path("${SECRET_FILE}")
p.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
p.chmod(0o600)
PY
  chown "${SVC_USER}:${SVC_USER}" "$SECRET_FILE"
  ok "Temporary session secret (Host A will replace)"
fi
chown -R "${SVC_USER}:${SVC_USER}" "$DATA_ROOT"

# Peer must listen on all interfaces so Host A / admin can reach :9443.
if [ -f "$ENV_FILE" ]; then
  if grep -q '^CONSOLE_BIND=' "$ENV_FILE"; then
    sed -i 's/^CONSOLE_BIND=.*/CONSOLE_BIND=0.0.0.0/' "$ENV_FILE"
  else
    printf 'CONSOLE_BIND=0.0.0.0\n' >>"$ENV_FILE"
  fi
  ok "CONSOLE_BIND=0.0.0.0 for peer console"
fi

# Sync AD console env from kin-mail config when present (no password print).
if [ -f /etc/kin-mail/config ] && [ -x "$PEER_PY" ]; then
  PYTHONPATH="${OPT_ROOT}/backend" "$PEER_PY" - <<'PY' || true
import sys
sys.path.insert(0, "/opt/kin-mail-console/backend")
from kin_console.ad_settings import load_ad_settings, sync_ad_env_from_kin_config
path = sync_ad_env_from_kin_config()
cfg = load_ad_settings()
print(f"ad_env={path} enabled={cfg.enabled} url_set={bool(cfg.ldap_url)}")
PY
  ok "Console AD env synced (passwords not printed)"
fi

# Light firewall: open console port without full apply/dead-man.
_derive_slash24() {
  # $1 = IPv4 → a.b.c.0/24
  local ip="$1" _a _rest _b _c
  _a=${ip%%.*}
  _rest=${ip#*.}
  _b=${_rest%%.*}
  _rest=${_rest#*.}
  _c=${_rest%%.*}
  printf '%s.%s.%s.0/24' "$_a" "$_b" "$_c"
}

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then
  say "Ensure UFW allows console port ${CONSOLE_PORT}"
  cluster_net=""
  ufw_ok=0
  if [ -f /etc/kin-mail/config ]; then
    # shellcheck disable=SC1091
    set -a; . /etc/kin-mail/config; set +a
    # Prefer VIP /24; fall back to this node's SERVER_IP (MAIL_HOST_IP is not a config key).
    if [ -n "${CLUSTER_VIP_IP:-}" ]; then
      cluster_net="$(_derive_slash24 "$CLUSTER_VIP_IP")"
    elif [ -n "${SERVER_IP:-}" ]; then
      cluster_net="$(_derive_slash24 "$SERVER_IP")"
    fi
    for ip in ${KIN_ADMIN_IPS:-}; do
      if ufw allow from "$ip" to any port "$CONSOLE_PORT" proto tcp comment 'KIN console admin-IP' >/dev/null 2>&1; then
        ufw_ok=1
      fi
    done
  fi
  if [ -z "$cluster_net" ] && [ -n "${KIN_PEER_CONSOLE_IP:-}" ]; then
    cluster_net="$(_derive_slash24 "$KIN_PEER_CONSOLE_IP")"
  fi
  if [ -n "$cluster_net" ]; then
    if ufw allow from "$cluster_net" to any port "$CONSOLE_PORT" proto tcp comment 'KIN console LAN' >/dev/null 2>&1; then
      ufw_ok=1
      ok "UFW console rules ensured for ${cluster_net}"
    else
      fail "UFW allow for ${CONSOLE_PORT} from ${cluster_net} failed"
      exit 1
    fi
  else
    fail "Could not derive CLUSTER_NET (need CLUSTER_VIP_IP, SERVER_IP, or KIN_PEER_CONSOLE_IP); refusing peer console with active UFW and no LAN allow"
    exit 1
  fi
  if [ "$ufw_ok" -ne 1 ]; then
    fail "UFW is active but no console allow rule was applied for port ${CONSOLE_PORT}"
    exit 1
  fi
fi

say "systemd - privhelperd + console"
install -m 644 "$PRIV_UNIT_SRC" "$PRIV_UNIT_DST"
install -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable kin-mail-privhelperd.service >/dev/null
systemctl enable kin-mail-console.service >/dev/null
# Never prompt; restart is required so the transferred tree is loaded.
systemctl restart kin-mail-privhelperd.service
sleep 1
if ! systemctl is-active --quiet kin-mail-privhelperd.service; then
  fail "privhelperd failed - journalctl -u kin-mail-privhelperd -n 40"
  journalctl -u kin-mail-privhelperd -n 40 --no-pager | sed 's/^/    /' || true
  exit 1
fi
ok "kin-mail-privhelperd.service active"

systemctl restart kin-mail-console.service
sleep 2
if ! systemctl is-active --quiet kin-mail-console.service; then
  fail "console failed - journalctl -u kin-mail-console -n 40"
  journalctl -u kin-mail-console -n 40 --no-pager | sed 's/^/    /' || true
  exit 1
fi
ok "kin-mail-console.service active"

code="$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:${CONSOLE_PORT}/api/health" || true)"
if [ "$code" != "200" ]; then
  fail "Health check failed (HTTP ${code:-none}) on https://127.0.0.1:${CONSOLE_PORT}/api/health"
  exit 1
fi
ok "Health https://127.0.0.1:${CONSOLE_PORT}/api/health → 200"
ok "Peer console activate complete"
exit 0
