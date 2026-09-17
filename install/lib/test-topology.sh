#!/usr/bin/env bash
# Tests for the topology resolver.
#
# Role resolution is the one that matters: every firewall, certificate and
# healthcheck decision in the split topology is taken from its answer, and the
# failure is silent in the direction that hurts - a mailbox node that thinks it
# is an edge opens the client ports to the internet and nothing complains.
# So the interesting cases here are the refusals, not the happy path.
#
# The library reads its inputs as globals - TOPOLOGY, EDGE_IP, MAIL_HOST and the
# rest - so every fixture here is assigned and never referenced again in this
# file. shellcheck cannot see across the source boundary and reports each one as
# dead. They are the test inputs. This directive must stay above the first
# command to apply to the whole file.
# shellcheck disable=SC2034
set -u

LIB="$(cd "$(dirname "$0")" && pwd)/topology.sh"
[ -f "$LIB" ] || { echo "missing $LIB" >&2; exit 1; }
# shellcheck disable=SC1090
. "$LIB"

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "ok  $1"; }
bad() { fail=$((fail+1)); echo "FAILED  $1"; }

# Reset every global the library reads, so one case cannot leak into the next.
reset() {
  TOPOLOGY=""; SERVER_IP=""; MAIL_HOST=""; EDGE_IP=""; EDGE_HOST=""
  MAILBOX_IP=""; MAILBOX_HOST=""; GATEWAY_HOST=""; KIN_LOCAL_IPV4S=""
}

is() {
  local label="$1" want="$2" got="$3"
  [ "$want" = "$got" ] && ok "$label -> $got" || bad "$label: want '$want', got '$got'"
}

# Documentation ranges throughout (RFC 5737). A test fixture must never carry a
# real deployment's addressing.
EDGE=192.0.2.19
MBOX=192.0.2.3
GW=192.0.2.102

# --- kin_valid_ipv4 ---------------------------------------------------------
reset
for good in 0.0.0.0 1.2.3.4 255.255.255.255 192.0.2.19; do
  kin_valid_ipv4 "$good" && ok "valid: $good" || bad "should be valid: $good"
done
for bad_ip in "" 1.2.3 1.2.3.4.5 1.2.3.256 1.2.3.x " 1.2.3.4" mail.example.com 1.2.3.; do
  kin_valid_ipv4 "$bad_ip" && bad "should be invalid: '$bad_ip'" || ok "invalid: '$bad_ip'"
done

# --- kin_topology -----------------------------------------------------------
# Empty is a config written before topology existed, and those are all single
# hosts. Anything unrecognised must refuse rather than default.
reset; is "empty TOPOLOGY" 1vm "$(kin_topology)"
reset; TOPOLOGY=1vm;   is "TOPOLOGY=1vm"   1vm   "$(kin_topology)"
reset; TOPOLOGY=2vm;   is "TOPOLOGY=2vm"   2vm   "$(kin_topology)"
reset; TOPOLOGY="split"; is "TOPOLOGY=split" split "$(kin_topology)"

reset; TOPOLOGY=3vm
if kin_topology >/dev/null 2>&1; then
  bad "an unrecognised TOPOLOGY was accepted"
else
  ok "unrecognised TOPOLOGY is refused, not defaulted to 1vm"
fi

reset; TOPOLOGY="split"
kin_topology_is_split && ok "kin_topology_is_split true on split" || bad "split not detected"
reset; TOPOLOGY=1vm
kin_topology_is_split && bad "1vm reported as split" || ok "kin_topology_is_split false on 1vm"

# --- kin_node_role ----------------------------------------------------------
reset; TOPOLOGY=1vm; is "1vm role" all "$(kin_node_role)"
reset; is "legacy config role" all "$(kin_node_role)"

reset; TOPOLOGY="split"; EDGE_IP=$EDGE; MAILBOX_IP=$MBOX
KIN_LOCAL_IPV4S="127.0.0.1 $EDGE"
is "host holding EDGE_IP" edge "$(kin_node_role 2>/dev/null)"

