#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 02 PREPARE OS
# Base preparation of a clean Ubuntu 22.04 / 24.04 host. Idempotent: safe to re-run.
#
#   sudo ./02-prepare-os.sh                    full OS prep (default)
#   sudo ./02-prepare-os.sh --revert-ssh-password
#       Turn password/root SSH back off once HA ansible no longer needs it
#       (see stage 1b below). Refuses if no SSH key is on file for kin/root,
#       so it can never lock the operator out.
#   sudo ./02-prepare-os.sh --ensure-ssh-password
#       Turn password/root SSH back ON. HA orchestration (Add second server,
#       Build HA pair) connects to every node - including this one - over
#       ansible_password, so a 1vm host that already reverted (above) needs
#       this before it can join a pair. Fast: only touches sshd config, does
#       not repeat OS-user creation or package install.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
# shellcheck source=lib/ssh-password-toggle.sh
. ./lib/ssh-password-toggle.sh
need_root

if [ "${1:-}" = "--revert-ssh-password" ]; then
  disable_ssh_password
  exit $?
fi

if [ "${1:-}" = "--ensure-ssh-password" ]; then
  enable_ssh_password
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

# Clear our own leftover apt service-start block (stage 3) if a previous run was
# killed between installing and removing it. Only ever remove OUR file: an
# operator's deliberate policy-rc.d is theirs. This runs before stage 3 on every
# path into stage 4 and beyond, so a stale block can never reach the Zimbra
# install and silently stop its packages from starting anything.
if [ -f /usr/sbin/policy-rc.d ] && grep -q 'KIN Mail install' /usr/sbin/policy-rc.d 2>/dev/null; then
  warn "Removing a leftover apt service-start block from an interrupted run"
  rm -f /usr/sbin/policy-rc.d
fi

echo
say "Preparing ${MAIL_HOST} (${SERVER_IP})"
echo

# --- 1. hostname and clock ---------------------------------------------------
say "1. Hostname and time"
hostnamectl set-hostname "$MAIL_HOST"
timedatectl set-timezone "$TIMEZONE"
timedatectl set-ntp true
ok "hostname : $(hostname -f)"
ok "timezone : ${TIMEZONE}"

# --- 1a. Service account ids must match the primary --------------------------
# Only on a peer, and only before Zimbra is installed. /opt/zimbra arrives over
# DRBD with the primary's numeric ownership on it; if `zimbra` is a different
# uid here, every file on that volume belongs to a stranger and Zimbra cannot
# start on this node at all. See install/lib/align-service-ids.sh.
if kin_ha_peer_install; then
  say "1a. Service account ids"
  # Read by the file sourced on the next line.
  # shellcheck disable=SC2034
  KIN_ALIGN_IDS_SOURCE_ONLY=1
  # shellcheck disable=SC1091
  . ./lib/align-service-ids.sh
  unset KIN_ALIGN_IDS_SOURCE_ONLY
  align_log "Aligning service account ids with the primary"
  if ! align_service_ids_from_env; then
    fail "Could not align this peer's service account ids with the primary."
    info "Zimbra cannot start on a node whose numeric ids disagree with the"
    info "replicated volume, so the build stops here rather than at the end."
    exit 1
  fi
  if align_verify_ids_from_env; then
    ok "Service account ids match the primary"
  else
    fail "Service account ids still disagree with the primary after aligning."
    exit 1
  fi
fi

# --- 1b. OS admin user + password SSH (HA ansible uses sshpass) --------------
# Cloud images default to key-only SSH and have no `kin` user. Build HA pair
# probes kin, then root, then ansible-ssh to every mail node and observability.
OS_USER="${KIN_OS_USER:-kin}"
if ! getent group sudo >/dev/null 2>&1; then
  apt-get -qq update
  apt-get -y install sudo
fi
if [ -n "${KIN_USER_PASS:-}" ] && [ "$OS_USER" != "root" ]; then
  if ! id -u "$OS_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo "$OS_USER" || {
      fail "useradd ${OS_USER} failed"
      exit 1
    }
    ok "Created OS user ${OS_USER}"
  else
    ok "OS user ${OS_USER} already exists"
  fi
  printf '%s:%s\n' "$OS_USER" "$KIN_USER_PASS" | chpasswd || {
    fail "chpasswd ${OS_USER} failed"
    exit 1
  }
  ok "Password set for ${OS_USER}"
