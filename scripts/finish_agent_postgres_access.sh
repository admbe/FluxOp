#!/usr/bin/env bash
# Second half of provision_agent_postgres_readonly.sh.
#
# That script creates the SELECT-only role and files its connection string in
# Key Vault. The agent still cannot use it: it has no Key Vault access, and
# the Postgres firewall only admits Azure services. This grants exactly those
# two things and nothing more.
#
# Run in Azure Cloud Shell as someone who can assign RBAC on the vault and
# edit the Postgres firewall. Idempotent; prints SKIP where already applied.
#
#   bash finish_agent_postgres_access.sh                 # workstation IP below
#   bash finish_agent_postgres_access.sh 203.0.113.7     # or pass one

set -euo pipefail

AGENT_APP_ID="f589122a-b275-49a5-a86c-fe69b44e859c"   # Flux-FinOps-Monitor
VAULT="kv-flux-prod"
SECRET="agent-database-url"
SERVER="pg-flux-prod"
RESOURCE_GROUP="prod-example-westus3-rg"
RULE="agent-workstation"
AGENT_IP="${1:-69.114.190.60}"

AGENT_OID=$(az ad sp show --id "$AGENT_APP_ID" --query id -o tsv)
VAULT_ID=$(az keyvault show -n "$VAULT" --query id -o tsv)
# Vault is in RBAC mode, so the scope can name the single secret rather than
# the whole vault. The agent gets this one credential and no other.
SECRET_SCOPE="$VAULT_ID/secrets/$SECRET"

echo "==> 1. Key Vault Secrets User on $SECRET only"
EXISTING=$(az role assignment list --assignee "$AGENT_APP_ID" --scope "$SECRET_SCOPE" \
  --role "Key Vault Secrets User" --query "[0].id" -o tsv 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  echo "    SKIP  already assigned"
else
  az role assignment create \
    --assignee-object-id "$AGENT_OID" \
    --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets User" \
    --scope "$SECRET_SCOPE" --output none
  echo "    GRANTED (scope: this secret only)"
fi

echo "==> 2. Postgres firewall rule for the agent workstation"
echo "    address: $AGENT_IP"
CURRENT=$(az postgres flexible-server firewall-rule list \
  -g "$RESOURCE_GROUP" -s "$SERVER" \
  --query "[?name=='$RULE'].startIpAddress | [0]" -o tsv 2>/dev/null || true)
if [ "$CURRENT" = "$AGENT_IP" ]; then
  echo "    SKIP  rule already points at this address"
else
  # create doubles as update when the rule name already exists, which is what
  # makes re-running this after an IP change the right move.
  az postgres flexible-server firewall-rule create \
    -g "$RESOURCE_GROUP" -s "$SERVER" --name "$RULE" \
    --start-ip-address "$AGENT_IP" --end-ip-address "$AGENT_IP" --output none
  [ -n "$CURRENT" ] && echo "    UPDATED from $CURRENT" || echo "    CREATED"
fi

echo
echo "Done. The agent can now read $SECRET and reach the server from"
echo "$AGENT_IP with SELECT-only rights."
echo
echo "Note: that is a residential address and will change eventually. When it"
echo "does, connections fail closed - re-run this with the new address."
echo "To revoke everything this script granted:"
echo "  az role assignment delete --assignee $AGENT_APP_ID --scope \"$SECRET_SCOPE\" --role \"Key Vault Secrets User\""
echo "  az postgres flexible-server firewall-rule delete -g $RESOURCE_GROUP -s $SERVER --name $RULE --yes"