reset; TOPOLOGY="split"; EDGE_IP=$EDGE; MAILBOX_IP=$MBOX
KIN_LOCAL_IPV4S="$MBOX"
is "host holding MAILBOX_IP" mailbox "$(kin_node_role 2>/dev/null)"

# A host that is in neither role must refuse. Defaulting to either one is how a
# stage runs the wrong firewall against a machine nobody meant to touch.
reset; TOPOLOGY="split"; EDGE_IP=$EDGE; MAILBOX_IP=$MBOX
KIN_LOCAL_IPV4S="198.51.100.7"
if kin_node_role >/dev/null 2>&1; then
  bad "a host in neither role was given one"
else
  ok "a host in neither role is refused"
fi

# Both on one machine is not a split at all, and silently picking one would
# produce an appliance that half-believes it is two.
reset; TOPOLOGY="split"; EDGE_IP=$EDGE; MAILBOX_IP=$MBOX
KIN_LOCAL_IPV4S="$EDGE $MBOX"
if kin_node_role >/dev/null 2>&1; then
  bad "one host holding both addresses was given a role"
else
  ok "one host holding both addresses is refused"
fi

reset; TOPOLOGY="split"; EDGE_IP=$EDGE
KIN_LOCAL_IPV4S="$EDGE"
if kin_node_role >/dev/null 2>&1; then
  bad "split with no MAILBOX_IP still resolved a role"
else
  ok "split with a missing address is refused"
fi

# Role must come from the address, never the hostname: stage 02 sets the
# hostname, so before it runs the name is whatever the VM template left behind.
reset; TOPOLOGY="split"; EDGE_IP=$EDGE; MAILBOX_IP=$MBOX
EDGE_HOST=mail.example.com; MAILBOX_HOST=store.example.com
KIN_LOCAL_IPV4S="$MBOX"
is "hostname does not override the address" mailbox "$(kin_node_role 2>/dev/null)"

# --- kin_node_ip / kin_node_host --------------------------------------------
# One config file is written on the edge and carried to the mailbox, so
# SERVER_IP/MAIL_HOST name the wrong machine there. A stage writing /etc/hosts
# or a certificate subject from those would configure the mailbox as an edge.
reset; TOPOLOGY=1vm; SERVER_IP=198.51.100.5; MAIL_HOST=mail.example.com
is "1vm own address" 198.51.100.5 "$(kin_node_ip)"
is "1vm own name" mail.example.com "$(kin_node_host)"

reset; TOPOLOGY="split"; SERVER_IP=$EDGE; EDGE_IP=$EDGE; MAILBOX_IP=$MBOX
EDGE_HOST=mail.example.com; MAILBOX_HOST=store.example.com; MAIL_HOST=mail.example.com
KIN_LOCAL_IPV4S="$EDGE"
is "edge own address" "$EDGE" "$(kin_node_ip 2>/dev/null)"
is "edge own name" mail.example.com "$(kin_node_host 2>/dev/null)"

# The same config file, now on the other machine. SERVER_IP still says the
# edge; the answer must not.
KIN_LOCAL_IPV4S="$MBOX"
is "mailbox own address ignores SERVER_IP" "$MBOX" "$(kin_node_ip 2>/dev/null)"
is "mailbox own name ignores MAIL_HOST" store.example.com "$(kin_node_host 2>/dev/null)"

KIN_LOCAL_IPV4S="198.51.100.7"
if kin_node_ip >/dev/null 2>&1; then
  bad "a host in neither role was given an address"
else
  ok "kin_node_ip refuses on a host in neither role"
fi

# --- kin_mta_ip -------------------------------------------------------------
# This is the value the gateway relays to. Wrong on a split means filtered mail
# is handed to a machine with no MTA on it.
reset; TOPOLOGY=1vm; SERVER_IP=198.51.100.5
is "1vm MTA address" 198.51.100.5 "$(kin_mta_ip)"
reset; TOPOLOGY="split"; SERVER_IP=$MBOX; EDGE_IP=$EDGE
is "split MTA address is the edge" "$EDGE" "$(kin_mta_ip)"

# --- kin_split_config_problem ----------------------------------------------
sane() {
  reset
  TOPOLOGY="split"; EDGE_IP=$EDGE; MAILBOX_IP=$MBOX
  EDGE_HOST=mail.example.com; MAILBOX_HOST=store.example.com; GATEWAY_HOST=$GW
}