fi
if [ -n "${HOST_ROOT_PASS:-}" ]; then
  printf '%s:%s\n' root "$HOST_ROOT_PASS" | chpasswd || {
    fail "chpasswd root failed"
    exit 1
  }
  ok "Password set for root"
fi
# OpenSSH uses the first obtained value; Include position varies by image.
# Write our drop-in AND comment key-only lines in other drop-ins / sshd_config.
enable_ssh_password

# --- 2. /etc/hosts -----------------------------------------------------------
# The FQDN must resolve to the LAN address during Zimbra install. Ubuntu's
# default 127.0.1.1 entry makes the installer bind services to loopback, so
# it has to go. After HA is wired, pacemaker_mail_stack remaps the *shared*
# Zimbra service hostname to 127.0.0.1 on every mail node (nginx/memcached
# co-located with mailboxd). That remap requires Corosync ring0_addr to be
# LAN IPs first - do not point the FQDN at loopback before cluster setup.
say "2. /etc/hosts"
[ -f /etc/hosts.kin-backup ] || cp /etc/hosts /etc/hosts.kin-backup
cat > /etc/hosts <<EOF
127.0.0.1       localhost
${SERVER_IP}    ${MAIL_HOST}  ${MAIL_HOST%%.*}

::1     ip6-localhost ip6-loopback
fe00::0 ip6-localnet
ff00::0 ip6-mcastprefix
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF
ok "FQDN mapped to ${SERVER_IP}, 127.0.1.1 line removed"

# --- 3. remove conflicts, install dependencies -------------------------------
say "3. Packages"
apt-get -y purge postfix exim4-base sendmail apache2 nginx bind9 dovecot-core >/dev/null 2>&1
apt-get -y autoremove >/dev/null 2>&1
apt-get -qq update

# Patch the base image before anything is built on top of it.
#
# Nothing here did that, so an appliance went to a customer carrying whatever
# its image shipped with. On a jammy image built today that was 231 upgradable
# packages, 157 of them from -security, including openssl and openssh-server -
# the two most exposed things on the box. unattended-upgrades does run (see
# 09-hardening.sh) but only from the next daily timer, so the machine spent its
# first day, and its whole acceptance test, unpatched.
#
# `upgrade`, not `dist-upgrade`: it patches in place and will not pull a new
# kernel or remove packages, so it cannot change the boot path underneath a
# deploy that has hours of Zimbra install still to run. Kernel updates keep
# arriving through unattended-upgrades and land on the next reboot, which is
# reported below rather than left for someone to notice.
if [ "${KIN_SKIP_BASE_UPGRADE:-0}" = "1" ]; then
  warn "KIN_SKIP_BASE_UPGRADE=1: base image left unpatched (not for production)"
else
  before=$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst' || true)
  info "Applying ${before:-0} pending package update(s) before installing Zimbra"
  if apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold       upgrade >/dev/null 2>&1; then
    after=$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst' || true)
    ok "Base packages upgraded (${before:-0} pending before, ${after:-0} after)"
  else
    # A failed upgrade must not take the deploy with it: the appliance is still
    # installable, it is just no better patched than the image it came from.
    warn "apt-get upgrade did not complete; continuing with the image as-is."
    warn "Run 'sudo apt-get update && sudo apt-get upgrade' on this host afterwards."
  fi
  if [ -f /var/run/reboot-required ]; then
    warn "A reboot is required to finish applying these updates."
    warn "Do it after the deploy completes, from the Cluster page (maintenance"
    warn "mode on an HA pair), not during install."
  fi
fi

. /etc/os-release
case "${VERSION_ID:-}" in
  24.04) PERL_LIB=libperl5.38 ;;
  *)     PERL_LIB=libperl5.34 ;;
