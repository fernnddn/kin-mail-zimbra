#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 07 Z-Push (ActiveSync + Autodiscover via Zimbra backend)
#
# Installs Z-Push on the local mail node (PHP-FPM + Zimbra nginx includes).
# Intended for the current Primary / Host A. Host B / Pacemaker HA placement
# is an open design question — do not treat this as a cluster resource yet.
#
#   sudo ./07-zpush.sh
#
# Idempotent: a host that already has the expected VERSION + backend is
# configure-only (no wipe/reinstall). Nginx proxy is restarted ONLY when the
# Zimbra template or included snippets actually change.
#
# Force a wipe+reinstall (dangerous on a live MX): KIN_ZPUSH_FORCE_REINSTALL=1
# Skip proxy restart even when snippets change:   KIN_ZPUSH_SKIP_PROXY_RESTART=1
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

ZPUSH_VERSION="${ZPUSH_VERSION:-2.7.6}"
ZPUSH_ROOT="${ZPUSH_ROOT:-/opt/z-push}"
ZPUSH_STATE="${ZPUSH_STATE:-/var/lib/z-push}"
ZPUSH_LOGDIR="${ZPUSH_LOGDIR:-/var/log/z-push}"
ZPUSH_ZIP_URL="${ZPUSH_ZIP_URL:-https://github.com/Z-Hub/Z-Push/archive/refs/tags/${ZPUSH_VERSION}.zip}"
ZIMBRA_BACKEND_TGZ_URL="${ZIMBRA_BACKEND_TGZ_URL:-https://downloads.sourceforge.net/project/zimbrabackend/Release75/zimbra75.tgz}"
ZIMBRA_BACKEND_URL_LOCAL="${ZIMBRA_BACKEND_URL_LOCAL:-https://127.0.0.1:8443}"
PHP_FPM_POOL="${PHP_FPM_POOL:-/etc/php/8.3/fpm/pool.d/www.conf}"
PHP_FPM_LISTEN="${PHP_FPM_LISTEN:-127.0.0.1:9000}"
NGX_TPL="${NGX_TPL:-/opt/zimbra/conf/nginx/templates/nginx.conf.web.https.default.template}"
WORKDIR="${KIN_ZPUSH_WORKDIR:-/var/tmp/kin-zpush}"

if [ ! -d /opt/zimbra ]; then
  fail "Zimbra is not installed yet - run 03-install-zimbra.sh first"
  exit 1
fi

if [ ! -f "$NGX_TPL" ]; then
  fail "Zimbra nginx HTTPS template missing: ${NGX_TPL}"
  info "Is this the Primary with /opt/zimbra mounted?"
  exit 1
fi

echo
say "Z-Push ${ZPUSH_VERSION} (Zimbra backend Release 75)"

# -----------------------------------------------------------------------------
install_packages() {
  say "1. Packages (PHP 8.3-FPM + helpers)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get -qq update
  apt-get -y install \
    php8.3-fpm php8.3-cli php8.3-curl php8.3-xml php8.3-mbstring \
    php8.3-intl php8.3-soap php8.3-zip unzip curl ca-certificates >/dev/null
  ok "PHP 8.3-FPM and dependencies present"
}

# -----------------------------------------------------------------------------
ensure_php_fpm_listen() {
  say "2. PHP-FPM listen ${PHP_FPM_LISTEN}"
  if [ ! -f "$PHP_FPM_POOL" ]; then
    fail "PHP-FPM pool missing: ${PHP_FPM_POOL}"
    exit 1
  fi
  if grep -qE "^listen\s*=\s*${PHP_FPM_LISTEN//./\\.}$" "$PHP_FPM_POOL"; then
    ok "listen already ${PHP_FPM_LISTEN}"
  else
    sed -i -E "s|^;?listen\s*=.*|listen = ${PHP_FPM_LISTEN}|" "$PHP_FPM_POOL"
    ok "listen set to ${PHP_FPM_LISTEN}"
  fi
  systemctl enable --now php8.3-fpm >/dev/null
  systemctl reload php8.3-fpm 2>/dev/null || systemctl restart php8.3-fpm
  if ! systemctl is-active --quiet php8.3-fpm; then
    fail "php8.3-fpm is not active"
    exit 1
  fi
  ok "php8.3-fpm active"
}

# -----------------------------------------------------------------------------
zpush_installed_ok() {
  [ -f "${ZPUSH_ROOT}/index.php" ] \
    && [ -f "${ZPUSH_ROOT}/VERSION" ] \
    && [ "$(tr -d '[:space:]' <"${ZPUSH_ROOT}/VERSION")" = "$ZPUSH_VERSION" ] \
    && [ -f "${ZPUSH_ROOT}/backend/zimbra/zimbra.php" ] \
    && [ -f "${ZPUSH_ROOT}/backend/zimbra/config.php" ]
}

