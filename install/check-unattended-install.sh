#!/usr/bin/env bash
# =============================================================================
# KIN Mail - can this Zimbra build be installed without a terminal?
#
# The split topology cannot drive Zimbra's interactive installer: each role has
# a different menu tree and the joining node has an LDAP-join menu that does not
# exist on a single-node install. It needs the unattended path instead -
# install.sh -s to lay down the software, then zmsetup.pl -c <file> to configure
# it from a file we generate.
#
# That path is documented by Zimbra but this product installs a community
# rebuild, so "documented" is not the same as "true here". This checks, against
# the exact tarball a deployment would use.
#
# It installs NOTHING. The answer lives inside the packages, so the packages are
# unpacked into a temporary directory and read. Safe to run on a workstation, on
# a live appliance, or on a throwaway VM; needs no root.
#
#   ./check-unattended-install.sh                     use $ZCS_SRC from the config
#   ./check-unattended-install.sh /path/to/zcs-dir    use an extracted tarball
#
# Exit 0 = the unattended path is usable. Exit 1 = it is not, and the reason is
# printed. Run this before trusting any multi-server work to it.
# =============================================================================
set -u

# Colours and say/ok/warn/fail, without dragging in the wizard: sourcing
# 00-config.sh would demand a config file this check does not need.
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

PROBLEMS=0
problem() { fail "$*"; PROBLEMS=$((PROBLEMS + 1)); }

# --- locate an extracted tarball ---------------------------------------------
ZDIR="${1:-}"
if [ -z "$ZDIR" ]; then
  # Read ZCS_SRC/ZCS_FILE without running the wizard.
  CONF="${KIN_MAIL_CONFIG:-/etc/kin-mail/config}"
  if [ -r "$CONF" ]; then
    _src=$(sed -n 's/^[[:space:]]*ZCS_SRC=//p' "$CONF" | tail -1 | tr -d "\"' \r")
    _file=$(sed -n 's/^[[:space:]]*ZCS_FILE=//p' "$CONF" | tail -1 | tr -d "\"' \r")
    if [ -n "$_src" ] && [ -n "$_file" ] && [ -d "${_src}/${_file%.tgz}" ]; then
      ZDIR="${_src}/${_file%.tgz}"
    fi
  fi
fi
if [ -z "$ZDIR" ]; then
  for d in /opt/zcs-src/zcs-*; do
    [ -d "$d" ] && { ZDIR="$d"; break; }
  done
fi

if [ -z "$ZDIR" ] || [ ! -d "$ZDIR" ]; then
  fail "No extracted Zimbra tarball found."
  info "Pass one:  $0 /opt/zcs-src/zcs-10.1.x_GA_....UBUNTU22_64.2026..."
  info "Stage 03 extracts it there; ./03-install-zimbra.sh --manual stops after extracting."
  exit 1
fi

say "Checking ${ZDIR}"
echo

# --- 1. install.sh must run from a file instead of asking questions -----------
#
# This is the contract the whole split install rests on, and it is one file, not
# two steps: install.sh takes a defaults file as a positional argument, sources
# it, skips the package prompts because the answers are already there, installs
# what INSTALL_PACKAGES names, and then hands the SAME file to zmsetup.pl -c.
# A different INSTALL_PACKAGES per role is what makes one machine a mailbox and
# the other an edge.
say "1. install.sh runs from a defaults file"
INSTALL_SH="${ZDIR}/install.sh"
if [ ! -r "$INSTALL_SH" ]; then
  problem "install.sh not found in ${ZDIR}"
