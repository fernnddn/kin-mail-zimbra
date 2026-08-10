#!/usr/bin/env bash
# =============================================================================
# KIN Mail - 03 INSTALL ZIMBRA
#
# Downloads the Maldua Zimbra FOSS build, verifies its checksum, and drives the
# interactive installer inside tmux by reading the screen and answering the
# prompts it recognises.
#
#   ./03-install-zimbra.sh            drive the installer automatically
#   ./03-install-zimbra.sh --manual   only download and extract, then print the
#                                     answer sequence and let you drive it
#
# Watch it live from another terminal:  tmux attach -t zcs
# =============================================================================
set -u
cd "$(dirname "$0")" && . ./00-config.sh
need_root

MANUAL=0; [ "${1:-}" = "--manual" ] && MANUAL=1
SESS="zcs"
LOG="/var/log/kin-mail-install.log"

# --- 1. download and verify --------------------------------------------------
say "1. Mengambil ${ZCS_FILE}"
mkdir -p "$ZCS_SRC"; cd "$ZCS_SRC"

[ -f "${ZCS_FILE}.sha256" ] || curl -sL -m 120 -o "${ZCS_FILE}.sha256" "${ZCS_BASE}/${ZCS_FILE}.sha256"
if [ ! -s "${ZCS_FILE}.sha256" ]; then fail "Checksum tidak bisa diunduh"; exit 1; fi

if [ ! -s "$ZCS_FILE" ]; then
  info "Mengunduh (beberapa ratus MB, mohon tunggu)"
  wget -q --show-progress -c -O "$ZCS_FILE" "${ZCS_BASE}/${ZCS_FILE}" || { fail "Unduhan gagal"; exit 1; }
fi

# Never install an unverified tarball: this is a community rebuild, so the
# checksum is the only integrity control available.
if sha256sum -c "${ZCS_FILE}.sha256" >/dev/null 2>&1; then
  ok "SHA-256 cocok"
else
  fail "SHA-256 TIDAK COCOK - berkas rusak atau dimodifikasi. Berhenti."
  exit 1
fi

ZDIR="${ZCS_SRC}/${ZCS_FILE%.tgz}"
[ -d "$ZDIR" ] || tar xzf "$ZCS_FILE"
[ -x "$ZDIR/install.sh" ] || { fail "install.sh tidak ditemukan di $ZDIR"; exit 1; }
ok "Diekstrak ke $ZDIR"

# --- manual mode -------------------------------------------------------------
if [ $MANUAL -eq 1 ]; then
  echo; say "MODE MANUAL - jalankan sendiri:"
  echo "    cd $ZDIR && ./install.sh --platform-override --skip-activation-check"
  echo
  say "Urutan jawaban yang benar:"
  cat <<EOF
    Do you agree with the terms ..............  Y
    Use Zimbra's package repository ..........  Y   <- WAJIB Y. Paket *-components
                                                     hanya ada di repo itu; kalau N
                                                     instalasi berhenti.
    Install zimbra-ldap / logger / mta .......  Y
    Install zimbra-dnscache ..................  N   <- hindari rebutan port 53
    Install zimbra-snmp / store / apache .....  Y
    Install zimbra-spell / memcached / proxy ..  Y   <- memcached muncul sebagai
                                                     prompt terpisah bila dari repo
    The system will be modified. Continue? ...  Y
    Change hostname ..........................  No
    Change domain name? ......................  Yes -> ${MAIL_DOMAIN}
    Menu 1 -> 7 TimeZone .....................  ${ZIMBRA_TZ_NAME}
    Menu 6 -> 4 Admin Password ...............  (password anda)
    a -> Yes -> Enter -> Yes .................  apply
    Notify Zimbra of your installation? ......  No  <- mengirim email admin ke Zimbra
EOF
  echo; exit 0
fi

# --- 2. drive the installer --------------------------------------------------
say "2. Menjalankan installer di tmux (attach: tmux attach -t ${SESS})"
tmux kill-session -t "$SESS" 2>/dev/null
tmux new-session -d -s "$SESS" -x 200 -y 50
tmux send-keys -t "$SESS" "cd $ZDIR && ./install.sh --platform-override --skip-activation-check 2>&1 | tee $LOG" Enter