install_zpush_tree() {
  say "3. Z-Push tree + Zimbra backend"
  if zpush_installed_ok && [ "${KIN_ZPUSH_FORCE_REINSTALL:-0}" != "1" ]; then
    ok "Already at ${ZPUSH_VERSION} with Zimbra backend — configure-only (no wipe)"
    return 0
  fi

  if [ -d "$ZPUSH_ROOT" ] && [ "${KIN_ZPUSH_FORCE_REINSTALL:-0}" = "1" ]; then
    warn "KIN_ZPUSH_FORCE_REINSTALL=1 — wiping ${ZPUSH_ROOT}"
  elif [ -d "$ZPUSH_ROOT" ] && ! zpush_installed_ok; then
    warn "Incomplete/unexpected install at ${ZPUSH_ROOT}; replacing"
  fi

  mkdir -p "$WORKDIR"
  rm -rf "${WORKDIR}/src" "${WORKDIR}/backend" \
    "${WORKDIR}/z-push-${ZPUSH_VERSION}.zip" "${WORKDIR}/zimbra75.tgz"
  mkdir -p "${WORKDIR}/src" "${WORKDIR}/backend"

  info "Downloading Z-Push ${ZPUSH_VERSION}"
  curl -fsSL -o "${WORKDIR}/z-push-${ZPUSH_VERSION}.zip" "$ZPUSH_ZIP_URL"
  unzip -q -o "${WORKDIR}/z-push-${ZPUSH_VERSION}.zip" -d "${WORKDIR}/src"
  local src
  src=$(find "${WORKDIR}/src" -type f -name index.php | head -1)
  [ -n "$src" ] || { fail "index.php not found in Z-Push zip"; exit 1; }
  src=$(dirname "$src")

  info "Downloading Zimbra backend Release 75"
  curl -fsSL -L -o "${WORKDIR}/zimbra75.tgz" "$ZIMBRA_BACKEND_TGZ_URL"
  tar -xzf "${WORKDIR}/zimbra75.tgz" -C "${WORKDIR}/backend"
  local zsrc
  zsrc=$(dirname "$(find "${WORKDIR}/backend" -type f -name zimbra.php | head -1)")
  [ -n "$zsrc" ] || { fail "zimbra.php not found in backend tarball"; exit 1; }

  rm -rf "$ZPUSH_ROOT"
  mkdir -p "$ZPUSH_ROOT"
  cp -a "${src}/." "$ZPUSH_ROOT/"
  printf '%s\n' "$ZPUSH_VERSION" >"${ZPUSH_ROOT}/VERSION"
  mkdir -p "${ZPUSH_ROOT}/backend/zimbra"
  cp -a "${zsrc}/." "${ZPUSH_ROOT}/backend/zimbra/"
  ok "Installed ${ZPUSH_ROOT} (${ZPUSH_VERSION} + backend 75)"
}

# -----------------------------------------------------------------------------
set_php_define() {
  # set_php_define FILE KEY VALUE [quote|raw]
  local file="$1" key="$2" value="$3" mode="${4:-quote}"
  local val desired
  if [ "$mode" = "raw" ]; then
    val="$value"
  else
    val="'${value}'"
  fi
  desired="define('${key}', ${val});"
  # Already correct — do not rewrite (keeps re-runs hash-stable).
  if grep -Fq "$desired" "$file"; then
    return 0
  fi
  if grep -qE "^[[:space:]]*define\(\s*'${key}'" "$file"; then
    sed -i -E "s|^[[:space:]]*define\(\s*'${key}'\s*,\s*[^)]+\)\s*;|${desired}|" "$file"
  elif grep -qE "^[[:space:]]*//[[:space:]]*define\(\s*'${key}'" "$file"; then
    sed -i -E "s|^[[:space:]]*//[[:space:]]*define\(\s*'${key}'\s*,\s*[^)]+\)\s*;|${desired}|" "$file"
  else
    printf '\n%s\n' "$desired" >>"$file"
  fi
}

