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
# Ubuntu 24.04 ships php8.3 in universe; 22.04 (jammy) does not — only 8.1.
# When the package is missing, pin ppa:ondrej/php by writing the deb line and
# GPG key ourselves. Do NOT call add-apt-repository: modern launchpadlib talks
# to Launchpad OAuth/API endpoints that time out even when curl to
# launchpad.net itself returns 200 (seen on operator VMs, 2026-08-20).
#
# Same pattern as 03-install-zimbra.sh (HTTPS keyserver, not HKP :11371,
# fingerprint gate, bundled install/lib fallback).
#
# Signing key from https://launchpad.net/~ondrej/+archive/ubuntu/php
# (2026-08-20): 4096R fingerprint B8DC7E53946656EFBCE4C1DD71DAEAAB4AD4CAB6
# (rsa4096, 2024-04-24, uid "Launchpad PPA for Ondřej Surý"). Cross-checked
# against keyserver.ubuntu.com before commit. Refresh:
#   curl -fsSL -o install/lib/ondrej-php.asc \
#     "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xB8DC7E53946656EFBCE4C1DD71DAEAAB4AD4CAB6"
#   gpg --show-keys install/lib/ondrej-php.asc
ONDREJ_PHP_APT_FPR="B8DC7E53946656EFBCE4C1DD71DAEAAB4AD4CAB6"
ONDREJ_PHP_KEYRING="/etc/apt/keyrings/ondrej-php.gpg"
ONDREJ_PHP_KEY_URL="https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${ONDREJ_PHP_APT_FPR}"
ONDREJ_PHP_KEY_LOCAL="${PWD}/lib/ondrej-php.asc"

