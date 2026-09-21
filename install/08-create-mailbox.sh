#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 08 CREATE MAILBOX (manual provisioning, quota-gated)
#
# The only CLI create path that must run the shared seat gate BEFORE zmprov ca.
# Console self-service must call this script (do not reimplement counting).
#
#   sudo ./08-create-mailbox.sh --status
#   sudo ./08-create-mailbox.sh --list
#   sudo ./08-create-mailbox.sh --delete user@domain
#   sudo ./08-create-mailbox.sh --rename user@domain newlocal
#   sudo ./08-create-mailbox.sh user@domain 'Pass' ['Display Name'] [--given N] [--sn N] [--account-status active|locked]
#
# SCOPE: new mailbox creation is quota-gated. Rename / delete / status / list
# are not create operations and must NOT call the seat gate.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# shellcheck disable=SC1091
. ./lib/quota-gate.sh
need_root

if [ ! -d /opt/zimbra ]; then
  fail "Zimbra is not installed yet - run 03-install-zimbra.sh first"
  exit 1
fi

usage() {
  cat <<EOF
Usage:
  sudo ./08-create-mailbox.sh --status
  sudo ./08-create-mailbox.sh --list
  sudo ./08-create-mailbox.sh --delete <email>
  sudo ./08-create-mailbox.sh --rename <email> <new-local-part>
  sudo ./08-create-mailbox.sh <email> <password> [displayName] [--given NAME] [--sn NAME] [--account-status active|locked]
  sudo ./08-create-mailbox.sh          # interactive prompts

Creates one mailbox on ${MAIL_DOMAIN:-<MAIL_DOMAIN>} only after the shared
quota gate allows it (NEW CREATE only, not for password or quota changes).
EOF
}

_seats_code() {
  local msg="${KIN_QUOTA_MESSAGE:-}"
  case "$msg" in
    *PLACEHOLDER_UNSET*) echo unset ;;
    *misconfigured*) echo invalid ;;
    *failed\ to\ count*) echo count_failed ;;
    *seat\ limit\ reached*) echo at_limit ;;
    *seat\ available*) echo ok ;;
    *) echo other ;;
  esac
}

emit_seats_json() {
  local rc="${1:-1}"
  local used="${KIN_QUOTA_USED:-}"
  local limit="${KIN_QUOTA_LIMIT:-}"
  local remaining="${KIN_QUOTA_REMAINING:-}"
  local code
  code="$(_seats_code)"
  python3 -c '
import json, sys
rc, used, limit, remaining, domain, message, code = sys.argv[1:8]
def maybe_int(v):
    return int(v) if v.isdigit() or (v.startswith("-") and v[1:].isdigit()) else None
print("SEATS_JSON:" + json.dumps({
    "ok": rc == "0",
    "used": maybe_int(used),
    "limit": maybe_int(limit),
    "remaining": maybe_int(remaining),
    "domain": domain,
    "message": message,
    "code": code,
}, ensure_ascii=True))
' "$rc" "$used" "$limit" "$remaining" "${MAIL_DOMAIN}" "${KIN_QUOTA_MESSAGE:-}" "$code"
}

show_status() {
  echo
  say "Seat / quota status (${MAIL_DOMAIN})"
  local rc=0
  kin_quota_status "$MAIL_DOMAIN" || rc=$?
  emit_seats_json "$rc"
  return "$rc"
}

require_same_domain() {
  local email="$1"
  local local_part domain_part
  case "$email" in
    *@*) ;;
    *)
      fail "Email must be user@${MAIL_DOMAIN}"
      return 2
      ;;
  esac
  local_part="${email%@*}"
  domain_part="${email#*@}"
  if [ "$domain_part" != "$MAIL_DOMAIN" ]; then
    fail "Refusing action outside configured domain (${email} vs ${MAIL_DOMAIN})"
    return 2
  fi
  if [ -z "$local_part" ]; then
    fail "Email local part is empty"
    return 2
  fi
  return 0
}