configure_zpush() {
  say "4. Configure Z-Push + backend"
  mkdir -p "$ZPUSH_STATE" "$ZPUSH_LOGDIR"
  chown -R www-data:www-data "$ZPUSH_STATE" "$ZPUSH_LOGDIR"
  chmod 755 "$ZPUSH_STATE" "$ZPUSH_LOGDIR"

  set_php_define "${ZPUSH_ROOT}/config.php" TIMEZONE "${TIMEZONE}"
  set_php_define "${ZPUSH_ROOT}/config.php" STATE_DIR "${ZPUSH_STATE}/"
  set_php_define "${ZPUSH_ROOT}/config.php" LOGFILEDIR "${ZPUSH_LOGDIR}/"
  set_php_define "${ZPUSH_ROOT}/config.php" BACKEND_PROVIDER BackendZimbra

  local bcfg="${ZPUSH_ROOT}/backend/zimbra/config.php"
  [ -f "$bcfg" ] || { fail "missing ${bcfg}"; exit 1; }
  if [ ! -f "${bcfg}.orig" ]; then
    cp -a "$bcfg" "${bcfg}.orig"
  fi
  set_php_define "$bcfg" ZIMBRA_URL "$ZIMBRA_BACKEND_URL_LOCAL"
  set_php_define "$bcfg" ZIMBRA_SSL_VERIFYPEER false raw
  set_php_define "$bcfg" ZIMBRA_SSL_VERIFYHOST false raw
  set_php_define "$bcfg" ZIMBRA_DISABLE_URL_OVERRIDE true raw
  set_php_define "$bcfg" ZIMBRA_USER_DIR zimbra
  set_php_define "$bcfg" ZIMBRA_LOCAL_CACHE true raw

  if [ -f "${ZPUSH_ROOT}/autodiscover/config.php" ]; then
    set_php_define "${ZPUSH_ROOT}/autodiscover/config.php" TIMEZONE "${TIMEZONE}"
    set_php_define "${ZPUSH_ROOT}/autodiscover/config.php" BACKEND_PROVIDER BackendZimbra
    set_php_define "${ZPUSH_ROOT}/autodiscover/config.php" ZPUSH_HOST "${MAIL_HOST}"
    set_php_define "${ZPUSH_ROOT}/autodiscover/config.php" USE_FULLEMAIL_FOR_LOGIN true raw
  fi

  chown -R www-data:www-data "$ZPUSH_ROOT"
  ok "config.php / backend / autodiscover tuned for ${MAIL_HOST}"
}

# -----------------------------------------------------------------------------
write_nginx_snippets() {
  say "5. Nginx fastcgi snippets"
  local as_tmp ad_tmp as_changed=0 ad_changed=0
  as_tmp=$(mktemp)
  ad_tmp=$(mktemp)
  cat >"$as_tmp" <<EOF
fastcgi_pass ${PHP_FPM_LISTEN};
include /etc/nginx/fastcgi_params;
fastcgi_param SCRIPT_FILENAME ${ZPUSH_ROOT}/index.php;
fastcgi_param SCRIPT_NAME /Microsoft-Server-ActiveSync;
fastcgi_param HTTPS on;
fastcgi_param PHP_VALUE "post_max_size=128M \\n upload_max_filesize=128M \\n max_execution_time=3660";
fastcgi_read_timeout 910;
client_max_body_size 128m;
EOF
  cat >"$ad_tmp" <<EOF
fastcgi_pass ${PHP_FPM_LISTEN};
include /etc/nginx/fastcgi_params;
fastcgi_param SCRIPT_FILENAME ${ZPUSH_ROOT}/autodiscover/autodiscover.php;
fastcgi_param SCRIPT_NAME /Autodiscover/Autodiscover.xml;
fastcgi_param HTTPS on;
fastcgi_read_timeout 60;
EOF

  if [ -f "${ZPUSH_ROOT}/nginx-zpush.conf" ] && cmp -s "$as_tmp" "${ZPUSH_ROOT}/nginx-zpush.conf"; then
    ok "nginx-zpush.conf unchanged"
  else
    cp "$as_tmp" "${ZPUSH_ROOT}/nginx-zpush.conf"
    as_changed=1
    ok "nginx-zpush.conf written"
  fi
  if [ -f "${ZPUSH_ROOT}/nginx-zpush-autodiscover.conf" ] && cmp -s "$ad_tmp" "${ZPUSH_ROOT}/nginx-zpush-autodiscover.conf"; then
    ok "nginx-zpush-autodiscover.conf unchanged"
  else
    cp "$ad_tmp" "${ZPUSH_ROOT}/nginx-zpush-autodiscover.conf"
    ad_changed=1
    ok "nginx-zpush-autodiscover.conf written"
  fi
  chown www-data:www-data \
    "${ZPUSH_ROOT}/nginx-zpush.conf" \
    "${ZPUSH_ROOT}/nginx-zpush-autodiscover.conf"
  rm -f "$as_tmp" "$ad_tmp"
  SNIPPET_CHANGED=$((as_changed + ad_changed))
}