sane
if kin_split_config_problem >/dev/null; then
  bad "a usable split config was reported as broken: $(kin_split_config_problem)"
else
  ok "a usable split config passes"
fi

refuses() {
  local label="$1"
  if kin_split_config_problem >/dev/null; then
    ok "refuses: $label"
  else
    bad "accepted a broken config: $label"
  fi
}

sane; EDGE_IP="";            refuses "no EDGE_IP"
sane; MAILBOX_IP="";         refuses "no MAILBOX_IP"
sane; EDGE_IP="not-an-ip";   refuses "EDGE_IP is not an address"
sane; MAILBOX_IP="1.2.3.4.5"; refuses "MAILBOX_IP is malformed"
sane; MAILBOX_IP=$EDGE;      refuses "both nodes on one address"
sane; GATEWAY_HOST=$EDGE;    refuses "gateway and edge share an address"
sane; GATEWAY_HOST=$MBOX;    refuses "gateway and mailbox share an address"
sane; EDGE_HOST="";          refuses "no EDGE_HOST"
sane; MAILBOX_HOST="";       refuses "no MAILBOX_HOST"
sane; MAILBOX_HOST=mail.example.com; refuses "both nodes share a hostname"
sane; EDGE_HOST=$EDGE;       refuses "EDGE_HOST is an address, not a name"
sane; MAILBOX_HOST=$MBOX;    refuses "MAILBOX_HOST is an address, not a name"

# A gateway recorded by name must not be compared against an address and must
# not block an otherwise sound config.
sane; GATEWAY_HOST=relay.example.com
if kin_split_config_problem >/dev/null; then
  bad "a gateway recorded by hostname was rejected: $(kin_split_config_problem)"
else
  ok "a gateway recorded by hostname is left to the gateway's own probe"
fi

# --- the wiring into 00-config.sh -------------------------------------------
# A resolver nothing sources is not a resolver. These assert the connection
# itself, because it is the kind of thing that gets quietly dropped in a merge
# and then every stage silently falls back to single-host behaviour.
CONF="$(cd "$(dirname "$0")/.." && pwd)/00-config.sh"

if grep -q 'lib/topology.sh' "$CONF"; then
  ok "00-config.sh sources the topology resolver"
else
  bad "00-config.sh does not source lib/topology.sh"
fi

# Resolved from an absolute path. Stage 03 cds into $ZCS_SRC and never comes
# back; a relative source would find nothing there.
if grep -q 'KIN_TOPOLOGY_LIB="${KIN_MAIL_INSTALL_DIR}/lib/topology.sh"' "$CONF"; then
  ok "the resolver is resolved from an absolute path"
else
  bad "the resolver path is not built from KIN_MAIL_INSTALL_DIR"
fi
if grep -qE '^[[:space:]]*\.[[:space:]]+\.?/lib/topology\.sh' "$CONF"; then
  bad "the resolver is also sourced relatively and will break once a stage cds away"
else
  ok "the resolver is never sourced relatively"
fi

for key in TOPOLOGY EDGE_IP EDGE_HOST MAILBOX_IP MAILBOX_HOST; do
  if grep -q "^${key}=\"\${${key}}\"" "$CONF"; then
    ok "the written config carries ${key}"
  else
    bad "the written config does not carry ${key}"
  fi
done

# A config written before the split existed must still load as a single host.
if grep -q '^: "${TOPOLOGY:=1vm}"' "$CONF"; then
  ok "a config with no TOPOLOGY still loads as a single host"
else
  bad "a legacy config has no TOPOLOGY default"
fi

# --- end to end through the real config file --------------------------------
# The point of the split resolver is that BOTH machines read one config file
# and still reach different answers. Prove that against 00-config.sh itself
# rather than against the library in isolation.
tmpdir=$(mktemp -d)
cat > "$tmpdir/config" <<EOF
MAIL_DOMAIN="example.test"
MAIL_HOST="mail.example.test"
SERVER_IP="${EDGE}"
TOPOLOGY="split"
EDGE_IP="${EDGE}"
EDGE_HOST="mail.example.test"
MAILBOX_IP="${MBOX}"
MAILBOX_HOST="store.example.test"
EOF

