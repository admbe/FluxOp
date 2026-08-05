from __future__ import annotations

import re
from typing import Any


FLUX_INTELLIGENCE_BRAND = "Flux Intelligence"
FLUX_INTELLIGENCE_RULE_VERSION = "2026-07-25.4"

# Freshness is a safety property, not a confidence bonus. These are starting
# policy values and should be tightened for execution workflows after source
# coverage is measured. Days are intentionally conservative for topology and
# state changes; longer windows are reserved for utilization/lifecycle review.
RULE_FRESHNESS_DAYS: dict[str, int] = {
    "stopped_allocated_vm": 1,
    "deallocated_vm_residual_cost": 1,
    "unattached_disk": 2,
    "aged_snapshot": 30,
    "premium_snapshot": 7,
    "premium_disk_underutilized_review": 30,
    "snapshot_source_deleted": 2,
    "public_ip_unattached": 2,
    "public_ip_orphan_nic": 2,
    "public_ip_deallocated_vm": 1,
    "empty_standard_load_balancer": 2,
    "empty_application_gateway": 2,
    "vnet_gateway_no_connections": 2,
    "empty_paid_app_service_plan": 2,
    "windows_ahb_eligibility_review": 7,
    "sql_vm_ahb_eligibility_review": 7,
    "unused_network_interface": 2,
    "idle_nat_gateway": 2,
    "empty_availability_set": 2,
    "orphaned_network_security_group": 2,
    "basic_public_ip_retired": 7,
    "basic_load_balancer_retired": 7,
}


