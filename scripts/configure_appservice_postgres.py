from __future__ import annotations

import os
from urllib.parse import quote
from pathlib import Path

from azure.identity import DeviceCodeCredential
from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient
from azure.mgmt.rdbms.postgresql_flexibleservers.models import FirewallRule
from azure.mgmt.web import WebSiteManagementClient
from azure.mgmt.web.models import StringDictionary


ROOT = Path(__file__).resolve().parents[1]
TMP_DIR = ROOT / ".tmp"
PASSWORD_PATH = TMP_DIR / "fluxfinops-postgres-admin-password.txt"

TENANT_ID = os.getenv("FLUX_AZURE_TENANT_ID", "")
SUBSCRIPTION_ID = os.getenv("FLUX_AZURE_SUBSCRIPTION_ID", "")
RESOURCE_GROUP = os.getenv("FLUX_APP_RESOURCE_GROUP", "")
WEB_APP_NAME = os.getenv("FLUX_APP_NAME", "FluxFinOps")
POSTGRES_SERVER = os.getenv("FLUX_POSTGRES_SERVER_NAME", "")
POSTGRES_DATABASE = os.getenv("FLUX_POSTGRES_DATABASE", "postgres")
POSTGRES_USER = os.getenv("FLUX_POSTGRES_ADMIN_LOGIN", "fluxadmin")


def main() -> None:
    if not PASSWORD_PATH.exists():
        raise SystemExit(f"Password file not found: {PASSWORD_PATH}")

    password = PASSWORD_PATH.read_text(encoding="utf-8").strip()
    credential = DeviceCodeCredential(tenant_id=TENANT_ID)

    web_client = WebSiteManagementClient(credential, SUBSCRIPTION_ID)
    postgres_client = PostgreSQLManagementClient(credential, SUBSCRIPTION_ID)

    site = web_client.web_apps.get(RESOURCE_GROUP, WEB_APP_NAME)
    outbound_ips = [ip.strip() for ip in (site.outbound_ip_addresses or "").split(",") if ip.strip()]
    possible_outbound_ips = [ip.strip() for ip in (site.possible_outbound_ip_addresses or "").split(",") if ip.strip()]
    firewall_rules = []
    firewall_rules.extend(outbound_ips)
    firewall_rules.extend(possible_outbound_ips)

    server_connection = (
        f"postgresql://{quote(POSTGRES_USER, safe='')}:{quote(password, safe='')}"
        f"@{POSTGRES_SERVER}.postgres.database.azure.com:5432/{POSTGRES_DATABASE}"
        "?sslmode=require"
    )

    settings = web_client.web_apps.list_application_settings(RESOURCE_GROUP, WEB_APP_NAME)
    properties = dict(settings.properties or {})
    properties["FLUX_OPERATIONAL_DATABASE_URL"] = server_connection
    web_client.web_apps.update_application_settings(
        RESOURCE_GROUP,
        WEB_APP_NAME,
        StringDictionary(properties=properties),
    )

    for index, ip in enumerate(dict.fromkeys(firewall_rules)):
        rule_name = f"allow-{index:02d}"
        postgres_client.firewall_rules.begin_create_or_update(
            RESOURCE_GROUP,
            POSTGRES_SERVER,
            rule_name,
            FirewallRule(start_ip_address=ip, end_ip_address=ip),
        ).result()

    print(
        {
            "webApp": WEB_APP_NAME,
            "resourceGroup": RESOURCE_GROUP,
            "postgresServer": POSTGRES_SERVER,
            "database": POSTGRES_DATABASE,
            "configuredOutboundCount": len(dict.fromkeys(firewall_rules)),
            "appSetting": "FLUX_OPERATIONAL_DATABASE_URL",
        }
    )


if __name__ == "__main__":
    main()
