#!/usr/bin/env bash
# =============================================================================
# KIN Mail - bootstrap installer
#
# Single entry point for deploying KIN Mail. Lives under install/ with the
# stage scripts. Ensures stages are present (clone if needed), then offers a
# menu to run a full install or one stage at a time.
#
#   sudo ./install/kin-mail.sh
#
# Override defaults when needed:
#   KIN_MAIL_REPO_URL=…  KIN_MAIL_DEPLOY_DIR=…  sudo -E ./install/kin-mail.sh
#   KIN_MAIL_DRY_RUN=1 - print the planned pipeline without executing stages
#
# Console (non-interactive) full install - ONLY with explicit operator confirm:
#   sudo KIN_CONSOLE_CONFIRMED=1 ./install/kin-mail.sh --full-install
# Skips /dev/tty y/n prompts; does NOT skip the ufw dead-man's switch.
# Dead-man cancel is a separate operator step (CLI or console action).
# =============================================================================
set -u

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'

say()  { printf '%s\n' "${BLU}==>${RST} ${BLD}$*${RST}"; }
ok()   { printf '%s\n' "  ${GRN}[ OK ]${RST}   $*"; }
warn() { printf '%s\n' "  ${YLW}[WARN]${RST}   $*"; }
fail() { printf '%s\n' "  ${RED}[FAIL]${RST}   $*"; }
info() { printf '%s\n' "  ${DIM}       $*${RST}"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || { fail "Run as root:  sudo $0"; exit 1; }
}

REPO_URL="${KIN_MAIL_REPO_URL:-https://github.com/fernnddn/kin-mail-zimbra.git}"
DEPLOY_DIR="${KIN_MAIL_DEPLOY_DIR:-/opt/kin-mail-deploy}"
DRY_RUN="${KIN_MAIL_DRY_RUN:-0}"
# Set only by admin console after an explicit UI confirm - never invent a default-yes.
CONSOLE_CONFIRMED="${KIN_CONSOLE_CONFIRMED:-0}"

REQUIRED_SCRIPTS=(
  00-config.sh
  01-preflight.sh
  02-prepare-os.sh
  03-install-zimbra.sh
  04-tls-dkim.sh
  05-healthcheck.sh
  06-hybrid-auth.sh
  07-zpush.sh
  08-create-mailbox.sh
  09-hardening.sh
  10-host-firewall.sh
  11-admin-path-lockdown.sh
  12-branding.sh
  check-zimbra-foss-update.sh
  kin-mail.sh
)

# Stages that need a live Zimbra tree after 03. Mid-handoff skips these in
# run_full_install (see full_install_zimbra_stage_action); 01-03 always run.
FULL_PIPELINE_ZIMBRA_STAGES=(
  04-tls-dkim.sh
  06-hybrid-auth.sh
)

hr() {
  printf '  %s\n' "${DIM}────────────────────────────────────────────────────────────${RST}"
}

banner() {
  clear 2>/dev/null || true
  printf '\n'
  printf '  %s%s┌──────────────────────────────────────────────────────────┐%s\n' "$BLD" "$BLU" "$RST"
  printf '  %s%s│%-58s│%s\n' "$BLD" "$BLU" "" "$RST"
  printf '  %s%s│  %-54s  │%s\n' "$BLD" "$BLU" "KIN Mail" "$RST"
  printf '  %s%s│  %-54s  │%s\n' "$DIM" "$BLU" "Managed email - PT Karya Informasi Nusantara" "$RST"
  printf '  %s%s│%-58s│%s\n' "$BLD" "$BLU" "" "$RST"
  printf '  %s%s└──────────────────────────────────────────────────────────┘%s\n' "$BLD" "$BLU" "$RST"
  printf '\n'
}

scripts_present() {
  local s
  for s in "${REQUIRED_SCRIPTS[@]}"; do
    [ -f "./$s" ] || return 1
  done
  return 0
}

# Resolve where stage scripts live inside a clone (install/ layout, or legacy root).
resolve_scripts_dir() {
  local base="$1"
  if [ -d "$base/install" ] && ( cd "$base/install" && scripts_present ); then
    printf '%s\n' "$base/install"
    return 0
  fi
  if ( cd "$base" && scripts_present ); then
    printf '%s\n' "$base"
    return 0
  fi
  return 1
}