RULES: dict[str, dict[str, str]] = {
    "stopped_allocated_vm": {
        "label": "Stopped but allocated VM",
        "category": "Cost",
        "impact": "High",
        "confidence": "High",
        "reason": (
            "The VM is stopped but remains allocated, so Azure continues billing "
            "for its compute capacity. Confirm the workload can be deallocated."
        ),
    },
    "deallocated_vm_residual_cost": {
        "label": "Deallocated VM residual cost",
        "category": "Cost exposure",
        "impact": "Low",
        "confidence": "Review",
        "reason": (
            "Compute is deallocated, but attached disks, reserved Public IPs, and "
            "other dependent resources can continue to incur charges."
        ),
    },
    "unattached_disk": {
        "label": "Unattached managed disk",
        "category": "Cost",
        "impact": "Medium",
        "confidence": "Medium",
        "reason": (
            "The managed disk is unattached and does not match the configured "
            "Azure Site Recovery artifact heuristic. Validate retention before deletion."
        ),
    },
    "aged_snapshot": {
        "label": "Aged snapshot",
        "category": "Lifecycle",
        "impact": "Low",
        "confidence": "Review",
        "reason": (
            "The snapshot exceeds the configured age threshold. Confirm backup, "
            "legal, and recovery retention requirements before changing it."
        ),
    },
    "premium_snapshot": {
        "label": "Premium managed-disk snapshot",
        "category": "Cost",
        "impact": "High",
        "confidence": "High",
        "reason": (
            "The snapshot uses Premium storage. Review whether Standard snapshot "
            "storage can satisfy the recovery requirement at lower cost."
        ),
    },
    "premium_disk_underutilized_review": {
        "label": "Attached Premium disk utilization review",
        "category": "Cost",
        "impact": "Medium",
        "confidence": "Review",
        "reason": (
            "Sustained disk telemetry is below the configured review thresholds "
            "for an attached Premium disk. Validate workload, burst requirements, "
            "latency, capacity, and recovery expectations before considering a "
            "lower-cost storage tier. This is review-only until an approved target "
            "SKU and price comparison are available."
        ),
    },
    "storage_gpv1_modernization": {
        "label": "GPv1 storage account",
        "category": "Modernization",
        "impact": "Low",
        "confidence": "Review",
        "reason": (
            "The storage account uses the legacy GPv1 kind. Review migration to GPv2 "
            "for current lifecycle, tiering, and reservation capabilities."
        ),
    },
    "missing_allocation_tags": {
        "label": "Missing allocation tags",
        "category": "Governance",
        "impact": "Low",
        "confidence": "Review",
        "reason": (
            "The resource has no tags available for ownership or cost allocation. "
            "Validate the required tagging policy for this resource type."
        ),
    },
    "snapshot_source_deleted": {
        "label": "Snapshot source disk no longer exists",
        "category": "Lifecycle",
        "impact": "Medium",
        "confidence": "Medium",
        "reason": (
            "The snapshot references a source managed disk that is no longer present. "
            "Confirm that the snapshot still has an approved recovery purpose."
        ),
    },
    "public_ip_unattached": {
        "label": "Unattached Public IP",
        "category": "Cost",
        "impact": "Medium",
        "confidence": "High",
        "reason": (
            "The Public IP has no IP configuration attachment. Confirm it is not "
            "reserved for an approved future use."
        ),
    },
    "public_ip_orphan_nic": {
        "label": "Public IP on orphaned NIC",
        "category": "Cost",
        "impact": "Medium",
        "confidence": "Medium",
        "reason": (
            "The Public IP is attached to a network interface that has no VM or "
            "private-endpoint owner."
        ),
    },
    "public_ip_deallocated_vm": {
        "label": "Public IP on deallocated VM",
        "category": "Cost exposure",
        "impact": "Low",
        "confidence": "Review",
        "reason": (
            "The Public IP is associated with a deallocated VM and may continue to "
            "incur charges. Validate whether the address must remain reserved."
        ),
    },
    "empty_standard_load_balancer": {
        "label": "Standard Load Balancer without targets",
        "category": "Cost",
        "impact": "Medium",
        "confidence": "Medium",
        "reason": (
            "The Standard Load Balancer has no backend targets. Confirm whether the "
            "resource is still required."
        ),
    },
    "empty_application_gateway": {
        "label": "Application Gateway without targets",
        "category": "Cost",
        "impact": "High",
        "confidence": "Medium",
        "reason": (
            "The Application Gateway has no backend targets. Validate configuration "
            "and deployment intent before removal."
        ),
    },
    "vnet_gateway_no_connections": {
        "label": "Virtual Network Gateway without connections",
        "category": "Cost",
        "impact": "High",
        "confidence": "Review",
        "reason": (
            "The gateway has no connection resources and no point-to-site "
            "configuration. Confirm it is not reserved for an approved design."
        ),
    },
    "empty_paid_app_service_plan": {
        "label": "Paid App Service plan without apps",
        "category": "Cost",
        "impact": "High",
        "confidence": "High",
        "reason": (
            "The paid App Service plan has no associated apps but can continue to "
            "reserve and bill compute instances."
        ),
    },
    "windows_ahb_eligibility_review": {
        "label": "Windows Azure Hybrid Benefit eligibility review",
        "category": "Rate optimization",
        "impact": "Medium",
        "confidence": "Review",
        "reason": (
            "This Windows Server VM or scale set does not report Azure Hybrid "
            "Benefit as enabled. Confirm that eligible on-premises licenses "
            "exist before changing its licensing configuration."
        ),
        "upstreamRule": "Recommendations-Microsoft-VMsWithoutAHB",
    },
    "sql_vm_ahb_eligibility_review": {
        "label": "SQL VM Azure Hybrid Benefit eligibility review",
        "category": "Rate optimization",
        "impact": "High",
        "confidence": "Review",
        "reason": (
            "This SQL virtual machine does not report Azure Hybrid Benefit as "
            "enabled. Confirm SQL edition and eligible license entitlement "
            "before changing its licensing configuration."
        ),
        "upstreamRule": "Recommendations-Microsoft-SQLVMsWithoutAHB",
    },
    "unused_network_interface": {
        "label": "Unused network interface",
        "category": "Lifecycle",
        "impact": "Low",
        "confidence": "High",
        "reason": (
            "The network interface has no virtual machine or private endpoint "
            "owner. Confirm it is not held for an approved recovery workflow."
        ),
    },
    "idle_nat_gateway": {
        "label": "NAT Gateway without subnets",
        "category": "Cost",
        "impact": "Medium",
        "confidence": "High",
        "reason": (
            "The NAT Gateway has no subnet associations and can continue to "
            "incur hourly and public IP charges."
        ),
    },
    "empty_availability_set": {
        "label": "Empty availability set",
        "category": "Lifecycle",
        "impact": "Low",
        "confidence": "High",
        "reason": (
            "No virtual machines reference this availability set. Confirm "
            "deployment intent before retiring the unused container."
        ),
    },
    "orphaned_network_security_group": {
        "label": "Orphaned network security group",
        "category": "Governance",
        "impact": "Low",
        "confidence": "High",
        "reason": (
            "The network security group is not associated with a subnet or "
            "network interface. Validate ownership before retirement."
        ),
    },
    "basic_public_ip_retired": {
        "label": "Retired Basic SKU Public IP",
        "category": "Service retirement",
        "impact": "High",
        "confidence": "High",
        "reason": (
            "Microsoft retired Basic SKU Public IP addresses on "
            "2025-09-30. Validate the supported migration path and dependent "
            "resources before upgrading to Standard."
        ),
        "retirementDate": "2025-09-30",
        "referenceUrl": (
            "https://learn.microsoft.com/azure/virtual-network/"
            "ip-services/public-ip-addresses"
        ),
    },
    "basic_load_balancer_retired": {
        "label": "Retired Basic Azure Load Balancer",
        "category": "Service retirement",
        "impact": "High",
        "confidence": "High",
        "reason": (
            "Microsoft retired Basic Azure Load Balancer on 2025-09-30. "
            "Validate frontend, backend, probe, NAT, and outbound behavior "
            "before migrating to Standard."
        ),
        "retirementDate": "2025-09-30",
        "referenceUrl": (
            "https://learn.microsoft.com/azure/load-balancer/"
            "load-balancer-best-practices"
        ),
    },
}


