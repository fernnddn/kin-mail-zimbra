#!/usr/bin/env bash
# enable_ssh_password / disable_ssh_password (install/lib/ssh-password-toggle.sh).
#
# Covers the bug this library exists to fix: HA orchestration connects to
# EVERY node, including the local one, over ansible_password - a 1vm host
# that already reverted to key-only SSH (post single-node install) must be
# able to turn password auth back on before Add-second-server/Build HA
# pair can reach it, and the revert itself must never lock the operator
# out if no SSH key is on file.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# ssh-password-toggle.sh calls these from 00-config.sh; stub them, and stub
# systemctl so the reload attempt is a harmless no-op in this sandbox (a
# real reload needs root + a real sshd, neither available here).
ok()   { :; }
warn() { :; }
fail() { :; }
info() { :; }
systemctl() { return 0; }

# shellcheck source=ssh-password-toggle.sh
. ./ssh-password-toggle.sh

setup_fake_sshd() {
  local dir="$1"
  rm -rf "$dir"
  mkdir -p "$dir/sshd_config.d"
  cat > "$dir/sshd_config.d/50-cloud-init.conf" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  cat > "$dir/sshd_config" <<'EOF'
Port 22
PasswordAuthentication no
Subsystem sftp /usr/lib/openssh/sftp-server
EOF
  export KIN_SSHD_CONFIG_D="$dir/sshd_config.d"
  export KIN_SSHD_CONFIG="$dir/sshd_config"
  export KIN_SSHD_DROPIN="$dir/sshd_config.d/00-kin-mail.conf"
}

tmpdir=$(mktemp -d)

# --- enable_ssh_password -----------------------------------------------------
setup_fake_sshd "$tmpdir/enable"
enable_ssh_password

if [ -f "$KIN_SSHD_DROPIN" ] && grep -q '^PasswordAuthentication yes$' "$KIN_SSHD_DROPIN"; then
  pass "enable_ssh_password writes a yes drop-in"
else
  bad "expected yes drop-in at $KIN_SSHD_DROPIN"
fi

if grep -q '^# KIN Mail: PasswordAuthentication no$' "$KIN_SSHD_CONFIG_D/50-cloud-init.conf"; then
  pass "enable_ssh_password comments out a conflicting no in another drop-in"
else
  bad "50-cloud-init.conf line was not commented"
fi

if grep -q '^PasswordAuthentication yes$' "$KIN_SSHD_CONFIG" \
  && grep -q '^Port 22$' "$KIN_SSHD_CONFIG" \
  && grep -q '^Subsystem sftp' "$KIN_SSHD_CONFIG"; then
  pass "enable_ssh_password flips sshd_config PasswordAuthentication, leaves other lines alone"
else
  bad "sshd_config not patched correctly: $(cat "$KIN_SSHD_CONFIG")"
fi

# --- disable_ssh_password: refuses with no authorized_keys anywhere ---------
setup_fake_sshd "$tmpdir/revert-no-key"
enable_ssh_password >/dev/null  # start "password enabled", like a real 1vm host

empty_home="$tmpdir/revert-no-key/home-empty"
empty_root="$tmpdir/revert-no-key/root-empty"
mkdir -p "$empty_home/.ssh" "$empty_root/.ssh"
export KIN_SSH_KEY_HOMES="$empty_home $empty_root"

if disable_ssh_password; then
  bad "disable_ssh_password should have refused (no authorized_keys anywhere)"
elif grep -q '^PasswordAuthentication yes$' "$KIN_SSHD_DROPIN"; then
  pass "disable_ssh_password refuses and leaves password auth enabled when no SSH key is on file"
else
  bad "drop-in was changed despite the refusal"
fi

# --- disable_ssh_password: succeeds once a key is on file -------------------
setup_fake_sshd "$tmpdir/revert-with-key"
enable_ssh_password >/dev/null

key_home="$tmpdir/revert-with-key/home-kin"
mkdir -p "$key_home/.ssh"
echo "ssh-ed25519 AAAAtest test@example" > "$key_home/.ssh/authorized_keys"
missing_root="$tmpdir/revert-with-key/root-not-present"
export KIN_SSH_KEY_HOMES="$key_home $missing_root"

if disable_ssh_password && grep -q '^PasswordAuthentication no$' "$KIN_SSHD_DROPIN"; then
  pass "disable_ssh_password reverts once an authorized_keys file is found"
else
  bad "expected a successful revert with a real authorized_keys file present"
fi

unset KIN_SSHD_CONFIG_D KIN_SSHD_CONFIG KIN_SSHD_DROPIN KIN_SSH_KEY_HOMES
rm -rf "$tmpdir"

if [ "$fails" -ne 0 ]; then
  printf '%s\n' "FAILED $fails test(s)"
  exit 1
fi
printf '%s\n' "All ssh-password-toggle tests passed"
exit 0