ensure_scripts() {
  local self_dir scripts_dir
  self_dir=$(cd "$(dirname "$0")" && pwd)
  cd "$self_dir" || exit 1

  if scripts_present; then
    ok "Stage scripts found in ${self_dir}"
    return 0
  fi

  say "Stage scripts incomplete in this directory; preparing deploy copy"
  info "Source : ${REPO_URL}"
  info "Target : ${DEPLOY_DIR}"

  command -v git >/dev/null 2>&1 || { fail "git is not installed"; exit 1; }

  if [ -d "$DEPLOY_DIR/.git" ] && scripts_dir=$(resolve_scripts_dir "$DEPLOY_DIR"); then
    ok "Reusing existing clone: ${scripts_dir}"
  elif [ -e "$DEPLOY_DIR" ] && [ ! -d "$DEPLOY_DIR/.git" ]; then
    fail "${DEPLOY_DIR} already exists but is not a KIN Mail git clone"
    info "Move/remove that folder, or set KIN_MAIL_DEPLOY_DIR to another path."
    exit 1
  elif [ -d "$DEPLOY_DIR/.git" ]; then
    warn "Clone exists but scripts are incomplete; trying git pull"
    git -C "$DEPLOY_DIR" pull --ff-only || {
      fail "git pull failed in ${DEPLOY_DIR}"
      exit 1
    }
  else
    mkdir -p "$(dirname "$DEPLOY_DIR")"
    git clone --depth 1 "$REPO_URL" "$DEPLOY_DIR" || {
      fail "Clone failed from ${REPO_URL}"
      exit 1
    }
    ok "Repository cloned to ${DEPLOY_DIR}"
  fi

  scripts_dir=$(resolve_scripts_dir "$DEPLOY_DIR") || {
    fail "After clone/pull, stage scripts are still incomplete in ${DEPLOY_DIR}"
    info "Expected at ${DEPLOY_DIR}/install/ (or legacy layout at clone root)."
    exit 1
  }

  cd "$scripts_dir" || exit 1

  # Continue from the cloned bootstrap so relative paths stay correct.
  if [ "$(cd "$(dirname "$0")" && pwd)" != "$scripts_dir" ]; then
    info "Continuing from ${scripts_dir}/kin-mail.sh"
    exec "$scripts_dir/kin-mail.sh" "$@"
  fi
}

load_install_config() {
  # shellcheck disable=SC1091
  . ./00-config.sh
  # shellcheck disable=SC1091
  . ./lib/zimbra-data-disk-probe.sh
}

run_stage() {
  local script="$1"
  shift || true
  # One line, flush left. The rules above and below cost three lines per stage
  # and told the reader nothing the stage name did not already say.
  echo
  say "${script}${*:+ ($*)}"
  if [ "$DRY_RUN" = "1" ]; then
    info "DRY_RUN=1 - would execute: ./${script} $*"
    ok "${script} skipped (dry-run)"
    return 0
  fi
  if [ ! -x "./$script" ]; then
    chmod +x "./$script" 2>/dev/null || true
  fi
  "./$script" "$@"
  local rc=$?
  echo
  if [ "$rc" -eq 0 ]; then
    ok "${script} finished (exit 0)"
  else
    fail "${script} failed (exit ${rc})"
  fi
  return "$rc"
}

ufw_is_active() {
  command -v ufw >/dev/null 2>&1 || return 1
  ufw status 2>/dev/null | grep -q '^Status: active'
}