else
  if grep -qE 'DEFAULTFILE=' "$INSTALL_SH"; then
    ok "install.sh accepts a defaults file"
  else
    problem "install.sh has no defaults-file argument"
    info "Without it every install stops at the package menu."
  fi

  # AUTOINSTALL is what suppresses the interactive package selection. Without
  # it the file is read and then the installer asks anyway.
  if grep -qE 'AUTOINSTALL="yes"' "$INSTALL_SH" &&
     grep -qE 'if \[ \$AUTOINSTALL = "no" \]' "$INSTALL_SH"; then
    ok "a defaults file switches the installer to non-interactive"
  else
    problem "install.sh does not skip the package prompts for a defaults file"
  fi

  # The handoff. If install.sh does not pass the file on, the machine ends up
  # with packages and no configuration, which is worse than not starting.
  if grep -qE 'zmsetup\.pl -c \$DEFAULTFILE' "$INSTALL_SH"; then
    ok "install.sh hands the same file to zmsetup.pl -c"
    grep -nE 'zmsetup\.pl' "$INSTALL_SH" | sed 's/^/        /'
  else
    problem "install.sh does not pass the defaults file to zmsetup.pl"
  fi

  if grep -qE '^[[:space:]]*-s\)|SOFTWAREONLY' "$INSTALL_SH"; then
    ok "install.sh also has -s (software only), for a two-step install"
  else
    warn "no -s / SOFTWAREONLY; only the single-pass defaults-file path is available"
  fi

  if grep -q 'platform-override' "$INSTALL_SH"; then
    ok "install.sh accepts --platform-override (already used by stage 03)"
  else
    warn "no --platform-override in install.sh; stage 03 passes it today"
  fi
fi
echo

# --- 1b. the per-role package variable ----------------------------------------
# INSTALL_PACKAGES lives in install.sh's own shell, not in zmsetup.pl's config
# hash, so it has to be looked for here rather than among the zmsetup keys.
say "1b. Per-role package selection"
if grep -qE '^INSTALL_PACKAGES=' "${ZDIR}/util/globals.sh" 2>/dev/null ||
   grep -qE 'INSTALL_PACKAGES=' "${ZDIR}/util/utilfunc.sh" 2>/dev/null; then
  ok "INSTALL_PACKAGES is the variable a defaults file sets per role"
else
  problem "no INSTALL_PACKAGES in util/ - cannot choose a package set per role"
fi
echo

# --- 2. zmsetup.pl must exist and take a config file --------------------------
say "2. zmsetup.pl config-file mode"
CORE_DEB=$(find "${ZDIR}/packages" -maxdepth 1 -name 'zimbra-core*.deb' 2>/dev/null | sort | tail -1)
if [ -z "$CORE_DEB" ]; then
  problem "no zimbra-core*.deb under ${ZDIR}/packages"
  info "Cannot read zmsetup.pl without it."
