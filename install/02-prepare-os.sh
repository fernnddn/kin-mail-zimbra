#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 02 PREPARE OS
# Base preparation of a clean Ubuntu 22.04 / 24.04 host. Idempotent: safe to re-run.
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

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

# --- 1b. OS admin user + password SSH (HA ansible uses sshpass) --------------
# Cloud images default to key-only SSH and have no `kin` user. Build HA pair
# probes kin, then root, then ansible-ssh to every mail node and observability.
OS_USER="${KIN_OS_USER:-kin}"
if [ -n "${KIN_USER_PASS:-}" ] && [ "$OS_USER" != "root" ]; then
  if ! id -u "$OS_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo "$OS_USER"
    ok "Created OS user ${OS_USER}"
  else
    ok "OS user ${OS_USER} already exists"
  fi
  echo "${OS_USER}:${KIN_USER_PASS}" | chpasswd
  ok "Password set for ${OS_USER}"
fi
if [ -n "${HOST_ROOT_PASS:-}" ]; then
  echo "root:${HOST_ROOT_PASS}" | chpasswd
  ok "Password set for root"
fi
install -d -m 755 /etc/ssh/sshd_config.d
# Must sort before 50-cloud-init.conf: sshd uses the first obtained value.
cat > /etc/ssh/sshd_config.d/00-kin-mail.conf <<'EOF'
# KIN Mail - password SSH for HA ansible (sshpass). Cloud-init key-only would
# make Build HA pair fail even when the wizard passwords are correct.
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PermitRootLogin yes
EOF
chmod 644 /etc/ssh/sshd_config.d/00-kin-mail.conf
if systemctl reload ssh >/dev/null 2>&1 || systemctl reload sshd >/dev/null 2>&1; then
  ok "sshd allows password login for kin/root (HA ansible)"
else
  warn "Could not reload sshd - password SSH drop-in is in place; reboot or reload sshd"
fi

# --- 2. /etc/hosts -----------------------------------------------------------
# The FQDN must resolve to the LAN address during Zimbra install. Ubuntu's
# default 127.0.1.1 entry makes the installer bind services to loopback, so
# it has to go. After HA is wired, pacemaker_mail_stack remaps the *shared*
# Zimbra service hostname to 127.0.0.1 on every mail node (nginx/memcached
# co-located with mailboxd). That remap requires Corosync ring0_addr to be
# LAN IPs first — do not point the FQDN at loopback before cluster setup.
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
. /etc/os-release
case "${VERSION_ID:-}" in
  24.04) PERL_LIB=libperl5.38 ;;
  *)     PERL_LIB=libperl5.34 ;;
esac
apt-get -y install \
  netcat-openbsd libidn12 libpcre3 libgmp10 libexpat1 libstdc++6 "$PERL_LIB" \
  unzip pax sysstat sqlite3 lsb-release dnsutils net-tools curl wget \
  dnsmasq tmux swaks tcpdump traceroute python3 parted e2fsprogs cryptsetup >/dev/null 2>&1
ok "Zimbra dependencies + test tools installed (${PERL_LIB})"

# --- 4. local resolver -------------------------------------------------------
# Two traps live here:
#  - /etc/resolv.conf is a managed symlink, so it must be replaced with ln -sf.
#  - zimbra-mta-components installs 'resolvconf', which reclaims the file
#    mid-install and points it back at the systemd-resolved stub. Driving
#    resolvconf's head file is what survives that.
say "4. Local resolver (dnsmasq)"
systemctl disable --now systemd-resolved >/dev/null 2>&1 || true

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
# still points at 127.0.0.1 — outbound DNS dies silently.
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

systemctl enable dnsmasq >/dev/null 2>&1
systemctl restart dnsmasq
sleep 2

if [ "$(systemctl is-active dnsmasq)" = "active" ]; then
  ok "dnsmasq active"
else
  fail "dnsmasq failed to start"; exit 1
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
info "Host firewall intentionally not enabled — perimeter control is on FortiGate."
info "If ufw is desired, allow port 22 first so you do not get locked out."
echo