php83_apt_available() {
  local cand
  cand=$(apt-cache policy php8.3-fpm 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
  [ -n "$cand" ] && [ "$cand" != "(none)" ]
}

fetch_ondrej_php_key_asc() {
  local dest="$1"
  ONDREJ_PHP_KEY_SOURCE=""

  if curl -fsSL -m 60 -o "$dest" "$ONDREJ_PHP_KEY_URL" \
    && grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$dest"; then
    ONDREJ_PHP_KEY_SOURCE="official remote (${ONDREJ_PHP_KEY_URL})"
    return 0
  fi
  warn "Official ondrej/php key URL failed (404/timeout/invalid body):"
  info "  ${ONDREJ_PHP_KEY_URL}"
  rm -f "$dest"

  if [ -f "$ONDREJ_PHP_KEY_LOCAL" ] \
    && grep -q "BEGIN PGP PUBLIC KEY BLOCK" "$ONDREJ_PHP_KEY_LOCAL"; then
    cp -f "$ONDREJ_PHP_KEY_LOCAL" "$dest"
    ONDREJ_PHP_KEY_SOURCE="bundled fallback (${ONDREJ_PHP_KEY_LOCAL})"
    warn "Using bundled key copy — refresh install/lib/ondrej-php.asc if Ondřej rotates keys"
    return 0
  fi

  fail "Could not obtain ondrej/php PPA GPG key from any source."
  info "  Tried official:  ${ONDREJ_PHP_KEY_URL}"
  info "  Tried fallback:  ${ONDREJ_PHP_KEY_LOCAL}"
  info "Fix HTTPS to keyserver.ubuntu.com, or restore install/lib/ondrej-php.asc, then re-run Deploy."
  return 1
}

install_ondrej_php_keyring() {
  local tmp_home tmp_asc tmp_kr gpg_err
  if [ -f "$ONDREJ_PHP_KEYRING" ] \
    && gpg --batch --show-keys "$ONDREJ_PHP_KEYRING" 2>/dev/null \
         | grep -qw "$ONDREJ_PHP_APT_FPR"; then
    info "ondrej/php signing key already in ${ONDREJ_PHP_KEYRING}"
    return 0
  fi

  command -v curl >/dev/null || { fail "curl required to fetch ondrej/php GPG key"; exit 1; }
  if ! command -v gpg >/dev/null 2>&1; then
    if ! apt-get -y install gnupg ca-certificates >/dev/null; then
      fail "gpg is required to import the ondrej/php PPA key (apt-get install gnupg failed)"
      exit 1
    fi
  fi

  tmp_home=$(mktemp -d "${TMPDIR:-/tmp}/kin-ondrej-gnupg.XXXXXX")
  chmod 700 "$tmp_home"
  tmp_asc=$(mktemp "${TMPDIR:-/tmp}/kin-ondrej-php.XXXXXX.asc")
  tmp_kr=$(mktemp "${TMPDIR:-/tmp}/kin-ondrej-php.XXXXXX.krring")
  rm -f "$tmp_kr"
  gpg_err=$(mktemp "${TMPDIR:-/tmp}/kin-ondrej-php.XXXXXX.err")

  if ! fetch_ondrej_php_key_asc "$tmp_asc"; then
    rm -rf "$tmp_home"
    rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
    exit 1
  fi
  info "ondrej/php key material from: ${ONDREJ_PHP_KEY_SOURCE}"

  if ! gpg --homedir "$tmp_home" --batch --no-default-keyring --keyring "$tmp_kr" \
        --import "$tmp_asc" >/dev/null 2>"$gpg_err"; then
    fail "gpg --import failed for ondrej/php PPA key"
    info "  Source: ${ONDREJ_PHP_KEY_SOURCE}"
    sed 's/^/    /' "$gpg_err" | tail -n 20
    rm -rf "$tmp_home"
    rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
    exit 1
  fi
  if ! gpg --homedir "$tmp_home" --batch --no-default-keyring --keyring "$tmp_kr" \
        --list-keys 2>/dev/null | grep -qw "$ONDREJ_PHP_APT_FPR"; then
    fail "ondrej/php key does not contain expected fingerprint ${ONDREJ_PHP_APT_FPR}"
    info "  Source: ${ONDREJ_PHP_KEY_SOURCE}"
    info "  Refusing to install an unverified key."
    rm -rf "$tmp_home"
    rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"
    exit 1
  fi

  mkdir -p "$(dirname "$ONDREJ_PHP_KEYRING")"
  rm -f "$ONDREJ_PHP_KEYRING"
  gpg --homedir "$tmp_home" --batch --no-default-keyring --keyring "$tmp_kr" \
    --export --output "$ONDREJ_PHP_KEYRING"
  chmod 644 "$ONDREJ_PHP_KEYRING"
  rm -rf "$tmp_home"
  rm -f "$tmp_asc" "$tmp_kr" "${tmp_kr}~" "$gpg_err"

  if ! gpg --batch --show-keys "$ONDREJ_PHP_KEYRING" 2>/dev/null \
       | grep -qw "$ONDREJ_PHP_APT_FPR"; then
    fail "Installed ${ONDREJ_PHP_KEYRING} but fingerprint check failed"
    exit 1
  fi
  info "Installed ondrej/php signing key → ${ONDREJ_PHP_KEYRING}"
}

write_ondrej_php_list() {
  local codename="$1" list
  list="/etc/apt/sources.list.d/ondrej-ubuntu-php-${codename}.list"
  cat > "$list" <<EOF
# KIN Mail — ppa:ondrej/php (written by 07-zpush.sh; not add-apt-repository).
# Signing key ${ONDREJ_PHP_APT_FPR} (Launchpad PPA for Ondřej Surý).
deb [signed-by=${ONDREJ_PHP_KEYRING}] https://ppa.launchpadcontent.net/ondrej/php/ubuntu ${codename} main
EOF
  chmod 644 "$list"
  info "Wrote ${list}"
}

ensure_php83_apt_source() {
  local codename
  if php83_apt_available; then
    info "php8.3-fpm already in apt"
    return 0
  fi

  info "php8.3-fpm not in current apt sources (expected on Ubuntu 22.04) — adding ppa:ondrej/php"
  # shellcheck disable=SC1091
  . /etc/os-release
  codename="${VERSION_CODENAME:-}"
  if [ -z "$codename" ]; then
    fail "Ubuntu VERSION_CODENAME is empty — cannot add ppa:ondrej/php"
    exit 1
  fi

  install_ondrej_php_keyring
  write_ondrej_php_list "$codename"

  if ! apt-get -qq update; then
    fail "apt-get update failed after adding ppa:ondrej/php"
    exit 1
  fi
  if ! php83_apt_available; then
    fail "php8.3-fpm still not available after adding ppa:ondrej/php"
    exit 1
  fi
  info "php8.3-fpm available via ppa:ondrej/php"
}

install_packages() {
  say "1. Packages (PHP 8.3-FPM + helpers)"
  export DEBIAN_FRONTEND=noninteractive
  if ! apt-get -qq update; then
    fail "apt-get update failed"
    exit 1
  fi
  ensure_php83_apt_source
  if ! apt-get -y install \
    php8.3-fpm php8.3-cli php8.3-curl php8.3-xml php8.3-mbstring \
    php8.3-intl php8.3-soap php8.3-zip unzip curl ca-certificates >/dev/null; then
    fail "apt-get failed to install PHP 8.3-FPM and dependencies"
    exit 1
  fi
  if ! dpkg -s php8.3-fpm >/dev/null 2>&1; then
    fail "php8.3-fpm is not installed after apt-get (refusing to print OK)"
    exit 1
  fi
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
# Zimbra's nginx does NOT ship /etc/nginx (distro package often absent). Including
# /etc/nginx/fastcgi_params made every zmproxy restart [emerg] and silently leave
# the old master running — ActiveSync kept returning 404 while the stage reported OK.
write_zpush_fastcgi_params() {
  local dest="${ZPUSH_ROOT}/fastcgi_params"
  local tmp
  tmp=$(mktemp)
  cat >"$tmp" <<'EOF'
fastcgi_param  QUERY_STRING       $query_string;
fastcgi_param  REQUEST_METHOD     $request_method;
fastcgi_param  CONTENT_TYPE       $content_type;
fastcgi_param  CONTENT_LENGTH     $content_length;
fastcgi_param  SCRIPT_NAME        $fastcgi_script_name;
fastcgi_param  REQUEST_URI        $request_uri;
fastcgi_param  DOCUMENT_URI       $document_uri;
fastcgi_param  DOCUMENT_ROOT      $document_root;
fastcgi_param  SERVER_PROTOCOL    $server_protocol;
fastcgi_param  REQUEST_SCHEME     $scheme;
fastcgi_param  HTTPS             $https if_not_empty;
fastcgi_param  GATEWAY_INTERFACE  CGI/1.1;
fastcgi_param  SERVER_SOFTWARE    nginx/$nginx_version;
fastcgi_param  REMOTE_ADDR        $remote_addr;
fastcgi_param  REMOTE_PORT        $remote_port;
fastcgi_param  SERVER_ADDR        $server_addr;
fastcgi_param  SERVER_PORT        $server_port;
fastcgi_param  SERVER_NAME        $server_name;
fastcgi_param  REDIRECT_STATUS    200;
EOF
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    rm -f "$tmp"
    return 1
  fi
  cp "$tmp" "$dest"
  rm -f "$tmp"
  return 0
}

write_nginx_snippets() {
  say "5. Nginx fastcgi snippets"
  local as_tmp ad_tmp as_changed=0 ad_changed=0 params_changed=0
  as_tmp=$(mktemp)
  ad_tmp=$(mktemp)

  if write_zpush_fastcgi_params; then
    params_changed=1
    ok "fastcgi_params written under ${ZPUSH_ROOT} (Zimbra-safe, not /etc/nginx)"
  else
    ok "fastcgi_params unchanged"
  fi

  cat >"$as_tmp" <<EOF
fastcgi_pass ${PHP_FPM_LISTEN};
include ${ZPUSH_ROOT}/fastcgi_params;
fastcgi_param SCRIPT_FILENAME ${ZPUSH_ROOT}/index.php;
fastcgi_param SCRIPT_NAME /Microsoft-Server-ActiveSync;
fastcgi_param HTTPS on;
fastcgi_param PHP_VALUE "post_max_size=128M \\n upload_max_filesize=128M \\n max_execution_time=3660";
fastcgi_read_timeout 910;
client_max_body_size 128m;
EOF
  cat >"$ad_tmp" <<EOF
fastcgi_pass ${PHP_FPM_LISTEN};
include ${ZPUSH_ROOT}/fastcgi_params;
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
  # Capture modes before chmod: Zimbra nginx runs as `zimbra` and must be able to
  # `include` these snippets. Mode 0600 www-data-only produced ActiveSync HTTP 404
  # (empty location after failed include) despite a "successful" proxy restart.
  as_mode=$(stat -c '%a' "${ZPUSH_ROOT}/nginx-zpush.conf" 2>/dev/null || echo 000)
  ad_mode=$(stat -c '%a' "${ZPUSH_ROOT}/nginx-zpush-autodiscover.conf" 2>/dev/null || echo 000)
  # Directory must be traversable by zimbra (o+x); files world-readable.
  chmod 755 "$ZPUSH_ROOT"
  chown www-data:www-data \
    "${ZPUSH_ROOT}/fastcgi_params" \
    "${ZPUSH_ROOT}/nginx-zpush.conf" \
    "${ZPUSH_ROOT}/nginx-zpush-autodiscover.conf"
  chmod 644 \
    "${ZPUSH_ROOT}/fastcgi_params" \
    "${ZPUSH_ROOT}/nginx-zpush.conf" \
    "${ZPUSH_ROOT}/nginx-zpush-autodiscover.conf"
  if [ "$as_mode" != "644" ] || [ "$ad_mode" != "644" ]; then
    warn "Fixed nginx snippet modes (${as_mode}/${ad_mode} → 644) so zimbra can include them"
    as_changed=1
  fi
  if ! sudo -u zimbra test -r "${ZPUSH_ROOT}/nginx-zpush.conf" \
    || ! sudo -u zimbra test -r "${ZPUSH_ROOT}/fastcgi_params"; then
    fail "zimbra still cannot read Z-Push nginx snippets — ActiveSync would 404"
    exit 1
  fi
  rm -f "$as_tmp" "$ad_tmp"
  SNIPPET_CHANGED=$((as_changed + ad_changed + params_changed))
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
  kin_zimbra_unmanage
  trap kin_zimbra_remanage EXIT
  if ! zimbra_cmd /opt/zimbra/libexec/zmproxyconfgen >/tmp/kin-zpush-confgen.out 2>&1; then
    fail "zmproxyconfgen failed — see /tmp/kin-zpush-confgen.out"
    kin_zimbra_remanage
    trap - EXIT
    exit 1
  fi
  # Config-test BEFORE restart: a failed include ([emerg]) leaves the old master
  # running while zmproxyctl can still look "OK".
  if ! sudo -u zimbra /opt/zimbra/common/sbin/nginx \
    -c /opt/zimbra/conf/nginx.conf -t >/tmp/kin-zpush-nginx-t.out 2>&1; then
    fail "nginx -t failed after confgen — ActiveSync include broken (see /tmp/kin-zpush-nginx-t.out)"
    sed -n '1,40p' /tmp/kin-zpush-nginx-t.out | sed 's/^/    /'
    kin_zimbra_remanage
    trap - EXIT
    exit 1
  fi
  local master_before
  master_before=$(pgrep -o -f '/opt/zimbra/common/sbin/nginx -c' || true)
  if ! zimbra_cmd zmproxyctl restart >/tmp/kin-zpush-proxy.out 2>&1; then
    fail "zmproxyctl restart failed — see /tmp/kin-zpush-proxy.out"
    kin_zimbra_remanage
    trap - EXIT
    exit 1
  fi
  sleep 2
  local master_after emerg_hit
  master_after=$(pgrep -o -f '/opt/zimbra/common/sbin/nginx -c' || true)
  emerg_hit=$(tail -n 80 /opt/zimbra/log/nginx.log 2>/dev/null | grep '\[emerg\]' | tail -n 3 || true)
  if [ -n "$emerg_hit" ]; then
    # Only fail if emerg is newer than this restart attempt (same second window is enough).
    if [ -n "$master_before" ] && [ "$master_before" = "$master_after" ]; then
      fail "zmproxy restart did not replace nginx master (still pid ${master_after}) — likely [emerg] on new config"
      printf '%s\n' "$emerg_hit" | sed 's/^/    /'
      kin_zimbra_remanage
      trap - EXIT
      exit 1
    fi
  fi
  if ! kin_zimbra_wait_healthy; then
    fail "Webmail/Zimbra not healthy after proxy restart"
    kin_zimbra_remanage
    trap - EXIT
    exit 1
  fi
  kin_zimbra_remanage
  trap - EXIT
  local code
  code=$(curl -sk -o /dev/null -w '%{http_code}' \
    --resolve "${MAIL_HOST}:443:127.0.0.1" "https://${MAIL_HOST}/" || true)
  ok "Proxy restarted; https://${MAIL_HOST}/ → ${code}"
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
  local hdrs code
  # Use the real server_name — default_server is often commented out on Zimbra.
  hdrs=$(curl -sk -D- -o /dev/null -X OPTIONS \
    --resolve "${MAIL_HOST}:443:127.0.0.1" \
    "https://${MAIL_HOST}/Microsoft-Server-ActiveSync" 2>/dev/null || true)
  code=$(printf '%s' "$hdrs" | awk 'toupper($0) ~ /^HTTP\//{print $2; exit}')
  if [ "$code" = "404" ]; then
    fail "ActiveSync OPTIONS → 404 (nginx location/include not loaded — check nginx -t / nginx.log [emerg])"
    printf '%s\n' "$hdrs" | sed -n '1,20p' | sed 's/^/    /'
    exit 1
  fi
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
      fail "ActiveSync OPTIONS failed (HTTP ${code:-?})"
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
