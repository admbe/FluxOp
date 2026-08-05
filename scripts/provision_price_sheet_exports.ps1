# Ensures a monthly PriceSheet export exists for every Flux-configured
# subscription, mirroring provision_focus_exports.ps1: same destination
# storage and container, its own folder per subscription under
# "pricesheet/<label>". Idempotent -- skips subscriptions that already have
# an export named $exportName.
#
# Container and prefix are exact requirements of api/jobs.py's
# price_sheet_sync importer (FLUX_PRICESHEET_STORAGE_PREFIX defaults to
# "pricesheet/").
#
# Price sheet exports are supported for Microsoft Customer Agreement
# subscriptions. Enterprise Agreement enrollments publish one sheet at the
# billing scope instead; if a subscription fails with an unsupported-offer
# error, create a single billing-scope export manually into the same
# container under pricesheet/enrollment.

$storageSubscriptionId = ""   # subscription that owns the export storage account
$resourceGroup          = ""   # resource group of the storage account
$storageAccount         = ""   # storage account receiving price-sheet exports
$container              = "cost-management"
$exportName             = "price-sheet-monthly"

# Same subscription map as provision_focus_exports.ps1.
$subscriptions = [ordered]@{
    "example-prod-sub" = "00000000-0000-0000-0000-000000000000"
    "example-dev-sub"  = "00000000-0000-0000-0000-000000000001"
}

az account set --subscription $storageSubscriptionId

$storageId = "/subscriptions/$storageSubscriptionId/resourceGroups/$resourceGroup/providers/Microsoft.Storage/storageAccounts/$storageAccount"
$location = az storage account show --ids $storageId --query primaryLocation --output tsv

$startDate = (Get-Date).ToUniversalTime().AddMinutes(10).ToString("yyyy-MM-ddTHH:mm:ssZ")
$endDate   = (Get-Date).ToUniversalTime().AddYears(10).ToString("yyyy-MM-ddTHH:mm:ssZ")

$failures = @()

foreach ($label in $subscriptions.Keys) {
    $subscriptionId = $subscriptions[$label]
    $scope = "/subscriptions/$subscriptionId"

    # The FOCUS provisioning run already handled RP registration for these
    # subscriptions; only verify here.
    $state = az provider show --namespace Microsoft.CostManagementExports --subscription $subscriptionId --query registrationState -o tsv 2>$null
    if ($state -ne "Registered") {
        Write-Host "FAIL  $label ($subscriptionId): Microsoft.CostManagementExports is '$state', not Registered. Run the FOCUS provisioning first."
        $failures += $label
        continue
    }

    $uri = "https://management.azure.com$scope/providers/Microsoft.CostManagement/exports/$exportName`?api-version=2025-03-01"

    $existing = az rest --method get --uri $uri 2>$null
    if ($LASTEXITCODE -eq 0 -and $existing) {
        Write-Host "SKIP  $label ($subscriptionId): export already exists"
        continue
    }

    # Classic delivery mode (no identity block), same as the FOCUS exports:
    # this identity holds ARM roles only, and Cost Management's first-party
    # principal delivers via the destination storage grant.
    $payload = @{
        location = $location
        properties = @{
            format                = "Csv"
            dataOverwriteBehavior = "OverwritePreviousReport"
            partitionData         = $true
            exportDescription     = "FluxFinOps monthly price sheet export for $label"
            definition = @{
                type      = "PriceSheet"
                timeframe = "MonthToDate"
                dataSet   = @{
                    granularity   = "Daily"
                    configuration = @{ dataVersion = "2023-05-01" }
                }
            }
            deliveryInfo = @{
                destination = @{
                    type           = "AzureBlob"
                    resourceId     = $storageId
                    container      = $container
                    rootFolderPath = "pricesheet/$label"
                }
            }
            schedule = @{
                status     = "Active"
                recurrence = "Monthly"
                recurrencePeriod = @{ from = $startDate; to = $endDate }
            }
        }
    } | ConvertTo-Json -Depth 20

    Write-Host "CREATE $label ($subscriptionId)"
    az rest --method PUT --uri $uri --body $payload | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAIL  $label ($subscriptionId): price sheet export creation failed (EA subscriptions need a billing-scope export instead; see the header comment)"
        $failures += $label
    }
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "Completed with $($failures.Count) failure(s): $($failures -join ', ')"
    exit 1
}
Write-Host "All price sheet exports are provisioned."
