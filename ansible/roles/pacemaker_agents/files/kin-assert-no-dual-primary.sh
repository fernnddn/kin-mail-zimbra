#!/bin/bash
set -euo pipefail
ROLE=$(drbdadm role kin-zimbra 2>/dev/null || echo NONE)
STATUS=$(drbdadm status kin-zimbra 2>/dev/null || true)
CRM=$(crm_mon -1 -r 2>/dev/null || true)
echo "drbd_role=$ROLE"
echo "$STATUS"
# Local/peer roles: dual-primary iff Primary/Primary
if [[ "$ROLE" == "Primary/Primary" ]]; then
  echo DUAL_PRIMARY_DETECTED
  exit 1
fi
PROMOTED_LINE=$(echo "$CRM" | grep -E 'Promoted:' || true)
echo "crm_promoted=$PROMOTED_LINE"
NODES=$(echo "$PROMOTED_LINE" | sed -n 's/.*Promoted:[[:space:]]*\[\(.*\)\].*/\1/p')
COUNT=0
for n in $NODES; do COUNT=$((COUNT+1)); done
if [[ "$COUNT" -gt 1 ]]; then
  echo DUAL_MASTER_IN_CRM count=$COUNT
  exit 1
fi
echo NO_DUAL_PRIMARY_OK
