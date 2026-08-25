#!/usr/bin/env bash
# KIN Mail - toggle password SSH for HA ansible (sshpass), library only.
#
# Sourced by 02-prepare-os.sh and 00-config.sh's helpers. No need_root call,
# no top-level side effects - safe to source from a test harness.
#
# Paths are overridable (KIN_SSHD_CONFIG_D / KIN_SSHD_CONFIG) so tests can
# point this at a scratch directory instead of the real /etc/ssh.

KIN_SSHD_CONFIG_D="${KIN_SSHD_CONFIG_D:-/etc/ssh/sshd_config.d}"
KIN_SSHD_CONFIG="${KIN_SSHD_CONFIG:-/etc/ssh/sshd_config}"
KIN_SSHD_DROPIN="${KIN_SSHD_CONFIG_D}/00-kin-mail.conf"

# Reload sshd if a reload command is available (real systemctl in production,
# stubbable in tests). Warns rather than fails - the drop-in itself is
# already correct even if the reload can't be confirmed.
_kin_reload_sshd() {
  if systemctl reload ssh >/dev/null 2>&1 || systemctl reload sshd >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Turn password/root SSH ON. HA ansible (sshpass) needs this on every node
# it connects to, including the local one during Add-second-server/Build
# HA pair - cloud images default to key-only and have no `kin` user.
enable_ssh_password() {
  install -d -m 755 "$KIN_SSHD_CONFIG_D"
  # OpenSSH uses the first obtained value; Include position varies by image.
  # Write our drop-in AND comment key-only lines in other drop-ins / sshd_config.
  cat > "$KIN_SSHD_DROPIN" <<'EOF'
# KIN Mail - password SSH for HA ansible (sshpass). Cloud-init key-only would
# make Build HA pair fail even when the wizard passwords are correct.
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PermitRootLogin yes
EOF
  chmod 644 "$KIN_SSHD_DROPIN"
  shopt -s nullglob
  local f
  for f in "$KIN_SSHD_CONFIG_D"/*.conf; do
    [ "$(basename "$f")" = "$(basename "$KIN_SSHD_DROPIN")" ] && continue
    sed -i -E \
      -e 's/^([[:space:]]*PasswordAuthentication[[:space:]]+no)/# KIN Mail: \1/' \
      -e 's/^([[:space:]]*KbdInteractiveAuthentication[[:space:]]+no)/# KIN Mail: \1/' \
      -e 's/^([[:space:]]*PermitRootLogin[[:space:]]+(no|prohibit-password|without-password))/# KIN Mail: \1/' \
      "$f"
  done
  if [ -f "$KIN_SSHD_CONFIG" ]; then
    sed -i -E \
      -e 's/^#?[[:space:]]*PasswordAuthentication[[:space:]].*/PasswordAuthentication yes/' \
      -e 's/^#?[[:space:]]*KbdInteractiveAuthentication[[:space:]].*/KbdInteractiveAuthentication yes/' \
      -e 's/^#?[[:space:]]*PermitRootLogin[[:space:]].*/PermitRootLogin yes/' \
      "$KIN_SSHD_CONFIG"
  fi
  if _kin_reload_sshd; then
    ok "sshd allows password login for kin/root (HA ansible)"
  else
    warn "Could not reload sshd - password SSH drop-in is in place; reboot or reload sshd"
  fi
}

# True (0) if any of the given homes have a non-empty authorized_keys.
_kin_has_ssh_key() {
  local home f
  for home in "$@"; do
    f="${home}/.ssh/authorized_keys"
    if [ -s "$f" ]; then
      info "Found authorized_keys: ${f}"
      return 0
    fi
  done
  return 1
}

# Turn password/root SSH back OFF once HA ansible no longer needs it.
# Refuses if no SSH key is on file for the given users, so it can never
# lock the operator out. os_user defaults to KIN_OS_USER / kin.
disable_ssh_password() {
  local os_user="${1:-${KIN_OS_USER:-kin}}"
  # KIN_SSH_KEY_HOMES lets a test point this at scratch directories instead
  # of the real /home/<user> and /root.
  local -a key_homes
  if [ -n "${KIN_SSH_KEY_HOMES:-}" ]; then
    # shellcheck disable=SC2206
    key_homes=(${KIN_SSH_KEY_HOMES})
  else
    key_homes=("/home/${os_user}" "/root")
  fi
  if ! _kin_has_ssh_key "${key_homes[@]}"; then
    fail "No authorized_keys found for ${os_user} or root - refusing to disable password SSH (would lock this host out)."
    return 1
  fi
  install -d -m 755 "$KIN_SSHD_CONFIG_D"
  cat > "$KIN_SSHD_DROPIN" <<'EOF'
# KIN Mail - reverted to key-only once HA ansible no longer needs sshpass.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  chmod 644 "$KIN_SSHD_DROPIN"
  if _kin_reload_sshd; then
    ok "sshd reverted to key-only (SSH key confirmed present first)"
  else
    warn "Could not reload sshd - key-only drop-in is in place; reboot or reload sshd"
  fi
  return 0
}
