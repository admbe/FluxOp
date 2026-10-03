# one-time-exports.ps1 — run locally as Flux-FinOps-Export-Provisioner
# Purpose: one-time FOCUS export (no schedule) for backfill — does NOT touch the daily focus-daily exports.
# The next flux-focus-cost (every 6h) will ingest the resulting manifest.json + charge/*.csv as a new runId.
# See docs/FOCUS-COST-INGESTION-FAQ.md §2-3 + provision_focus_exports.ps1 for the daily shape this clones.
$storageAccount="prodfinopswestus3sa"
$container="cost-management"
$prefixRoot="focus"
# maps scopes -> display names; extended by provision_focus_exports.ps1
$map = @{
  "97bb8d59-9dbf-4623-aa31-18d2347794dd" = "prod-eu-iaas-sub"
  "459ea29f-364a-4285-a4c7-7020e36ac0f3" = "dev-eu-iaas-sub"
  # add more ids here as needed
}
$storageId = (az storage account show -n $storageAccount --query id -o tsv)
$location  = (az storage account show --ids $storageId --query primaryLocation -o tsv)
$exportName="focus-onetime-$(Get-Date -Format yyyyMMdd-HHmm)"   # avoid name clash with daily focus-daily
foreach ($id in $map.Keys) {
  $scope="/subscriptions/$id"
  $label=$map[$id]
  $payload = @{
    location=$location
    properties=@{
      format="Csv"; dataOverwriteBehavior="OverwritePreviousReport"; partitionData=$true
      definition=@{ type="FocusCost"; timeframe="MonthToDate"; dataSet=@{ granularity="Daily"; configuration=@{ dataVersion="1.0" } } }
      deliveryInfo=@{ destination=@{ type="AzureBlob"; resourceId=$storageId; container=$container; rootFolderPath="$prefixRoot/$label" } }
      schedule=@{ status="Active"; recurrence="OneTime"; recurrencePeriod=@{ from=(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"); to=(Get-Date).AddHours(2).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") } }
    }
  } | ConvertTo-Json -Depth 20
  "CREATE onetime for $label"
  az rest --method PUT --uri "https://management.azure.com$scope/providers/Microsoft.CostManagement/exports/$exportName`?api-version=2025-03-01" --body $payload
}
