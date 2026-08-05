# Creates the single billing-scope PriceSheet export for an Enterprise
# Agreement enrollment. Subscription-scope price sheet exports are rejected
# on EA (confirmed live 2026-08-01: the RP returns "Unauthorized.
# Authentication failed." for every subscription); the enrollment publishes
# one sheet covering all of them.
#
# Run as a user holding an EA billing role (Enterprise Administrator or
# Enterprise Reader). Find the billing account id first:
#   az billing account list --query "[].{name:name, displayName:displayName}" -o table
#
# The export lands in the same container the app already reads, under
# pricesheet/enrollment, which FLUX_PRICESHEET_STORAGE_PREFIX covers.

param(
    [Parameter(Mandatory = $true)][string]$BillingAccountName
)

$storageId = "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Storage/storageAccounts/<storage-account>"
$container = "cost-management"
$exportName = "price-sheet-monthly"

$scope = "/providers/Microsoft.Billing/billingAccounts/$BillingAccountName"
$uri = "https://management.azure.com$scope/providers/Microsoft.CostManagement/exports/$exportName`?api-version=2025-03-01"

$existing = az rest --method get --uri $uri 2>$null
if ($LASTEXITCODE -eq 0 -and $existing) {
    Write-Host "SKIP: billing-scope price sheet export already exists"
    exit 0
}

$startDate = (Get-Date).ToUniversalTime().AddMinutes(10).ToString("yyyy-MM-ddTHH:mm:ssZ")
$endDate   = (Get-Date).ToUniversalTime().AddYears(10).ToString("yyyy-MM-ddTHH:mm:ssZ")

$payload = @{
    properties = @{
        format                = "Csv"
        dataOverwriteBehavior = "OverwritePreviousReport"
        partitionData         = $true
        exportDescription     = "FluxFinOps monthly enrollment price sheet"
        definition = @{
            type      = "PriceSheet"
            timeframe = "MonthToDate"
            dataSet   = @{
                configuration = @{ dataVersion = "2023-05-01" }
            }
        }
        deliveryInfo = @{
            destination = @{
                type           = "AzureBlob"
                resourceId     = $storageId
                container      = $container
                rootFolderPath = "pricesheet/enrollment"
            }
        }
        schedule = @{
            status     = "Active"
            recurrence = "Monthly"
            recurrencePeriod = @{ from = $startDate; to = $endDate }
        }
    }
} | ConvertTo-Json -Depth 20

Write-Host "CREATE billing-scope price sheet export on $BillingAccountName"
az rest --method PUT --uri $uri --body $payload
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAIL: creation failed -- confirm the signed-in user holds an EA billing role on the enrollment."
    exit 1
}
Write-Host "Done. The flux-price-sheet job ingests it after the export's first run."