role_via_config() {
  KIN_MAIL_CONFIG="$tmpdir/config" KIN_LOCAL_IPV4S="$1" \
    bash -c '. "$0" >/dev/null 2>&1 && kin_node_role' "$CONF" 2>/dev/null
}

is "one config, seen from the edge" edge "$(role_via_config "$EDGE")"
is "one config, seen from the mailbox" mailbox "$(role_via_config "$MBOX")"
is "one config, seen from a stranger" "" "$(role_via_config 198.51.100.7)"

# --- an incomplete deploy tree must stop, not carry on -----------------------
# 00-config.sh has no `set -e`, so a failed source prints one line and
# CONTINUES - every stage would then run with no role resolver and fail
# somewhere further downstream, where the reason is no longer legible. A tree
# that arrived without lib/ has to die here.
treedir="$tmpdir/tree"
mkdir -p "$treedir/lib"
cp "$CONF" "$treedir/00-config.sh"
# lib/topology.sh deliberately absent.
out=$(KIN_MAIL_CONFIG="$tmpdir/config" bash -c '. "$0"; echo REACHED_THE_END' \
       "$treedir/00-config.sh" 2>&1)
if printf '%s\n' "$out" | grep -q 'REACHED_THE_END'; then
  bad "a deploy tree with no lib/topology.sh kept going"
else
  ok "a deploy tree with no lib/topology.sh stops immediately"
fi
if printf '%s\n' "$out" | grep -q 'deploy tree is incomplete'; then
  ok "and says why, naming the missing file"
else
  bad "it stopped without a legible reason: $out"
fi

rm -rf "$tmpdir"

# --- a split config must never reach the single-node installer ---------------
STAGE3="$(cd "$(dirname "$0")/.." && pwd)/03-install-zimbra.sh"
BOOT="$(cd "$(dirname "$0")/.." && pwd)/kin-mail.sh"

# Stage 03 handles a split by writing a per-role defaults file and letting the
# installer read it. What it must never do is fall through to the tmux driver,
# which answers a single-node menu tree and would install every role on one
# machine while the config says this is half of a pair.
if grep -q 'kin_topology_is_split' "$STAGE3"; then
  ok "03-install-zimbra.sh branches on the topology"
else
  bad "03-install-zimbra.sh would drive the single-node installer on a split config"
fi
if grep -q 'kin_zcs_defaults_mailbox' "$STAGE3" && grep -q 'kin_zcs_defaults_edge' "$STAGE3"; then
  ok "03-install-zimbra.sh installs each role from its own defaults file"
else
  bad "03-install-zimbra.sh has no per-role defaults generation"
fi
# The branch must exit rather than continue into the tmux driver below it.
if grep -A200 'kin_topology_is_split' "$STAGE3" | grep -q 'exit 0'; then
  ok "the split branch exits instead of falling through to the tmux driver"
else
  bad "the split branch can fall through into the single-node driver"
fi

# The branch must sit BELOW the --manual early exit: downloading and extracting
# the tarball is what the capability check needs, and blocking that would make
# the split impossible to even evaluate.
manual_line=$(grep -n 'MANUAL -eq 1' "$STAGE3" | head -1 | cut -d: -f1)
guard_line=$(grep -n 'kin_topology_is_split' "$STAGE3" | head -1 | cut -d: -f1)
if [ -n "$manual_line" ] && [ -n "$guard_line" ] && [ "$guard_line" -gt "$manual_line" ]; then
  ok "the split branch sits below --manual, so the tarball stays reachable"
else
  bad "the split branch blocks --manual (manual=${manual_line:-?} branch=${guard_line:-?})"
fi

# The full-install pipeline runs stages against ONE machine. On a split the
# mailbox has to be built first, so this entry point hands over to the
# orchestrator rather than running its own stage list. That delegation is what
# makes the console's Deploy button work for both topologies - it runs this
# script and streams the output to the browser - so losing it silently turns
# the button back into a single-machine install.
SPLITTER="$(cd "$(dirname "$0")/.." && pwd)/kin-mail-split.sh"

