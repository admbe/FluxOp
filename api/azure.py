from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from azure.identity import ManagedIdentityCredential

from .intelligence import (
    flux_intelligence_queries,
    normalize_flux_intelligence,
)


class AzureSyncError(RuntimeError):
    pass


def opportunity_for(resource: dict[str, Any]) -> tuple[str | None, str | None]:
    # Opportunity rules are evaluated independently by Flux Intelligence so a
    # resource can carry multiple findings. Keep these legacy columns empty.
    return None, None


def normalize_arg_resources(
    raw_resources: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    labels = {
        item["subscriptionId"].lower(): item.get("label") or item["subscriptionId"]
        for item in subscriptions
    }
    normalized = []
    for raw in raw_resources:
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        subscription_id = str(raw.get("subscriptionId", "")).lower()
        resource = {
            "resourceId": raw.get("id", ""),
            "name": raw.get("name", ""),
            "resourceType": str(raw.get("resourceType", "")).lower(),
            "subscriptionId": subscription_id,
            "subscriptionName": labels.get(subscription_id, subscription_id),
            "resourceGroup": raw.get("resourceGroup", ""),
            "region": raw.get("location", ""),
            "kind": raw.get("resourceKind") or raw.get("kind", ""),
            "sku": raw.get("vmSize") or raw.get("skuName") or "",
            "provisioningState": raw.get("provisioningState", ""),
            "managedBy": raw.get("managedBy", ""),
            "tags": tags,
            "estimatedMonthlyCost": None,
            "costSource": None,
            "utilizationPercent": None,
            "utilizationSource": None,
            "estimatedMonthlySavings": None,
            "raw": raw,
        }
        kind, reason = opportunity_for(resource)
        resource["opportunityKind"] = kind
        resource["opportunityReason"] = reason
        normalized.append(resource)
    return normalized


def normalize_advisor_recommendations(
    raw_recommendations: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    labels = {
        item["subscriptionId"].lower(): item.get("label") or item["subscriptionId"]
        for item in subscriptions
    }
    recommendations: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in raw_recommendations:
        resource_id = str(raw.get("resourceId") or "").lower()
        subscription_id = str(raw.get("subscriptionId") or "").lower()
        if not resource_id and subscription_id:
            resource_id = f"/subscriptions/{subscription_id}"
        subscription_scope = resource_id.rstrip("/") == (
            f"/subscriptions/{subscription_id}"
        )
        extended = (
            raw.get("extendedProperties")
            if isinstance(raw.get("extendedProperties"), dict)
            else {}
        )
        extended_by_name = {
            str(key).casefold(): value
            for key, value in extended.items()
            if value is not None and value != ""
        }

        def extended_value(*names: str) -> str:
            for name in names:
                value = extended_by_name.get(name.casefold())
                if value is not None and value != "":
                    return str(value)
            return ""

        action_parts = []
        for label, value in (
            (
                "SKU",
                extended_value(
                    "recommendedSku", "targetSku", "displaySKU", "skuName",
                    "vmSize", "productName", "instanceFlexibilityGroup",
                ),
            ),
            (
                "region",
                extended_value("region", "location", "armRegionName"),
            ),
            ("term", extended_value("term")),
            (
                "quantity",
                extended_value(
                    "recommendedQuantity", "targetResourceCount", "quantity",
                ),
            ),
            (
                "lookback",
                extended_value("lookbackPeriod"),
            ),
        ):
            if value:
                action_parts.append(f"{label} {value}")
        action_context = " · ".join(action_parts)
        learn_more_link = str(raw.get("learnMoreLink") or "")
        if not learn_more_link.lower().startswith(("https://", "http://")):
            learn_more_link = ""
        if not subscription_id and resource_id.startswith("/subscriptions/"):
            subscription_id = resource_id.split("/", 3)[2]
        recommendation_id = str(
            raw.get("recommendationId") or raw.get("id") or ""
        )
        if not recommendation_id:
            recommendation_id = "|".join(
                [
                    subscription_id,
                    resource_id,
                    str(raw.get("recommendationTypeId") or ""),
                    str(raw.get("problem") or ""),
                    action_context,
                ]
            )
        category = str(raw.get("category") or "Advisor")
        problem = str(raw.get("problem") or "")
        solution = str(raw.get("solution") or "")
        recommendation_type_id = str(raw.get("recommendationTypeId") or "")
        current_sku = str(
            raw.get("currentSku")
            or raw.get("currentSkuName")
            or raw.get("vmSize")
            or ""
        )
        recommended_sku = str(
            raw.get("recommendedSku")
            or raw.get("targetSku")
            or raw.get("recommendedSkuName")
            or raw.get("recommendedVMSize")
            or raw.get("targetVmSize")
            or ""
        )
        normalized = {
            "recommendationId": recommendation_id,
            "resourceId": resource_id,
            "subscriptionId": subscription_id,
            "subscriptionName": labels.get(subscription_id, subscription_id),
            "category": category,
            "impact": str(raw.get("impact") or ""),
            "problem": problem,
            "solution": solution,
            "resourceType": str(raw.get("resourceType") or "").lower(),
            "savingsAmount": float(raw["savingsAmount"])
            if raw.get("savingsAmount") not in {None, ""}
            else None,
            "savingsCurrency": str(raw.get("savingsCurrency") or ""),
            "annualSavingsAmount": float(raw["annualSavingsAmount"])
            if raw.get("annualSavingsAmount") not in {None, ""}
            else None,
            "recommendationTypeId": recommendation_type_id,
            "currentSku": current_sku,
            "recommendedSku": recommended_sku,
            "lastUpdated": str(raw.get("lastUpdated") or ""),
            "learnMoreLink": learn_more_link,
            "raw": {
                **raw,
                "_fluxScopeType": (
                    "subscription" if subscription_scope else "resource"
                ),
                "_fluxActionContext": action_context,
            },
        }
        semantic_key = tuple(
            value.casefold()
            for value in (
                subscription_id,
                resource_id,
                category,
                recommendation_type_id,
                problem,
                solution,
                current_sku,
                recommended_sku,
                action_context,
            )
        )
        existing = recommendations.get(semantic_key)
        if existing is None or (
            normalized["lastUpdated"],
            normalized["annualSavingsAmount"] or 0,
            normalized["savingsAmount"] or 0,
        ) > (
            existing["lastUpdated"],
            existing["annualSavingsAmount"] or 0,
            existing["savingsAmount"] or 0,
        ):
            recommendations[semantic_key] = normalized
    return list(recommendations.values())


ARG_QUERY = """
Resources
| extend
    skuName = tostring(sku.name),
    provisioningState = tostring(properties.provisioningState),
    vmSize = tostring(properties.hardwareProfile.vmSize),
    osType = tostring(properties.storageProfile.osDisk.osType),
    licenseType = tostring(properties.licenseType)
| project id, name, resourceType=tostring(type), subscriptionId, resourceGroup,
    location, resourceKind=tostring(kind), skuName, vmSize, osType, licenseType,
    provisioningState, managedBy=tostring(managedBy), tags
| order by subscriptionId asc, resourceType asc, name asc
""".strip()

ADVISOR_QUERY = """
AdvisorResources
| where type =~ 'microsoft.advisor/recommendations'
| extend
    recommendationStatus = tostring(properties.recommendationStatus),
    category = tostring(properties.category),
    impact = tostring(properties.impact),
    problem = tostring(properties.shortDescription.problem),
    solution = tostring(properties.shortDescription.solution),
    resourceId = tostring(properties.resourceMetadata.resourceId),
    resourceType = tostring(properties.impactedField),
    savingsAmount = todouble(properties.extendedProperties.savingsAmount),
    annualSavingsAmount = todouble(properties.extendedProperties.annualSavingsAmount),
    savingsCurrency = tostring(properties.extendedProperties.savingsCurrency),
    recommendationTypeId = tostring(properties.recommendationTypeId),
    currentSku = tostring(properties.extendedProperties.currentSku),
    recommendedSku = tostring(properties.extendedProperties.recommendedSku),
    targetSku = tostring(properties.extendedProperties.targetSku),
    extendedProperties = properties.extendedProperties,
    lastUpdated = tostring(properties.lastUpdated),
    learnMoreLink = tostring(properties.learnMoreLink)
| where category in~ ('Cost', 'Performance')
| where recommendationStatus in~ ('New', 'InProgress')
| where isempty(properties.tracked) or properties.tracked == false
| project recommendationId=tostring(name), id, subscriptionId, resourceId,
    resourceType, category, impact, problem, solution, savingsAmount,
    annualSavingsAmount, savingsCurrency, recommendationTypeId, currentSku,
    recommendedSku, targetSku, extendedProperties, lastUpdated, learnMoreLink
| order by category asc, impact asc, recommendationId asc
""".strip()

POLICY_POSTURE_QUERY = """
PolicyResources
| where type =~ 'microsoft.policyinsights/policystates'
| extend
    assignmentId=tolower(tostring(properties.policyAssignmentId)),
    assignmentName=tostring(properties.policyAssignmentName),
    definitionId=tolower(tostring(properties.policyDefinitionId)),
    complianceState=tostring(properties.complianceState),
    resourceId=tolower(tostring(properties.resourceId))
| where isnotempty(assignmentId)
| summarize
    evaluatedCount=count(),
    compliantCount=countif(complianceState =~ 'Compliant'),
    nonCompliantCount=countif(complianceState =~ 'NonCompliant'),
    exemptCount=countif(complianceState =~ 'Exempt'),
    unknownCount=countif(
        complianceState !in~ ('Compliant', 'NonCompliant', 'Exempt')
    ),
    resourceCount=dcount(resourceId),
    definitionCount=dcount(definitionId)
    by subscriptionId=tolower(subscriptionId), assignmentId, assignmentName
| order by nonCompliantCount desc, assignmentName asc
""".strip()

POLICY_RESOURCE_QUERY = """
PolicyResources
| where type =~ 'microsoft.policyinsights/policystates'
| extend
    assignmentId=tolower(tostring(properties.policyAssignmentId)),
    assignmentName=tostring(properties.policyAssignmentName),
    definitionId=tolower(tostring(properties.policyDefinitionId)),
    definitionName=tostring(properties.policyDefinitionName),
    complianceState=tostring(properties.complianceState),
    resourceId=tolower(tostring(properties.resourceId)),
    resourceType=tolower(tostring(properties.resourceType)),
    resourceLocation=tostring(properties.resourceLocation),
    exemptionId=tolower(tostring(properties.policyExemptionId)),
    timestamp=todatetime(properties.timestamp)
| where isnotempty(assignmentId) and isnotempty(resourceId)
| where complianceState in~ ('NonCompliant', 'Exempt')
| project subscriptionId=tolower(subscriptionId), assignmentId,
    assignmentName, definitionId, definitionName, complianceState,
    resourceId, resourceType, resourceLocation, exemptionId, timestamp
| order by complianceState desc, assignmentName asc, resourceId asc
""".strip()


def normalize_policy_posture(
    rows: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    labels = {
        item["subscriptionId"].lower(): item.get("label") or item["subscriptionId"]
        for item in subscriptions
    }
    return [
        {
            "subscriptionId": str(item.get("subscriptionId") or "").lower(),
            "subscriptionName": labels.get(
                str(item.get("subscriptionId") or "").lower(),
                str(item.get("subscriptionId") or "").lower(),
            ),
            "assignmentId": str(item.get("assignmentId") or "").lower(),
            "assignmentName": str(
                item.get("assignmentName") or item.get("assignmentId") or ""
            ),
            "evaluatedCount": int(item.get("evaluatedCount") or 0),
            "compliantCount": int(item.get("compliantCount") or 0),
            "nonCompliantCount": int(item.get("nonCompliantCount") or 0),
            "exemptCount": int(item.get("exemptCount") or 0),
            "unknownCount": int(item.get("unknownCount") or 0),
            "resourceCount": int(item.get("resourceCount") or 0),
            "definitionCount": int(item.get("definitionCount") or 0),
        }
        for item in rows
        if item.get("assignmentId")
    ]

def normalize_policy_resources(
    rows: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    labels = {
        item["subscriptionId"].lower(): item.get("label") or item["subscriptionId"]
        for item in subscriptions
    }
    return [
        {
            "subscriptionId": str(item.get("subscriptionId") or "").lower(),
            "subscriptionName": labels.get(
                str(item.get("subscriptionId") or "").lower(),
                str(item.get("subscriptionId") or "").lower(),
            ),
            "assignmentId": str(item.get("assignmentId") or "").lower(),
            "assignmentName": str(item.get("assignmentName") or ""),
            "definitionId": str(item.get("definitionId") or "").lower(),
            "definitionName": str(item.get("definitionName") or ""),
            "complianceState": str(item.get("complianceState") or "Unknown"),
            "resourceId": str(item.get("resourceId") or "").lower(),
            "resourceName": str(item.get("resourceId") or "").rsplit("/", 1)[-1],
            "resourceType": str(item.get("resourceType") or "").lower(),
            "region": str(item.get("resourceLocation") or ""),
            "exemptionId": str(item.get("exemptionId") or "").lower(),
            "evaluatedAt": str(item.get("timestamp") or ""),
        }
        for item in rows
        if item.get("assignmentId") and item.get("resourceId")
    ]


class LocalPowerShellArgProvider:
    def __init__(
        self,
        executable: str = "pwsh",
        timeout_seconds: int = 180,
        intelligence_snapshot_age_days: int = 30,
        intelligence_required_tags: tuple[str, ...] = (),
        intelligence_tag_excluded_types: tuple[str, ...] = (),
        finops_toolkit_ahb_enabled: bool = True,
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self._latest_advisor: list[dict[str, Any]] = []
        self._latest_advisor_error = ""
        self._latest_intelligence: list[dict[str, Any]] = []
        self._latest_intelligence_error = ""
        self._latest_policy: list[dict[str, Any]] = []
        self._latest_policy_resources: list[dict[str, Any]] = []
        self._latest_policy_error = ""
        self.intelligence_queries = flux_intelligence_queries(
            intelligence_snapshot_age_days,
            intelligence_required_tags,
            intelligence_tag_excluded_types,
            finops_toolkit_ahb_enabled,
        )

    def _command(self) -> str:
        for candidate in [self.executable, "pwsh", "powershell"]:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        raise AzureSyncError(
            "PowerShell was not found. Install PowerShell and the Az.Accounts module."
        )

    def fetch(self, integration: dict[str, Any]) -> list[dict[str, Any]]:
        subscriptions = integration.get("subscriptions", [])
        if not subscriptions:
            raise AzureSyncError("Add at least one Azure subscription before synchronizing.")
        payload = json.dumps(
            {
                "tenantId": integration.get("tenantId", ""),
                "subscriptions": subscriptions,
            },
            separators=(",", ":"),
        )
        intelligence_payload = json.dumps(
            list(self.intelligence_queries),
            separators=(",", ":"),
        )
        script = r"""
$ErrorActionPreference = 'Stop'
$WarningPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$config = @'
__CONFIG__
'@ | ConvertFrom-Json

if (-not (Get-Command Get-AzContext -ErrorAction SilentlyContinue)) {
    throw 'Azure PowerShell is unavailable. Install Az.Accounts and run Connect-AzAccount.'
}
if (-not (Get-Command Invoke-AzRestMethod -ErrorAction SilentlyContinue)) {
    throw 'Invoke-AzRestMethod is unavailable. Update Az.Accounts.'
}

$context = Get-AzContext -ErrorAction SilentlyContinue
if (-not $context -or -not $context.Account) {
    throw 'No Azure session found. Run Connect-AzAccount before synchronizing.'
}
$activeTenantId = [string]$context.Tenant.Id
if ($config.tenantId -and $activeTenantId -and $config.tenantId -ne $activeTenantId) {
    throw "Active tenant $activeTenantId does not match configured tenant $($config.tenantId)."
}

$subscriptionIds = @($config.subscriptions | ForEach-Object { [string]$_.subscriptionId })
$resourceQuery = @"
__ARG_QUERY__
"@
$advisorQuery = @"
__ADVISOR_QUERY__
"@
$policyQuery = @"
__POLICY_QUERY__
"@
$policyResourceQuery = @"
__POLICY_RESOURCE_QUERY__
"@
$intelligenceQueries = @'
__INTELLIGENCE_QUERIES__
'@ | ConvertFrom-Json

$path = '/providers/Microsoft.ResourceGraph/resources?api-version=2024-04-01'
function Invoke-GraphQuery([string]$query) {
    $rows = @()
    $skipToken = $null
    do {
        $body = @{
            subscriptions = $subscriptionIds
            query = $query
            options = @{ resultFormat = 'objectArray'; '$top' = 1000 }
        }
        if ($skipToken) { $body.options['$skipToken'] = $skipToken }
        $response = Invoke-AzRestMethod -Path $path -Method POST -Payload ($body | ConvertTo-Json -Depth 20 -Compress)
        $result = $response.Content | ConvertFrom-Json
        if ($result.error) { throw [string]$result.error.message }
        $rows += @($result.data)
        $skipToken = $result.'$skipToken'
    } while ($skipToken)
    return @($rows)
}

$advisorRows = @()
$advisorError = ''
try {
    $advisorRows = @(Invoke-GraphQuery $advisorQuery)
} catch {
    $advisorError = [string]$_.Exception.Message
}

$intelligenceRows = @()
$intelligenceError = ''
try {
    foreach ($intelligenceQuery in $intelligenceQueries) {
        $intelligenceRows += @(Invoke-GraphQuery ([string]$intelligenceQuery))
    }
} catch {
    $intelligenceRows = @()
    $intelligenceError = [string]$_.Exception.Message
}

$policyRows = @()
$policyResourceRows = @()
$policyError = ''
try {
    $policyRows = @(Invoke-GraphQuery $policyQuery)
    $policyResourceRows = @(Invoke-GraphQuery $policyResourceQuery)
} catch {
    $policyError = [string]$_.Exception.Message
}

[ordered]@{
    tenantId = $activeTenantId
    resources = @(Invoke-GraphQuery $resourceQuery)
    advisorRecommendations = $advisorRows
    advisorError = $advisorError
    intelligenceFindings = $intelligenceRows
    intelligenceError = $intelligenceError
    policyPosture = $policyRows
    policyResources = $policyResourceRows
    policyError = $policyError
} | ConvertTo-Json -Depth 30 -Compress
""".replace("__CONFIG__", payload).replace("__ARG_QUERY__", ARG_QUERY).replace(
            "__ADVISOR_QUERY__", ADVISOR_QUERY
        ).replace(
            "__POLICY_QUERY__", POLICY_POSTURE_QUERY
        ).replace(
            "__POLICY_RESOURCE_QUERY__", POLICY_RESOURCE_QUERY
        ).replace(
            "__INTELLIGENCE_QUERIES__", intelligence_payload
        )
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".ps1", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(script)
                temp_path = Path(handle.name)
            result = subprocess.run(
                [
                    self._command(),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(temp_path),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise AzureSyncError(
                f"Azure Resource Graph synchronization exceeded {self.timeout_seconds} seconds."
            ) from error
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise AzureSyncError(message or f"PowerShell exited with {result.returncode}.")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AzureSyncError("Azure PowerShell returned an invalid JSON response.") from error
        self._latest_advisor = normalize_advisor_recommendations(
            response.get("advisorRecommendations", []),
            subscriptions,
        )
        self._latest_advisor_error = str(response.get("advisorError") or "")
        self._latest_intelligence = normalize_flux_intelligence(
            response.get("intelligenceFindings", []),
            subscriptions,
        )
        self._latest_intelligence_error = str(
            response.get("intelligenceError") or ""
        )
        self._latest_policy = normalize_policy_posture(
            response.get("policyPosture", []),
            subscriptions,
        )
        self._latest_policy_resources = normalize_policy_resources(
            response.get("policyResources", []),
            subscriptions,
        )
        self._latest_policy_error = str(response.get("policyError") or "")
        return normalize_arg_resources(response.get("resources", []), subscriptions)

    def fetch_advisor(
        self,
        integration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self._latest_advisor_error:
            raise AzureSyncError(
                f"Azure Resource Graph could not collect Advisor recommendations: "
                f"{self._latest_advisor_error}"
            )
        return self._latest_advisor

    def fetch_intelligence(
        self,
        integration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self._latest_intelligence_error:
            raise AzureSyncError(
                f"Azure Resource Graph could not collect Flux Intelligence findings: "
                f"{self._latest_intelligence_error}"
            )
        return self._latest_intelligence

    def fetch_policy(self, integration: dict[str, Any]) -> list[dict[str, Any]]:
        if self._latest_policy_error:
            raise AzureSyncError(
                "Azure Resource Graph could not collect Policy posture: "
                f"{self._latest_policy_error}"
            )
        return self._latest_policy

    def fetch_policy_resources(
        self,
        integration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self._latest_policy_error:
            raise AzureSyncError(
                "Azure Resource Graph could not collect Policy resources: "
                f"{self._latest_policy_error}"
            )
        return self._latest_policy_resources


class ManagedIdentityArgProvider:
    def __init__(
        self,
        *,
        management_endpoint: str = "https://management.azure.com",
        client_id: str = "",
        timeout_seconds: int = 180,
        intelligence_snapshot_age_days: int = 30,
        intelligence_required_tags: tuple[str, ...] = (),
        intelligence_tag_excluded_types: tuple[str, ...] = (),
        finops_toolkit_ahb_enabled: bool = True,
        credential: ManagedIdentityCredential | None = None,
    ):
        self.management_endpoint = management_endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.intelligence_queries = flux_intelligence_queries(
            intelligence_snapshot_age_days,
            intelligence_required_tags,
            intelligence_tag_excluded_types,
            finops_toolkit_ahb_enabled,
        )
        self.credential = credential or ManagedIdentityCredential(
            client_id=client_id or None
        )

    def _access_token(self) -> str:
        try:
            return self.credential.get_token(
                f"{self.management_endpoint}/.default"
            ).token
        except Exception as error:
            raise AzureSyncError(
                "Managed identity could not obtain an Azure management token. "
                "Confirm that an identity is enabled on the App Service."
            ) from error

    def _query(
        self,
        *,
        subscription_ids: list[str],
        query: str,
        access_token: str,
    ) -> list[dict[str, Any]]:
        endpoint = (
            f"{self.management_endpoint}/providers/Microsoft.ResourceGraph/resources"
            "?api-version=2024-04-01"
        )
        rows: list[dict[str, Any]] = []
        skip_token = ""
        while True:
            options: dict[str, Any] = {
                "resultFormat": "objectArray",
                "$top": 1000,
            }
            if skip_token:
                options["$skipToken"] = skip_token
            body = json.dumps(
                {
                    "subscriptions": subscription_ids,
                    "query": query,
                    "options": options,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            request = Request(
                endpoint,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(detail)
                    azure_error = parsed.get("error", {})
                    messages = [
                        item
                        for item in [
                            azure_error.get("message"),
                            *[
                                entry.get("message")
                                for entry in azure_error.get("details", [])
                                if isinstance(entry, dict)
                            ],
                        ]
                        if item
                    ]
                    message = " ".join(dict.fromkeys(messages)) or detail
                except json.JSONDecodeError:
                    message = detail
                raise AzureSyncError(
                    f"Azure Resource Graph returned HTTP {error.code}: {message}"
                ) from error
            except (URLError, TimeoutError) as error:
                raise AzureSyncError(
                    f"Azure Resource Graph request failed: {error}"
                ) from error
            except json.JSONDecodeError as error:
                raise AzureSyncError(
                    "Azure Resource Graph returned an invalid JSON response."
                ) from error

            rows.extend(payload.get("data") or [])
            skip_token = payload.get("$skipToken") or ""
            if not skip_token:
                break
        return rows

    def fetch(self, integration: dict[str, Any]) -> list[dict[str, Any]]:
        subscriptions = integration.get("subscriptions", [])
        if not subscriptions:
            raise AzureSyncError("Add at least one Azure subscription before synchronizing.")
        subscription_ids = [
            item["subscriptionId"]
            for item in subscriptions
            if item.get("subscriptionId")
        ]
        rows = self._query(
            subscription_ids=subscription_ids,
            query=ARG_QUERY,
            access_token=self._access_token(),
        )
        return normalize_arg_resources(rows, subscriptions)

    def fetch_advisor(
        self,
        integration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        subscriptions = integration.get("subscriptions", [])
        subscription_ids = [
            item["subscriptionId"]
            for item in subscriptions
            if item.get("subscriptionId")
        ]
        rows = self._query(
            subscription_ids=subscription_ids,
            query=ADVISOR_QUERY,
            access_token=self._access_token(),
        )
        return normalize_advisor_recommendations(rows, subscriptions)

    def fetch_intelligence(
        self,
        integration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        subscriptions = integration.get("subscriptions", [])
        subscription_ids = [
            item["subscriptionId"]
            for item in subscriptions
            if item.get("subscriptionId")
        ]
        access_token = self._access_token()
        rows: list[dict[str, Any]] = []
        for query in self.intelligence_queries:
            rows.extend(
                self._query(
                    subscription_ids=subscription_ids,
                    query=query,
                    access_token=access_token,
                )
            )
        return normalize_flux_intelligence(rows, subscriptions)

    def fetch_policy(
        self,
        integration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        subscriptions = integration.get("subscriptions", [])
        subscription_ids = [
            item["subscriptionId"]
            for item in subscriptions
            if item.get("subscriptionId")
        ]
        rows = self._query(
            subscription_ids=subscription_ids,
            query=POLICY_POSTURE_QUERY,
            access_token=self._access_token(),
        )
        return normalize_policy_posture(rows, subscriptions)

    def fetch_policy_resources(
        self,
        integration: dict[str, Any],
    ) -> list[dict[str, Any]]:
        subscriptions = integration.get("subscriptions", [])
        subscription_ids = [
            item["subscriptionId"]
            for item in subscriptions
            if item.get("subscriptionId")
        ]
        rows = self._query(
            subscription_ids=subscription_ids,
            query=POLICY_RESOURCE_QUERY,
            access_token=self._access_token(),
        )
        return normalize_policy_resources(rows, subscriptions)
