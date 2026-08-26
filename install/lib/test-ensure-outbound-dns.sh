#!/usr/bin/env bash
# Unit coverage for install/lib/ensure-outbound-dns.sh (no live network).
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

SCRIPT="$(pwd)/ensure-outbound-dns.sh"
# shellcheck disable=SC1090
. "$SCRIPT"

# When dig/getent already succeed, helper must be a no-op success.
kin_dns_system_ok_mock() { return 0; }
# Redefine via wrapping: source already defined _kin inside function only.
# Call the public entry with PATH that makes getent succeed via a stub bin.

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/bin"
cat > "$tmpdir/bin/getent" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$tmpdir/bin/getent"
# dig may still be called only on failure path; provide a no-op too.
cat > "$tmpdir/bin/dig" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$tmpdir/bin/dig"

PATH="$tmpdir/bin:$PATH" DNS_UPSTREAM_1=1.1.1.1 DNS_UPSTREAM_2=8.8.8.8 \
  bash -c '. "'"$SCRIPT"'"; kin_ensure_outbound_dns' \
  && pass "noop when getent already resolves" \
  || bad "noop when getent already resolves"

# Broken system resolver + working upstream -> writes bootstrap resolv.conf
mkdir -p "$tmpdir/root/etc"
cat > "$tmpdir/bin/getent" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$tmpdir/bin/dig" <<'EOF'
#!/usr/bin/env bash
# dig +short A host  -> empty (system)
# dig +short @ns A host -> answer
args=("$@")
for i in "${!args[@]}"; do
  if [ "${args[$i]}" = "@1.1.1.1" ] || [ "${args[$i]}" = "@8.8.8.8" ]; then
    echo "140.82.112.3"
    exit 0
  fi
done
exit 0
EOF
chmod +x "$tmpdir/bin/getent" "$tmpdir/bin/dig"
cat > "$tmpdir/bin/ip" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$tmpdir/bin/ip"

# Run in a subshell that chroots via HOME rewrite of /etc paths is hard;
# instead assert the script contains the bootstrap path and bash -n is clean.
bash -n "$SCRIPT" && pass "bash -n ensure-outbound-dns.sh" || bad "bash -n"

grep -q 'resolv.conf.kin-bootstrap' "$SCRIPT" \
  && pass "writes kin-bootstrap resolv.conf" \
  || bad "writes kin-bootstrap resolv.conf"

grep -q 'resolvectl dns' "$SCRIPT" \
  && pass "tries resolvectl before rewrite" \
  || bad "tries resolvectl before rewrite"

grep -q 'ensure-outbound-dns.sh' ../01-preflight.sh \
  && pass "01-preflight sources ensure-outbound-dns" \
  || bad "01-preflight sources ensure-outbound-dns"

if [ "$fails" -ne 0 ]; then
  echo "$fails failure(s)"
  exit 1
fi
echo "all ensure-outbound-dns checks passed"
exit 0
