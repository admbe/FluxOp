#!/usr/bin/env bash
# Creates a SELECT-only PostgreSQL login for the automation agent.
#
# Run this in Azure Cloud Shell (bash). Cloud Shell already has psql and an
# authenticated az session, which is why it is the recommended place: nothing
# is installed locally and the admin credential never touches a file.
#
#   1. Open https://shell.azure.com  and choose Bash
#   2. Upload this script (the {} upload button) or paste it into a file
#   3. bash provision_agent_postgres_readonly.sh
#
# The script fetches the admin connection string from Key Vault, opens a
# temporary firewall window for Cloud Shell, creates the role, stores the new
# password back in Key Vault, and closes the firewall window again. The admin
# credential is only ever held in a shell variable.
#
# Everything is idempotent: re-running rotates the agent password rather than
# failing.

set -euo pipefail

VAULT="kv-flux-prod"
ADMIN_SECRET="operational-database-url"
AGENT_SECRET="agent-database-url"
SERVER="pg-flux-prod"
RESOURCE_GROUP="prod-example-westus3-rg"
AGENT_ROLE="flux_agent_ro"
RULE="cloudshell-temp-$(date +%s)"

cleanup() {
  if [ "${RULE_CREATED:-0}" = "1" ]; then
    echo "Removing temporary firewall rule..."
    az postgres flexible-server firewall-rule delete \
      --resource-group "$RESOURCE_GROUP" --server-name "$SERVER" \
      --name "$RULE" --yes >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "==> Reading the admin connection string from Key Vault"
ADMIN_URL=$(az keyvault secret show --vault-name "$VAULT" --name "$ADMIN_SECRET" \
  --query value -o tsv)
if [ -z "$ADMIN_URL" ]; then
  echo "Could not read $ADMIN_SECRET from $VAULT. Check your Key Vault access." >&2
  exit 1
fi

# postgresql://user:pass@host:port/dbname?params  -> pull out user and dbname
DB_NAME=$(printf '%s' "$ADMIN_URL" | sed -E 's#^[^/]+//[^/]+/([^?]+).*$#\1#')
DB_USER=$(printf '%s' "$ADMIN_URL" | sed -E 's#^[^/]+//([^:]+):.*$#\1#')
echo "    database=$DB_NAME admin=$DB_USER"

echo "==> Opening a temporary firewall window for Cloud Shell"
MY_IP=$(curl -s https://api.ipify.org)
echo "    Cloud Shell egress: $MY_IP"
# Verified against `az postgres flexible-server firewall-rule create --help`:
# --server-name is the server, --name is the rule. There is no --rule-name.
az postgres flexible-server firewall-rule create \
  --resource-group "$RESOURCE_GROUP" --server-name "$SERVER" \
  --name "$RULE" --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" \
  >/dev/null
RULE_CREATED=1

# A generated password never shown on screen or written to disk.
AGENT_PASSWORD=$(python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(40)))")

echo "==> Creating (or rotating) the read-only role"
# CREATE ROLE is not idempotent, so branch on existence. Grants are re-applied
# either way: a role that already exists may predate a new table.
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${AGENT_ROLE}') THEN
    ALTER ROLE ${AGENT_ROLE} WITH LOGIN PASSWORD '${AGENT_PASSWORD}';
    RAISE NOTICE 'rotated password for ${AGENT_ROLE}';
  ELSE
    CREATE ROLE ${AGENT_ROLE} WITH LOGIN PASSWORD '${AGENT_PASSWORD}';
    RAISE NOTICE 'created ${AGENT_ROLE}';
  END IF;
END
\$\$;

GRANT CONNECT ON DATABASE "${DB_NAME}" TO ${AGENT_ROLE};
GRANT USAGE ON SCHEMA public TO ${AGENT_ROLE};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${AGENT_ROLE};
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO ${AGENT_ROLE};
-- Tables created later are covered without re-running this script.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ${AGENT_ROLE};

-- Belt and braces: this role must never write, even if a future grant is
-- applied carelessly to PUBLIC.
REVOKE CREATE ON SCHEMA public FROM ${AGENT_ROLE};
SQL

echo "==> Verifying the role is genuinely read-only"
AGENT_URL=$(printf '%s' "$ADMIN_URL" \
  | sed -E "s#^(postgresql://)[^:]+:[^@]+@#\1${AGENT_ROLE}:${AGENT_PASSWORD}@#")
psql "$AGENT_URL" -v ON_ERROR_STOP=1 -c "SELECT count(*) AS visible_tables FROM information_schema.tables WHERE table_schema='public';"
if psql "$AGENT_URL" -c "CREATE TABLE agent_write_probe(x int);" >/dev/null 2>&1; then
  echo "FAIL: the role could create a table. Investigate before using it." >&2
  psql "$ADMIN_URL" -c "DROP TABLE IF EXISTS agent_write_probe;" >/dev/null 2>&1 || true
  exit 1
fi
echo "    write attempt correctly refused"

echo "==> Storing the agent connection string in Key Vault"
az keyvault secret set --vault-name "$VAULT" --name "$AGENT_SECRET" \
  --value "$AGENT_URL" --output none
echo "    saved as $VAULT/$AGENT_SECRET"

echo
echo "Done. The agent reads the connection string from Key Vault; the password"
echo "was never printed. Re-run this script to rotate it."
echo
echo "One more grant is needed before the agent can connect from the"
echo "workstation - add its egress address:"
echo "  az postgres flexible-server firewall-rule create \\"
echo "    --resource-group $RESOURCE_GROUP --server-name $SERVER \\"
echo "    --name agent-workstation --start-ip-address <IP> --end-ip-address <IP>"
