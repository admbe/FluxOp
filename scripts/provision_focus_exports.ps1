# Ensures a FOCUS v1.0 daily cost export exists for every Flux-configured
# subscription. Idempotent: skips subscriptions that already have an export
# named $exportName, so this is safe to run monthly as a catch-all for newly
# onboarded subscriptions without disturbing existing schedules.
#
# Format/container/prefix/version below are exact requirements of
# api/database.py's FOCUS importer (read_csv, all_varchar; container
# "cost-management"; prefix "focus/") -- do not change without also
# updating FLUX_FOCUS_STORAGE_CONTAINER / FLUX_FOCUS_STORAGE_PREFIX.

$storageSubscriptionId = ""   # subscription that owns the export storage account
$resourceGroup          = ""   # resource group of the storage account
$storageAccount         = ""   # storage account receiving FOCUS exports
$container              = "cost-management"
$exportName             = "focus-daily"

# label -> subscription id, for every Flux-configured subscription that can
# carry a FOCUS export. Notes from operating this at scale:
#   - A subscription frozen by Azure (inactivity deny assignment) rejects
#     every write, including export creation; leave it out until unfrozen.
#   - WebDirect agreement-type subscriptions are rejected outright by the
#     FocusCost export API ("not supported for Agreement Type: WebDirect");
#     their cost still arrives through the Query API collector.
$subscriptions = [ordered]@{
    "example-prod-sub" = "00000000-0000-0000-0000-000000000000"
    "example-dev-sub"  = "00000000-0000-0000-0000-000000000001"
}

az account set --subscription $storageSubscriptionId

$storageId = "/subscriptions/$storageSubscriptionId/resourceGroups/$resourceGroup/providers/Microsoft.Storage/storageAccounts/$storageAccount"

az storage container create `
    --name $container `
    --account-name $storageAccount `
    --auth-mode login | Out-Null

$location = az storage account show --ids $storageId --query primaryLocation --output tsv

$startDate = (Get-Date).ToUniversalTime().AddMinutes(10).ToString("yyyy-MM-ddTHH:mm:ssZ")
$endDate   = (Get-Date).ToUniversalTime().AddYears(10).ToString("yyyy-MM-ddTHH:mm:ssZ")

$failures = @()

foreach ($label in $subscriptions.Keys) {
    $subscriptionId = $subscriptions[$label]
    $scope = "/subscriptions/$subscriptionId"

    # Microsoft.CostManagementExports is a distinct RP namespace from
    # Microsoft.CostManagement (used for reading cost data) and is
    # registered per subscription, not tenant-wide. "Cost Management
    # Contributor" does not include register/action, so this identity can
    # only ever read registration state, not perform a fresh registration --
    # confirmed live 2026-07-30, when register calls failed with
    # AuthorizationFailed on all 7 subscriptions despite the role grant.
    # Reading state needs no extra permission, so check first and only
    # attempt (and fail loudly on) a register call when actually needed --
    # that keeps already-registered subscriptions working every month even
    # though this identity can't register a genuinely new one itself.
    $state = az provider show --namespace Microsoft.CostManagementExports --subscription $subscriptionId --query registrationState -o tsv 2>$null
    if ($state -ne "Registered") {
        try {
            az provider register --namespace Microsoft.CostManagementExports --subscription $subscriptionId --wait 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "provider register exited $LASTEXITCODE" }
        } catch {
            Write-Host "FAIL  $label ($subscriptionId): Microsoft.CostManagementExports is '$state', not Registered, and this identity cannot register it ($_). Register it manually (az provider register --namespace Microsoft.CostManagementExports --subscription $subscriptionId), then re-run."
            $failures += $label
            continue
        }
    }

    $uri = "https://management.azure.com$scope/providers/Microsoft.CostManagement/exports/$exportName`?api-version=2025-03-01"

    $existing = az rest --method get --uri $uri 2>$null
    if ($LASTEXITCODE -eq 0 -and $existing) {
        Write-Host "SKIP  $label ($subscriptionId): export already exists"
        continue
    }

    # No "identity" block: that requests Cost Management's newer
    # identity-based delivery mode, which provisions a new system-assigned
    # managed identity on the export resource -- a directory-level operation
    # requiring more than ARM RBAC. Flux-FinOps-Export-Provisioner
    # deliberately holds only ARM roles (no directory role), so that mode
    # failed with an opaque RBACAccessDenied regardless of its correctly
    # scoped Cost Management Contributor grant (confirmed live 2026-07-30).
    # The classic mode below relies on Cost Management's own first-party
    # service principal for delivery, authorized purely via the caller's
    # Storage Blob Data Contributor grant on the destination.
    $payload = @{
        location = $location
        properties = @{
            format                = "Csv"
            dataOverwriteBehavior = "OverwritePreviousReport"
            partitionData         = $true
            exportDescription     = "FluxFinOps daily FOCUS cost export for $label"
            definition = @{
                type      = "FocusCost"
                timeframe = "MonthToDate"
                dataSet   = @{
                    granularity   = "Daily"
                    configuration = @{ dataVersion = "1.0" }
                }
            }
            deliveryInfo = @{
                destination = @{
                    type           = "AzureBlob"
                    resourceId     = $storageId
                    container      = $container
                    rootFolderPath = "focus/$label"
                }
            }
            schedule = @{
                status     = "Active"
                recurrence = "Daily"
                recurrencePeriod = @{ from = $startDate; to = $endDate }
            }
        }
    } | ConvertTo-Json -Depth 20

    Write-Host "CREATE $label ($subscriptionId)"
    az rest --method PUT --uri $uri --body $payload | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAIL  $label ($subscriptionId): export creation failed"
        $failures += $label
    }
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "Completed with $($failures.Count) failure(s): $($failures -join ', ')"
    exit 1
}
