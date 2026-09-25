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

LDAPSEARCH=/opt/zimbra/common/bin/ldapsearch
[ -x "$LDAPSEARCH" ] || { fail "Zimbra's ldapsearch is missing"; exit 1; }

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
RAW=$(LDAPTLS_REQCERT=allow "$LDAPSEARCH" -LLL \
  -H "$AD_LDAP_URL" \
  -D "$AD_SEARCH_BIND_DN" -w "$AD_SEARCH_BIND_PASSWORD" \
  -b "$AD_SEARCH_BASE" "$AD_SYNC_FILTER" sAMAccountName givenName sn displayName 2>&1) || {
  fail "Could not read the directory."
  printf '%s\n' "$RAW" | sed 's/^/    /' | tail -4
  info "Check AD_LDAP_URL, AD_SEARCH_BIND_DN and AD_SEARCH_BIND_PASSWORD."
  exit 1
}

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
MISSING=""

MISSING_FILE=$(mktemp)
trap 'rm -f "$MISSING_FILE"' EXIT
while IFS=$'\t' read -r name given surname display; do
  [ -n "$name" ] || continue
  lname=$(printf '%s' "$name" | tr 'A-Z' 'a-z')
  case " $AD_SYNC_SKIP " in
    *" $lname "*)
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
  while IFS=$'\t' read -r email given surname display; do
    [ -n "$email" ] || continue
    # The seat gate, per account, exactly as the console and the CLI do it. A
    # directory sync must not be a way around a contracted limit - the point of
    # the gate is that it holds no matter who is asking.
    if ! kin_quota_gate_allow_new_mailbox "$MAIL_DOMAIN" >/dev/null 2>&1; then
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
          info "${KIN_QUOTA_MESSAGE:-}"
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
Persistent=true

[Install]
WantedBy=timers.target
EOF
  chmod 0644 /etc/systemd/system/kin-ad-sync.service /etc/systemd/system/kin-ad-sync.timer
  systemctl daemon-reload >/dev/null 2>&1 || true
  if systemctl enable --now kin-ad-sync.timer >/dev/null 2>&1; then
    ok "Sync installed: new AD people get a mailbox within ${AD_SYNC_INTERVAL}"
    info "Check it with: systemctl list-timers kin-ad-sync.timer"
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