if grep -q 'kin_topology_is_split' "$BOOT"; then
  ok "kin-mail.sh branches on the topology"
else
  bad "kin-mail.sh would run the single-machine pipeline against a split config"
fi
if grep -q 'kin-mail-split.sh' "$BOOT"; then
  ok "kin-mail.sh delegates a split to the orchestrator"
else
  bad "kin-mail.sh does not hand a split to kin-mail-split.sh"
fi
# It must hand over BEFORE running any stage, or the edge gets built first.
# Anchored on the stage loop itself, not on the first mention of a stage name:
# kin-mail.sh also lists the scripts it expects to find and names them in the
# menu, both of which appear far earlier and neither of which runs anything.
boot_split=$(grep -n 'kin_topology_is_split' "$BOOT" | head -1 | cut -d: -f1)
boot_stage=$(grep -n 'for s in 01-preflight' "$BOOT" | head -1 | cut -d: -f1)
if [ -n "$boot_split" ] && [ -n "$boot_stage" ] && [ "$boot_split" -lt "$boot_stage" ]; then
  ok "the handover happens before any stage runs"
else
  bad "kin-mail.sh starts running stages before it notices the topology"
fi

[ -x "$SPLITTER" ] && ok "the orchestrator is executable" \
  || bad "kin-mail-split.sh is missing or not executable"

# The orchestrator must build the mailbox first and refuse to run anywhere else.
if grep -q 'ROLE" != edge' "$SPLITTER"; then
  ok "the orchestrator refuses to run from the mailbox"
else
  bad "the orchestrator does not check which machine it is on"
fi
mbox_line=$(grep -n 'mailbox-installed' "$SPLITTER" | head -1 | cut -d: -f1)
edge_line=$(grep -n 'edge-installed' "$SPLITTER" | head -1 | cut -d: -f1)
if [ -n "$mbox_line" ] && [ -n "$edge_line" ] && [ "$mbox_line" -lt "$edge_line" ]; then
  ok "the mailbox is built before the edge"
else
  bad "the orchestrator would build the edge before the directory exists"
fi
# install/ alone leaves the second disk unused: the disk selector lives in
# ansible/, and without it Zimbra silently lands on the OS volume.
if grep -q 'select_drbd_disk.py' "$SPLITTER"; then
  ok "the pushed tree carries the data-disk selector"
else
  bad "the orchestrator ships install/ only; the mailbox's second disk would be ignored"
fi
# The password must never reach the process list.
if grep -q 'sshpass -e' "$SPLITTER" && ! grep -qE 'sshpass -p' "$SPLITTER"; then
  ok "the mailbox password is passed by environment, never in argv"
else
  bad "the orchestrator puts the SSH password where ps can read it"
fi

# Remote privileged steps must run inside one root shell. `sudo -S A && B`
# elevates only A - the shell splits on && before sudo sees it - so a step that
# removed a directory as root then recreated it as the login user failed with a
# permission error naming the wrong command.
if grep -q "sudo -S -p '' bash -c" "$SPLITTER"; then
  ok "remote sudo wraps the whole command in one root shell"
else
  bad "remote sudo elevates only the first command of a chain"
fi
# And the preflight has to prove it for a chain: a single-command check reports
# sudo as working while every multi-step operation still runs unprivileged.
if grep -q 'multi-command steps' "$SPLITTER"; then
  ok "preflight checks sudo across a command chain, not just one command"
else
  bad "preflight would pass with sudo that breaks on the first &&"
fi

# --- a failed step must not report success -----------------------------------
# Piping a stage through sed for indentation makes the pipeline report sed's
# status, and sed always succeeds. A mailbox install that died on a permission
# error was reported as done, and the run carried on to collect passwords it had
# never written. Only PIPESTATUS sees the stage itself.
if grep -q 'PIPESTATUS\[0\]' "$SPLITTER"; then
  ok "a piped stage is judged by the stage, not by sed"
else
  bad "a failing stage would be reported as done"
