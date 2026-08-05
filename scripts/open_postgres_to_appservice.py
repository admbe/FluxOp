from __future__ import annotations

import os
from urllib.parse import quote

from azure.identity import DeviceCodeCredential
from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient
from azure.mgmt.rdbms.postgresql_flexibleservers.models import FirewallRule
from azure.mgmt.web import WebSiteManagementClient
from azure.mgmt.web.models import StringDictionary


TENANT_ID = os.getenv("FLUX_AZURE_TENANT_ID", "")
SUBSCRIPTION_ID = os.getenv("FLUX_AZURE_SUBSCRIPTION_ID", "")
RESOURCE_GROUP = os.getenv("FLUX_APP_RESOURCE_GROUP", "")
WEB_APP_NAME = os.getenv("FLUX_APP_NAME", "FluxFinOps")
POSTGRES_SERVER = os.getenv("FLUX_POSTGRES_SERVER_NAME", "")
POSTGRES_DATABASE = os.getenv("FLUX_POSTGRES_DATABASE", "postgres")
POSTGRES_USER = os.getenv("FLUX_POSTGRES_ADMIN_LOGIN", "fluxadmin")
PASSWORD_PATH = os.getenv("FLUX_POSTGRES_PASSWORD_PATH", r"D:\Code\FainOps\Codex\.tmp\fluxfinops-postgres-admin-password.txt")


def _get_credential() -> object:
    return DeviceCodeCredential(tenant_id=TENANT_ID, prompt_callback=_prompt_callback)


def _prompt_callback(*args: object, **kwargs: object) -> None:
    url = args[0] if len(args) > 0 else "https://login.microsoftonline.com/device"
    code = args[1] if len(args) > 1 else ""
    expiry = args[2] if len(args) > 2 else None
    print({"url": str(url), "code": str(code), "expires": str(expiry)}, flush=True)


def main() -> None:
    password = open(PASSWORD_PATH, "r", encoding="utf-8").read().strip()
    credential = _get_credential()
    web_client = WebSiteManagementClient(credential, SUBSCRIPTION_ID)
    postgres_client = PostgreSQLManagementClient(credential, SUBSCRIPTION_ID)

    conn_str = (
        f"postgresql://{quote(POSTGRES_USER, safe='')}:{quote(password, safe='')}"
        f"@{POSTGRES_SERVER}.postgres.database.azure.com:5432/{POSTGRES_DATABASE}"
        "?sslmode=require"
    )

    settings = web_client.web_apps.list_application_settings(RESOURCE_GROUP, WEB_APP_NAME)
    properties = dict(settings.properties or {})
    properties["FLUX_OPERATIONAL_DATABASE_URL"] = conn_str
    web_client.web_apps.update_application_settings(
        RESOURCE_GROUP,
        WEB_APP_NAME,
        StringDictionary(properties=properties),
    )

    postgres_client.firewall_rules.begin_create_or_update(
        RESOURCE_GROUP,
        POSTGRES_SERVER,
        "allow-appservice-azure",
        FirewallRule(start_ip_address="0.0.0.0", end_ip_address="0.0.0.0"),
    ).result()

    web_client.web_apps.restart(RESOURCE_GROUP, WEB_APP_NAME)
    print(
        {
            "webApp": WEB_APP_NAME,
            "postgresServer": POSTGRES_SERVER,
            "firewallRule": "0.0.0.0",
            "restarted": True,
        }
    )


if __name__ == "__main__":
    main()