send()   { tmux send-keys -t "$SESS" "$1" Enter; sleep "${2:-2}"; }
scr()    { tmux capture-pane -p -t "$SESS" | grep -v '^[[:space:]]*$'; }
lastln() { scr | tail -1; }

STAGE="packages"
DEADLINE=$(( $(date +%s) + 3600 ))

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 4
  L="$(lastln)"

  case "$L" in
    *"agree with the terms"*)              info "EULA -> Y";              send Y 6 ;;
    *"Use Zimbra's package repository"*)   info "repo Zimbra -> Y";       send Y 45 ;;
    *"Install zimbra-dnscache"*)           info "dnscache -> N";          send N 3 ;;
    *"Install zimbra-"*)                   send Y 3 ;;
    *"system will be modified"*)           info "commit instalasi";       send Y 20 ;;
    *"Change hostname"*)                   info "hostname dipertahankan"; send No 6 ;;
    *"Change domain name"*)                info "ganti domain -> Yes";    send Yes 5 ;;
    *"Create domain:"*)                    info "domain -> ${MAIL_DOMAIN}"; send "$MAIL_DOMAIN" 12 ;;
    *"Notify Zimbra of your installation"*) info "telemetry -> No";       send No 10 ;;
    *"press return to exit"*)              info "selesai";                send "" 5; break ;;

    *"Address unconfigured"*|*"press 'a' to apply"*)
        if [ "$STAGE" = "packages" ]; then
          STAGE="menus"
          info "Menu konfigurasi tercapai - mengatur timezone, domain, password"

          # --- timezone (Common Configuration -> 7) ---
          send 1 4
          send 7 4
          TZIDX=$(tmux capture-pane -p -S -400 -t "$SESS" \
                  | grep -E "^[0-9]+ ${ZIMBRA_TZ_NAME}$" | tail -1 | awk '{print $1}')
          if [ -n "$TZIDX" ]; then
            info "timezone ${ZIMBRA_TZ_NAME} -> pilihan ${TZIDX}"; send "$TZIDX" 5
          else
            warn "Timezone ${ZIMBRA_TZ_NAME} tidak ada di daftar, dilewati"; send "" 3
          fi
          send r 4

          # --- admin password (Store -> 4) ---
          send 6 4
          send 4 3
          tmux send-keys -t "$SESS" "$ADMIN_PASS" Enter; sleep 5
          send r 4

          # --- apply ---
          info "apply"
          send a 6
          send Yes 6      # save config to file
          send "" 6       # accept default filename
          send Yes 30     # the system will be modified
        fi
        ;;
  esac
done

# --- 3. verify ---------------------------------------------------------------
echo; say "3. Verifikasi"
sleep 5
if [ ! -d /opt/zimbra ]; then fail "/opt/zimbra tidak ada - instalasi gagal. Lihat $LOG"; exit 1; fi

STATUS=$(su - zimbra -c "zmcontrol status" 2>&1)
STOPPED=$(printf '%s' "$STATUS" | grep -v Running | grep -vE "^Host|^$" | wc -l)
printf '%s\n' "$STATUS" | sed 's/^/    /'

if [ "$STOPPED" -eq 0 ]; then
  ok "Semua service berjalan"
else
  fail "$STOPPED service tidak berjalan"
fi

su - zimbra -c "zmcontrol -v" 2>/dev/null | sed 's/^/    /'
echo "    domain: $(su - zimbra -c 'zmprov gad' 2>/dev/null | tr '\n' ' ')"

echo
say "SELESAI"
info "Webmail : https://${MAIL_HOST}"
info "Admin   : https://${MAIL_HOST}:7071  (admin@${MAIL_DOMAIN})"
info "Sertifikat masih self-signed. Lanjut ke 04-tls-dkim.sh"
echo
tmux kill-session -t "$SESS" 2>/dev/null
