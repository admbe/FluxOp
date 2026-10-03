# Ensures a FOCUS v1.0 daily cost export exists for every Flux-configured
# subscription. Idempotent: skips subscriptions that already have an export
# named $exportName, so this is safe to run monthly as a catch-all for newly
# onboarded subscriptions without disturbing existing schedules.
#
# Format/container/prefix/version below are exact requirements of
# api/database.py's FOCUS importer (read_csv, all_varchar; container
# "cost-management"; prefix "focus/") -- do not change without also
# updating FLUX_FOCUS_STORAGE_CONTAINER / FLUX_FOCUS_STORAGE_PREFIX.

$storageSubscriptionId = "11111111-1111-1111-1111-111111111111"  # prod-example-sub
$resourceGroup          = "prod-example-westus3-rg"
$storageAccount         = "prodfinopswestus3sa"
$container              = "cost-management"
$exportName             = "focus-daily"

# label -> subscription id, for every Flux-configured subscription that can
# carry a FOCUS export. Extended 2026-08-02 when the estate grew to all 22
# tenant subscriptions. Deliberately absent:
#   - contoso-test-sub (7423d636-177c-470f-a0eb-ce58994da78b): Azure froze it
#     for inactivity; a deny assignment blocks every write until Microsoft
#     support unfreezes it.
#   - visual-studio-professional (ec3e5ff1-b4d3-4d62-afe6-0176d4938c41):
#     WebDirect agreement type; Azure rejects FocusCost exports for it
#     outright ("not supported for Agreement Type: WebDirect", run 414).
#     Its cost still arrives through the Query API collector.
$subscriptions = [ordered]@{
    "dev-eu-iaas-sub"             = "459ea29f-364a-4285-a4c7-7020e36ac0f3"
    "prod-connectivity-sub"      = "8be294b3-4c2f-4591-a513-ba9c50862da1"
    "dev-uk-iaas-sub"             = "b2032b7b-8231-499a-ab32-65e7c2909d59"
    "prod-uk-iaas-sub-ot"         = "57963df7-bcb9-4d8d-a62f-4025fa269f6a"
    "shared-services-sub"        = "81cdd09d-83eb-468f-a855-250b71181e5f"
    "prod-example-sub"           = "11111111-1111-1111-1111-111111111111"
    "prod-uk-avd-sub"             = "fa3723e8-7e58-4e2f-a723-cddc4ca9eb2a"
    "contoso-prod-sub"               = "6c96c0f2-638a-406e-a8b9-db3fb304d62e"
    "contoso-dev-sub"                = "ca8136ae-b22d-47bd-afe0-9f55a609ce14"
    "azure-subscription-1"       = "48d1687e-3d7e-4ee7-a641-5388a8f7ced7"
    "apps-fabric-prod-sub"    = "552d56da-18d9-4f5c-ab24-b17478b3988e"
    "apps-ado-prod-sub"       = "86c06b47-d2d2-4920-a3b9-83c41ae5fc81"
    "apps-adf-prod-sub"       = "a82850d1-df97-4be9-acef-6c8b4c7ca5a9"
    "contoso-sandbox-sub"            = "82c97113-e00d-4334-a6ea-f68729cf8f4c"
    "apps-fabric-dev-sub"     = "1f7b04dc-908c-4e48-ad29-4d1eea75f6ad"
    "dev-team-sub"                = "5e356753-8fd9-4093-a294-ebc517f9c3c6"
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
