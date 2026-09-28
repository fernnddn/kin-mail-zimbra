#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 15 AD SYNC (bring Active Directory's people into Zimbra)
#
#   sudo ./15-ad-sync.sh              create the mailboxes that are missing
#   sudo ./15-ad-sync.sh --dry-run    list what it would create, change nothing
#   sudo ./15-ad-sync.sh --schedule   also install a timer to keep it in step
#
# WHY THIS EXISTS
#
# zimbraAuthMech=ad checks passwords. It does not read the directory's account
# list, so a person who exists in AD has no mailbox until something creates one.
#
# Zimbra's own answer is auto-provisioning, and two of its three modes are not
# usable here: LAZY only creates the mailbox when that person first signs in -
# so an operator comparing AD against the admin console sees most of their
# staff missing (QA, 24 Sep 2026) - and EAGER, the mode that would sweep the
# directory on a timer, has no thread in this FOSS build: mailbox.log says
# "auto provision thread is not running" and the class is absent. MANUAL's
# searchAutoProvDirectory is not in this build's zmprov either.
#
# So the sweep happens here instead, where it can be read, tested and fixed.
#
# WHAT IT WILL NOT DO
#
# It only ever CREATES. It never deletes, disables or renames a mailbox,
# because a person vanishing from an LDAP search - a filter typo, a moved OU, a
# directory that answered slowly - must never be able to destroy their mail.
# Removing people stays a decision someone makes deliberately.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# shellcheck disable=SC1091
. ./lib/quota-gate.sh
# shellcheck source=lib/zimbra-store.sh
. ./lib/zimbra-store.sh
# shellcheck source=lib/ad-ldap.sh
. ./lib/ad-ldap.sh
need_root

DRY_RUN=0
SCHEDULE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --schedule) SCHEDULE=1 ;;
    -h | --help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      fail "Unknown argument: ${arg}"
      exit 2
      ;;
  esac
done

echo
say "Bringing Active Directory's people into Zimbra"

if [ "${AD_AUTH_ENABLED}" != "yes" ]; then
  ok "AD_AUTH_ENABLED=${AD_AUTH_ENABLED:-no} - nothing to sync."
  exit 0
fi
if ! kin_mailboxd_is_local; then
  info "Accounts are created in the directory, which lives with the mail store."
  info "Run this on the mailbox node instead."
  exit 0
fi
for req in AD_LDAP_URL AD_SEARCH_BASE AD_SEARCH_BIND_DN AD_SEARCH_BIND_PASSWORD; do
  eval "val=\${$req}"
  if [ -z "$val" ]; then
    fail "$req is empty - fix ${CONF_FILE}"
    exit 1
  fi
done

[ -x "$KIN_AD_LDAPSEARCH" ] || {
  fail "Zimbra's ldapsearch is missing"
  exit 1
}

# Enabled people only.
#
# userAccountControl bit 2 is "account disabled"; the matching rule below is
# Active Directory's bitwise AND. Without it a sync brings across every
# disabled leaver and spends a contracted seat on each. Guest and krbtgt are
# disabled by default and drop out here for the same reason.
AD_SYNC_FILTER="${AD_SYNC_FILTER:-(&(objectClass=user)(objectCategory=person)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))}"
# Names that are Windows' own, not anybody's mailbox.
AD_SYNC_SKIP="${AD_SYNC_SKIP:-administrator guest krbtgt}"

say "1. Reading the directory"
# The bind password goes in a 0600 file, not in argv where `ps` would show it,
# and an ldaps:// certificate is verified before the password is sent. Both are
# in lib/ad-ldap.sh, shared with 14-ad-trust.sh so the two cannot drift.
# The decision is made HERE, in this shell, before the command substitution
# below. Command substitution runs in a subshell, so a decision made inside it
# would be discarded - and reading KIN_AD_TLS_MODE afterwards under `set -u`
# would then abort the stage on an unbound variable rather than report anything.
kin_ad_tls_decision "$AD_LDAP_URL" verify
RAW=$(kin_ad_ldapsearch "$AD_LDAP_URL" "$AD_SEARCH_BIND_DN" "$AD_SEARCH_BIND_PASSWORD" verify \
  -LLL -b "$AD_SEARCH_BASE" "$AD_SYNC_FILTER" \
  sAMAccountName givenName sn displayName 2>&1)
_rc=$?
case "$KIN_AD_TLS_MODE" in
  pinned | system) ok "$KIN_AD_TLS_NOTE" ;;
  chain | none) [ -z "$KIN_AD_TLS_NOTE" ] || warn "$KIN_AD_TLS_NOTE" ;;