fi
if grep -qE 'if ! (mbox_sudo|"\$\{KIN_MAIL_INSTALL_DIR\}).*\| sed' "$SPLITTER"; then
  bad "a stage is still judged by the exit status of its pipeline"
else
  ok "no stage is judged by its pipeline's last command"
fi

# --- the deploy must work when driven from the console -----------------------
# privhelperd runs with ProtectHome=true, so /root is not writable: ssh cannot
# create /root/.ssh and the host key has nowhere to go.
if grep -q 'UserKnownHostsFile=' "$SPLITTER"; then
  ok "known_hosts is kept somewhere writable under ProtectHome"
else
  bad "ssh would try to write /root/.ssh, which the console cannot"
fi

# The disk selector is not always beside install/: bootstrap syncs the install
# tree to /opt/kin-mail-deploy and ansible to /opt/kin-mail-console, so a
# console-driven deploy finds one without the other and silently skips the
# mailbox's spare disk.
if grep -q '/opt/kin-mail-console' "$SPLITTER"; then
  ok "the selector is looked for in the console's tree too"
else
  bad "a console-driven deploy would ship no disk selector"
fi

# EVERY marker gets the same treatment, not just the interesting ones. The
# uninstaller removes /opt/kin-mail-deploy, so a mailbox wiped between attempts
# keeps "tree-pushed" while the tree is gone - and the run reaches the install
# step to find no installer there.
if grep -q "step_done config-pushed && ! mbox_run" "$SPLITTER"; then
  ok "config-pushed is confirmed on the machine before it is trusted"
else
  bad "config-pushed is trusted without asking the mailbox"
fi
# The installer tree is not cached at all. A marker can attest that a tree
# exists; it cannot attest that it is the CURRENT one, and a mailbox running a
# stale stage while the edge runs the fixed copy fails in a way that looks like
# the fix did not work.
if grep -q 'fresh every run' "$SPLITTER" && ! grep -q 'mark_done tree-pushed' "$SPLITTER"; then
  ok "the installer is copied fresh every run, never cached"
else
  bad "a cached installer tree could be stale on the mailbox"
fi

# The same rule inside stage 03. It carries its own copy of the check, and
# fixing only the orchestrator's left the deploy passing preflight and then
# failing on the identical condition one step later.
if grep -q 'if \[ -x /opt/zimbra/bin/zmcontrol \]; then' "$STAGE3"; then
  ok "stage 03 also judges by the install, not the directory"
else
  bad "stage 03 still refuses a prepared-but-empty /opt/zimbra"
fi

# An empty /opt/zimbra is the NORMAL state of a prepared mailbox: stage 02
# mounts the spare data disk there and leaves it empty. Refusing on the
# directory alone rejects a machine this deployment just finished preparing.
if grep -q "test -x /opt/zimbra/bin/zmcontrol" "$SPLITTER"; then
  ok "an existing install is detected by zmcontrol, not by the directory"
else
  bad "a prepared-but-empty mailbox would be refused as already installed"
fi

# --- the deploy changes the password it is using, and must follow it ---------
# 02-prepare-os.sh calls chpasswd to set the OS account from KIN_USER_PASS.
# MAILBOX_SSH_PASS is what the operator typed on the Topology step and what got
# the orchestrator in; the moment stage 02 runs on the mailbox, that secret
# stops working and every later step is refused. The failure reads as a wrong
# password when nothing was typed wrong - the deploy changed it.
if grep -q 'KIN_USER_PASS' "$SPLITTER"; then
  ok "the orchestrator follows the password 02-prepare-os sets"
else
  bad "every step after mailbox 02-prepare-os would be refused"
fi
# And preflight itself must try both, because a re-run meets a mailbox that an
# earlier attempt already re-passworded: only trying the wizard's value refuses
# to resume work the machine has already done.
if grep -q 'try_mailbox_password' "$SPLITTER"; then
  ok "preflight tries both passwords before giving up"
else
  bad "a re-run after 02-prepare-os would be refused at preflight"