RESOURCE_STATE_QUERY = r"""
Resources
| extend
    resourceType=tolower(tostring(type)),
    powerState=tostring(properties.extended.instanceView.powerState.code),
    managedByRef=coalesce(tostring(managedBy), tostring(properties.managedBy)),
    diskState=tostring(properties.diskState),
    tagsText=tostring(tags),
    timeCreated=todatetime(properties.timeCreated),
    skuName=tostring(sku.name)
| extend ruleIds=pack_array(
    iff(resourceType == 'microsoft.compute/virtualmachines'
        and powerState =~ 'PowerState/stopped', 'stopped_allocated_vm', ''),
    iff(resourceType == 'microsoft.compute/virtualmachines'
        and powerState =~ 'PowerState/deallocated',
        'deallocated_vm_residual_cost', ''),
    iff(resourceType == 'microsoft.compute/disks'
        and isempty(managedByRef)
        and (diskState =~ 'Unattached' or isempty(diskState))
        and not(name matches regex @'(?i).*(-ASRReplica|asrseeddisk).*')
        and not(tagsText contains 'ASR'), 'unattached_disk', ''),
    iff(resourceType == 'microsoft.compute/snapshots'
        and timeCreated < ago(__SNAPSHOT_AGE_DAYS__d), 'aged_snapshot', ''),
    iff(resourceType == 'microsoft.compute/snapshots'
        and skuName contains 'Premium', 'premium_snapshot', ''),
    iff(resourceType == 'microsoft.storage/storageaccounts'
        and tostring(kind) =~ 'Storage', 'storage_gpv1_modernization', ''),
    iff(resourceType == 'microsoft.network/publicipaddresses'
        and skuName =~ 'Basic', 'basic_public_ip_retired', ''),
    iff(resourceType == 'microsoft.network/loadbalancers'
        and skuName =~ 'Basic', 'basic_load_balancer_retired', ''),
    iff(isnull(tags) or array_length(bag_keys(tags)) == 0,
        'missing_allocation_tags', '')
)
| mv-expand ruleId=ruleIds to typeof(string)
| where isnotempty(ruleId)
| project
    ruleId, resourceId=tolower(id), relatedResourceId='',
    subscriptionId=tolower(subscriptionId), resourceGroup,
    region=location, resourceType, resourceName=name,
    powerState, diskState, skuName, timeCreated
""".strip()