esac
if [ "$_rc" -eq 77 ]; then
  # Nothing was sent. Saying so matters: the operator's next question is
  # whether the service account's password just crossed the network.
  fail "Refused to read the directory - its certificate could not be verified."
  info "$KIN_AD_TLS_NOTE"
  info "No password was sent. Nothing on the directory side was contacted with credentials."
  exit 1
fi
if [ "$_rc" -ne 0 ]; then
  fail "Could not read the directory."
  printf '%s\n' "$RAW" | sed 's/^/    /' | tail -4
  info "Check AD_LDAP_URL, AD_SEARCH_BIND_DN and AD_SEARCH_BIND_PASSWORD."
  exit 1
fi

# LDIF folds long values onto continuation lines beginning with a space.
UNFOLDED=$(printf '%s\n' "$RAW" | sed -e ':a' -e 'N' -e '$!ba' -e 's/\n //g' | tr -d '\r')

# One record per person: name, given name, surname, display name. Entries are
# separated by a blank line, and any of the name fields may be absent - a
# directory is not obliged to be tidy, and a missing surname must not shift the
# other fields along.
PEOPLE=$(printf '%s\n' "$UNFOLDED" | awk -F': ' '
  function flush(  out) {
    if (n != "") { printf "%s\t%s\t%s\t%s\n", n, g, s, d }
    n = ""; g = ""; s = ""; d = ""
  }
  /^$/                 { flush(); next }
  /^sAMAccountName: /  { n = substr($0, 17); next }
  /^givenName: /       { g = substr($0, 12); next }
  /^sn: /              { s = substr($0, 5);  next }
  /^displayName: /     { d = substr($0, 14); next }
  END { flush() }
')
NAMES=$(printf '%s\n' "$PEOPLE" | cut -f1)
TOTAL=$(printf '%s\n' "$NAMES" | grep -c . || true)
if [ "${TOTAL:-0}" -eq 0 ]; then
  warn "The directory returned nobody. Refusing to treat that as 'everyone has left'."
  info "Check AD_SEARCH_BASE and the filter before assuming this is correct."
  exit 1
fi
ok "${TOTAL} enabled people in the directory"

say "2. Comparing against Zimbra"
EXISTING=$(zimbra_cmd zmprov -l gaa "$MAIL_DOMAIN" 2>/dev/null | tr 'A-Z' 'a-z')
CREATED=0
SKIPPED=0
BLOCKED=0
POLICY_SKIPPED=0
POLICY_NAMES=""
MISSING=""

# One seat off the budget established before the loop. Returns 1 when there is
# none left, leaving KIN_QUOTA_MESSAGE as the gate set it so the two refusals -
# "never configured" and "contract full" - stay distinguishable.
SEAT_BUDGET=""
kin_ad_seat_available() {
  case "${KIN_QUOTA_LIMIT:-}" in
    unlimited) return 0 ;;
  esac
  case "$SEAT_BUDGET" in
    '' | *[!0-9-]*)
      # The gate refused before a number was ever established: not configured,
      # or the count itself failed. Either way nothing may be created.
      return 1
      ;;
  esac
  [ "$SEAT_BUDGET" -gt 0 ] || return 1
  SEAT_BUDGET=$((SEAT_BUDGET - 1))
  return 0
}

MISSING_FILE=$(mktemp)
trap 'rm -f "$MISSING_FILE"' EXIT
while IFS=$'\t' read -r name given surname display; do
  [ -n "$name" ] || continue
  lname=$(printf '%s' "$name" | tr 'A-Z' 'a-z')
  case " $AD_SYNC_SKIP " in
    *" $lname "*)
      POLICY_SKIPPED=$((POLICY_SKIPPED + 1))
      POLICY_NAMES="${POLICY_NAMES}${lname} "
      continue
      ;;
  esac
  email="${lname}@${MAIL_DOMAIN}"
  if printf '%s\n' "$EXISTING" | grep -qxF "$email"; then
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  printf '%s\t%s\t%s\t%s\n' "$email" "$given" "$surname" "$display" >>"$MISSING_FILE"
  MISSING="${MISSING}${email} "
done <<EOF
$(printf '%s\n' "$PEOPLE")
EOF

MISSING_N=0
for _ in $MISSING; do MISSING_N=$((MISSING_N + 1)); done
info "${SKIPPED} already have a mailbox; ${MISSING_N} do not"
# The numbers have to add up to the directory total, or the operator is left
# with the question this product already received once: "AD has six people and
# Zimbra shows five - which one is missing, and why?" Accounts dropped by
# policy were skipped silently, so 6 / 5 / 0 was the whole story an operator
# got, and the only way to find the sixth was to read this script.
if [ "$POLICY_SKIPPED" -gt 0 ]; then
  info "${POLICY_SKIPPED} not a person, so no mailbox: ${POLICY_NAMES}"
