<#
.SYNOPSIS
  Provision a dedicated, non-interactive identity for the scheduled FOCUS
  export provisioning pipeline.

.DESCRIPTION
  Creates a distinct Entra app registration + service principal
  ("Flux-FinOps-Export-Provisioner"), separate from the flux-prod-wif
  deploy identity and from the app's own runtime managed identity, so that
  billing-export write permission never lives on either of those.

  Grants:
    - Cost Management Contributor on each of the 11 Flux-configured
      subscriptions (required to create/read Microsoft.CostManagement/exports).
    - Storage Blob Data Contributor on prodfinopswestus3sa only (required to
      create the destination container if it does not already exist).

  Mode is ado-only: this identity is never used interactively or from a dev
  box, only from the scheduled azure-pipelines-focus-exports.yml pipeline via
  workload identity federation.

  This script deliberately stops short of creating the federated credential.
  The "Azure Resource Manager using App registration or managed identity
  (manual)" ADO connection type generates its own opaque issuer/subject pair
  per connection instance -- it is NOT the predictable sc://org/project/name
  pattern used by older ADO WIF connection types, and cannot be known until
  the connection shell already exists in the ADO UI. Create the connection
  shell first, then run register_focus_export_federation.ps1 with the
  Subject it displays. See the runbook for the exact order.
#>
[CmdletBinding()]
param(
    [string]$AppName = "Flux-FinOps-Export-Provisioner",
    [string]$AdoServiceConnectionName = "flux-focus-export-provisioner",
    [string]$StorageSubscription = "prod-example-sub",
    [string]$StorageAccount = "prodfinopswestus3sa",
    # All tenant subscriptions except contoso-test-sub
    # (7423d636-177c-470f-a0eb-ce58994da78b): Azure froze it for inactivity
    # and a deny assignment blocks role-assignment writes there.
    [string[]]$TargetSubscriptions = @(
        "459ea29f-364a-4285-a4c7-7020e36ac0f3", # dev-eu-iaas-sub
        "97bb8d59-9dbf-4623-aa31-18d2347794dd", # prod-eu-iaas-sub
        "b28ae5a3-031e-4715-aa2c-8f9e801e7549", # dev-sub
        "e553629f-8e78-4678-a2fd-36d439ff2de4", # prod-sub
        "8be294b3-4c2f-4591-a513-ba9c50862da1", # prod-connectivity-sub
        "b2032b7b-8231-499a-ab32-65e7c2909d59", # dev-uk-iaas-sub
        "5da08b9a-781f-4a4e-a020-0681ffea915d", # prod-uk-iaas-sub
        "57963df7-bcb9-4d8d-a62f-4025fa269f6a", # prod-uk-iaas-sub-ot
        "81cdd09d-83eb-468f-a855-250b71181e5f", # shared-services-sub
        "11111111-1111-1111-1111-111111111111", # prod-example-sub
        "fa3723e8-7e58-4e2f-a723-cddc4ca9eb2a", # prod-uk-avd-sub
        "ec3e5ff1-b4d3-4d62-afe6-0176d4938c41", # visual-studio-professional
        "6c96c0f2-638a-406e-a8b9-db3fb304d62e", # contoso-prod-sub
        "ca8136ae-b22d-47bd-afe0-9f55a609ce14", # contoso-dev-sub
        "48d1687e-3d7e-4ee7-a641-5388a8f7ced7", # azure-subscription-1
        "552d56da-18d9-4f5c-ab24-b17478b3988e", # apps-fabric-prod-sub
        "86c06b47-d2d2-4920-a3b9-83c41ae5fc81", # apps-ado-prod-sub
        "a82850d1-df97-4be9-acef-6c8b4c7ca5a9", # apps-adf-prod-sub
        "82c97113-e00d-4334-a6ea-f68729cf8f4c", # contoso-sandbox-sub
        "1f7b04dc-908c-4e48-ad29-4d1eea75f6ad", # apps-fabric-dev-sub
        "5e356753-8fd9-4093-a294-ebc517f9c3c6"  # dev-team-sub
    )
)

$ErrorActionPreference = "Continue"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    $env:PATH = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:PATH"
}

function Invoke-Az {
    param([string]$Description, [string[]]$ArgumentList)
    Write-Host "-> $Description"
    & az @ArgumentList 2>&1 | ForEach-Object { Write-Host "   $_" }
    if ($LASTEXITCODE -ne 0) { throw "az failed (exit $LASTEXITCODE): $Description" }
}