SNAPSHOT_SOURCE_QUERY = r"""
Resources
| where type =~ 'microsoft.compute/snapshots'
| extend sourceResourceId=tolower(tostring(properties.creationData.sourceResourceId))
| where isnotempty(sourceResourceId)
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.compute/disks'
    | project liveDiskId=tolower(id)
) on $left.sourceResourceId == $right.liveDiskId
| where isempty(liveDiskId)
| project
    ruleId='snapshot_source_deleted', resourceId=tolower(id),
    relatedResourceId=sourceResourceId, subscriptionId=tolower(subscriptionId),
    resourceGroup, region=location, resourceType=tolower(tostring(type)),
    resourceName=name, timeCreated=todatetime(properties.timeCreated),
    skuName=tostring(sku.name)
""".strip()


PUBLIC_IP_QUERY = r"""
Resources
| where type =~ 'microsoft.network/publicipaddresses'
| project
    pipId=tolower(id), subscriptionId=tolower(subscriptionId), resourceGroup,
    region=location, resourceName=name, resourceType=tolower(tostring(type)),
    skuName=tostring(sku.name),
    ipConfigId=tolower(tostring(properties.ipConfiguration.id))
| extend attachedNicId=iff(
    ipConfigId has '/networkinterfaces/',
    substring(ipConfigId, 0, indexof(ipConfigId, '/ipconfigurations/')),
    ''
)
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.network/networkinterfaces'
    | project
        nicId=tolower(id),
        attachedVmId=tolower(tostring(properties.virtualMachine.id)),
        privateEndpointId=tolower(tostring(properties.privateEndpoint.id))
) on $left.attachedNicId == $right.nicId
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.compute/virtualmachines'
    | mv-expand nic=properties.networkProfile.networkInterfaces
    | project
        vmNicId=tolower(tostring(nic.id)),
        vmId=tolower(id),
        vmPowerState=tostring(properties.extended.instanceView.powerState.code)
) on $left.attachedNicId == $right.vmNicId
| extend ruleId=case(
    isempty(ipConfigId), 'public_ip_unattached',
    isnotempty(attachedNicId)
        and isempty(attachedVmId)
        and isempty(privateEndpointId), 'public_ip_orphan_nic',
    isnotempty(attachedNicId)
        and vmPowerState =~ 'PowerState/deallocated',
        'public_ip_deallocated_vm',
    ''
)
| where isnotempty(ruleId)
| project
    ruleId, resourceId=pipId,
    relatedResourceId=coalesce(vmId, attachedNicId),
    subscriptionId, resourceGroup, region, resourceType, resourceName,
    skuName, ipConfigId, attachedNicId, attachedVmId, vmPowerState
""".strip()


EMPTY_FRONTEND_QUERY = r"""
Resources
| where type in~ (
    'microsoft.network/loadbalancers',
    'microsoft.network/applicationgateways'
)
| where type !~ 'microsoft.network/loadbalancers'
    or tostring(sku.name) =~ 'Standard'
| extend pools=properties.backendAddressPools
| extend pools=iff(
    isnull(pools) or array_length(pools) == 0,
    dynamic([{}]),
    pools
)
| mv-expand pool=pools to typeof(dynamic)
| extend targetCount=
    coalesce(array_length(pool.properties.backendIPConfigurations), 0)
    + coalesce(array_length(pool.properties.loadBalancerBackendAddresses), 0)
    + coalesce(array_length(pool.properties.backendAddresses), 0)
| summarize totalTargets=sum(targetCount)
    by id, subscriptionId, resourceGroup, location, name, type
| where totalTargets == 0
| extend ruleId=iff(
    type =~ 'microsoft.network/loadbalancers',
    'empty_standard_load_balancer',
    'empty_application_gateway'
)
| project
    ruleId, resourceId=tolower(id), relatedResourceId='',
    subscriptionId=tolower(subscriptionId), resourceGroup,
    region=location, resourceType=tolower(tostring(type)),
    resourceName=name, totalTargets
""".strip()


