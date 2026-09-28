# shellcheck shell=bash
# =============================================================================
# KIN Mail - reading Active Directory without giving the password away
#
# Sourced by 14-ad-trust.sh and 15-ad-sync.sh. Two things every LDAP read in
# this product has to get right, and both were got wrong independently:
#
# 1. THE PASSWORD MUST NOT TRAVEL IN ARGV.
#    ldapsearch -w puts it in /proc/<pid>/cmdline, which is world-readable on
#    Linux: any local account - the console service user, a customer admin with
#    a shell - can read the directory service account's password out of `ps`
#    for as long as the search runs. The sync runs on a timer, so that window
#    reopened every AD_SYNC_INTERVAL, forever. -y reads it from a 0600 file.
#
# 2. AN ldaps:// BIND MUST VERIFY THE CERTIFICATE.
#    14-ad-trust.sh exists to fetch the directory's CA and prove it signs what
#    the server presents. The sync then passed LDAPTLS_REQCERT=allow, which
#    means "continue even if the certificate is wrong" - discarding that proof
#    and handing the service account's password to whatever host answers the
#    address. Only the CA fetch itself may skip verification, because at that
#    moment there is nothing to verify against yet.
#
# WHY THIS IS NOT SIMPLY REQCERT=require
#
# OpenLDAP checks the hostname as well as the chain once REQCERT is require,
# and operators here point AD_LDAP_URL at an address, not a name: the deploy on
# 26 Sep 2026 used ldaps://<ip>:636 and a domain controller's certificate names
# its FQDN. Turning on full verification would therefore have broken a working
# directory link in the name of securing it.
#
# So the chain is verified separately, with openssl, which checks the chain and
# not the name. If the peer presents a certificate signed by the CA this
# appliance pinned, the bind proceeds even when the name does not match - the
# peer holds a private key for a certificate the customer's own CA issued,
# which is a different and far smaller risk than "anything that answers". If
# the chain does NOT verify, the read is refused rather than sent anyway.
# =============================================================================

# Where 14-ad-trust.sh imports the directory's CA. Named once so both steps
# agree; a second copy of this path is how the two halves drift apart.
: "${KIN_AD_CA_PEM:=/opt/zimbra/conf/kin-ad-ca.cer}"
# Zimbra ships its own OpenLDAP client. Overridable only so the tests can stub it.
: "${KIN_AD_LDAPSEARCH:=/opt/zimbra/common/bin/ldapsearch}"
: "${KIN_AD_OPENSSL:=openssl}"

# Set at source time, not only inside kin_ad_tls_decision. Every caller here
# runs under `set -u`, and the decision is frequently made inside a command
# substitution - so a caller reading these afterwards would otherwise expand an
# unbound variable and take the whole stage down with it.
: "${KIN_AD_TLS_MODE:=unknown}"
: "${KIN_AD_TLS_NOTE:=}"
: "${KIN_AD_TLS_FOR:=}"

