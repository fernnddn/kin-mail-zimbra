#!/usr/bin/env bash
# =============================================================================
# KIN Mail - reset a console password from the machine itself
#
#   sudo /opt/kin-mail-console/deploy/reset-console-password.sh
#   sudo ./reset-console-password.sh admin
#   sudo ./reset-console-password.sh admin --print
#
# For the one case the console cannot fix from inside itself: nobody can log
# in. Changing a password is a console operation, and a console operation needs
# a session, so an operator who has lost the only Super Admin password has no
# way back in through the browser.
#
# The first-boot password file is not a fallback. It is deleted after the first
# successful login on purpose - leaving a plaintext Super Admin password on
# disk for the life of the appliance is a worse trade than this script.
#
# WHAT IT DOES NOT DO
#
#   create accounts            - that is Users in the console
#   change a role              - likewise
#   touch an AD-backed account - there is no local password to set; it refuses
#   print the old password     - there isn't one. bcrypt is one-way.
#
# It uses the console's own users module, so the account file, the legacy
# admin.hash and the first-boot cleanup all stay consistent with what the
# console itself would have written. Hand-editing users.json is how the two
# drift apart and the login starts failing for a reason nobody can see.
# =============================================================================
set -u

OPT_ROOT="${KIN_CONSOLE_ROOT:-/opt/kin-mail-console}"
SVC="${KIN_CONSOLE_SERVICE:-kin-mail-console}"
USERNAME="${1:-}"
PRINT=0
case "${2:-}" in
  --print) PRINT=1 ;;
  "") ;;
  *) printf 'unknown option: %s\n' "$2" >&2; exit 2 ;;
esac
case "$USERNAME" in
  --print) USERNAME=""; PRINT=1 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  printf 'This needs root: the account file is owned by the console service user.\n' >&2
  exit 1
fi
if [ ! -x "${OPT_ROOT}/venv/bin/python" ]; then
  printf 'No console install at %s\n' "$OPT_ROOT" >&2
  exit 1
fi

# Generated, not asked for. A password typed at a prompt on a machine the
# operator is already logged into ends up in shell history, in a screenshot, or
# reused from somewhere else; a random one is shown once and then changed in
# the browser if they want something memorable.
NEWPASS=$(KIN_PRINT="$PRINT" KIN_USERNAME="$USERNAME" \
  PYTHONPATH="${OPT_ROOT}/backend" "${OPT_ROOT}/venv/bin/python" - <<'PY'
import os
import secrets
import sys

from kin_console import users as U
from kin_console.settings import settings

wanted = (os.environ.get("KIN_USERNAME") or "").strip()
current = U.load_users()
if not current:
    print("no console accounts exist; run console/bootstrap.sh", file=sys.stderr)
    raise SystemExit(3)

if not wanted:
    # The account this appliance was built with, and in practice the only one.
    wanted = (settings.console_user or "admin").strip() or "admin"

target = next((u for u in current if u.username == wanted), None)
if target is None:
    have = ", ".join(u.username for u in current)
    print(f"no such console account: {wanted} (have: {have})", file=sys.stderr)
    raise SystemExit(3)
if target.auth_type != U.AUTH_LOCAL:
    print(
        f"{wanted} authenticates against AD; there is no local password to reset. "
        "Fix it in the directory, or create a local account with the console.",
        file=sys.stderr,
    )
    raise SystemExit(3)

password = secrets.token_urlsafe(18)
updated, new_list = U.apply_set_local_password(current, wanted, password=password)
U.save_users(new_list)
U.finalize_password_side_effects(wanted, updated.password_hash)

# Prove it against the hash that was just written, not by logging in.
#
# A login is the obvious way to check and it is the wrong one: the first
# successful login deletes the file this password is about to be left in, so
# testing it here would consume the very thing the operator has not read yet.
# Found by doing exactly that (20 Sep 2026).
reloaded = next(u for u in U.load_users() if u.username == wanted)
if not U.auth.verify_password(password, reloaded.password_hash):
    print("the new password does not verify against what was saved", file=sys.stderr)
    raise SystemExit(4)

print(password)
PY
) || exit $?

# Restart so a session minted against the old hash cannot outlive it. Without
# this a browser tab already open keeps working, which is the opposite of what
# someone resetting a password wants.
systemctl restart "$SVC" >/dev/null 2>&1 || true

printf '\n'
printf 'Console password reset for %s.\n' "${USERNAME:-admin}"
printf '\n'
if [ "$PRINT" -eq 1 ]; then
  printf '  %s\n' "$NEWPASS"
  printf '\n'
  printf 'Shown once, and it is in this terminal scrollback now. Sign in and\n'
  printf 'change it under Security.\n'
else
  # Same place bootstrap.sh leaves the first-boot password, with the same
  # ownership and mode: root-only, and cleared by the privhelper after the
  # next successful login.
  install -d -m 0755 -o root -g root /etc/kin-mail-console
  umask 077
  printf '%s\n' "$NEWPASS" > /etc/kin-mail-console/initial-admin-password
  chown root:root /etc/kin-mail-console/initial-admin-password
  chmod 600 /etc/kin-mail-console/initial-admin-password
  printf 'Read it once with:\n'
  printf '\n'
  printf '  sudo cat /etc/kin-mail-console/initial-admin-password\n'
  printf '\n'
  printf 'It is deleted automatically after the next successful login, so it\n'
  printf 'does not sit on disk. Add --print to show it here instead.\n'
  printf '\n'
  printf 'Do not test it by signing in from a shell first - that login is what\n'
  printf 'deletes the file, and the password would be gone before anyone read\n'
  printf 'it. It has already been checked against the stored hash.\n'
fi
printf '\n'
printf 'Every existing session was ended by the restart.\n'