list_mailboxes() {
  local raw quota tmp_raw tmp_quota
  say "Listing mailboxes on ${MAIL_DOMAIN}"
  if ! raw=$(zimbra_cmd zmprov -l gaa -v "$MAIL_DOMAIN" 2>/dev/null); then
    fail "Could not list mailboxes"
    emit_seats_json 1
    return 1
  fi
  # getQuotaUsage takes a SERVER, and this asked it for a DOMAIN. Zimbra
  # resolves the argument as a hostname, so it answered "No address associated
  # with hostname <domain>" and returned nothing - the Storage column has been
  # empty for every mailbox since it was written, and an empty answer renders
  # as "no cap set" rather than as a failed call.
  #
  # It also needs SOAP, which is why it goes through the mail store: zmprov on
  # an edge has no local mailboxd and refuses this one outright.
  quota=$(zmprov_on_store getQuotaUsage "$(mailbox_server | head -1)" 2>/dev/null || true)
  tmp_raw=$(mktemp)
  tmp_quota=$(mktemp)
  printf '%s\n' "$raw" > "$tmp_raw"
  printf '%s\n' "$quota" > "$tmp_quota"
  python3 - "$tmp_raw" "$tmp_quota" <<'PY'
import json, sys

raw_path, quota_path = sys.argv[1], sys.argv[2]
text = open(raw_path, encoding="utf-8", errors="replace").read()
quota = {}
for line in open(quota_path, encoding="utf-8", errors="replace"):
    parts = line.split()
    # "<account> <limit> <used>", proven on a live store by setting a 1 GiB cap
    # and watching which column moved. This was read the other way round for its
    # whole life - the cap reported as bytes in use and vice versa - and had no
    # symptom only because the call above never returned a row to misread.
    if len(parts) >= 2 and "@" in parts[0]:
        try:
            limit = int(parts[1])
        except ValueError:
            limit = None
        used = None
        if len(parts) >= 3:
            try:
                used = int(parts[2])
            except ValueError:
                used = None
        quota[parts[0].lower()] = {"used_bytes": used, "quota_bytes": limit}

rows = []
acct = {}

def flush():
    global acct
    if not acct.get("email"):
        acct = {}
        return
    if str(acct.get("sys", "")).lower() == "true":
        acct = {}
        return
    if str(acct.get("sres", "")).lower() == "true":
        acct = {}
        return
    if str(acct.get("admin", "")).lower() == "true":
        acct = {}
        return
    if str(acct.get("ext", "")).lower() == "true":
        acct = {}
        return
    email = acct["email"]
    q = quota.get(email.lower(), {})
    try:
        cap = int(acct["quota"])
    except (KeyError, TypeError, ValueError):
        cap = q.get("quota_bytes")
    rows.append({
        "email": email,
        "display_name": acct.get("display") or "",
        "given_name": acct.get("given") or "",
        "surname": acct.get("sn") or "",
        "status": acct.get("status") or "",
        "used_bytes": q.get("used_bytes"),
        # The cap is an LDAP attribute and already in the listing above, so it
        # survives a mail store that cannot be reached; the SOAP figure is only
        # a fallback. Zimbra writes 0 for "no cap", which is not the same as
        # unknown - a deployment whose default COS is 10 GB must not read as
        # uncapped just because the store did not answer.
        "quota_bytes": cap,
    })
    acct = {}

for line in text.splitlines():
    if line.startswith("# name "):
        flush()
        parts = line.split()
        acct = {"email": parts[2] if len(parts) >= 3 else ""}
        continue
    if not acct:
        continue
    if line.startswith("displayName:"):
        acct["display"] = line.split(":", 1)[1].strip()
    elif line.startswith("givenName:"):
        acct["given"] = line.split(":", 1)[1].strip()
    elif line.startswith("sn:"):
        acct["sn"] = line.split(":", 1)[1].strip()
    elif line.startswith("zimbraAccountStatus:"):
        acct["status"] = line.split(":", 1)[1].strip()
    elif line.startswith("zimbraMailQuota:"):
        acct["quota"] = line.split(":", 1)[1].strip()
    elif line.startswith("zimbraIsSystemAccount:"):
        acct["sys"] = line.split(":", 1)[1].strip()
    elif line.startswith("zimbraIsSystemResource:"):
        acct["sres"] = line.split(":", 1)[1].strip()
    elif line.startswith("zimbraIsAdminAccount:"):
        acct["admin"] = line.split(":", 1)[1].strip()
    elif line.startswith("zimbraIsExternalVirtualAccount:"):
        acct["ext"] = line.split(":", 1)[1].strip()
flush()
print("MAILBOX_JSON:" + json.dumps(rows, ensure_ascii=True))
PY
  rm -f "$tmp_raw" "$tmp_quota"
  local rc=0
  kin_quota_gate_allow_new_mailbox "$MAIL_DOMAIN" || rc=$?
  emit_seats_json "$rc"
  ok "Listed ${MAIL_DOMAIN}"
  return 0
}

