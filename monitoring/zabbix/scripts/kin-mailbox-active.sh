#!/bin/bash
# Active (billable) mailbox count on the Promoted mail node.
# Same definition as install/lib/quota-gate.sh (keep in sync):
#   zmprov -l gaa -v <MAIL_DOMAIN>, zimbraAccountStatus=active,
#   not system / system-resource / admin / external-virtual
#   (spam/ham/virus-quarantine/galsync are system-resource).
# LDAP read-only. Does not run on Unpromoted (no /opt/zimbra mount).
set -euo pipefail

# Unpromoted: ZBX_NOTSUPPORTED so the unsigned item is not fed an empty string.
if ! findmnt -n /opt/zimbra >/dev/null 2>&1; then
  echo ZBX_NOTSUPPORTED
  exit 0
fi
if [[ ! -d /opt/zimbra/store ]]; then
  echo ZBX_NOTSUPPORTED
  exit 0
fi

conf="/etc/kin-mail/config"
if [[ ! -r "$conf" ]]; then
  echo "missing $conf" >&2
  exit 1
fi
domain="$(awk -F= '$1=="MAIL_DOMAIN"{v=$2; gsub(/^[ \t"]+|[ \t"]+$/,"",v); print v; exit}' "$conf")"
if [[ -z "$domain" || ! "$domain" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "MAIL_DOMAIN missing or invalid" >&2
  exit 1
fi

raw="$(timeout 25 su - zimbra -c "zmprov -l gaa -v ${domain}" 2>/dev/null)" || {
  echo "zmprov -l gaa -v failed" >&2
  exit 1
}

printf '%s\n' "$raw" | awk '
  BEGIN { count = 0; acct = ""; status = ""; sys = ""; sres = ""; admin = ""; ext = "" }
  function flush() {
    if (acct == "") return
    if (tolower(sys) == "true") return
    if (tolower(sres) == "true") return
    if (tolower(admin) == "true") return
    if (tolower(ext) == "true") return
    if (status != "active") return
    count++
  }
  /^# name / {
    flush()
    acct = $3
    status = ""; sys = ""; sres = ""; admin = ""; ext = ""
    next
  }
  /^zimbraAccountStatus:/            { status = $2; next }
  /^zimbraIsSystemAccount:/          { sys = $2; next }
  /^zimbraIsSystemResource:/         { sres = $2; next }
  /^zimbraIsAdminAccount:/           { admin = $2; next }
  /^zimbraIsExternalVirtualAccount:/ { ext = $2; next }
  END { flush(); print count }
'