fi
_accounted=$((SKIPPED + MISSING_N + POLICY_SKIPPED))
if [ "$_accounted" -ne "${TOTAL:-0}" ]; then
  warn "${TOTAL} read from the directory but only ${_accounted} accounted for."
  info "Somebody is being dropped without being counted. Treat the list below"
  info "as incomplete and report this - do not assume the directory is in sync."
fi

if [ "$MISSING_N" -eq 0 ]; then
  echo
  ok "Nothing to create - Zimbra already matches the directory."
  [ "$SCHEDULE" -eq 1 ] || exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  say "3. Would create (dry run - nothing changed)"
  for email in $MISSING; do info "  ${email}"; done
  echo
  ok "Dry run only. Re-run without --dry-run to create them."
  exit 0
fi

if [ "$MISSING_N" -gt 0 ]; then
  say "3. Creating the missing mailboxes"
  # The seat gate, exactly as the console and the CLI apply it: a directory
  # sync must not be a way around a contracted limit.
  #
  # It is asked ONCE and then counted down, rather than re-asked before every
  # account. The gate answers by running `zmprov -l gaa -v`, which dumps every
  # attribute of every mailbox on the domain through a fresh JVM - about three
  # seconds on an idle lab pair, and longer with each mailbox created. Per
  # account that is O(n^2) work: a first sync of a 200-person directory spent
  # most of an hour re-reading accounts it had just made. The arithmetic here
  # is the gate's own, so the ceiling is identical.
  #
  # A mailbox created by some other path DURING a sweep is the one thing this
  # cannot see. The next run re-reads the real count from scratch and stops
  # earlier, so the effect is at worst a refusal delayed by one run - never a
  # higher ceiling.
  kin_quota_gate_allow_new_mailbox "$MAIL_DOMAIN" >/dev/null 2>&1
  SEAT_BUDGET="${KIN_QUOTA_REMAINING:-}"
  if [ -n "${KIN_QUOTA_LIMIT:-}" ] && [ "${KIN_QUOTA_LIMIT}" != unlimited ]; then
    info "${KIN_QUOTA_USED}/${KIN_QUOTA_LIMIT} seats used; ${SEAT_BUDGET} available for this sweep"
  fi
  # Each mailbox is one `zmprov ca` plus one read-back through su - zimbra, so
  # a large first sync is minutes rather than seconds. Said here so a quiet
  # terminal reads as work in progress rather than as a hang.
  if [ "$MISSING_N" -ge 25 ]; then
    info "${MISSING_N} to create - this takes a few seconds each. Leave it running."
  fi
  while IFS=$'\t' read -r email given surname display; do
    [ -n "$email" ] || continue
    if ! kin_ad_seat_available; then
      # Two different situations arrive here and they need different answers.
      # "Not configured" is a step the operator skipped in the wizard; "limit
      # reached" is a contract they have filled. Reporting the first as the
      # second sends them to buy seats they already have.
      case "${KIN_QUOTA_MESSAGE:-}" in
        *PLACEHOLDER_UNSET* | *"not configured"*)
          fail "The contracted seat count is not set, so no mailbox can be created."
          info "Nobody from the directory was brought across. Set the seat count:"
          info "  Mailbox seats in the console, or CONTRACTED_SEATS in ${CONF_FILE}"
          info "Then re-run:  sudo ${KIN_MAIL_INSTALL_DIR}/15-ad-sync.sh"
          ;;
        *)
          warn "Seat limit reached - stopping at ${email}"
          # Recomposed, not echoed. KIN_QUOTA_MESSAGE was written by the single
          # gate call before the loop, when seats were still available, so
          # repeating it here would print "seat available: 3/10" underneath
          # "Seat limit reached" and leave the operator with two answers.
          info "${KIN_QUOTA_LIMIT}/${KIN_QUOTA_LIMIT} seats are in use - contact KIN to add seats"
          info "${CREATED} of ${MISSING_N} were created; the rest are listed above as missing."
          ;;
      esac
      BLOCKED=$((BLOCKED + 1))
      break
    fi
    # A password nobody knows, including us. AD holds the real one; this exists
    # only because Zimbra requires the field. Anyone who could use it would
    # have to read the directory as root first.
    placeholder="KinAdSync-$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
    # The person's name comes across with them. A mailbox that is only an
    # address has nobody's name on it in the address book, in the From line, or
    # anywhere a colleague would look for them.
    attrs=()
    [ -n "$given" ] && attrs+=(givenName "$given")
    [ -n "$surname" ] && attrs+=(sn "$surname")
    [ -n "$display" ] && attrs+=(displayName "$display")
    if [ "${#attrs[@]}" -gt 0 ]; then
      out=$(zimbra_cmd zmprov ca "$email" "$placeholder" "${attrs[@]}" 2>&1)
    else
      out=$(zimbra_cmd zmprov ca "$email" "$placeholder" 2>&1)
    fi
    if printf '%s' "$out" | grep -qiE "ERROR|exception"; then
      warn "could not create ${email}"
      printf '%s\n' "$out" | sed 's/^/      /' | tail -2
    else
      # Checked, not announced.
      if zimbra_cmd zmprov -l ga "$email" >/dev/null 2>&1; then
        ok "created ${email}${display:+ (${display})}"
        CREATED=$((CREATED + 1))
      else
        fail "zmprov ca reported success but ${email} is not in the directory"
      fi
    fi
  done <"$MISSING_FILE"
