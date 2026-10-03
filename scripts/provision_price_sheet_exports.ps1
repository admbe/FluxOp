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

$storageSubscriptionId = "11111111-1111-1111-1111-111111111111"  # prod-example-sub
$resourceGroup          = "prod-example-westus3-rg"
$storageAccount         = "prodfinopswestus3sa"
$container              = "cost-management"
$exportName             = "price-sheet-monthly"

# Same subscription map as provision_focus_exports.ps1.
$subscriptions = [ordered]@{
    "dev-eu-iaas-sub"        = "459ea29f-364a-4285-a4c7-7020e36ac0f3"
    "prod-connectivity-sub" = "8be294b3-4c2f-4591-a513-ba9c50862da1"
    "dev-uk-iaas-sub"        = "b2032b7b-8231-499a-ab32-65e7c2909d59"
    "prod-uk-iaas-sub-ot"    = "57963df7-bcb9-4d8d-a62f-4025fa269f6a"
    "shared-services-sub"   = "81cdd09d-83eb-468f-a855-250b71181e5f"
    "prod-example-sub"      = "11111111-1111-1111-1111-111111111111"
    "prod-uk-avd-sub"        = "fa3723e8-7e58-4e2f-a723-cddc4ca9eb2a"
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
                # Confirmed live 2026-08-07 against the enrollment: a
                # PriceSheet export requires TheCurrentMonth with a Daily
                # schedule and no dataset granularity. MonthToDate/Monthly
                # with granularity Daily -- what this script carried -- is
                # rejected on all three counts. It was never noticed because
                # EA refuses subscription-scope price sheets before payload
                # validation is reached; on an MCA billing account, where
                # this script does apply, it would have failed outright.
                timeframe = "TheCurrentMonth"
                dataSet   = @{
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
                recurrence = "Daily"
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
