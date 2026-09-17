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
# The next thing this script does with $STAGING is `rm -rf` it, as root, on a
# live production mail node. $STAGING comes from the backup VM's config file,
# so a typo there is a destructive command here. Refuse anything that is not
# plainly a scratch directory before that line is reached.
assert_safe_staging() {
  local dir="$1"
  case "$dir" in
  /) fail "Staging must not be /"; exit 2 ;;
  /*) : ;;
  *) fail "Staging must be an absolute path (got '$dir')"; exit 2 ;;
  esac
  case "$dir" in
  */. | */..) fail "Staging must not end in . or .."; exit 2 ;;
  *//*) fail "Staging path contains an empty component: '$dir'"; exit 2 ;;
  esac
  case "$dir" in
  /opt/zimbra | /opt/zimbra/*)
    fail "Staging must not be on /opt/zimbra (DRBD)"
    exit 2
    ;;
  /bin | /boot | /dev | /etc | /home | /lib | /opt | /proc | /root | /run | /sbin | /srv | /sys | /usr | /var)
    fail "Staging must not be a system directory ('$dir')"
    exit 2
    ;;
  /bin/* | /boot/* | /dev/* | /etc/* | /lib/* | /proc/* | /sbin/* | /sys/* | /usr/*)
    fail "Staging must not be under a system directory ('$dir')"
    exit 2
    ;;
  esac
  # At least two path components, so /var/tmp is allowed but /var is not, and
  # a single stray component like /staging cannot become the target either.
  local depth
  depth=$(printf '%s' "${dir#/}" | awk -F/ '{print NF}')
  if [ "${depth:-0}" -lt 2 ]; then
    fail "Staging must be at least two levels deep (got '$dir')"
    exit 2
  fi
  if [ -e "$dir" ] && [ ! -d "$dir" ]; then
    fail "Staging exists and is not a directory: '$dir'"
    exit 2
  fi
}
assert_safe_staging "$STAGING"

if ! is_promoted_here; then
  # Why this node has no mail store depends on the deployment, and the wrong
  # explanation sends an operator looking in the wrong place.
  #
  # On a split the edge is SUPPOSED to have no store: it runs the MTA and the
  # proxy, and the mail lives on the mailbox node. Telling that operator the
  # node "is not Promoted" is Pacemaker vocabulary for a cluster they do not
  # have, and the real instruction - run this on the other machine - never
  # reaches them. A backup that refuses for a reason nobody understands is how
  # an estate ends up with no backups at all.
  if grep -qs '^[[:space:]]*TOPOLOGY="\?split' /etc/kin-mail/config 2>/dev/null; then
    mbox=$(sed -n 's/^[[:space:]]*MAILBOX_HOST=//p' /etc/kin-mail/config 2>/dev/null |
           tail -1 | tr -d '"'"'"' \r')
    mbox_ip=$(sed -n 's/^[[:space:]]*MAILBOX_IP=//p' /etc/kin-mail/config 2>/dev/null |
              tail -1 | tr -d '"'"'"' \r')
    fail "This is the edge node of a split; the mail store is on the mailbox node."
    warn "Run the backup on ${mbox:-the mailbox} (${mbox_ip:-address not recorded})."
    warn "The edge holds no mail, so a backup taken here would contain none."
    exit 3
  fi
  fail "This node is not Promoted (/opt/zimbra not mounted with store)"
  exit 3
fi

# Staging is deliberately on the root filesystem so nothing is written to the
# DRBD volume. That means a full copy of the mail store lands on the OS disk -
# and the shipped sizing is a 100GB OS disk beside a 500GB data disk, so a
# store that has outgrown the OS disk would fill / on a live mail node and take
# mail down. That is a worse outcome than skipping a night's backup, so this
# refuses before writing anything.
#
# Needed is the store plus index plus redolog, times a factor covering the
# mysqldump and the per-account tgz exports (which are a compressed second copy
# of the same mail). Override the factor, or skip the check outright, for a
# node whose staging lives on its own disk.
: "${KIN_BACKUP_SPACE_FACTOR:=150}"
: "${KIN_BACKUP_SKIP_SPACE_CHECK:=0}"

human() { numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || printf '%sB' "${1:-0}"; }

assert_staging_space() {
  local dir="$1" parent src_bytes need_bytes avail_bytes
  parent="$dir"
  while [ ! -d "$parent" ] && [ "$parent" != "/" ]; do parent=$(dirname "$parent"); done

  src_bytes=0
  local d
  for d in /opt/zimbra/store /opt/zimbra/index /opt/zimbra/redolog; do
    [ -d "$d" ] || continue
    local b
    b=$(du -sb "$d" 2>/dev/null | awk '{print $1}')
    case "$b" in '' | *[!0-9]*) b=0 ;; esac
    src_bytes=$((src_bytes + b))
  done
  if [ "$src_bytes" -eq 0 ]; then
    warn "could not size /opt/zimbra/store - skipping the free-space check"
    return 0
  fi

  avail_bytes=$(df -B1 --output=avail "$parent" 2>/dev/null | tail -1 | tr -dc '0-9')
  case "$avail_bytes" in '' | *[!0-9]*) avail_bytes=0 ;; esac
  if [ "$avail_bytes" -eq 0 ]; then
    warn "could not read free space on $parent - skipping the free-space check"
    return 0
  fi

  need_bytes=$((src_bytes / 100 * KIN_BACKUP_SPACE_FACTOR))
  ok "staging needs ~$(human "$need_bytes"), $(human "$avail_bytes") free on $parent"
  if [ "$avail_bytes" -ge "$need_bytes" ]; then
    return 0
  fi
  fail "Not enough room on $parent to stage this backup."
  fail "  mail data:  $(human "$src_bytes")"
  fail "  needed:     ~$(human "$need_bytes")  (${KIN_BACKUP_SPACE_FACTOR}% of it, for the dump and tgz exports)"
  fail "  available:  $(human "$avail_bytes")"
  fail "Staging on this node is on the OS disk. Filling it would take mail"
  fail "down, so this run is refusing rather than starting a copy it cannot"
  fail "finish. Point REMOTE_STAGING at a larger filesystem on this node, or"
  fail "set KIN_BACKUP_SKIP_SPACE_CHECK=1 if the estimate is wrong for it."
  exit 4
}

if [ "$KIN_BACKUP_SKIP_SPACE_CHECK" != "1" ]; then
  assert_staging_space "$STAGING"
else
  warn "free-space check skipped (KIN_BACKUP_SKIP_SPACE_CHECK=1)"
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

# An empty mailbox still exports a valid tgz with a header in it, so anything
# smaller than this is a truncated or error-page response, not a mailbox.
# getRestURL can exit 0 and still write one, which would otherwise be counted
# as a successful export and pass the SHA256SUMS check as a good backup.
MIN_TGZ_BYTES=64

expected=0
exported=0
failed_accounts=""
while IFS= read -r acct; do
  [ -n "$acct" ] || continue
  expected=$((expected + 1))
  safe=$(printf '%s' "$acct" | tr '@.' '__')
  dest="$STAGING/mailboxes/${safe}.tgz"
  rc=0
  zimbra_sh "zmmailbox -z -m '$acct' getRestURL '//?fmt=tgz'" > "$dest" || rc=$?
  size=$(wc -c <"$dest" 2>/dev/null | tr -d ' ')
  case "$size" in '' | *[!0-9]*) size=0 ;; esac
  if [ "$rc" -eq 0 ] && [ "$size" -ge "$MIN_TGZ_BYTES" ]; then
    exported=$((exported + 1))
    ok "exported $acct ($size bytes)"
  else
    if [ "$rc" -eq 0 ]; then
      warn "export for $acct returned success but only $size bytes - discarding"
    else
      warn "export failed for $acct (exit $rc)"
    fi
    rm -f "$dest"
    failed_accounts="$failed_accounts $acct"
  fi
done <<< "$ACCOUNTS"

# Record the counts. Without them a set that exported none of its mailboxes
# looked exactly like one that exported all of them: the run still succeeded,
# checksums still matched, and the backup was still marked verified.
{
  echo "mailboxes_expected=$expected"
  echo "mailboxes_exported=$exported"
  echo "mailboxes_failed=${failed_accounts# }"
} >> "$MARKER_FILE"

# A shortfall is recorded and shouted about, but it does not abort the run.
# The store, MySQL and LDAP layers are the restore path; a set carrying them is
# genuinely restorable and far better than no backup at all. Aborting here
# would also strand this staging tree - a full copy of the mail store - on the
# production node's OS disk until the next night's run cleared it.
if [ "$expected" -gt 0 ] && [ "$exported" -eq 0 ]; then
  echo "complete=false" >> "$MARKER_FILE"
  fail "none of the $expected mailboxes exported - check zmmailbox on this node"
  fail "store, MySQL and LDAP were still collected, so this set can still be"
  fail "restored, but it has no per-account tgz exports in it at all."
elif [ "$exported" -lt "$expected" ]; then
  echo "complete=false" >> "$MARKER_FILE"
else
  echo "complete=true" >> "$MARKER_FILE"
fi
if [ "$exported" -lt "$expected" ] && [ "$exported" -gt 0 ]; then
  warn "$((expected - exported)) of $expected mailbox export(s) failed:${failed_accounts}"
  warn "The set is still restorable from store/MySQL/LDAP, but these mailboxes"
  warn "have no per-account tgz in it."
fi
ok "$exported of $expected mailbox tgz file(s)"

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