fi

if [ "$SCHEDULE" -eq 1 ]; then
  say "4. Keeping it in step"
  cat >/etc/systemd/system/kin-ad-sync.service <<EOF
[Unit]
Description=KIN Mail - create Zimbra mailboxes for new Active Directory people
After=network-online.target

[Service]
Type=oneshot
ExecStart=${KIN_MAIL_INSTALL_DIR}/15-ad-sync.sh
EOF
  # systemd's own spelling, straight from the operator. Refused rather than
  # guessed at: a timer that silently fell back to a default nobody asked for
  # would be a sync that quietly runs at the wrong rate forever.
  case "${AD_SYNC_INTERVAL}" in
    *[0-9]s | *[0-9]sec | *[0-9]min | *[0-9]m | *[0-9]h | *[0-9]hour*) ;;
    *)
      fail "AD_SYNC_INTERVAL=${AD_SYNC_INTERVAL} is not a systemd interval (30s, 5min, 1h)"
      exit 2
      ;;
  esac
  # A first run soon after boot, so a machine that was off overnight catches up
  # before anyone notices people missing.
  cat >/etc/systemd/system/kin-ad-sync.timer <<EOF
[Unit]
Description=KIN Mail - Active Directory sync every ${AD_SYNC_INTERVAL}

[Timer]
OnBootSec=2min
OnUnitActiveSec=${AD_SYNC_INTERVAL}
# systemd's default accuracy is one minute, which it spends by firing LATE.
# On an hourly timer that is invisible; on a two-minute one it means a sweep
# the operator was told happens every two minutes can take three, and the
# person watching for their new AD account to appear is timing it.
AccuracySec=10s
Persistent=true

[Install]
WantedBy=timers.target
EOF
  chmod 0644 /etc/systemd/system/kin-ad-sync.service /etc/systemd/system/kin-ad-sync.timer
  systemctl daemon-reload >/dev/null 2>&1 || true
  if systemctl enable --now kin-ad-sync.timer >/dev/null 2>&1; then
    ok "Sync installed: new AD people get a mailbox within ${AD_SYNC_INTERVAL}"
    # Not `systemctl list-timers`. This is a monotonic timer, so its realtime
    # columns are empty and that command prints "n/a  n/a" for NEXT and LEFT -
    # which reads as a dead timer to anyone who did not write it. `status`
    # prints the real thing: "Trigger: <time>; 1min 13s left".
    info "Check it with: systemctl status kin-ad-sync.timer"
    _next=$(systemctl show kin-ad-sync.timer -p NextElapseUSecMonotonic --value 2>/dev/null)
    if [ -n "$_next" ] && [ "$_next" != "0" ]; then
      ok "Next sweep is scheduled"
    else
      # An enabled timer with nothing scheduled runs once and never again,
      # which looks identical to a working one until somebody is not created.
      warn "The timer is enabled but has no next run scheduled."
      info "Run it by hand after adding people: sudo ./15-ad-sync.sh"
    fi
  else
    warn "Could not enable the timer; run this stage by hand when people are added."
  fi
fi

echo
say "DONE"
ok "created ${CREATED}, already present ${SKIPPED}"
# A run that was blocked before creating anybody is not a success, and must not
# be reported as one. It looked like one on 25 Sep 2026: the sync ran, the timer
# was installed, the stage exited 0, and the admin console stayed empty because
# the seat count had never been set. Nothing anywhere said so.
if [ "$BLOCKED" -ne 0 ] && [ "$CREATED" -eq 0 ]; then
  echo
  fail "Nobody was brought across from the directory."
  info "The mailboxes listed above still do not exist, and those people cannot sign in."
  exit 4
fi
[ "$BLOCKED" -eq 0 ] || warn "stopped early on the seat limit - raise CONTRACTED_SEATS and re-run"
info "Removing people is never done here. A mailbox outliving its AD account is"
info "a decision for an operator, not a side effect of a search returning less."
exit 0
