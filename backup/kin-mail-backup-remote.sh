#!/usr/bin/env bash
# =============================================================================
# KIN Mail - backup collector (runs on the Promoted mail node)
#
# Invoked by kin-mail-backup.sh via SSH. Do not cron this on both mail nodes.
# Staging is always on the root filesystem (/var/tmp), never on DRBD /opt/zimbra.
#
#   kin-mail-backup-remote.sh --probe
#   kin-mail-backup-remote.sh --backup --staging DIR
#
# FOSS has no zmbackup (Network Edition). This collector uses zmslapcat,
# mysqldump --single-transaction, and a read-only rsync of store/index.
# =============================================================================
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Run as root"; exit 1; }
}

zimbra_sh() {
  su - zimbra -c "$*"
}

is_promoted_here() {
  findmnt -n /opt/zimbra >/dev/null 2>&1 || return 1
  [ -d /opt/zimbra/store ] || return 1
  [ -x /opt/zimbra/libexec/zmslapcat ] || return 1
  return 0
}

MODE=""
STAGING=""
while [ $# -gt 0 ]; do
  case "$1" in
    --probe) MODE=probe; shift ;;
    --backup) MODE=backup; shift ;;
    --staging) STAGING="${2:-}"; shift 2 ;;
    -h|--help)
      printf '%s\n' "Usage: $0 --probe | --backup --staging DIR"
      exit 0
      ;;
    *) fail "Unknown arg: $1"; exit 2 ;;
  esac
done

need_root

if [ "$MODE" = "probe" ]; then
  if is_promoted_here; then
    src=$(findmnt -n -o SOURCE /opt/zimbra)
    host=$(hostname -f 2>/dev/null || hostname)
    printf 'PROMOTED host=%s mount=%s\n' "$host" "$src"
    exit 0
  fi
  printf 'NOT_PROMOTED host=%s\n' "$(hostname -f 2>/dev/null || hostname)"
  exit 1
fi

if [ "$MODE" != "backup" ]; then
  fail "Specify --probe or --backup"
  exit 2
fi
if [ -z "$STAGING" ]; then
  fail "--staging DIR is required"
  exit 2