VNET_GATEWAY_QUERY = r"""
Resources
| where type =~ 'microsoft.network/virtualnetworkgateways'
| extend
    gatewayId=tolower(id),
    hasPointToSite=isnotempty(properties.vpnClientConfiguration)
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.network/connections'
    | extend gatewayIds=pack_array(
        tolower(tostring(properties.virtualNetworkGateway1.id)),
        tolower(tostring(properties.virtualNetworkGateway2.id))
    )
    | mv-expand connectedGatewayId=gatewayIds to typeof(string)
    | where isnotempty(connectedGatewayId)
    | project connectedGatewayId, connectionId=tolower(id)
) on $left.gatewayId == $right.connectedGatewayId
| summarize
    connectionCount=countif(isnotempty(connectionId)),
    hasPointToSite=any(hasPointToSite),
    subscriptionId=any(subscriptionId),
    resourceGroup=any(resourceGroup),
    region=any(location),
    resourceName=any(name),
    resourceType=any(type)
    by gatewayId
| where connectionCount == 0 and hasPointToSite == false
| project
    ruleId='vnet_gateway_no_connections', resourceId=gatewayId,
    relatedResourceId='', subscriptionId=tolower(subscriptionId),
    resourceGroup, region, resourceType=tolower(tostring(resourceType)),
    resourceName, connectionCount
""".strip()


APP_SERVICE_PLAN_QUERY = r"""
Resources
| where type =~ 'microsoft.web/serverfarms'
| extend
    serverFarmId=tolower(id),
    skuTier=tostring(sku.tier),
    skuName=tostring(sku.name)
| where skuTier !in~ ('Free', 'Shared', 'Dynamic')
    and skuName !in~ ('F1', 'D1', 'Y1')
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.web/sites'
    | extend siteServerFarmId=tolower(tostring(properties.serverFarmId))
    | summarize siteCount=count() by siteServerFarmId
) on $left.serverFarmId == $right.siteServerFarmId
| where coalesce(siteCount, 0) == 0
| project
    ruleId='empty_paid_app_service_plan', resourceId=serverFarmId,
    relatedResourceId='', subscriptionId=tolower(subscriptionId),
    resourceGroup, region=location, resourceType=tolower(tostring(type)),
    resourceName=name, skuTier, skuName, siteCount=coalesce(siteCount, 0)
""".strip()


# Adapted from Microsoft FinOps Toolkit v14 FinOps hubs recommendation queries.
# Flux deliberately emits review-only findings because license entitlement is
# not observable from Azure Resource Graph.
WINDOWS_AHB_QUERY = r"""
Resources
| where type in~ (
    'microsoft.compute/virtualmachines',
    'microsoft.compute/virtualmachinescalesets'
)
| extend
    directOsType=tostring(properties.storageProfile.osDisk.osType),
    scaleOsType=tostring(
        properties.virtualMachineProfile.storageProfile.osDisk.osType
    ),
    directPublisher=tostring(
        properties.storageProfile.imageReference.publisher
    ),
    scalePublisher=tostring(
        properties.virtualMachineProfile.storageProfile.imageReference.publisher
    ),
    directLicense=tostring(properties.licenseType),
    scaleLicense=tostring(properties.virtualMachineProfile.licenseType),
    vmSize=coalesce(
        tostring(properties.hardwareProfile.vmSize),
        tostring(properties.virtualMachineProfile.hardwareProfile.vmSize)
    )
| extend
    osType=coalesce(directOsType, scaleOsType),
    publisher=coalesce(directPublisher, scalePublisher),
    licenseType=coalesce(directLicense, scaleLicense)
| where osType =~ 'Windows'
    and publisher !in~ ('microsoftwindowsdesktop', 'microsoftvisualstudio')
    and licenseType !startswith 'Windows'
| project
    ruleId='windows_ahb_eligibility_review',
    resourceId=tolower(id), relatedResourceId='',
    subscriptionId=tolower(subscriptionId), resourceGroup,
    region=location, resourceType=tolower(tostring(type)),
    resourceName=name, osType, publisher, licenseType, vmSize,
    upstreamRule='Recommendations-Microsoft-VMsWithoutAHB',
    upstreamVersion='v14'
""".strip()


