#!/usr/bin/env bash
# The one recovery the console cannot perform on itself.
#
# Changing a password is a console operation and a console operation needs a
# session, so an operator who has lost the only Super Admin password has no way
# back in through the browser. The first-boot file is not a fallback: it is
# deleted after the first successful login, on purpose.
#
# These are the guards that stop this script becoming a second, worse way to
# manage accounts.
set -u
cd "$(dirname "$0")" || exit 1
fails=0
pass() { printf 'ok  %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

S="$(pwd)/reset-console-password.sh"
[ -f "$S" ] || { printf 'FAIL reset-console-password.sh not found\n'; exit 1; }

bash -n "$S" && pass "parses" || bad "syntax error"
[ -x "$S" ] && pass "is executable" || bad "not executable"

# --- refusals ----------------------------------------------------------------
OUT=$(bash "$S" 2>&1); RC=$?
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -qi "needs root"; then
  pass "refuses to run without root"
else
  bad "ran as an ordinary user (rc=$RC): $OUT"
fi

OUT=$(KIN_CONSOLE_ROOT=/nonexistent bash "$S" 2>&1); RC=$?
if [ "$RC" -ne 0 ]; then
  pass "refuses when there is no console install to act on"
else
  bad "claimed success with no console installed"
fi

OUT=$(bash "$S" admin --nonsense 2>&1); RC=$?
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q "unknown option"; then
  pass "refuses an option it does not know"
else
  bad "accepted an unknown option (rc=$RC)"
fi

# --- it must use the console's own code --------------------------------------
# Hand-editing users.json is how that file and the legacy admin.hash drift
# apart, and the login then fails for a reason nobody can see.
if grep -q "from kin_console import users as U" "$S" \
  && grep -q "U.apply_set_local_password" "$S" \
  && grep -q "U.save_users" "$S" \
  && grep -q "U.finalize_password_side_effects" "$S"; then
  pass "writes through the console's own users module"
else
  bad "hand-rolls the account file instead of using the product's code"
fi
if grep -qE "json\.dump|\.write_text\(.*users" "$S"; then
  bad "writes users.json directly"
else
  pass "never writes the account file itself"
fi

# --- it must not spend the artefact it just created --------------------------
# The first successful login deletes the password file. Verifying by logging in
# would therefore destroy the password before anyone read it - which is exactly
# what happened the first time this was tried (20 Sep 2026).
if grep -q "U.auth.verify_password" "$S"; then
  pass "checks the new password against the stored hash"
else
  bad "does not verify the password it wrote"
fi
if grep -qE "curl .*(/api/login|9443)" "$S"; then
  bad "verifies by logging in, which deletes the password file"
else
  pass "does not verify by logging in"
fi
if grep -q "deletes the file" "$S"; then
  pass "warns the operator not to test it with a shell login"
else
  bad "no warning that a test login consumes the password"
fi

# --- scope -------------------------------------------------------------------
# An AD-backed account has no local password. Setting one would create a second
# credential for an identity the directory owns.
if grep -q "AUTH_LOCAL" "$S" && grep -qi "authenticates against AD" "$S"; then
  pass "refuses an AD-backed account"
else
  bad "would set a local password on a directory-backed account"
fi
# It resets. It does not become a second way to manage accounts.
for verb in apply_create_user apply_delete_user validate_role; do
  if grep -q "$verb" "$S"; then
    bad "does more than reset a password ($verb)"
  else
    pass "does not $verb"
  fi
done

# --- sessions ----------------------------------------------------------------
# A tab already open keeps working against a session minted from the old hash,
# which is the opposite of what someone resetting a password wants.
if grep -q "systemctl restart" "$S"; then
  pass "ends existing sessions by restarting the console"
else
  bad "leaves sessions from the old password alive"
fi

# --- the file it leaves ------------------------------------------------------
if grep -q "chmod 600 /etc/kin-mail-console/initial-admin-password" "$S" \
  && grep -q "chown root:root /etc/kin-mail-console/initial-admin-password" "$S"; then
  pass "leaves the password root-only, where bootstrap leaves the first one"
else
  bad "the password file is not root-only"
fi

if [ "$fails" -eq 0 ]; then
  printf 'All reset-console-password tests passed\n'
  exit 0
fi
printf '%s test(s) failed\n' "$fails"
exit 1
