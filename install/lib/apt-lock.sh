#!/usr/bin/env bash
# KIN Mail - make apt survive a freshly booted Ubuntu.
#
# A cloud image boots, and within seconds systemd starts apt-daily.timer and
# unattended-upgrades. Both take /var/lib/dpkg/lock-frontend and hold it for
# minutes. Any apt-get that runs in that window does not wait; it prints
#
#   E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by
#      process 6811 (unattended-upgr)
#
# and exits immediately. An installer that treats that as a real failure stops
# a perfectly good deployment thirty seconds after it started, on a machine
# where the same commands work by hand two minutes later. That is exactly what
# happened on the first Phase 9 attempt, and the base-image upgrade added in
# 0.1.3 made the window wide enough to hit almost every time.
#
# Three things fix it, in order of politeness:
#
#   1. Ask the background jobs to stand down for the duration of the install,
#      through systemd, so they finish the transaction they are in and stop
#      cleanly. They are put back exactly as they were afterwards.
#   2. Wait for whatever still holds the lock, saying who it is and for how
#      long, instead of failing silently.
#   3. Pass DPkg::Lock::Timeout to every apt-get so even a race that slips
#      through the first two waits instead of dying.
#
# What this deliberately does NOT do is kill dpkg. A SIGKILL mid-transaction
# leaves the package database half-written, and the next apt-get refuses to do
# anything until somebody runs `dpkg --configure -a` by hand. Stopping the
# service is the version of "kill it" that is safe; killing the process is not.

KIN_APT_LOCK_WAIT="${KIN_APT_LOCK_WAIT:-900}"
KIN_DPKG_LOCK_FILE="${KIN_DPKG_LOCK_FILE:-/var/lib/dpkg/lock-frontend}"

# Units that take the lock on their own schedule. Timers first: stopping a
# service while its timer is live just means it starts again a minute later.
KIN_APT_BACKGROUND_UNITS="apt-daily.timer apt-daily-upgrade.timer \
apt-daily.service apt-daily-upgrade.service unattended-upgrades.service"

_kin_apt_paused=""

apt_log() { printf '%s\n' "$*"; }

# Who holds the dpkg frontend lock right now? Prints "<pid> (<comm>)" or nothing.
apt_lock_holder() {
  local pid comm
  if command -v fuser >/dev/null 2>&1; then
    for pid in $(fuser "$KIN_DPKG_LOCK_FILE" 2>/dev/null); do
      case "$pid" in '' | *[!0-9]*) continue ;; esac
      comm=$(cat "/proc/${pid}/comm" 2>/dev/null || echo "?")
      printf '%s (%s)' "$pid" "$comm"
      return 0
    done
    return 1
  fi
  # No psmisc: read /proc directly rather than assume the lock is free.
  local fd target
  for pid in /proc/[0-9]*; do
    for fd in "$pid"/fd/*; do
      [ -e "$fd" ] || continue
      target=$(readlink "$fd" 2>/dev/null) || continue
      [ "$target" = "$KIN_DPKG_LOCK_FILE" ] || continue
      comm=$(cat "${pid}/comm" 2>/dev/null || echo "?")
      printf '%s (%s)' "${pid##*/}" "$comm"
      return 0
    done
  done
  return 1
}

# Stop the background updaters for the length of the install, remembering which
# were actually active so they can be put back exactly as found.
apt_pause_background_upgrades() {
  local unit
  _kin_apt_paused=""
  for unit in $KIN_APT_BACKGROUND_UNITS; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
      _kin_apt_paused="${_kin_apt_paused}${unit} "
    fi
  done
  [ -n "$_kin_apt_paused" ] || return 0
  apt_log "Pausing background package updates for the install: ${_kin_apt_paused}"
  # `stop` on unattended-upgrades lets it finish the package it is unpacking
  # and exit; it is built to handle that. This is the safe form of "kill it".
  # shellcheck disable=SC2086
  systemctl stop $_kin_apt_paused >/dev/null 2>&1 || true
}

apt_resume_background_upgrades() {
  [ -n "$_kin_apt_paused" ] || return 0
  # shellcheck disable=SC2086
  systemctl start $_kin_apt_paused >/dev/null 2>&1 || true
  apt_log "Background package updates restored: ${_kin_apt_paused}"
  _kin_apt_paused=""
}

# Wait until nothing holds the lock. Returns 1 on timeout, having said who won.
apt_wait_for_lock() {
  local limit="${1:-$KIN_APT_LOCK_WAIT}" waited=0 holder last=""
  while :; do
    holder=$(apt_lock_holder) || return 0
    if [ "$holder" != "$last" ]; then
      apt_log "Waiting for the package lock, held by ${holder}"
      last="$holder"
    elif [ $((waited % 30)) -eq 0 ] && [ "$waited" -gt 0 ]; then
      apt_log "  still waiting for ${holder} (${waited}s of ${limit}s)"
    fi
    [ "$waited" -lt "$limit" ] || {
      apt_log "Package lock still held by ${holder} after ${limit}s"
      return 1
    }
    sleep 2
    waited=$((waited + 2))
  done
}

# An interrupted transaction leaves dpkg half-configured, and every later
# apt-get refuses until it is finished. Do that here rather than making an
# operator discover it.
apt_repair_dpkg() {
  if dpkg --audit 2>/dev/null | grep -q .; then
    apt_log "Finishing an interrupted package transaction (dpkg --configure -a)"
    DEBIAN_FRONTEND=noninteractive dpkg --configure -a >/dev/null 2>&1 || true
  fi
}

# Every apt-get in the installer goes through this. The lock timeout is the
# backstop for a race that starts after apt_wait_for_lock returned.
kin_apt() {
  apt-get -o DPkg::Lock::Timeout="$KIN_APT_LOCK_WAIT" "$@"
}

# Clear the way once, at the start of a stage that is about to use apt.
apt_prepare() {
  apt_pause_background_upgrades
  apt_wait_for_lock || return 1
  apt_repair_dpkg
  return 0
}