SQL_VM_AHB_QUERY = r"""
Resources
| where type =~ 'microsoft.sqlvirtualmachine/sqlvirtualmachines'
| extend
    licenseType=tostring(properties.sqlServerLicenseType),
    sqlSku=tostring(properties.sqlImageSku),
    sqlVersion=tostring(properties.sqlImageOffer),
    relatedVmId=tolower(tostring(properties.virtualMachineResourceId))
| where licenseType !~ 'AHUB'
    and sqlSku !in~ ('Developer', 'Express')
| project
    ruleId='sql_vm_ahb_eligibility_review',
    resourceId=tolower(id), relatedResourceId=relatedVmId,
    subscriptionId=tolower(subscriptionId), resourceGroup,
    region=location, resourceType=tolower(tostring(type)),
    resourceName=name, licenseType, sqlSku, sqlVersion,
    upstreamRule='Recommendations-Microsoft-SQLVMsWithoutAHB',
    upstreamVersion='v14'
""".strip()


UNUSED_NIC_QUERY = r"""
Resources
| where type =~ 'microsoft.network/networkinterfaces'
| extend
    vmId=tolower(tostring(properties.virtualMachine.id)),
    privateEndpointId=tolower(tostring(properties.privateEndpoint.id))
| where isempty(vmId) and isempty(privateEndpointId)
| project
    ruleId='unused_network_interface', resourceId=tolower(id),
    relatedResourceId='', subscriptionId=tolower(subscriptionId),
    resourceGroup, region=location, resourceType=tolower(tostring(type)),
    resourceName=name, vmId, privateEndpointId
""".strip()


IDLE_NAT_GATEWAY_QUERY = r"""
Resources
| where type =~ 'microsoft.network/natgateways'
| extend subnetCount=coalesce(array_length(properties.subnets), 0)
| where subnetCount == 0
| project
    ruleId='idle_nat_gateway', resourceId=tolower(id),
    relatedResourceId='', subscriptionId=tolower(subscriptionId),
    resourceGroup, region=location, resourceType=tolower(tostring(type)),
    resourceName=name, subnetCount, skuName=tostring(sku.name)
""".strip()


EMPTY_AVAILABILITY_SET_QUERY = r"""
Resources
| where type =~ 'microsoft.compute/availabilitysets'
| extend availabilitySetId=tolower(id)
| join kind=leftouter (
    Resources
    | where type =~ 'microsoft.compute/virtualmachines'
    | project
        vmId=tolower(id),
        vmAvailabilitySetId=tolower(tostring(properties.availabilitySet.id))
) on $left.availabilitySetId == $right.vmAvailabilitySetId
| summarize
    virtualMachineCount=countif(isnotempty(vmId)),
    subscriptionId=any(subscriptionId),
    resourceGroup=any(resourceGroup), region=any(location),
    resourceName=any(name), resourceType=any(type)
    by availabilitySetId
| where virtualMachineCount == 0
| project
    ruleId='empty_availability_set', resourceId=availabilitySetId,
    relatedResourceId='', subscriptionId=tolower(subscriptionId),
    resourceGroup, region, resourceType=tolower(tostring(resourceType)),
    resourceName, virtualMachineCount
""".strip()


ORPHANED_NSG_QUERY = r"""
Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| extend
    subnetCount=coalesce(array_length(properties.subnets), 0),
    networkInterfaceCount=coalesce(
        array_length(properties.networkInterfaces), 0
    )
| where subnetCount == 0 and networkInterfaceCount == 0
| project
    ruleId='orphaned_network_security_group', resourceId=tolower(id),
    relatedResourceId='', subscriptionId=tolower(subscriptionId),
    resourceGroup, region=location, resourceType=tolower(tostring(type)),
    resourceName=name, subnetCount, networkInterfaceCount
""".strip()


def _safe_kql_names(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value for value in values if re.fullmatch(r"[a-z0-9_.:/-]+", value))