else
  ok "core package: $(basename "$CORE_DEB")"
  if ! command -v dpkg-deb >/dev/null 2>&1; then
    problem "dpkg-deb is not installed; cannot unpack the core package to read zmsetup.pl"
  else
    TMPD=$(mktemp -d "${TMPDIR:-/tmp}/kin-unattended-check.XXXXXX")
    # Everything below reads only from here, and it is removed on every exit.
    trap 'rm -rf "$TMPD"' EXIT

    if ! dpkg-deb -x "$CORE_DEB" "$TMPD" 2>/dev/null; then
      problem "dpkg-deb could not unpack $(basename "$CORE_DEB")"
    else
      ZMSETUP=$(find "$TMPD" -name 'zmsetup.pl' -type f 2>/dev/null | head -1)
      if [ -z "$ZMSETUP" ]; then
        problem "zmsetup.pl is not in the core package"
        info "The unattended path has nothing to drive."
      else
        ok "zmsetup.pl found at ${ZMSETUP#"$TMPD"}"

        # -c <file> is the whole point. Zimbra parses it with GetOptions; a
        # rebuild that dropped it would leave us with no unattended path.
        if grep -qE "GetOptions.*\bc=s|'c=s'|\"c=s\"|opt_c|\-c[[:space:]]+<config" "$ZMSETUP"; then
          ok "zmsetup.pl parses -c <config file>"
          grep -nE "GetOptions|c=s" "$ZMSETUP" | head -5 | sed 's/^/        /'
        else
          problem "zmsetup.pl has no -c <config file> option"
          info "Without it the configuration cannot be supplied from a file."
        fi

        # The keys the generated file has to use. This is the actual deliverable
        # of this check: guessing them is how a config is accepted and ignored.
        say "   config keys zmsetup.pl reads"
        KEYS=$(grep -oE '\$config\{[A-Za-z_][A-Za-z0-9_]*\}' "$ZMSETUP" 2>/dev/null |
               sed 's/\$config{//; s/}//' | sort -u)
        KEYCOUNT=$(printf '%s\n' "$KEYS" | grep -c . || true)
        if [ "${KEYCOUNT:-0}" -lt 10 ]; then
          warn "only ${KEYCOUNT:-0} config keys found - the parsing may differ in this build"
        else
          ok "${KEYCOUNT} config keys"
        fi

        # Named individually because each one is load-bearing for the split and
        # a missing one changes the design rather than just the file.
        # INSTALL_PACKAGES is deliberately NOT here: it belongs to install.sh's
        # shell, checked in 1b, and looking for it among these keys is what made
        # an earlier revision of this script call a working build unusable.
        for want in HOSTNAME LDAPHOST LDAPPORT; do
          if printf '%s\n' "$KEYS" | grep -qx "$want"; then
            ok "  ${want}"
          else
            problem "  ${want} is not a key this zmsetup.pl reads"
          fi
        done

        # The secrets the edge needs to join the mailbox's directory. This build
        # spells some of them in lower case (ldap_nginx_password) and some in
        # upper (LDAPADMINPASS), so accept either rather than reporting a
        # working build as broken over a naming convention.
        for want in LDAPADMINPASS LDAPROOTPASS LDAPREPPASS LDAPPOSTPASS LDAPAMAVISPASS LDAPNGINXPASS; do
          lower=$(printf '%s' "$want" | sed 's/^LDAP/ldap_/; s/PASS$/_password/' | tr '[:upper:]' '[:lower:]')
          if printf '%s\n' "$KEYS" | grep -qx "$want"; then
            ok "  ${want}  (edge joins LDAP with this)"
          elif printf '%s\n' "$KEYS" | grep -qix "$lower"; then
            ok "  ${lower}  (edge joins LDAP with this)"
          else
            problem "  no key for ${want} in either spelling"
          fi
        done

        info "Full key list written to: ${PWD}/zmsetup-config-keys.txt"
        printf '%s\n' "$KEYS" > "${PWD}/zmsetup-config-keys.txt" 2>/dev/null ||
          warn "could not write the key list to $PWD"
      fi
    fi
  fi
fi
echo

# --- 3. the packages each role needs must be present --------------------------
say "3. Per-role package sets"
have_pkg() {
  find "${ZDIR}/packages" -maxdepth 1 -name "$1*.deb" 2>/dev/null | grep -q .
}
check_role() {
  local role="$1"; shift
  local missing="" p
  for p in "$@"; do
    have_pkg "$p" || missing="${missing} ${p}"
  done
  if [ -z "$missing" ]; then
    ok "${role}: $*"
  else
    problem "${role} is missing:${missing}"
  fi
}
check_role "mailbox" zimbra-core zimbra-ldap zimbra-store zimbra-apache zimbra-spell
check_role "edge   " zimbra-core zimbra-mta zimbra-proxy

# zimbra-memcached is deliberately not in the two lists above. It is not shipped
# in the tarball - it comes from Zimbra's apt repository, which is why the
# interactive installer asks about it separately and why stage 03 pre-seeds the
# packaging key. Treating its absence here as a missing package reported a
# perfectly good build as unusable.
if have_pkg zimbra-memcached; then
  ok "memcached: in the tarball"
else
  ok "memcached: not in the tarball, comes from the apt repository (expected)"
fi
echo

# --- verdict ------------------------------------------------------------------
if [ "$PROBLEMS" -eq 0 ]; then
  say "USABLE"
  ok "This build can be installed unattended; the split topology can rely on it."
  info "Generate the per-role config files from the key list above."
  exit 0
fi

say "NOT USABLE"
fail "${PROBLEMS} problem(s) above."
info "The split topology cannot use zmsetup.pl -c on this build."
info "Do not build the multi-server install on this path until they are resolved."
exit 1
