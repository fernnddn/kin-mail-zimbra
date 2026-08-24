#!/usr/bin/env bash
# =============================================================================
# KIN Mail - restore a backup set onto a SCRATCH Zimbra (never a cluster node)
#
# Refuses to run if Pacemaker is active or /opt/zimbra is on DRBD. That is the
# guard against restoring onto live Host A / Host B.
#
#   kin-mail-restore.sh --set DIR --mode full|ldap|mysql|store \
#       --i-understand-this-overwrites-zimbra [--stop-after]
#   kin-mail-restore.sh --set DIR --verify-only
#
# FOSS restore path: zmslapadd (LDAP), mysql import (mailbox metadata),
# rsync store/index. zmbackup/zmrestore are Network Edition and are not used.
# =============================================================================
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }

SET=""
MODE=""
CONFIRM=0
VERIFY_ONLY=0
STOP_AFTER=0
MARKER="${KIN_MAIL_RESTORE_MARKER:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --set) SET="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --stop-after) STOP_AFTER=1; shift ;;
    --i-understand-this-overwrites-zimbra) CONFIRM=1; shift ;;
    -h|--help)
      printf '%s\n' "Usage: $0 --set DIR [--mode full|ldap|mysql|store] [--verify-only] [--stop-after] --i-understand-this-overwrites-zimbra"
      printf '%s\n' "  --stop-after  zmcontrol stop when the restore finishes (scratch must not stay on the VLAN)"
      exit 0
      ;;
    *) fail "Unknown arg: $1"; exit 2 ;;
  esac
done

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Run as root"; exit 1; }
}

zimbra_sh() {
  su - zimbra -c "$*"
}

assert_scratch() {
  if command -v pcs >/dev/null 2>&1 && systemctl is-active --quiet pacemaker 2>/dev/null; then
    fail "Pacemaker is active - refusing restore (this looks like a cluster node)"
    exit 99
  fi
  if findmnt -n /opt/zimbra >/dev/null 2>&1; then
    src=$(findmnt -n -o SOURCE /opt/zimbra)
    case "$src" in
      *drbd*)
        fail "/opt/zimbra is on DRBD ($src) - refusing restore onto live replica"
        exit 99
        ;;
    esac
  fi
  if [ ! -x /opt/zimbra/libexec/zmslapadd ]; then
    fail "Zimbra FOSS tools not installed on this host (need matching version scratch install)"
    exit 2
  fi
}

assert_set() {
  if [ -z "$SET" ] || [ ! -d "$SET" ]; then
    fail "--set DIR must be an existing backup directory"
    exit 2
  fi
  if [ ! -f "$SET/MANIFEST.txt" ] || [ ! -f "$SET/SHA256SUMS" ]; then
    fail "Not a KIN Mail backup set (missing MANIFEST.txt / SHA256SUMS)"
    exit 2
  fi
}

verify_checksums() {
  say "Checksums"
  ( cd "$SET" && sha256sum -c SHA256SUMS ) >/dev/null
  ok "SHA256SUMS match"
}

restore_ldap() {
  say "LDAP restore (zmslapadd main DB)"
  [ -f "$SET/ldap/ldap.bak" ] || { fail "ldap.bak missing"; exit 1; }
  zimbra_sh 'ldap stop' || true
  rm -rf /opt/zimbra/data/ldap/mdb/db
  mkdir -p /opt/zimbra/data/ldap/mdb/db
  chown -R zimbra:zimbra /opt/zimbra/data/ldap
  # Copy off the 0700 backup tree so the zimbra user can read the LDIF.
  rm -f /tmp/kin-restore-ldap.bak
  cp -a "$SET/ldap/ldap.bak" /tmp/kin-restore-ldap.bak
  chown zimbra:zimbra /tmp/kin-restore-ldap.bak
  # Keep this scratch host's cn=config. Production ldap-config.bak is for
  # same-hostname rebuilds; overlaying it here breaks slapd on a different VM.
  zimbra_sh '/opt/zimbra/libexec/zmslapadd /tmp/kin-restore-ldap.bak'
  rm -f /tmp/kin-restore-ldap.bak
  zimbra_sh 'ldap start'
  sleep 2
  ok "LDAP restored"
}

restore_mysql() {
  say "MySQL restore"
  [ -f "$SET/mysql/zimbra-mysql.sql" ] || { fail "zimbra-mysql.sql missing"; exit 1; }
  zimbra_sh 'mysql.server start' || true
  sleep 2
  cat "$SET/mysql/zimbra-mysql.sql" | su - zimbra -c '/opt/zimbra/bin/mysql'
  ok "MySQL dump imported"
}