fi
case "$STAGING" in
  /opt/zimbra|/*/opt/zimbra/*)
    fail "Staging must not be on /opt/zimbra (DRBD)"
    exit 2
    ;;
esac
if ! is_promoted_here; then
  fail "This node is not Promoted (/opt/zimbra not mounted with store)"
  exit 3
fi

rm -rf "$STAGING"
mkdir -p "$STAGING"/{ldap,mysql,store,index,redolog,conf,mailboxes}
# zimbra must be able to mkdir/write inside staging (zmslapcat does mkdir -p DEST).
chown -R zimbra:zimbra "$STAGING"
chmod 700 "$STAGING"

MARKER_FILE="$STAGING/MANIFEST.txt"
{
  echo "kind=kin-mail-backup"
  echo "version=1"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "taken_on=$(hostname -f 2>/dev/null || hostname)"
  echo "zimbra_mount=$(findmnt -n -o SOURCE /opt/zimbra)"
  echo "zimbra_version=$(zimbra_sh 'zmcontrol -v' | head -1)"
} > "$MARKER_FILE"

say "1. LDAP (zmslapcat - live-safe export, not a raw DB copy)"
zimbra_sh "/opt/zimbra/libexec/zmslapcat '$STAGING/ldap'"
zimbra_sh "/opt/zimbra/libexec/zmslapcat -c '$STAGING/ldap'"
# Keep stable names; zmslapcat also writes timestamped copies.
if [ ! -f "$STAGING/ldap/ldap.bak" ]; then
  fail "zmslapcat did not produce ldap.bak"
  exit 1
fi
ok "LDAP main + config exported"

say "2. MySQL (mysqldump --single-transaction)"
DUMP="$STAGING/mysql/zimbra-mysql.sql"
DBS=$(zimbra_sh '/opt/zimbra/bin/mysql -N -e "SHOW DATABASES"' \
  | grep -E '^(zimbra|mboxgroup[0-9]+|chat)$' | tr '\n' ' ')
if [ -z "$DBS" ]; then
  fail "No zimbra/mboxgroup databases visible"
  exit 1
fi
# Password goes in a mode-600 --defaults-extra-file, not -p"$pass" on argv
# (a command-line password is visible to any local user via ps/proc for the
# whole dump duration).
# shellcheck disable=SC2086
su - zimbra -c "source /opt/zimbra/bin/zmshutil && zmsetvars && \
  MYCNF=\$(mktemp) && chmod 600 \"\$MYCNF\" && \
  printf '[client]\npassword=%s\n' \"\$zimbra_mysql_password\" > \"\$MYCNF\" && \
  /opt/zimbra/common/bin/mysqldump --defaults-extra-file=\"\$MYCNF\" --single-transaction --routines --triggers -S \"\$mysql_socket\" -u zimbra --databases $DBS; \
  rc=\$?; rm -f \"\$MYCNF\"; exit \$rc" > "$DUMP"
chmod 600 "$DUMP"
ok "mysqldump $(wc -c <"$DUMP" | tr -d ' ') bytes  ($DBS)"

say "3. Mail store + index (read-only rsync onto root-fs staging)"
rsync -aH --delete /opt/zimbra/store/ "$STAGING/store/"
rsync -aH --delete /opt/zimbra/index/ "$STAGING/index/"
if [ -d /opt/zimbra/redolog ]; then
  rsync -aH --delete /opt/zimbra/redolog/ "$STAGING/redolog/"
fi
ok "store=$(du -sh "$STAGING/store" | awk '{print $1}') index=$(du -sh "$STAGING/index" | awk '{print $1}')"

say "4. Non-LDAP config"
tar -C /opt/zimbra -czf "$STAGING/conf/zimbra-conf.tar.gz" conf
if [ -d /etc/kin-mail ]; then
  tar -C /etc -czf "$STAGING/conf/etc-kin-mail.tar.gz" kin-mail
fi
if [ -d /etc/letsencrypt/renewal-hooks ]; then
  tar -C /etc -czf "$STAGING/conf/letsencrypt-hooks.tar.gz" letsencrypt/renewal-hooks 2>/dev/null || true
fi
ok "config tarballs written"

say "5. Per-account REST tgz (Zimbra-native hot export)"
ACCOUNTS=$(zimbra_sh 'zmprov -l gaa' | grep -vE '^(spam\.|ham\.|virus-quarantine\.|galsync\.)' || true)
echo "accounts=$(printf '%s' "$ACCOUNTS" | tr '\n' ' ')" >> "$MARKER_FILE"
exported=0
while IFS= read -r acct; do
  [ -n "$acct" ] || continue
  safe=$(printf '%s' "$acct" | tr '@.' '__')
  if zimbra_sh "zmmailbox -z -m '$acct' getRestURL '//?fmt=tgz'" > "$STAGING/mailboxes/${safe}.tgz"; then
    exported=$((exported + 1))
    ok "exported $acct"
  else
    warn "export failed for $acct (continuing)"
    rm -f "$STAGING/mailboxes/${safe}.tgz"
  fi
done <<< "$ACCOUNTS"
ok "$exported mailbox tgz file(s)"

say "6. Checksums"
du -sh "$STAGING" | awk '{print "staging_size="$1}' >> "$MARKER_FILE"
( cd "$STAGING" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
if [ -n "${SUDO_USER:-}" ]; then
  chown -R "$SUDO_USER:$SUDO_USER" "$STAGING"
else
  chown -R root:root "$STAGING"
fi
chmod -R go-rwx "$STAGING"
chmod 700 "$STAGING"
ok "staging $STAGING"
printf 'STAGING=%s\n' "$STAGING"
exit 0