# Which machine runs mailboxd.
#
# zmprov on a node without a local mailboxd falls back to LDAP-only mode
# WITHOUT saying so. In that mode the destructive operations refuse to act
# unattended: `da` and `ra` print "Continue? [Y]es, [N]o", read end-of-file,
# print "aborted" - and exit 0. Nothing here runs on a terminal, so that is
# every console-driven delete and rename on a multi deployment.
#
# The console checked the exit status, saw 0, and told the operator the mailbox
# was deleted. It was still there: still receiving mail, still holding a seat,
# and for a departed employee still reachable. Rename reported the new address
# while the old one kept working. Measured on the live pair, 21 Sep 2026.
mailbox_server() {
  if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
    kin_mailbox_host
    return 0
  fi
  zimbra_cmd zmhostname 2>/dev/null | tr -d '\r'
}

# zmprov, aimed at the machine that can actually carry the request out. -s
# names the SOAP target, which is what keeps it out of the LDAP-only path.
zmprov_on_store() {
  local host
  host=$(mailbox_server | head -1)
  if [ -n "$host" ]; then
    zimbra_cmd zmprov -s "$host" "$@"
  else
    # No idea which node holds the store: better to try than to refuse, and
    # the result is checked by the caller either way.
    zimbra_cmd zmprov "$@"
  fi
}

account_exists() {
  zimbra_cmd zmprov -l ga "$1" >/dev/null 2>&1
}

delete_mailbox() {
  local email="$1"
  require_same_domain "$email" || return $?
  if ! account_exists "$email"; then
    fail "Account not found: ${email}"
    return 1
  fi
  say "Deleting mailbox ${email}"
  local out
  if ! out=$(zmprov_on_store da "$email" 2>&1); then
    fail "zmprov da failed"
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    return 1
  fi
  # Checked, not announced. A delete that quietly did nothing and a delete that
  # worked are the same exit status, and the difference is a mailbox that still
  # takes mail for someone who is supposed to be gone.
  if account_exists "$email"; then
    fail "The directory still lists ${email} after the delete."
    info "Nothing was removed. Do not treat this mailbox as deleted."
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    return 1
  fi
  ok "Deleted ${email}"
  echo "DELETE_JSON:{\"email\":\"${email}\"}"
  return 0
}

rename_mailbox() {
  local email="$1" new_local="$2"
  require_same_domain "$email" || return $?
  if [ -z "$new_local" ] || [[ "$new_local" == *@* ]]; then
    fail "new local part must be the mailbox name only"
    return 2
  fi
  local new_email="${new_local}@${MAIL_DOMAIN}"
  if ! account_exists "$email"; then
    fail "Account not found: ${email}"
    return 1
  fi
  if account_exists "$new_email"; then
    fail "Account already exists: ${new_email}"
    return 1
  fi
  say "Renaming ${email} to ${new_email}"
  local out
  if ! out=$(zmprov_on_store ra "$email" "$new_email" 2>&1); then
    fail "zmprov ra failed"
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    return 1
  fi
  # Both halves, because a rename that half happened is worse than one that did
  # not: the console said the address changed, and the old one still delivers.
  if ! account_exists "$new_email"; then
    fail "The directory does not list ${new_email} after the rename."
    info "Nothing changed. ${email} is still the address."
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    return 1
  fi
  if account_exists "$email"; then
    fail "The directory still lists the old address ${email}."
    info "Mail sent to it will still be delivered. Do not treat it as renamed."
    return 1
  fi
  ok "Renamed to ${new_email}"
  echo "RENAME_JSON:{\"email\":\"${new_email}\",\"previous\":\"${email}\"}"
  return 0
}