fi
tryfn=$(grep -n 'try_mailbox_password "${KIN_USER_PASS' "$SPLITTER" | head -1 | cut -d: -f1)
pf=$(grep -n 'say "1/8 Preflight"' "$SPLITTER" | head -1 | cut -d: -f1)
if [ -n "$tryfn" ] && [ -n "$pf" ] && [ "$tryfn" -gt "$pf" ]; then
  ok "the fallback happens inside preflight, before any step runs"
else
  bad "the password fallback is not reached during preflight"
fi

pwswitch=$(grep -n 'was reset by 02-prepare-os' "$SPLITTER" | head -1 | cut -d: -f1)
mbox_os=$(grep -n 'run_remote_stage mailbox-os' "$SPLITTER" | head -1 | cut -d: -f1)
mbox_inst=$(grep -n 'run_remote_stage mailbox-installed' "$SPLITTER" | head -1 | cut -d: -f1)
if [ -n "$pwswitch" ] && [ -n "$mbox_os" ] && [ -n "$mbox_inst" ] \
   && [ "$pwswitch" -gt "$mbox_os" ] && [ "$pwswitch" -lt "$mbox_inst" ]; then
  ok "it follows the change after stage 02 and before the next remote step"
else
  bad "the password switch is in the wrong place to help"
fi

# 02-prepare-os must describe the machine it is on, not the one that wrote the
# config: on the mailbox, SERVER_IP names the edge.
STAGE2="$(cd "$(dirname "$0")/.." && pwd)/02-prepare-os.sh"
if grep -q 'FQDN mapped to ${KIN_THIS_IP}' "$STAGE2"; then
  ok "stage 02 reports the address it actually wrote"
else
  bad "stage 02 reports SERVER_IP, which on the mailbox is the edge"
fi

# --- the operator must be told WHERE to fix a bad password -------------------
# Pressing Deploy in the console runs apply_wizard_draft first, which rewrites
# /etc/kin-mail/config from the saved wizard answers. An operator who edits that
# file by hand and then deploys from the browser loses the edit before the first
# step runs, and the same failure comes back looking identical.
if grep -q 'the password comes from the WIZARD' "$SPLITTER"; then
  ok "a login failure says to fix the password where it is actually read from"
else
  bad "the operator would be sent to a file the next deploy overwrites"
fi

# --- a marker must never be the only evidence a step happened ----------------
# /etc/kin-mail outlives the machines it describes. A rebuilt mailbox, a swapped
# disk or a re-run against a different address leaves markers claiming work that
# no longer exists - and "mailbox-installed" is the expensive one, because
# skipping it means the edge later hunts for a directory nobody created.
if grep -q 'test -x /opt/zimbra/bin/zmcontrol' "$SPLITTER"; then
  ok "a recorded install is confirmed against the machine before it is skipped"
else
  bad "the orchestrator trusts its own marker that Zimbra is installed"
fi
if grep -q 'deployment-identity' "$SPLITTER"; then
  ok "recorded progress is tied to the addresses it was recorded for"
else
  bad "progress recorded for one pair of machines would be reused for another"
fi
if grep -q -- '--fresh' "$SPLITTER"; then
  ok "there is a way to discard recorded progress deliberately"
else
  bad "no way to start over without deleting files by hand"
fi
# The identity check must run before any step consults a marker, or the first
# step reads stale progress before it can be invalidated.
ident_line=$(grep -n 'invalidate_markers_if_moved$' "$SPLITTER" | tail -1 | cut -d: -f1)
first_step=$(grep -n 'run_remote_stage mailbox-os' "$SPLITTER" | head -1 | cut -d: -f1)
if [ -n "$ident_line" ] && [ -n "$first_step" ] && [ "$ident_line" -lt "$first_step" ]; then
  ok "stale progress is discarded before the first step reads it"
else
  bad "the first step could act on progress recorded for other machines"
fi

# An edge cannot be built before the mailbox exists: without the directory's
# passwords it installs cleanly and then binds to nothing.
if grep -q 'KIN_LDAP_ADMIN_PASS' "$STAGE3"; then
  ok "the edge branch refuses without the mailbox's LDAP passwords"
else
  bad "the edge branch would install without the directory secrets"
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL OK ($pass checks)"; exit 0; fi
echo "FAILED $fail test(s) ($pass ok)"; exit 1