esac
# dnsmasq's postinst starts the daemon the moment it unpacks, and port 53 is
# still held by systemd-resolved at this point - deliberately, so a host is
# never left without a resolver (see stage 4). That start always fails, and it
# printed a red "Job for dnsmasq.service failed" block into the operator's
# deploy log for a condition this script resolves ten lines later. policy-rc.d
# is the supported way to tell a package not to start what it installs; the unit
# is still enabled, and stage 4 starts dnsmasq itself once port 53 is free.
# Scoped to dnsmasq by name, so every other package in the transaction still
# starts its service exactly as it normally would.
kin_block_pkg_service_start() {
  mkdir -p /usr/sbin
  cat > /usr/sbin/policy-rc.d <<'POLICY'
#!/bin/sh
# KIN Mail install: dnsmasq alone must not start as it unpacks - systemd-resolved
# still holds port 53, and stage 4 starts dnsmasq itself once that is free.
# Everything else in this transaction starts normally. Removed before stage 4.
case "$1" in
dnsmasq) exit 101 ;;
esac
exit 0
POLICY
  chmod +x /usr/sbin/policy-rc.d
}
kin_unblock_pkg_service_start() { rm -f /usr/sbin/policy-rc.d; }
# Never leave it behind: a host that keeps this file silently stops starting
# services on every future apt install.
trap 'kin_unblock_pkg_service_start' EXIT HUP INT TERM
kin_block_pkg_service_start

if apt-get -y install \
  netcat-openbsd libidn12 libpcre3 libgmp10 libexpat1 libstdc++6 "$PERL_LIB" \
  unzip pax sysstat sqlite3 lsb-release dnsutils net-tools curl wget \
  dnsmasq tmux swaks tcpdump traceroute python3 parted e2fsprogs cryptsetup
then
  ok "Zimbra dependencies + test tools installed (${PERL_LIB})"
else
  warn "Some Zimbra dependency packages did not install; dnsmasq is still required"
fi

# A missing optional package used to fail the whole apt-get (stderr swallowed),
# so dnsmasq never landed, /etc/dnsmasq.d did not exist, and this stage died
# after systemd-resolved was already off (live Host A, 29 Aug 2026).
if ! dpkg -s dnsmasq >/dev/null 2>&1; then
  apt-get -qq update
  if ! apt-get -y install dnsmasq; then
    fail "dnsmasq package failed to install"
    exit 1
  fi
fi
if ! command -v dnsmasq >/dev/null 2>&1; then
  fail "dnsmasq is not installed"
  exit 1
fi
kin_unblock_pkg_service_start
trap - EXIT HUP INT TERM
ok "dnsmasq package is installed"

# Stage 3 purged packages, patched 60-odd more, and installed a dozen: any of
# those postinsts can allocate a system id, and a purge can in principle take
# an account away. Re-run the aligner - it is a no-op when nothing moved - so
# the numbers this peer carries into the Zimbra install are provably still the
# primary's, rather than whatever survived the package churn.
if kin_ha_peer_install; then
  if ! align_verify_ids_from_env 2>/dev/null; then
    warn "Package installation moved a service account id; realigning"
    align_service_ids_from_env || {
      fail "Could not realign service account ids after installing packages."
      exit 1
    }
  fi
  align_verify_ids_from_env || {
    fail "Service account ids do not match the primary after the package stage."
    exit 1
  }
  ok "Service account ids still match the primary"
fi

# --- 4. local resolver -------------------------------------------------------
# Two traps live here:
#  - /etc/resolv.conf is a managed symlink, so it must be replaced with ln -sf.
#  - zimbra-mta-components installs 'resolvconf', which reclaims the file
#    mid-install and points it back at the systemd-resolved stub. Driving
#    resolvconf's head file is what survives that.
# Install + write conf BEFORE disabling systemd-resolved so a failed start
# does not leave the host with no resolver.
say "4. Local resolver (dnsmasq)"
mkdir -p /etc/dnsmasq.d

cat > /etc/dnsmasq.d/kin-mail.conf <<EOF
# KIN Mail - split-horizon resolver for the mail host
no-resolv
listen-address=127.0.0.1
bind-interfaces
domain-needed
bogus-priv
cache-size=1000

server=${DNS_UPSTREAM_1}
server=${DNS_UPSTREAM_2}
$( [ -n "${INTERNAL_ZONE}" ] && echo "server=/${INTERNAL_ZONE}/${INTERNAL_DNS}" )

