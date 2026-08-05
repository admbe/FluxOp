from __future__ import annotations

import json
import os
import secrets
import string
from pathlib import Path

from azure.identity import DeviceCodeCredential
from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient
from azure.mgmt.rdbms.postgresql_flexibleservers.models import Backup, Server, Sku, Storage
from azure.mgmt.resource.resources import ResourceManagementClient


ROOT = Path(__file__).resolve().parents[1]
TMP_DIR = ROOT / ".tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

TENANT_ID = os.getenv("FLUX_AZURE_TENANT_ID", "")
SUBSCRIPTION_ID = os.getenv("FLUX_AZURE_SUBSCRIPTION_ID", "")
RESOURCE_GROUP = os.getenv("FLUX_POSTGRES_RESOURCE_GROUP", "")
LOCATION = os.getenv("FLUX_POSTGRES_LOCATION", "westus3")
SERVER_NAME = os.getenv("FLUX_POSTGRES_SERVER_NAME", "")
ADMIN_LOGIN = os.getenv("FLUX_POSTGRES_ADMIN_LOGIN", "fluxadmin")
PASSWORD_PATH = TMP_DIR / "fluxfinops-postgres-admin-password.txt"
DETAILS_PATH = TMP_DIR / "fluxfinops-postgres-server.json"


def generate_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(ch.islower() for ch in candidate)
            and any(ch.isupper() for ch in candidate)
            and any(ch.isdigit() for ch in candidate)
            and any(ch in "!@#$%^&*()-_=+" for ch in candidate)
        ):
            return candidate


def ensure_password() -> str:
    if PASSWORD_PATH.exists():
        return PASSWORD_PATH.read_text(encoding="utf-8").strip()
    password = generate_password()
    PASSWORD_PATH.write_text(password, encoding="utf-8")
    return password


def main() -> None:
    credential = DeviceCodeCredential(tenant_id=TENANT_ID)
    resource_client = ResourceManagementClient(credential, SUBSCRIPTION_ID)
    postgres_client = PostgreSQLManagementClient(credential, SUBSCRIPTION_ID)

    try:
        resource_group = resource_client.resource_groups.get(RESOURCE_GROUP)
        print(f"resource group exists: {resource_group.name}")
    except Exception:
        print(f"creating resource group: {RESOURCE_GROUP}")
        resource_client.resource_groups.create_or_update(RESOURCE_GROUP, {"location": LOCATION})

    existing = {server.name: server for server in postgres_client.servers.list_by_resource_group(RESOURCE_GROUP)}
    if SERVER_NAME in existing:
        server = postgres_client.servers.get(RESOURCE_GROUP, SERVER_NAME)
        print(json.dumps({"status": "exists", "name": server.name, "fqdn": getattr(server, "fully_qualified_domain_name", None)}, indent=2))
        return

    password = ensure_password()
    server = Server(
        location=LOCATION,
        sku=Sku(name="Standard_B1ms", tier="Burstable"),
        administrator_login=ADMIN_LOGIN,
        administrator_login_password=password,
        version="16",
        storage=Storage(storage_size_gb=32),
        backup=Backup(backup_retention_days=7, geo_redundant_backup="Disabled"),
    )

    print(f"creating PostgreSQL Flexible Server: {SERVER_NAME}")
    poller = postgres_client.servers.begin_create(RESOURCE_GROUP, SERVER_NAME, server)
    created = poller.result()

    details = {
        "status": "created",
        "name": created.name,
        "resource_group": RESOURCE_GROUP,
        "location": created.location,
        "sku": getattr(created.sku, "name", None),
        "tier": getattr(created.sku, "tier", None),
        "version": getattr(created, "version", None),
        "fqdn": getattr(created, "fully_qualified_domain_name", None),
        "administrator_login": ADMIN_LOGIN,
        "admin_password_file": str(PASSWORD_PATH),
        "admin_password_length": len(password),
    }
    DETAILS_PATH.write_text(json.dumps(details, indent=2), encoding="utf-8")
    print(json.dumps(details, indent=2))


if __name__ == "__main__":
    main()