restore_store() {
  say "Store + index restore"
  [ -d "$SET/store" ] || { fail "store/ missing"; exit 1; }
  rsync -aH --delete "$SET/store/" /opt/zimbra/store/
  if [ -d "$SET/index" ]; then
    rsync -aH --delete "$SET/index/" /opt/zimbra/index/
  fi
  if [ -d "$SET/redolog" ] && [ -d /opt/zimbra/redolog ]; then
    rsync -aH --delete "$SET/redolog/" /opt/zimbra/redolog/
  fi
  chown -R zimbra:zimbra /opt/zimbra/store /opt/zimbra/index /opt/zimbra/redolog 2>/dev/null || true
  ok "store/index restored"
}

apply_ldap_secrets_from_backup() {
  # Restored LDAP uses the source cluster's bind passwords. Scratch mysqld and
  # keystore stay this VM's - only LDAP-related localconfig keys are overlaid.
  local tar="$SET/conf/zimbra-conf.tar.gz"
  [ -f "$tar" ] || { warn "no conf tarball - LDAP bind may fail"; return 0; }
  mkdir -p /tmp/kin-restore-conf
  tar -C /tmp/kin-restore-conf -xzf "$tar" conf/localconfig.xml
  python3 - <<'PY'
import xml.etree.ElementTree as ET
from pathlib import Path

def load(p):
    tree = ET.parse(p)
    nodes = {}
    for key in tree.getroot().iter("key"):
        name = key.get("name") or ""
        val_el = None
        for v in list(key):
            if v.tag.endswith("value") or v.tag == "value":
                val_el = v
        if name and val_el is not None:
            nodes[name] = val_el
    return tree, nodes

live_path = "/opt/zimbra/conf/localconfig.xml"
bak_path = "/tmp/kin-restore-conf/conf/localconfig.xml"
t_live, n_live = load(live_path)
_, n_bak = load(bak_path)
want = [
    k
    for k in n_bak
    if (k == "zimbra_ldap_password" or k.startswith("ldap_"))
    and k not in ("ldap_host", "ldap_url")
]
for k in want:
    if k in n_live:
        n_live[k].text = n_bak[k].text
t_live.write(live_path)
Path("/tmp/kin-restore-conf-merged").write_text("ok\n")
PY
  chown zimbra:zimbra /opt/zimbra/conf/localconfig.xml
  chmod 640 /opt/zimbra/conf/localconfig.xml
  rm -rf /tmp/kin-restore-conf
  ok "LDAP bind secrets overlaid from backup localconfig (mysql/keystore unchanged)"
}

tune_scratch_ram() {
  # Backup VM is smaller than a mail node. Keep scratch mailboxd in a box so
  # clamd/amavis from the restored LDAP do not OOM the host.
  zimbra_sh 'zmlocalconfig -e mailboxd_java_heap_size=512' || true
  zimbra_sh 'zmlocalconfig -e mailboxd_java_heap_new_size_percent=20' || true
  zimbra_sh 'zmprov -l ms mail.gits-it.site -zimbraServiceEnabled antivirus' || true
  zimbra_sh 'zmprov -l ms mail.gits-it.site -zimbraServiceEnabled antispam' || true
  zimbra_sh 'zmprov -l ms mail.gits-it.site -zimbraServiceEnabled snmp' || true
}

verify_functional() {
  say "Functional checks"
  local accts marker_hit
  accts=$(zimbra_sh 'zmprov -l gaa' || true)
  printf '%s\n' "$accts" | sed 's/^/      /'
  echo "$accts" | grep -q 'test1@' || { fail "test1@ missing from LDAP"; return 1; }
  echo "$accts" | grep -q 'admin@' || { fail "admin@ missing from LDAP"; return 1; }
  ok "LDAP has admin@ and test1@"

  if ! zimbra_sh 'zmmailbox -z -m test1@gits-it.site gaf' >/tmp/kin-restore-gaf.txt; then
    fail "zmmailbox gaf test1 failed (mailboxd not usable)"
    return 1
  fi
  cat /tmp/kin-restore-gaf.txt | sed 's/^/      /'
  ok "test1 mailbox reachable"

  if [ -n "$MARKER" ]; then
    marker_hit=$(zimbra_sh "zmmailbox -z -m test1@gits-it.site s -t message --limit 20 '$MARKER'" || true)
    printf '%s\n' "$marker_hit" | sed 's/^/      /'
    echo "$marker_hit" | grep -q "$MARKER" || { fail "marker mail not found in test1: $MARKER"; return 1; }
    ok "marker mail present in test1"
  else
    # Count inbox messages - a restored empty lab inbox is still a valid
    # mailbox; operator should set KIN_MAIL_RESTORE_MARKER for a stronger bar.
    warn "KIN_MAIL_RESTORE_MARKER unset - skipped message-body check"
  fi
  return 0
}