# Create path: gate FIRST, then zmprov ca. Never ca before the gate returns 0.
create_mailbox_gated() {
  local email="$1" pass="$2" dname="${3:-}"
  local given="${4:-}" sn="${5:-}" account_status="${6:-active}"
  local out

  require_same_domain "$email" || return $?
  if [ -z "$pass" ]; then
    fail "Email and password are required"
    return 2
  fi
  if [ ${#pass} -lt 8 ]; then
    fail "Password must be at least 8 characters"
    return 2
  fi
  case "$account_status" in
    active|locked) ;;
    *)
      fail "account status must be active or locked"
      return 2
      ;;
  esac

  echo
  say "Quota gate (new mailbox create only)"
  if ! kin_quota_gate_allow_new_mailbox "$MAIL_DOMAIN"; then
    fail "${KIN_QUOTA_MESSAGE}"
    info "No zmprov ca was run."
    emit_seats_json 1
    return 1
  fi
  ok "${KIN_QUOTA_MESSAGE}"

  if zimbra_cmd zmprov -l ga "$email" >/dev/null 2>&1; then
    fail "Account already exists: ${email}"
    return 1
  fi

  say "Creating mailbox ${email}"
  local -a attrs=()
  [ -n "$dname" ] && attrs+=(displayName "$dname")
  [ -n "$given" ] && attrs+=(givenName "$given")
  [ -n "$sn" ] && attrs+=(sn "$sn")
  [ "$account_status" != "active" ] && attrs+=(zimbraAccountStatus "$account_status")
  if [ "${#attrs[@]}" -gt 0 ]; then
    if ! out=$(zimbra_cmd zmprov ca "$email" "$pass" "${attrs[@]}" 2>&1); then
      fail "zmprov ca failed"
      printf '%s\n' "$out" | sed 's/^/    /' >&2
      return 1
    fi
  else
    if ! out=$(zimbra_cmd zmprov ca "$email" "$pass" 2>&1); then
      fail "zmprov ca failed"
      printf '%s\n' "$out" | sed 's/^/    /' >&2
      return 1
    fi
  fi

  # Checked, not announced - the same reason delete and rename now verify.
  # `ca` does work in LDAP-only mode today, unlike `da` and `ra`, which is the
  # only reason this path was not silently broken too. That is a property of
  # zmprov, not a guarantee, so confirm rather than assume.
  if ! account_exists "$email"; then
    fail "zmprov ca reported success but the directory does not list ${email}."
    info "No mailbox was created. Nothing here should be treated as done."
    return 1
  fi

  # zmprov ca already succeeded - the account exists and a seat is consumed
  # regardless of what happens below.
  #
  # On a split the operator creates accounts from the edge, because the edge is
  # the only machine they ever log into. Logging in needs mailboxd, which is on
  # the mailbox. So the probe below cannot pass here - and without this branch
  # every account created on a healthy split would be reported as failed, after
  # six seconds of retries, with a seat already spent and a retry that refuses
  # as already-existing. The operator would conclude the product is broken on
  # the one operation they perform most.
  if ! kin_auth_provable_here; then
    ok "Created: ${email}"
    info "Login not proved from this node: mailboxd is on the mailbox, and this"
    info "is the edge. The account and its password are in the directory."
    kin_quota_status "$MAIL_DOMAIN" >/dev/null || true
    info "Seats now: ${KIN_QUOTA_USED}/${KIN_QUOTA_LIMIT}"
    emit_seats_json 0
    echo "CREATE_JSON:{\"email\":\"${email}\",\"auth_probe_skipped\":true}"
    return 0
  fi

  # mailboxd can take a moment to see a brand-new LDAP account, so a single
  # immediate auth probe can false-negative on a perfectly good account; retry
  # briefly before giving up.
  local auth_attempt auth_ok=0
  for auth_attempt in 1 2 3; do
    if zimbra_user_auth_ok "$email" "$pass"; then
      auth_ok=1
      break
    fi
    [ "$auth_attempt" -lt 3 ] && sleep 2
  done
  if [ "$auth_ok" -ne 1 ]; then
    fail "Account created but auth probe failed for ${email} (${auth_attempt} attempts)"
    info "The account already exists and a seat is already consumed - do not"
    info "retry create, it will refuse as already-existing. Investigate"
    info "mailboxd/LDAP, or verify by hand: zmmailbox -m ${email} -p '<password>' gaf"
    kin_quota_status "$MAIL_DOMAIN" >/dev/null || true
    info "Seats now: ${KIN_QUOTA_USED}/${KIN_QUOTA_LIMIT}"
    emit_seats_json 1
    echo "CREATE_JSON:{\"email\":\"${email}\",\"auth_probe_failed\":true}"
    return 1
  fi
  ok "Created and authenticated: ${email}"
  kin_quota_status "$MAIL_DOMAIN" >/dev/null || true
  info "Seats now: ${KIN_QUOTA_USED}/${KIN_QUOTA_LIMIT}"
  emit_seats_json 0
  echo "CREATE_JSON:{\"email\":\"${email}\"}"
  return 0
}

