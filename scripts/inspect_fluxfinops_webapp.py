from __future__ import annotations

import os

from azure.identity import DeviceCodeCredential
from azure.mgmt.web import WebSiteManagementClient


def main() -> None:
    cred = DeviceCodeCredential(tenant_id=os.environ["FLUX_AZURE_TENANT_ID"])
    client = WebSiteManagementClient(cred, os.environ["FLUX_AZURE_SUBSCRIPTION_ID"])
    site = client.web_apps.get(
        os.environ["FLUX_APP_RESOURCE_GROUP"],
        os.environ.get("FLUX_APP_NAME", "FluxFinOps"),
    )
    print("outbound_ip_addresses:", getattr(site, "outbound_ip_addresses", None))
    print(
        "possible_outbound_ip_addresses:",
        getattr(site, "possible_outbound_ip_addresses", None),
    )
    print("default_host_name:", getattr(site, "default_host_name", None))
    print("host_names:", getattr(site, "host_names", None))
    print("keys:", sorted(site.__dict__.keys()))
    raw = getattr(site, "_data", {})
    print("raw_keys:", sorted(raw.keys()))
    print("raw_properties_keys:", sorted((raw.get("properties") or {}).keys()))
    print(
        "raw_outbound:",
        (raw.get("properties") or {}).get("outboundIpAddresses"),
    )
    print(
        "raw_possible_outbound:",
        (raw.get("properties") or {}).get("possibleOutboundIpAddresses"),
    )


if __name__ == "__main__":
    main()