az account set --subscription $StorageSubscription
if ($LASTEXITCODE -ne 0) { throw "Cannot set subscription $StorageSubscription" }
$tenantId = (az account show --query tenantId -o tsv)
Write-Host "Tenant: $tenantId"

# 1. App registration + service principal (idempotent).
$appId = (az ad app list --display-name $AppName --query "[0].appId" -o tsv 2>$null)
if ($appId) {
    Write-Host "Reusing existing app '$AppName' (appId=$appId)"
} else {
    $appId = (az ad app create --display-name $AppName --query appId -o tsv)
    if ($LASTEXITCODE -ne 0 -or -not $appId) { throw "Failed to create app" }
    Write-Host "Created app '$AppName' (appId=$appId)"
}
az ad sp create --id $appId --query id -o tsv 2>&1 | Out-Null
$spId = (az ad sp show --id $appId --query id -o tsv 2>$null)
Write-Host "Service principal objectId=$spId"

# 2. Cost Management Contributor on every target subscription.
#    (Note: "role assignment list" does not accept --assignee-principal-type
#    on this az CLI version -- only "create" does. Filtering by
#    --assignee-object-id + --role + --scope alone is unambiguous.)
foreach ($subscriptionId in $TargetSubscriptions) {
    $scope = "/subscriptions/$subscriptionId"
    $existing = az role assignment list --assignee-object-id $spId --role "Cost Management Contributor" --scope $scope --query "[].id" -o tsv 2>$null
    if (-not $existing) {
        Invoke-Az "Grant Cost Management Contributor on $subscriptionId" @(
            "role", "assignment", "create",
            "--assignee-object-id", $spId,
            "--assignee-principal-type", "ServicePrincipal",
            "--role", "Cost Management Contributor",
            "--scope", $scope
        )
    } else {
        Write-Host "Cost Management Contributor already assigned on $subscriptionId"
    }
}

# 3. Storage Blob Data Contributor (data-plane: this script's own
#    "az storage container create" call) plus Storage Account Contributor
#    (management-plane: resolving the account's primaryLocation, and --
#    confirmed live 2026-07-30 -- required by Cost Management's own export
#    creation flow, which validates/configures the destination storage
#    account as part of creating an export). Both scoped to just this
#    account, never subscription-wide.
#
#    Storage Blob Data Contributor alone produced a misleading, generic
#    "RBACAccessDenied" from the Cost Management export-create API itself
#    (not from the storage account), with zero trace in Activity Log,
#    despite Cost Management Contributor being correctly assigned and
#    scoped on every target subscription. Reader was tried first and ruled
#    out (it fixed the unrelated "az storage account show" failure but not
#    export creation); Storage Account Contributor was the actual fix and
#    supersedes Reader for this account.
$storageId = az storage account show --name $StorageAccount --query id -o tsv 2>$null
if ($storageId) {
    foreach ($role in "Storage Blob Data Contributor", "Storage Account Contributor") {
        $existing = az role assignment list --assignee-object-id $spId --role $role --scope $storageId --query "[].id" -o tsv 2>$null
        if (-not $existing) {
            Invoke-Az "Grant $role on $StorageAccount" @(
                "role", "assignment", "create",
                "--assignee-object-id", $spId,
                "--assignee-principal-type", "ServicePrincipal",
                "--role", $role,
                "--scope", $storageId
            )
        } else {
            Write-Host "$role already assigned on $StorageAccount"
        }
    }
} else {
    Write-Host "WARNING: could not resolve $StorageAccount; grant Storage Blob Data Contributor and Reader manually."
}

Write-Host ""
Write-Host "=== summary ==="
Write-Host "AppName  : $AppName"
Write-Host "AppId    : $appId"
Write-Host "ObjectId : $spId"
Write-Host "TenantId : $tenantId"
Write-Host ""
Write-Host "NEXT (manual, in Azure DevOps):"
Write-Host "  Project settings -> Service connections -> New -> Azure Resource Manager"
Write-Host "  -> App registration or managed identity (manual)"
Write-Host "  Name              : $AdoServiceConnectionName"
Write-Host "  Subscription      : $StorageSubscription"
Write-Host "  Application (client) ID : $appId"
Write-Host "  Tenant ID               : $tenantId"
Write-Host ""
Write-Host "  This creates a draft connection and shows its auto-generated"
Write-Host "  'Issuer' and 'Subject identifier'. Copy the Subject identifier,"
Write-Host "  then run:"
Write-Host ""
Write-Host "    .\scripts\register_focus_export_federation.ps1 -Subject '<copied value>'"
Write-Host ""
Write-Host "  Then go back and click 'Verify and save' -- it will succeed once"
Write-Host "  the federated credential matches."