# Host and port out of an LDAP URL. Echoes "host port".
kin_ad_host_port() {
  local url="$1" rest hp host port
  rest=${url#*://}
  hp=${rest%%/*}
  case "$url" in
    ldaps://* | LDAPS://*) port=636 ;;
    *) port=389 ;;
  esac
  # An IPv6 literal is bracketed, and splitting on ":" would cut it to pieces.
  case "$hp" in
    \[*\]*)
      host=${hp%%\]*}
      host=${host#\[}
      case "$hp" in *\]:*) port=${hp##*\]:} ;; esac
      ;;
    *:*)
      host=${hp%%:*}
      port=${hp##*:}
      ;;
    *) host=$hp ;;
  esac
  printf '%s %s\n' "$host" "$port"
}

kin_ad_url_is_ldaps() {
  case "$1" in ldaps://* | LDAPS://*) return 0 ;; *) return 1 ;; esac
}

# Does the peer present a certificate this CA file signs?
#
# openssl s_client verifies the chain and does not check the hostname unless it
# is asked to, which is exactly the question being asked here. Echoes nothing;
# the exit status is the answer.
kin_ad_chain_verifies() {
  local host="$1" port="$2" cafile="$3"
  [ -r "$cafile" ] || return 1
  # </dev/null so s_client does not sit waiting for something to send.
  printf '' | timeout 15 "$KIN_AD_OPENSSL" s_client \
    -connect "${host}:${port}" -CAfile "$cafile" 2>/dev/null |
    grep -q "Verify return code: 0"
}

# Decide how this read should treat the certificate.
#
#   kin_ad_tls_decision <url> <mode>
#
# mode is "verify" for ordinary reads, or "bootstrap" for the one read in
# 14-ad-trust.sh that fetches the CA before any trust exists.
#
# Sets KIN_AD_TLS_MODE to one of:
#   none      not ldaps:// - there is no certificate in play
#   bootstrap deliberately unverified, CA fetch only
#   pinned    verify against the CA this appliance imported
#   system    verify against the system trust store
#   chain     the pinned CA signs the peer, but the name does not match
#   refuse    nothing verifies it; the caller must not send the password
# and KIN_AD_TLS_NOTE to a sentence an operator can act on.
kin_ad_tls_decision() {
  local url="$1" mode="${2:-verify}" host port
  # Deciding costs up to two TLS handshakes. The sync calls this in the parent
  # shell (so the answer survives the command substitution that follows) and
  # kin_ad_ldapsearch calls it again from inside that substitution; without
  # this, a two-minute timer would open four connections per sweep instead of
  # two, to say the same thing twice.
  if [ "$KIN_AD_TLS_FOR" = "${url}|${mode}" ] && [ "$KIN_AD_TLS_MODE" != unknown ]; then
    return 0
  fi
  KIN_AD_TLS_FOR="${url}|${mode}"
  KIN_AD_TLS_MODE=none
  KIN_AD_TLS_NOTE=""

  if ! kin_ad_url_is_ldaps "$url"; then
    if [ "${url#ldap://}" != "$url" ] || [ "${url#LDAP://}" != "$url" ]; then
      KIN_AD_TLS_NOTE="This directory link is ldap://, so the service account's password crosses the network unencrypted. Use ldaps:// if the directory offers it."
    fi
    return 0
  fi

  if [ "$mode" = bootstrap ]; then
    KIN_AD_TLS_MODE=bootstrap
    KIN_AD_TLS_NOTE="Reading the CA before it can be trusted; what comes back is proved afterwards."
    return 0
  fi

  read -r host port <<EOF
$(kin_ad_host_port "$url")
EOF

  if [ -r "$KIN_AD_CA_PEM" ]; then
    if kin_ad_chain_verifies "$host" "$port" "$KIN_AD_CA_PEM"; then
      # The chain is good. Prefer full verification, and fall back to
      # chain-only if the name is the part that does not match.
      if kin_ad_name_matches "$host" "$port" "$KIN_AD_CA_PEM"; then
        KIN_AD_TLS_MODE=pinned
        KIN_AD_TLS_NOTE="Verified against the directory CA this appliance imported."
      else
        KIN_AD_TLS_MODE=chain
        KIN_AD_TLS_NOTE="The directory's certificate is signed by the CA this appliance imported, but it does not name ${host}. Point AD_LDAP_URL at the name on the certificate to get full verification."
      fi
      return 0
    fi
    KIN_AD_TLS_MODE=refuse
    KIN_AD_TLS_NOTE="The certificate ${host}:${port} presents is not signed by the CA at ${KIN_AD_CA_PEM}. Re-run 14-ad-trust.sh, or set AD_CA_FILE to the CA the directory really uses."
    return 0
  fi

  # No pinned CA. The system store is all there is; a public certificate
  # verifies against it and a private one does not.
  if kin_ad_chain_verifies "$host" "$port" /etc/ssl/certs/ca-certificates.crt; then
    KIN_AD_TLS_MODE=system
    KIN_AD_TLS_NOTE="Verified against the system trust store."
    return 0
  fi
  KIN_AD_TLS_MODE=refuse
  KIN_AD_TLS_NOTE="Nothing on this machine can verify the certificate ${host}:${port} presents. Run 14-ad-trust.sh to import the directory's CA, or set AD_CA_FILE to it."
}

# Does the certificate actually name this host?
#
# Separate from the chain check so the two failures can be told apart: a bad
# chain is a refusal, a bad name is a warning. -verify_return_error makes
# s_client exit non-zero rather than only printing, and -verify_hostname is
# what turns the name check on.
kin_ad_name_matches() {
  local host="$1" port="$2" cafile="$3"
  printf '' | timeout 15 "$KIN_AD_OPENSSL" s_client \
    -connect "${host}:${port}" -CAfile "$cafile" \
    -verify_hostname "$host" -verify_return_error >/dev/null 2>&1
}

# Run a read against Active Directory.
#
#   kin_ad_ldapsearch <url> <bind-dn> <password> <mode> [ldapsearch args...]
#
# Exit status is ldapsearch's, except 77 when the certificate could not be
# verified - in which case nothing was sent. Output is ldapsearch's output.
# The password never reaches argv and is never printed.
kin_ad_ldapsearch() {
  local url="$1" binddn="$2" password="$3" mode="${4:-verify}"
  shift 4

  if [ ! -x "$KIN_AD_LDAPSEARCH" ]; then
    printf 'kin_ad_ldapsearch: %s is not executable\n' "$KIN_AD_LDAPSEARCH" >&2
    return 127
  fi

  kin_ad_tls_decision "$url" "$mode"
  local tls=()
  case "$KIN_AD_TLS_MODE" in
    none) ;;
    bootstrap) tls=(LDAPTLS_REQCERT=never) ;;
    pinned) tls=(LDAPTLS_REQCERT=require LDAPTLS_CACERT="$KIN_AD_CA_PEM") ;;
    system) tls=(LDAPTLS_REQCERT=require) ;;
    # Chain proved, name did not. "allow" skips OpenLDAP's name check; the
    # chain was verified a moment ago by openssl, which is why this is a
    # warning and not a hole.
    chain) tls=(LDAPTLS_REQCERT=allow LDAPTLS_CACERT="$KIN_AD_CA_PEM") ;;
    refuse)
      printf '%s\n' "$KIN_AD_TLS_NOTE" >&2
      return 77
      ;;
  esac

  # On tmpfs where possible: the bind file holds the directory service
  # account's password, and a search interrupted between writing it and
  # removing it would otherwise leave that password in /tmp until something
  # swept it - on disk, in any filesystem backup taken meanwhile. /run is
  # memory-backed and gone at reboot, and 0700 keeps it out of everyone else's
  # reach even while it is there.
  #
  # A DIRECTORY OF OUR OWN, and never /run/kin-mail.
  #
  # The first version of this used /run/kin-mail and chmod 0700 on it. That
  # directory belongs to the privhelper: it holds privhelper.sock and the
  # daemon sets it root:kin-console 0750 precisely so the console, which runs
  # as kin-console, can traverse it and reach the socket. Taking the group bit
  # off cut the console off from its own helper, and every privileged action -
  # Deploy, the mail gateway, all of it - failed with a permission error
  # (reported from live QA, 28 Sep 2026). Nothing here has any business
  # touching a path another component owns.
  local pwdir="${TMPDIR:-/tmp}" rundir=/run/kin-ad-bind pwfile rc
  if [ -d /run ] && mkdir -p "$rundir" 2>/dev/null &&
    chmod 0700 "$rundir" 2>/dev/null; then
    pwdir="$rundir"
    # An earlier run that was killed mid-search. Best effort, never fatal.
    find "$rundir" -maxdepth 1 -name 'kin-ad-bind.*' -mmin +10 -delete 2>/dev/null || true
  fi
  pwfile=$(mktemp "${pwdir}/kin-ad-bind.XXXXXX") || return 1
  chmod 0600 "$pwfile" 2>/dev/null || {
    rm -f "$pwfile"
    return 1
  }
  # No trailing newline: ldapsearch -y uses the file's bytes verbatim, so a
  # newline would become part of the password and every bind would fail.
  printf '%s' "$password" >"$pwfile"

  env ${tls[@]+"${tls[@]}"} "$KIN_AD_LDAPSEARCH" \
    -H "$url" -D "$binddn" -y "$pwfile" "$@"
  rc=$?
  rm -f "$pwfile"
  return "$rc"
}