ensure_admin_ips_for_firewall() {
  if [ -n "${KIN_ADMIN_IPS:-}" ]; then
    return 0
  fi
  if [ "$CONSOLE_CONFIRMED" = "1" ]; then
    # Not a refusal any more. Refusing meant the stage never ran, and a stage
    # that never runs leaves ufw off - every port on an internet-facing host
    # open, including SSH, the console and Zimbra admin. The firewall stage
    # itself firewalls fine without an admin list: admin access falls back to
    # this host's own subnet and the public ports stay the mail ports.
    #
    # The wizard calls this field optional and says it can be narrowed later
    # from the console. Turning a blank optional field into an unfirewalled
    # mail server is not what anyone reading that sentence would expect.
    warn "KIN_ADMIN_IPS is empty - applying the firewall with LAN-only admin access"
    info "Set Admin IPs in the console and re-run stage 10 to narrow it."
    return 0
  fi
  say "KIN_ADMIN_IPS is empty"
  info "Set once in the wizard, or enter sources now (also written to ${CONF_FILE})."
  info "Leave it blank to firewall this host with admin access limited to its own LAN."
  ask KIN_ADMIN_IPS "Admin source IPs/CIDRs (space-separated)" ""
  KIN_ADMIN_IPS=$(printf '%s' "$KIN_ADMIN_IPS" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')
  if [ -z "$KIN_ADMIN_IPS" ]; then
    # The operator already said yes to applying the firewall at the prompt
    # before this one. Having no particular admin address is not withdrawing
    # that. Refusing here was the fourth copy of the same wrong decision.
    warn "No admin sources given - applying the firewall with LAN-only admin access"
    return 0
  fi
  if [ -f "$CONF_FILE" ]; then
    if grep -q '^KIN_ADMIN_IPS=' "$CONF_FILE" 2>/dev/null; then
      # Replace existing assignment (simple line rewrite).
      local tmp
      tmp=$(mktemp)
      awk -v v="$KIN_ADMIN_IPS" '
        BEGIN { done=0 }
        /^KIN_ADMIN_IPS=/ { print "KIN_ADMIN_IPS=\"" v "\""; done=1; next }
        { print }
        END { if (!done) print "KIN_ADMIN_IPS=\"" v "\"" }
      ' "$CONF_FILE" >"$tmp" && mv "$tmp" "$CONF_FILE"
      chmod 600 "$CONF_FILE"
    else
      printf '\nKIN_ADMIN_IPS="%s"\n' "$KIN_ADMIN_IPS" >>"$CONF_FILE"
      chmod 600 "$CONF_FILE"
    fi
    ok "Persisted KIN_ADMIN_IPS into ${CONF_FILE}"
  fi
  export KIN_ADMIN_IPS
}

# Set by run_firewall_stage_interactive when it returns success WITHOUT having
# applied ufw. run_full_install turns that into a named warning in the closing
# summary. A single internet-facing mail node that finished its deploy with no
# host firewall, and said so only in one line twenty minutes up a scrolling
# transcript, is reported to the operator as a clean install.
FIREWALL_STAGE_SKIPPED=0

run_firewall_stage_interactive() {
  FIREWALL_STAGE_SKIPPED=0
  echo
  hr
  say "Host firewall (stage 10) - HIGH RISK"
  hr
  info "Applies ufw on THIS host only (dead-man's switch always armed)."
  info "For HA: finish + verify this host before running full install / stage 10 on the peer."
  info "Do NOT apply Host A and Host B in parallel."
  echo

  local default_ans=y admin_ips
  if ufw_is_active; then
    warn "ufw is already active on this host."
    info "Re-apply resets rules and re-arms the dead-man switch."
    default_ans=n
  fi

  if [ "$DRY_RUN" = "1" ]; then
    info "DRY_RUN=1 - would prompt: apply firewall now? (default ${default_ans})"
    ok "10-host-firewall.sh skipped (dry-run)"
    return 0
  fi

  if [ "$CONSOLE_CONFIRMED" = "1" ]; then
    # An empty optional field is not an operator declining the firewall.
    #
    # This branch used to treat it as exactly that - "Empty = skip stage 10
    # (same as CLI no)" - and it is the THIRD place that decided the same
    # thing. The other two were fixed and this one still ran first, so the
    # deploy still finished with ufw off on the internet-facing edge and one
    # warning to show for it. The rule now lives in the firewall stage alone,
    # which firewalls either way: admin access falls back to this host's own
    # subnet and the public ports stay the mail ports.
    #
    # Declining is still possible and still honoured - that is the CLI prompt
    # in the else branch below. Saying nothing is not declining.
    admin_ips=$(printf '%s' "${KIN_ADMIN_IPS:-}" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')
    if [ -z "$admin_ips" ]; then
      warn "No admin IPs configured - the firewall still applies, with LAN-only admin access."
      info "Set Admin access IPs in the console and re-run this stage to narrow it."
    fi
    # Console UI button is the operator confirm - never invent a silent default-yes
    # for bare non-TTY runs without this flag.
    say "KIN_CONSOLE_CONFIRMED=1 - applying firewall (console operator confirmed)"
    # Five minutes is the right window for someone who just typed the command
    # and is watching the terminal. It is the wrong one here: from the console
    # this stage runs near the end of a 20-40 minute deploy, and an operator
    # who is not staring at the screen at that exact moment misses it entirely.
    #
    # What that cost, twice, on live internet-facing edges (20 and 24 Sep 2026):
    # ufw switched itself back off and the machine ran with no host firewall,
    # with nothing saying so until the Cluster page learned to report it.
    #
    # A longer window does not weaken the protection the dead man exists for -
    # an operator genuinely locked out still gets back in, they just wait
    # longer. An unfirewalled mail server on the internet is the worse of the
    # two, and it is the one that kept happening. An explicit
    # KIN_UFW_DEADMAN_SEC still wins: this only replaces the default.
    if [ -z "${KIN_UFW_DEADMAN_SEC:-}" ]; then
      KIN_UFW_DEADMAN_SEC=1800
      export KIN_UFW_DEADMAN_SEC
    fi
    warn "Dead-man will be armed for ${KIN_UFW_DEADMAN_SEC}s. Console will NOT auto-cancel it."
    info "After verifying SSH + cluster + mail, cancel via console or:"
    info "  sudo ./10-host-firewall.sh cancel-deadman"
  else
    if ! ask_yn "Apply host firewall (ufw) on THIS host now?" "$default_ans"; then
      warn "Skipped 10-host-firewall.sh - host perimeter unchanged"
      info "Run later: sudo ./10-host-firewall.sh apply"
      FIREWALL_STAGE_SKIPPED=1
      return 0
    fi
  fi

  ensure_admin_ips_for_firewall || return 1
  export KIN_ADMIN_IPS
  run_stage 10-host-firewall.sh apply || return $?

  echo
  say "Dead-man is armed - verify SSH + cluster, then cancel"
  info "sudo ./10-host-firewall.sh cancel-deadman"

  if [ "$CONSOLE_CONFIRMED" = "1" ]; then
    # Dead-man stays armed in the background. Do not stall or fail Deploy
    # waiting for cancel - the operator cancels from the console after SSH
    # still works. If they never cancel, ufw reverts when the timer ends.
    warn "Dead-man stays armed for ${KIN_UFW_DEADMAN_SEC:-300}s. Verify SSH, then cancel from the console (or sudo ./10-host-firewall.sh cancel-deadman)."
    warn "If nobody cancels it, this machine ends up with NO host firewall, facing the internet."
    info "Continuing the install; ufw will auto-disable if you do not cancel in time."
    return 0
  fi

  if ask_yn "Cancel dead-man now (keep ufw enabled after your checks)?" y; then
    ./10-host-firewall.sh cancel-deadman || {
      fail "cancel-deadman failed - ufw may auto-disable when the timer ends"
      return 1
    }
  else
    warn "Dead-man still armed - cancel manually before the timer fires"
  fi
  return 0
}

run_full_install() {
  local s rc mid_handoff=0 zimbra_dir_exists=0 hardening_mode stage_action split_built=0
  # Stages that warn-and-continue on failure (07/09/11/revert-ssh) instead of
  # stopping the pipeline. Tracked so the final banner cannot claim a clean
  # "All selected pipeline stages exited 0" when hardening/lockdown actually
  # failed - the console UI treats that exact line as proof of a fully
  # successful install (re-audit, 25 Aug 2026).
  local -a soft_failed_stages=()
  load_install_config

  # This pipeline runs every stage against ONE machine, in order, which is not
  # how a split is built: the mailbox has to exist before the edge has a
  # directory to join, and the edge's install needs passwords that only exist
  # once the mailbox is built. kin-mail-split.sh does that ordering.
  #
  # Delegating here rather than asking the operator to run a different command
  # is what makes the console's Deploy button work for both topologies: it runs
  # this script and streams whatever it prints into the browser.
  if declare -F kin_topology_is_split >/dev/null 2>&1 && kin_topology_is_split; then
    local splitter="${KIN_MAIL_INSTALL_DIR}/kin-mail-split.sh"
    if [ ! -x "$splitter" ]; then
      fail "This config is TOPOLOGY=split but ${splitter} is missing."
      info "Nothing on this host has been changed; re-sync install/ and try again."
      return 1
    fi
    say "Split topology - building the mailbox first, then this machine"
    info "Running ${splitter}"
    echo
    "$splitter" || {
      rc=$?
      echo
      fail "The split build did not finish"
      info "The output above names the step. Re-running resumes from the last good one."
      return "$rc"
    }
    # And then carry on.
    #
    # Returning here was the whole bug: the splitter builds Zimbra on both
    # machines and stops, so on a split NOTHING after stage 03 ever ran on the
    # edge - no TLS, no DKIM, no hardening, no firewall, no admin lockdown, no
    # branding. The deployment looked finished because the splitter printed
    # SPLIT BUILD DONE, and it was an unhardened, uncertificated, unfirewalled
    # mail server facing the internet.
    #
    # The mailbox is dealt with inside the splitter, over the SSH channel it
    # already has. The edge is this machine, so it just continues the ordinary
    # pipeline below with 01-03 already satisfied.
    split_built=1
    echo
    say "Split built. Continuing with the stages that come after it, on this machine."
  fi

  say "Full install"
  info "Stops automatically if any stage exits non-zero."
  info "This host only - for HA, install/verify one node before the peer."
  if [ "$DRY_RUN" = "1" ]; then
    warn "KIN_MAIL_DRY_RUN=1 - no stage will modify the system"
  fi
  echo

  # 01-03 always run. Mid-handoff is only meaningful after 03 (data disk probe).
  #
  # On a split the splitter above has already run all three on this machine, in
  # the only order that works, so repeating them would re-drive an installer
  # over a working tree.
  for s in $([ "${split_built:-0}" = 1 ] || printf '%s ' 01-preflight.sh 02-prepare-os.sh 03-install-zimbra.sh); do
    run_stage "$s" || {
      rc=$?
      echo
      fail "Pipeline stopped at ${s}"
      info "Fix the issue above, then re-run the menu or that stage only."
      return "$rc"
    }
  done

  if zimbra_is_mid_handoff; then
    mid_handoff=1
    if data_disk_held_by_drbd "${KIN_DRBD_DATA_DISK:-/dev/sdb1}"; then
      warn "DRBD already holds the data disk (Secondary or attached); skipping Zimbra-touching stages."
      info "Pacemaker owns kin-zimbra on the Promoted node. Still running 09 --os-only and 10 host firewall."
    else
      warn "Mid-handoff detected: real Zimbra is on the data disk; /opt/zimbra is unmounted."
      info "Skipping Zimbra-touching stages 04/05/06/07/11. Pacemaker/DRBD own bringing the tree live."
      info "Still running 09 --os-only (fail2ban/unattended) and 10 host firewall."
    fi
  fi

  # 04 + 06 need a live Zimbra tree (zmcertmgr / zmprov / auth probes).
  #
  # 04 is soft. Everything it can fail on - public DNS, propagation, a CA's rate
  # limits, a human publishing a TXT record inside 25 minutes - is outside this
  # machine, and none of it stops mail: Zimbra keeps serving its self-signed
  # certificate and clients get a warning. What stopping here DID cost was every
  # stage after it: hardening, the host firewall, the admin-path lockdown, the
  # healthcheck. On a split that is the edge, the machine facing the internet,
  # left running with no firewall because a certificate could not be issued.
  # The smaller failure must not cause the larger one.
  #
  # 06 stays hard: it fails on this deployment's own configuration, which is
  # something the operator can fix here and now, and a broken auth path is not
  # something to harden around and call finished.
  for s in "${FULL_PIPELINE_ZIMBRA_STAGES[@]}"; do
    stage_action="$(full_install_zimbra_stage_action "$mid_handoff")"
    if [ "$stage_action" = "skip_mid_handoff" ]; then
      warn "Skipping ${s} (mid-handoff; empty /opt/zimbra mountpoint)"
      continue
    fi
    if [ "$s" = "04-tls-dkim.sh" ]; then
      run_stage "$s" || {
        warn "04-tls-dkim.sh did not complete - continuing so this host still gets"
        warn "hardened and firewalled. Re-run it once the certificate can be issued:"
        info "  sudo ./04-tls-dkim.sh"
        soft_failed_stages+=("04-tls-dkim.sh")
      }
      continue
    fi
    run_stage "$s" || {
      rc=$?
      echo
      fail "Pipeline stopped at ${s}"
      info "Fix the issue above, then re-run the menu or that stage only."
      return "$rc"
    }
  done

  # 06 configures authentication and exits 0 even when no mailbox could be
  # created, because stopping here would leave the host unhardened over a
  # mailbox problem. That is the right call for the pipeline and the wrong one
  # for the summary: the deploy then ends "all selected pipeline stages exited
  # 0" while the admin console lists nobody from the directory, which is the
  # entire feature missing. Reported from a live deploy, 26 Sep 2026.
  if [ -f /etc/kin-mail/.ad-sync-incomplete ]; then
    soft_failed_stages+=("06-hybrid-auth.sh (no Active Directory users were created - they cannot sign in)")
  fi

  # 07 - optional (wizard ZPUSH_ENABLED); needs live /opt/zimbra
  if [ "${ZPUSH_ENABLED:-yes}" != "yes" ]; then
    warn "ZPUSH_ENABLED=${ZPUSH_ENABLED:-} - skipping 07-zpush.sh"
  elif [ "$(full_install_zimbra_stage_action "$mid_handoff")" = "skip_mid_handoff" ]; then
    warn "Skipping 07-zpush.sh (mid-handoff; empty /opt/zimbra mountpoint)"
  elif [ ! -d /opt/zimbra ]; then
    warn "/opt/zimbra missing - skipping 07-zpush.sh (expected on Secondary without mount)"
  else
    run_stage 07-zpush.sh || {
      warn "07-zpush.sh failed - mail install continues. Re-run later: sudo ./07-zpush.sh"
      soft_failed_stages+=("07-zpush.sh")
    }
  fi

  # 09 - full when live tree present; --os-only for Secondary or mid-handoff
  [ -d /opt/zimbra ] && zimbra_dir_exists=1
  hardening_mode="$(full_install_hardening_mode "$mid_handoff" "$zimbra_dir_exists")"
  if [ "$hardening_mode" = "full" ]; then
    run_stage 09-hardening.sh || {
      warn "09-hardening.sh failed - mail install continues. Re-run later: sudo ./09-hardening.sh"
      soft_failed_stages+=("09-hardening.sh")
    }
  else
    if [ "$mid_handoff" -eq 1 ]; then
      warn "Mid-handoff - running 09-hardening.sh --os-only (Zimbra attrs wait for Pacemaker)"
    else
      warn "/opt/zimbra missing - running 09-hardening.sh --os-only"
    fi
    run_stage 09-hardening.sh --os-only || {
      warn "09-hardening.sh --os-only failed - mail install continues. Re-run later: sudo ./09-hardening.sh --os-only"
      soft_failed_stages+=("09-hardening.sh --os-only")
    }
  fi

  # 10 - always interactive + dead-man (never silent auto-apply); safe mid-handoff
  run_firewall_stage_interactive || {
    rc=$?
    fail "Pipeline stopped at 10-host-firewall.sh"
    return "$rc"
  }
  # Skipping is allowed (no admin IPs yet, or the operator said no), but it is
  # not a clean install. Naming it here puts it in the closing summary and
  # flips the transcript to "complete, with warnings", which the console reads
  # as needs-attention instead of proof that nothing does.
  if [ "${FIREWALL_STAGE_SKIPPED:-0}" -eq 1 ]; then
    soft_failed_stages+=("10-host-firewall.sh apply (skipped: host firewall not applied)")
  fi

  # 11 - admin path lockdown on public 443 (Primary / mounted Zimbra only)
  if [ "$(full_install_zimbra_stage_action "$mid_handoff")" = "skip_mid_handoff" ]; then
    warn "Skipping 11-admin-path-lockdown.sh (mid-handoff; empty /opt/zimbra mountpoint)"
  elif [ -d /opt/zimbra ]; then
    run_stage 11-admin-path-lockdown.sh || {
      warn "11-admin-path-lockdown.sh failed - mail install continues. Re-run later: sudo ./11-admin-path-lockdown.sh"
      soft_failed_stages+=("11-admin-path-lockdown.sh")
    }
  else
    warn "/opt/zimbra missing - skipping 11-admin-path-lockdown.sh"
  fi

  if [ "$(full_install_zimbra_stage_action "$mid_handoff")" = "skip_mid_handoff" ]; then
    warn "Skipping 05-healthcheck.sh (mid-handoff; zmcontrol not reachable on mountpoint)"
  else
    # Operator is watching the same log that printed the DKIM TXT. Wait so a
    # paste during 06-11 plus this poll can pass. Customer TLS has no ACME
    # paste; do not stall Deploy for ten minutes. Standalone 05 keeps WAIT=0.
    if [ "${TLS_METHOD:-}" = "customer" ]; then
      KIN_DKIM_WAIT_SEC="${KIN_DKIM_WAIT_SEC:-0}"
    else
      KIN_DKIM_WAIT_SEC="${KIN_DKIM_WAIT_SEC:-600}"
    fi
    export KIN_DKIM_WAIT_SEC
    run_stage 05-healthcheck.sh || {
      rc=$?
      fail "Pipeline stopped at 05-healthcheck.sh"
      return "$rc"
    }
  fi

  # Single-node installs never need password SSH again (no peer to ansible
  # into) - HA installs/mid-handoff still might (Add second server can run
  # later), so leave those as-is. Refuses on its own if no SSH key is on
  # file yet, so this can never lock the operator out.
  if [ "$mid_handoff" -eq 0 ] && [ "${TOPOLOGY:-1vm}" = "1vm" ]; then
    run_stage 02-prepare-os.sh --revert-ssh-password || {
      warn "Could not revert password SSH automatically. Re-run later: sudo ./02-prepare-os.sh --revert-ssh-password"
      soft_failed_stages+=("02-prepare-os.sh --revert-ssh-password")
    }
  fi

  echo
  if [ "$mid_handoff" -eq 1 ]; then
    say "Full install complete (mid-handoff path)"
    ok "OS stages finished; Zimbra-touching stages deferred to DRBD attach + Pacemaker"
  elif [ "${#soft_failed_stages[@]}" -gt 0 ]; then
    # Mail itself is up - 01-03, 06 and 10 all stop the pipeline on failure, so
    # reaching here means the server runs and delivers. 04 is in this list too
    # now: a certificate that could not be issued leaves mail working on a
    # self-signed one, and is not worth leaving a host unfirewalled over.
    # Claiming every stage exited 0 here was false, and the console UI takes
    # that exact string as proof nothing needs attention (re-audit, 25 Aug 2026).
    say "Full install complete, with warnings"
    warn "${#soft_failed_stages[@]} stage(s) failed and were skipped, mail is live but not fully hardened:"
    for s in "${soft_failed_stages[@]}"; do
      warn "  - ${s}"
    done
    info "Re-run each one above once the underlying issue is fixed."
  else
    say "Full install complete"
    ok "All selected pipeline stages exited 0"
  fi
  mkdir -p /etc/kin-mail
  chmod 755 /etc/kin-mail
  printf 'complete %s\n' "$(date -Is 2>/dev/null || date)" > /etc/kin-mail/setup-complete
  chmod 644 /etc/kin-mail/setup-complete
}

pick_one_stage() {
  local choice
  while true; do
    echo
    say "Pick one stage"
    hr
    printf '  %s%s%s  %s\n' "$BLD" "1)" "$RST" "01-preflight.sh          ${DIM}host readiness check${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "2)" "$RST" "02-prepare-os.sh         ${DIM}hostname, resolver, dependencies${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "3)" "$RST" "03-install-zimbra.sh     ${DIM}download & install Zimbra FOSS${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "4)" "$RST" "04-tls-dkim.sh           ${DIM}Let's Encrypt + DKIM${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "5)" "$RST" "06-hybrid-auth.sh        ${DIM}AD LDAP + local fallback${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "6)" "$RST" "07-zpush.sh              ${DIM}ActiveSync (Z-Push + Zimbra backend)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "7)" "$RST" "08-create-mailbox.sh     ${DIM}create mailbox (quota-gated)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "8)" "$RST" "09-hardening.sh          ${DIM}Part A + SMTP rate limits${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "9)" "$RST" "10-host-firewall.sh      ${DIM}ufw (dead-man; THIS host only)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "10)" "$RST" "11-admin-path-lockdown.sh ${DIM}block /zimbraAdmin on :443${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "11)" "$RST" "12-branding.sh          ${DIM}customer logo/theme (optional)${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "12)" "$RST" "05-healthcheck.sh        ${DIM}acceptance tests${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "13)" "$RST" "00-config.sh --reset     ${DIM}re-run configuration wizard${RST}"
    printf '  %s%s%s  %s\n' "$BLD" "0)" "$RST" "Back"
    hr
    printf '  %sChoice%s [0-13]: ' "$BLD" "$RST"
    read -r choice </dev/tty || return 0
    case "$choice" in
      1) run_stage 01-preflight.sh; return $? ;;
      2) run_stage 02-prepare-os.sh; return $? ;;
      3) run_stage 03-install-zimbra.sh; return $? ;;
      4) run_stage 04-tls-dkim.sh; return $? ;;
      5) run_stage 06-hybrid-auth.sh; return $? ;;
      6) run_stage 07-zpush.sh; return $? ;;
      7) run_stage 08-create-mailbox.sh; return $? ;;
      8) run_stage 09-hardening.sh; return $? ;;
      9)
        load_install_config
        run_firewall_stage_interactive
        return $?
        ;;
      10) run_stage 11-admin-path-lockdown.sh; return $? ;;
      11) run_stage 12-branding.sh; return $? ;;
      12) run_stage 05-healthcheck.sh; return $? ;;
      13)
        echo
        say "Running 00-config.sh --reset"
        ./00-config.sh --reset
        return $?
        ;;
      0) return 0 ;;
      *) warn "Unknown choice: ${choice}" ;;
    esac
  done
}