GIVEN=""
SN=""
ACCOUNT_STATUS="active"

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --status)
      show_status
      exit $?
      ;;
    --list)
      list_mailboxes
      exit $?
      ;;
    --delete)
      shift
      delete_mailbox "${1:-}"
      exit $?
      ;;
    --rename)
      shift
      rename_mailbox "${1:-}" "${2:-}"
      exit $?
      ;;
    --given)
      GIVEN="${2:-}"
      shift 2
      ;;
    --sn)
      SN="${2:-}"
      shift 2
      ;;
    --account-status)
      ACCOUNT_STATUS="${2:-}"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      fail "Unknown option: $1"
      usage
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [ "$#" -ge 2 ]; then
  EMAIL_ARG="$1"
  PASS_ARG="$2"
  shift 2
  DNAME_ARG=""
  if [ "$#" -gt 0 ] && [[ "$1" != --* ]]; then
    DNAME_ARG="$1"
    shift
  fi
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --given)
        GIVEN="${2:-}"
        shift 2
        ;;
      --sn)
        SN="${2:-}"
        shift 2
        ;;
      --account-status)
        ACCOUNT_STATUS="${2:-}"
        shift 2
        ;;
      *)
        fail "Unknown option: $1"
        usage
        exit 2
        ;;
    esac
  done
  create_mailbox_gated "$EMAIL_ARG" "$PASS_ARG" "$DNAME_ARG" "$GIVEN" "$SN" "$ACCOUNT_STATUS"
  exit $?
fi

if [ "$#" -eq 1 ]; then
  usage
  exit 2
fi

# Interactive
echo
say "Create mailbox (quota-gated)"
info "Domain: ${MAIL_DOMAIN}"
info "This path calls kin_quota_gate_allow_new_mailbox before zmprov ca."
show_status || {
  warn "Create is blocked until seats are available / CONTRACTED_SEATS is set."
  exit 1
}

ask EMAIL "Full email address" ""
while :; do
  ask_secret PASS "Password (min 8 characters)"
  [ ${#PASS} -ge 8 ] && break
  warn "Too short."
done
ask DNAME "Display name (optional)" ""

create_mailbox_gated "$EMAIL" "$PASS" "$DNAME" "$GIVEN" "$SN" "$ACCOUNT_STATUS"
exit $?