apply_nginx_template_patches() {
  say "6. Zimbra nginx template (ActiveSync + Autodiscover)"
  TEMPLATE_CHANGED=0
  local py_out
  py_out=$(python3 - "$NGX_TPL" "$ZPUSH_ROOT" <<'PY'
from pathlib import Path
import datetime
import sys

tpl = Path(sys.argv[1])
zroot = Path(sys.argv[2])
text = tpl.read_text()
as_inc = f"include {zroot}/nginx-zpush.conf;"
ad_inc = f"include {zroot}/nginx-zpush-autodiscover.conf;"

# Idempotent short-circuit: already wired to our snippets.
if (
    as_inc in text
    and ad_inc in text
    and "location ^~ /Microsoft-Server-ActiveSync" in text
    and "location ^~ /autodiscover" in text
    and "location ^~ /Autodiscover" in text
):
    print("unchanged")
    raise SystemExit(0)

changed = False

def replace_location(text, needles, replacement):
    idxs = [(text.find(n), n) for n in needles if text.find(n) >= 0]
    if not idxs:
        raise SystemExit(f"none of {needles} found")
    idx, _needle = min(idxs, key=lambda x: x[0])
    line_start = text.rfind("\n", 0, idx) + 1
    prev_nl = text.rfind("\n", 0, line_start - 1) if line_start > 0 else -1
    if prev_nl >= 0:
        candidate = text[prev_nl + 1:line_start]
        if any(k in candidate for k in ("ActiveSync", "autodiscover", "Autodiscover", "Microsoft")):
            line_start = prev_nl + 1
    brace_open = text.find("{", idx)
    depth = 0
    end = None
    for i in range(brace_open, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                if end < len(text) and text[end] == "\n":
                    end += 1
                break
    if end is None:
        raise SystemExit("unbalanced brace")
    old = text[line_start:end]
    if old == replacement:
        return text, False
    return text[:line_start] + replacement + text[end:], True

as_repl = (
    "    # For Microsoft ActiveSync — KIN Mail Z-Push\n"
    "    location ^~ /Microsoft-Server-ActiveSync\n"
    "    {\n"
    f"        include {zroot}/nginx-zpush.conf;\n"
    "    }\n"
)
ad_repl = (
    "    # Autodiscover — KIN Mail Z-Push\n"
    "    location ^~ /autodiscover\n"
    "    {\n"
    f"        include {zroot}/nginx-zpush-autodiscover.conf;\n"
    "    }\n"
    "\n"
    "    location ^~ /Autodiscover\n"
    "    {\n"
    f"        include {zroot}/nginx-zpush-autodiscover.conf;\n"
    "    }\n"
)

if as_inc not in text:
    text, c1 = replace_location(
        text,
        ["location ^~ /Microsoft-Server-ActiveSync"],
        as_repl,
    )
    changed = changed or c1

if ad_inc not in text or "location ^~ /Autodiscover" not in text:
    text, c2 = replace_location(
        text,
        ["location ^~ /autodiscover", "location /autodiscover", "location ^~ /Autodiscover"],
        ad_repl,
    )
    changed = changed or c2

if not changed:
    print("unchanged")
    raise SystemExit(0)

bak = Path(str(tpl) + ".bak.pre-zpush." + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
bak.write_text(tpl.read_text())
tpl.write_text(text)
print(f"changed:{bak}")
PY
) || {
    fail "nginx template patch failed"
    exit 1
  }

  if [[ "$py_out" == unchanged ]]; then
    ok "HTTPS template already wires Z-Push"
  else
    TEMPLATE_CHANGED=1
    ok "HTTPS template patched (${py_out#changed:})"
  fi
}

reload_proxy_if_needed() {
  say "7. Proxy reload (only if needed)"
  if [ "${SNIPPET_CHANGED:-0}" -eq 0 ] && [ "${TEMPLATE_CHANGED:-0}" -eq 0 ]; then
    ok "No nginx changes — leaving zmproxy running (live-safe)"
    return 0
  fi
  if [ "${KIN_ZPUSH_SKIP_PROXY_RESTART:-0}" = "1" ]; then
    warn "Snippets/template changed but KIN_ZPUSH_SKIP_PROXY_RESTART=1 — NOT restarting"
    info "Run: su - zimbra -c 'zmproxyconfgen && zmproxyctl restart' in a window"
    return 0
  fi

  warn "Nginx wiring changed — regenerating config and restarting zmproxy"
  if ! zimbra_cmd /opt/zimbra/libexec/zmproxyconfgen >/tmp/kin-zpush-confgen.out 2>&1; then
    fail "zmproxyconfgen failed — see /tmp/kin-zpush-confgen.out"
    exit 1
  fi
  if ! zimbra_cmd zmproxyctl restart >/tmp/kin-zpush-proxy.out 2>&1; then
    fail "zmproxyctl restart failed — see /tmp/kin-zpush-proxy.out"
    exit 1
  fi
  sleep 2
  local code
  code=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/ || true)
  if [ "$code" != "200" ]; then
    fail "Webmail HTTPS returned ${code} after proxy restart"
    exit 1
  fi
  ok "Proxy restarted; https://127.0.0.1/ → ${code}"
}

# -----------------------------------------------------------------------------
ensure_throttle_safe_ip() {
  say "8. zimbraHttpThrottleSafeIPs (loopback for backend)"
  local cur
  cur=$(zimbra_cmd zmprov gs "$(zimbra_cmd zmhostname)" zimbraHttpThrottleSafeIPs 2>/dev/null | awk '/zimbraHttpThrottleSafeIPs:/{print $2}' || true)
  if printf '%s\n' "$cur" | grep -qx '127.0.0.1'; then
    ok "127.0.0.1 already in zimbraHttpThrottleSafeIPs"
    return 0
  fi
  # Append without wiping other entries
  if [ -n "$cur" ]; then
    zimbra_cmd zmprov ms "$(zimbra_cmd zmhostname)" +zimbraHttpThrottleSafeIPs 127.0.0.1 \
      || zimbra_cmd zmprov ms "$(zimbra_cmd zmhostname)" zimbraHttpThrottleSafeIPs 127.0.0.1
  else
    zimbra_cmd zmprov ms "$(zimbra_cmd zmhostname)" zimbraHttpThrottleSafeIPs 127.0.0.1
  fi
  ok "Ensured 127.0.0.1 in zimbraHttpThrottleSafeIPs"
}

# -----------------------------------------------------------------------------
verify_activesync() {
  say "9. Verify ActiveSync endpoint"
  local hdrs
  hdrs=$(curl -sk -D- -o /dev/null -X OPTIONS \
    "https://127.0.0.1/Microsoft-Server-ActiveSync" 2>/dev/null || true)
  if printf '%s' "$hdrs" | grep -qi 'MS-Server-ActiveSync'; then
    ok "OPTIONS returns MS-Server-ActiveSync headers"
  elif printf '%s' "$hdrs" | grep -qi 'x-z-push-version'; then
    ok "OPTIONS hits Z-Push (x-z-push-version present)"
  elif printf '%s' "$hdrs" | grep -qi 'realm="ZPush"'; then
    ok "OPTIONS hits Z-Push (401 Basic realm=ZPush — auth required, expected)"
  else
    # 401 without AS headers can still mean nginx→php path works
    if printf '%s' "$hdrs" | grep -qE 'HTTP/[0-9.]+ (200|401|403)'; then
      warn "ActiveSync responded but without AS headers — check auth/backend logs"
      printf '%s\n' "$hdrs" | sed -n '1,15p' | sed 's/^/    /'
    else
      fail "ActiveSync OPTIONS failed"
      printf '%s\n' "$hdrs" | sed -n '1,20p' | sed 's/^/    /'
      exit 1
    fi
  fi

  if grep -q 'nginx-zpush.conf' "$NGX_TPL" \
    && grep -q 'nginx-zpush-autodiscover.conf' "$NGX_TPL"; then
    ok "Template includes both Z-Push snippets"
  else
    fail "Template missing Z-Push includes"
    exit 1
  fi
}

# -----------------------------------------------------------------------------
install_packages
ensure_php_fpm_listen
install_zpush_tree
configure_zpush
SNIPPET_CHANGED=0
TEMPLATE_CHANGED=0
write_nginx_snippets
apply_nginx_template_patches
reload_proxy_if_needed
ensure_throttle_safe_ip
verify_activesync

echo
say "Z-Push stage complete"
ok "Tree: ${ZPUSH_ROOT} (VERSION $(tr -d '[:space:]' <"${ZPUSH_ROOT}/VERSION"))"
ok "State/log: ${ZPUSH_STATE} / ${ZPUSH_LOGDIR}"
info "Autodiscover DNS (CNAME/SRV) is operator-approved separately — not published by this script."
info "Pacemaker HA for Z-Push remains an open question (not in kin-mail-svc)."
exit 0