pause_return() {
  echo
  printf '  %sPress Enter to return to the menu…%s ' "$DIM" "$RST"
  read -r _ </dev/tty || true
}

main_menu() {
  local choice workdir
  workdir=$(pwd)
  while true; do
    banner
    info "Working directory : ${workdir}"
    info "Run as            : root"
    [ "$DRY_RUN" = "1" ] && warn "DRY_RUN mode is ON (KIN_MAIL_DRY_RUN=1)"
    echo
    say "Main menu"
    hr
    printf '  %s%s%s  %s\n' "$BLD" "1)" "$RST" "Full install from scratch"
    printf '      %s%s\n' "$DIM" "01→02→03→04→06→[07?]→09→[10?]→11→05  (firewall always prompted)${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "2)" "$RST" "Run a specific stage"
    printf '      %s%s\n' "$DIM" "submenu includes Z-Push, branding, hardening, firewall, admin lockdown${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "3)" "$RST" "Healthcheck only"
    printf '      %s%s\n' "$DIM" "05-healthcheck.sh${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "4)" "$RST" "Check Zimbra FOSS update"
    printf '      %s%s\n' "$DIM" "check-zimbra-foss-update.sh${RST}"
    echo
    printf '  %s%s%s  %s\n' "$BLD" "0)" "$RST" "Exit"
    hr
    printf '  %sChoice%s [0-4]: ' "$BLD" "$RST"
    read -r choice </dev/tty || exit 0
    case "$choice" in
      1) run_full_install; pause_return ;;
      2) pick_one_stage; pause_return ;;
      3) run_stage 05-healthcheck.sh; pause_return ;;
      4) run_stage check-zimbra-foss-update.sh; pause_return ;;
      0)
        echo
        say "Done"
        info "Goodbye."
        echo
        exit 0
        ;;
      *)
        warn "Unknown choice: ${choice}"
        sleep 1
        ;;
    esac
  done
}

# --- entry -------------------------------------------------------------------
need_root

# Non-interactive full install for admin console only.
# Require confirm BEFORE ensure_scripts so a rejected call cannot trigger git pull.
if [ "${1:-}" = "--full-install" ]; then
  if [ "$CONSOLE_CONFIRMED" != "1" ]; then
    fail "--full-install requires KIN_CONSOLE_CONFIRMED=1"
    info "That flag is set by the admin console after an explicit UI confirm."
    info "For interactive CLI use, run without --full-install and pick menu option 1."
    exit 2
  fi
  ensure_scripts "$@"
  chmod +x ./*.sh 2>/dev/null || true
  say "Console full install (KIN_CONSOLE_CONFIRMED=1)"
  info "TTY prompts skipped; ufw dead-man's switch still applies on stage 10."
  run_full_install
  exit $?
fi

ensure_scripts "$@"
chmod +x ./*.sh 2>/dev/null || true
main_menu
