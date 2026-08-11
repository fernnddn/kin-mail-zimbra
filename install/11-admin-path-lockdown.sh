#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 11 ADMIN PATH LOCKDOWN (public 443)
#
# Port 7071 can be ufw-restricted, but Zimbra still serves the Administration
# UI at https://<mail>/zimbraAdmin on public 443 (same nginx as webmail).
# This patches the HTTPS nginx template to return 404 for admin UI / admin SOAP
# on 443. Real admin stays on :7071 behind host firewall allowlist.
#
#   sudo ./11-admin-path-lockdown.sh
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

NGX_TPL="${NGX_TPL:-/opt/zimbra/conf/nginx/templates/nginx.conf.web.https.default.template}"
MARKER_BEGIN="# KIN Mail — block admin UI on public HTTPS (use :7071 + ufw)"
MARKER_END="# KIN Mail — end admin path lockdown"

zimbra_cmd() {
  # shellcheck disable=SC2086
  su - zimbra -c "$*"
}

if [ ! -f "$NGX_TPL" ]; then
  fail "Missing nginx HTTPS template: ${NGX_TPL}"
  exit 1
fi

say "Patching nginx HTTPS template to block /zimbraAdmin + /service/admin on :443"
py_out=$(python3 - "$NGX_TPL" <<'PY'
from pathlib import Path
import datetime
import sys

tpl = Path(sys.argv[1])
text = tpl.read_text()
begin = "# KIN Mail — block admin UI on public HTTPS (use :7071 + ufw)"
end = "# KIN Mail — end admin path lockdown"
block = (
    f"    {begin}\n"
    "    location ^~ /zimbraAdmin {\n"
    "        return 404;\n"
    "    }\n"
    "    location ^~ /service/admin {\n"
    "        return 404;\n"
    "    }\n"
    f"    {end}\n"
)

if begin in text and "location ^~ /zimbraAdmin" in text and "return 404" in text:
    print("unchanged")
    raise SystemExit(0)

# Insert immediately before the first "location /" (catch-all), so ^~ wins.
needle = "\n    location /\n"
idx = text.find(needle)
if idx < 0:
    needle = "\n    location / {"
    idx = text.find(needle)
if idx < 0:
    raise SystemExit("could not find catch-all location / in template")

# Drop any prior KIN block if half-applied
if begin in text and end in text:
    a = text.find(begin)
    # include leading whitespace/newline of comment line
    line_start = text.rfind("\n", 0, a) + 1
    b = text.find(end)
    b = text.find("\n", b)
    if b < 0:
        b = len(text)
    else:
        b += 1
    text = text[:line_start] + text[b:]
    idx = text.find(needle)
    if idx < 0:
        raise SystemExit("catch-all location / missing after cleanup")

bak = Path(str(tpl) + ".bak.pre-admin-lock." + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
bak.write_text(tpl.read_text())
tpl.write_text(text[:idx] + "\n" + block + text[idx:])
print(f"changed:{bak}")
PY
) || {
  fail "template patch failed: ${py_out:-}"
  exit 1
}

if [ "$py_out" = "unchanged" ]; then
  ok "Template already blocks admin paths on :443"
  NEED_RELOAD=0
else
  ok "Template patched (${py_out#changed:})"
  NEED_RELOAD=1
fi

if [ "$NEED_RELOAD" -eq 1 ]; then
  warn "Regenerating nginx + restarting zmproxy (kin-zimbra temporarily unmanaged)"
  pcs resource unmanage kin-zimbra 2>/dev/null || true
  if ! zimbra_cmd /opt/zimbra/libexec/zmproxyconfgen >/tmp/kin-admin-lock-confgen.out 2>&1; then
    pcs resource manage kin-zimbra 2>/dev/null || true
    fail "zmproxyconfgen failed — see /tmp/kin-admin-lock-confgen.out"
    exit 1
  fi
  if ! zimbra_cmd zmproxyctl restart >/tmp/kin-admin-lock-proxy.out 2>&1; then
    pcs resource manage kin-zimbra 2>/dev/null || true
    fail "zmproxyctl restart failed — see /tmp/kin-admin-lock-proxy.out"
    exit 1
  fi
  sleep 2
  pcs resource manage kin-zimbra 2>/dev/null || true
fi

say "Verify"
code443=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 https://127.0.0.1/zimbraAdmin/ || true)
code_root=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 https://127.0.0.1/ || true)
code7071=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 https://127.0.0.1:7071/zimbraAdmin/ || true)
info "443 /zimbraAdmin/ → ${code443} (expect 404)"
info "443 / → ${code_root} (expect 200)"
info "7071 /zimbraAdmin/ → ${code7071} (expect 200/302 — local)"

if [ "$code443" != "404" ]; then
  fail "Admin path on :443 still reachable (got ${code443})"
  exit 1
fi
if [ "$code_root" != "200" ]; then
  fail "Webmail broken after lockdown (got ${code_root})"
  exit 1
fi
ok "Public :443 admin paths blocked; webmail OK"