# internal view of the mail domain
address=/${MAIL_HOST}/${SERVER_IP}
mx-host=${MAIL_DOMAIN},${MAIL_HOST},10
EOF

if [ -d /etc/resolvconf/resolv.conf.d ]; then
  printf 'nameserver 127.0.0.1\n' > /etc/resolvconf/resolv.conf.d/head
  : > /etc/resolvconf/resolv.conf.d/base
  resolvconf -u 2>/dev/null || true
else
  printf 'nameserver 127.0.0.1\n' > /etc/resolv.conf.kin
  ln -sf /etc/resolv.conf.kin /etc/resolv.conf
fi

# Ubuntu's dnsmasq unit reads every file in /etc/dnsmasq.d except dpkg suffixes.
# A leftover *.bak (illegal repeated keyword) fails the unit while resolv.conf
# still points at 127.0.0.1 - outbound DNS dies silently.
mkdir -p /var/backups/kin-mail/dnsmasq
shopt -s nullglob
for f in /etc/dnsmasq.d/*; do
  base=$(basename "$f")
  case "$base" in
    *.conf|*.dpkg-dist|*.dpkg-old|*.dpkg-new) ;;
    *) mv "$f" /var/backups/kin-mail/dnsmasq/ && info "quarantined stray dnsmasq file: $base" ;;
  esac
done

mkdir -p /etc/systemd/system/dnsmasq.service.d
cat > /etc/systemd/system/dnsmasq.service.d/kin-restart.conf <<'EOF'
[Service]
Restart=on-failure
RestartSec=3
EOF
systemctl daemon-reload

# Port 53: stop the stub resolver only after dnsmasq is installed and configured.
systemctl disable --now systemd-resolved >/dev/null 2>&1 || true

systemctl enable dnsmasq >/dev/null 2>&1
systemctl restart dnsmasq
sleep 2

if [ "$(systemctl is-active dnsmasq)" = "active" ]; then
  ok "dnsmasq active"
else
  journalctl -u dnsmasq -n 40 --no-pager >&2 || true
  warn "Restoring upstream DNS so this stage can be retried"
  printf 'nameserver %s\nnameserver %s\n' \
    "${DNS_UPSTREAM_1:-1.1.1.1}" "${DNS_UPSTREAM_2:-8.8.8.8}" > /etc/resolv.conf.kin-retry
  ln -sf /etc/resolv.conf.kin-retry /etc/resolv.conf
  fail "dnsmasq failed to start"
  exit 1
fi

a=$(dig +short +time=4 A "$MAIL_HOST" 2>/dev/null | head -1)
mx=$(dig +short +time=4 MX "$MAIL_DOMAIN" 2>/dev/null | head -1)
up=$(dig +short +time=4 A google.com 2>/dev/null | head -1)
[ "$a" = "$SERVER_IP" ] && ok "A  ${MAIL_HOST} -> ${a}"     || warn "A  ${MAIL_HOST} -> ${a:-empty}"
[ -n "$mx" ]            && ok "MX ${MAIL_DOMAIN} -> ${mx}"  || warn "MX ${MAIL_DOMAIN} -> empty"
[ -n "$up" ]            && ok "Upstream forwarding working" || warn "Upstream forwarding failed"

# --- 5. optional spare disk at /opt/zimbra (before 03 writes Zimbra) ---------
# Unique blank spare: same GPT as drbd_disk_prep (data + 256 MiB unformatted
# meta), then mkfs.ext4 + fstab UUID + mount. No/ambiguous spare: OS volume.
say "5. Zimbra data disk"
PREPARE_DISK="$(cd "$(dirname "$0")" && pwd)/lib/prepare-zimbra-data-disk.sh"
if [ -f "$PREPARE_DISK" ]; then
  bash "$PREPARE_DISK" || exit 1
else
  info "prepare-zimbra-data-disk.sh missing; Zimbra will install on the OS volume"
fi

# --- 6. verdict --------------------------------------------------------------
echo
say "DONE"
ok "Host ready. Continue to 03-install-zimbra.sh"
info "Host firewall intentionally not enabled - perimeter control is on FortiGate."
info "If ufw is desired, allow port 22 first so you do not get locked out."
echo