def flux_intelligence_queries(
    snapshot_age_days: int = 30,
    required_tags: tuple[str, ...] = (),
    tag_excluded_types: tuple[str, ...] = (),
    finops_toolkit_ahb_enabled: bool = True,
) -> tuple[str, ...]:
    age_days = min(max(snapshot_age_days, 1), 3650)
    safe_tags = _safe_kql_names(required_tags)
    safe_types = _safe_kql_names(tag_excluded_types)
    if safe_tags:
        tag_condition = " or ".join(
            f"isempty(tostring(tags['{tag}']))" for tag in safe_tags
        )
    else:
        tag_condition = "isnull(tags) or array_length(bag_keys(tags)) == 0"
    exclusion = (
        " and resourceType !in~ ("
        + ", ".join(f"'{value}'" for value in safe_types)
        + ")"
        if safe_types
        else ""
    )
    resource_query = RESOURCE_STATE_QUERY.replace(
        "isnull(tags) or array_length(bag_keys(tags)) == 0,\n"
        "        'missing_allocation_tags', '')",
        f"({tag_condition}){exclusion},\n        'missing_allocation_tags', '')",
    )
    queries = (
        resource_query.replace(
            "__SNAPSHOT_AGE_DAYS__",
            str(age_days),
        ),
        SNAPSHOT_SOURCE_QUERY,
        PUBLIC_IP_QUERY,
        EMPTY_FRONTEND_QUERY,
        VNET_GATEWAY_QUERY,
        APP_SERVICE_PLAN_QUERY,
        UNUSED_NIC_QUERY,
        IDLE_NAT_GATEWAY_QUERY,
        EMPTY_AVAILABILITY_SET_QUERY,
        ORPHANED_NSG_QUERY,
    )
    return queries + (
        (WINDOWS_AHB_QUERY, SQL_VM_AHB_QUERY)
        if finops_toolkit_ahb_enabled
        else ()
    )


FLUX_INTELLIGENCE_QUERIES = flux_intelligence_queries()


def normalize_flux_intelligence(
    raw_findings: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    labels = {
        item["subscriptionId"].lower(): item.get("label") or item["subscriptionId"]
        for item in subscriptions
    }
    normalized: dict[str, dict[str, Any]] = {}
    for raw in raw_findings:
        rule_id = str(raw.get("ruleId") or "")
        rule = RULES.get(rule_id)
        resource_id = str(raw.get("resourceId") or "").lower()
        if not rule or not resource_id:
            continue
        subscription_id = str(raw.get("subscriptionId") or "").lower()
        resource_name = str(raw.get("resourceName") or resource_id.rsplit("/", 1)[-1])
        finding_id = f"{rule_id}:{resource_id}"
        normalized[finding_id] = {
            "findingId": finding_id,
            "ruleId": rule_id,
            "source": "flux_intelligence",
            "resourceId": resource_id,
            "relatedResourceId": str(raw.get("relatedResourceId") or "").lower(),
            "subscriptionId": subscription_id,
            "subscriptionName": labels.get(subscription_id, subscription_id),
            "resourceType": str(raw.get("resourceType") or "").lower(),
            "resourceGroup": str(raw.get("resourceGroup") or ""),
            "region": str(raw.get("region") or ""),
            "category": rule["category"],
            "impact": rule["impact"],
            "confidence": rule["confidence"],
            "title": f"{resource_name}: {rule['label']}",
            "reason": rule["reason"],
            "evidence": {
                **raw,
                **({
                    "upstream": {
                        "project": "Microsoft FinOps Toolkit",
                        "projectUrl": (
                            "https://github.com/microsoft/finops-toolkit"
                        ),
                        "version": "v14",
                        "license": "MIT",
                        "rule": rule["upstreamRule"],
                        "adaptation": (
                            "Flux review-only ARG adaptation; license "
                            "entitlement is not asserted."
                        ),
                    }
                } if rule.get("upstreamRule") else {}),
                **({
                    "retirement": {
                        "date": rule["retirementDate"],
                        "referenceUrl": rule["referenceUrl"],
                        "source": "Microsoft Learn",
                        "maintenance": (
                            "Versioned reference; review against Microsoft "
                            "retirement announcements each release."
                        ),
                    }
                } if rule.get("retirementDate") else {}),
            },
            "estimatedMonthlySavings": None,
            "savingsCurrency": "",
            "ruleVersion": FLUX_INTELLIGENCE_RULE_VERSION,
        }
    return list(normalized.values())
