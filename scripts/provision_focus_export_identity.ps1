<#
.SYNOPSIS
  Provision a dedicated, non-interactive identity for the scheduled FOCUS
  export provisioning pipeline.

.DESCRIPTION
  Creates a distinct Entra app registration + service principal
  ("Flux-FinOps-Export-Provisioner"), separate from the FluxFinOps-Prod-WIF
  deploy identity and from the app's own runtime managed identity, so that
  billing-export write permission never lives on either of those.

  Grants:
    - Cost Management Contributor on each Flux-configured subscription
      (required to create/read Microsoft.CostManagement/exports).
    - Storage Blob Data Contributor on the export storage account only
      (required to create the destination container if it does not exist).

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
    [string]$StorageSubscription = "",
    [string]$StorageAccount = "",
    # Every Flux-configured subscription. Skip subscriptions Azure has frozen
    # for inactivity: their deny assignment blocks role-assignment writes.
    [string[]]$TargetSubscriptions = @(
        "00000000-0000-0000-0000-000000000000", # example-prod-sub
        "00000000-0000-0000-0000-000000000001"  # example-dev-sub
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