finish_scratch() {
  # Scratch Zimbra uses the production mail hostname. Leaving proxy/mta up
  # puts a second mail.gits-it.site on the VLAN. Default is remind-only so a
  # drill can still inspect; --stop-after turns it into zmcontrol stop.
  if [ "$STOP_AFTER" = 1 ]; then
    say "Stopping scratch Zimbra (--stop-after)"
    zimbra_sh 'zmcontrol stop' || true
  fi
  local listeners
  listeners=$(ss -lnt 2>/dev/null | awk '$4 ~ /:(443|25)$/ { print }' || true)
  if [ -n "$listeners" ]; then
    fail "scratch still has :443 or :25 listeners:"
    printf '%s\n' "$listeners" | sed 's/^/      /'
    warn "Stop before leaving: su - zimbra -c 'zmcontrol stop'"
    warn "Or rerun this script with --stop-after."
  else
    ok "no :443/:25 listeners on this host"
    if [ "$STOP_AFTER" != 1 ]; then
      warn "If this drill started mailboxd/proxy/mta, stop them before leaving the VLAN:"
      warn "  su - zimbra -c 'zmcontrol stop'   # or pass --stop-after"
    fi
  fi
}

need_root
assert_scratch
assert_set
verify_checksums

if [ "$VERIFY_ONLY" = "1" ]; then
  verify_functional
  exit $?
fi

if [ "$CONFIRM" != "1" ]; then
  fail "Refusing to overwrite /opt/zimbra without --i-understand-this-overwrites-zimbra"
  exit 2
fi
if [ -z "$MODE" ]; then
  fail "--mode full|ldap|mysql|store is required"
  exit 2
fi

say "Restore mode=$MODE set=$SET host=$(hostname -f 2>/dev/null || hostname)"

case "$MODE" in
  ldap)
    restore_ldap
    zimbra_sh 'zmprov -l gaa' | sed 's/^/      /'
    echo
    zimbra_sh 'zmprov -l gaa' | grep -q 'test1@' && ok "LDAP-only: test1@ present" || { fail "LDAP-only: test1@ missing"; exit 1; }
    ;;
  mysql)
    restore_mysql
    zimbra_sh '/opt/zimbra/bin/mysql -N -e "SELECT id,comment FROM zimbra.mailbox"' | sed 's/^/      /'
    zimbra_sh '/opt/zimbra/bin/mysql -N -e "SELECT comment FROM zimbra.mailbox"' \
      | grep -q 'test1@' && ok "database-only: test1 mailbox row present" \
      || { fail "database-only: test1 mailbox row missing"; exit 1; }
    ;;
  store)
    restore_store
    [ -d /opt/zimbra/store ] && ok "store-only: /opt/zimbra/store populated" || { fail "store missing"; exit 1; }
    ;;
  full)
    zimbra_sh 'zmcontrol stop' || true
    restore_ldap
    if ! grep -q 'mail.gits-it.site' /etc/hosts; then
      echo "127.0.0.1 mail.gits-it.site" >> /etc/hosts
    fi
    apply_ldap_secrets_from_backup
    zimbra_sh 'zmlocalconfig -e zimbra_server_hostname=mail.gits-it.site' || true
    zimbra_sh 'zmlocalconfig -e ldap_host=mail.gits-it.site' || true
    zimbra_sh 'ldap stop' || true
    zimbra_sh 'ldap start' || true
    restore_mysql
    restore_store
    tune_scratch_ram
    say "Starting Zimbra (scratch)"
    zimbra_sh 'zmcontrol start' || true
    sleep 20
    if ! verify_functional; then
      warn "zmcontrol status:"
      zimbra_sh 'zmcontrol status' | sed 's/^/      /' || true
      finish_scratch
      exit 1
    fi
    ok "full restore verified"
    ;;
  *)
    fail "Unknown --mode $MODE"
    exit 2
    ;;
esac
finish_scratch
exit 0
