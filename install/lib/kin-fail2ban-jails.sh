#!/bin/sh
# KIN Mail — write /etc/fail2ban/jail.d/kin-mail.conf and reload fail2ban.
#
# Fast path shared by:
#   install/09-hardening.sh
#   ansible/roles/os_hardening (mail-os-hardening.yml)
#   ocf:kin:zimbra start/stop (must stay well under Pacemaker timeouts)
#
# Spec matches ansible/roles/os_hardening/templates/kin-mail.jail.j2
# (sshd always; zimbra-auth / zpush-auth only when those logs exist, unless
# unmounted mode omits them even while the volume is still mounted).
#
# Usage: kin-fail2ban-jails [auto|mounted|unmounted]
#   auto|mounted|start|promote  — include zimbra/zpush jails iff log files exist
#   unmounted|stop|demote       — sshd only (call before DRBD unmount)
#
# Environment:
#   FAIL2BAN_IGNORE_IP              — use this ignoreip (Ansible / 09-hardening)
#   KIN_FAIL2BAN_JAILS_BEST_EFFORT=1 — never exit non-zero (OCF start/stop)
#
# Does not recompute admin IPs. If FAIL2BAN_IGNORE_IP is unset, reuse ignoreip
# from the existing jail file, else loopback only.

set -u

JAIL_PATH="${KIN_FAIL2BAN_JAIL_PATH:-/etc/fail2ban/jail.d/kin-mail.conf}"
MAILBOX_LOG="${KIN_FAIL2BAN_MAILBOX_LOG:-/opt/zimbra/log/mailbox.log}"
NGINX_LOG="${KIN_FAIL2BAN_NGINX_LOG:-/opt/zimbra/log/nginx.access.log}"
BEST_EFFORT="${KIN_FAIL2BAN_JAILS_BEST_EFFORT:-0}"

finish() {
	_rc=$1
	if [ "$BEST_EFFORT" = "1" ]; then
		return 0
	fi
	return "$_rc"
}

mode="${1:-auto}"
case "$mode" in
auto|mounted|start|promote) include_zimbra=auto ;;
unmounted|stop|demote) include_zimbra=never ;;
-h|--help)
	sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
	exit 0
	;;
*)
	echo "usage: $0 [auto|mounted|unmounted]" >&2
	finish 2
	exit $?
	;;
esac

ignoreip="${FAIL2BAN_IGNORE_IP:-}"
if [ -z "$ignoreip" ] && [ -f "$JAIL_PATH" ]; then
	ignoreip=$(awk -F= '/^ignoreip[[:space:]]*=/ {
		sub(/^[[:space:]]+/, "", $2)
		sub(/[[:space:]]+$/, "", $2)
		print $2
		exit
	}' "$JAIL_PATH" 2>/dev/null || true)
fi
[ -n "$ignoreip" ] || ignoreip="127.0.0.1/8 ::1"

jail_zimbra=""
jail_zpush=""
if [ "$include_zimbra" = "auto" ] && [ -f "$MAILBOX_LOG" ]; then
	jail_zimbra="
[zimbra-auth]
enabled  = true
filter   = zimbra-auth
port     = http,https
logpath  = ${MAILBOX_LOG}
maxretry = 8
findtime = 10m
bantime  = 1h
"
fi
if [ "$include_zimbra" = "auto" ] && [ -f "$NGINX_LOG" ]; then
	jail_zpush="
[zpush-auth]
enabled  = true
filter   = zpush-auth
port     = https
logpath  = ${NGINX_LOG}
maxretry = 10
findtime = 10m
bantime  = 1h
"
fi

tmpdir=$(dirname "$JAIL_PATH")
mkdir -p "$tmpdir" || {
	echo "kin-fail2ban-jails: cannot mkdir $tmpdir" >&2
	finish 1
	exit $?
}

tmp=$(mktemp "${tmpdir}/kin-mail.conf.XXXXXX") || {
	echo "kin-fail2ban-jails: mktemp failed" >&2
	finish 1
	exit $?
}

cat > "$tmp" <<EOF
# KIN Mail Part A — managed by install/lib/kin-fail2ban-jails.sh
[DEFAULT]
ignoreip = ${ignoreip}
bantime  = 1h
findtime = 10m
maxretry = 8
backend  = auto

[sshd]
enabled = true
port    = ssh
mode    = normal
${jail_zimbra}${jail_zpush}
EOF

chmod 0644 "$tmp" || true

if [ -f "$JAIL_PATH" ] && cmp -s "$tmp" "$JAIL_PATH"; then
	rm -f "$tmp"
	echo "unchanged"
else
	mv -f "$tmp" "$JAIL_PATH" || {
		rm -f "$tmp"
		echo "kin-fail2ban-jails: cannot write $JAIL_PATH" >&2
		finish 1
		exit $?
	}
	echo "updated"
fi

reload_fail2ban() {
	_sys() {
		if command -v timeout >/dev/null 2>&1; then
			timeout 15 "$@"
		else
			"$@"
		fi
	}
	if command -v systemctl >/dev/null 2>&1; then
		if systemctl is-active --quiet fail2ban; then
			_sys systemctl reload fail2ban 2>/dev/null && return 0
			_sys systemctl restart fail2ban 2>/dev/null && return 0
			return 1
		fi
		# Recover a failed unit (typical after demote left stale log paths).
		_sys systemctl restart fail2ban 2>/dev/null && return 0
		return 1
	fi
	return 0
}

# Always reload so a previously-failed unit (stale log paths after demote)
# recovers even when the jail file already matches.
if ! reload_fail2ban; then
	echo "kin-fail2ban-jails: fail2ban reload/restart failed" >&2
	finish 1
	exit $?
fi

finish 0
exit $?
