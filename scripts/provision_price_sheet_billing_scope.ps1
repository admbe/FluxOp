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
    # Optional: when omitted the script discovers the Enterprise Agreement
    # enrollment the signed-in identity can see. Discovery returning nothing
    # is itself the answer -- an identity with no billing-plane visibility
    # cannot create this export, whatever Azure RBAC it holds.
    [string]$BillingAccountName
)

if (-not $BillingAccountName) {
    Write-Host "No -BillingAccountName supplied; discovering the enrollment."
    $accountsJson = az billing account list --only-show-errors -o json 2>$null
    $accounts = if ($LASTEXITCODE -eq 0 -and $accountsJson) {
        $accountsJson | ConvertFrom-Json
    } else { @() }
    $enrollment = $accounts |
        Where-Object { $_.agreementType -eq "EnterpriseAgreement" } |
        Select-Object -First 1
    if (-not $enrollment) {
        Write-Host "ACTION REQUIRED: this identity sees no Enterprise Agreement billing account."
        Write-Host "  Azure RBAC (Cost Management Contributor, Storage roles) does not grant"
        Write-Host "  billing-plane access. Creating a price sheet export at enrollment scope"
        Write-Host "  needs an EA billing role, and Azure only assigns read-only enrollment"
        Write-Host "  roles to service principals, so this very likely needs a human"
        Write-Host "  Enterprise Administrator to run:"
        Write-Host "    pwsh -File scripts/provision_price_sheet_billing_scope.ps1 -BillingAccountName <enrollment-id>"
        Write-Host "  Until then the commitment optimizer has no negotiated rates and falls"
        Write-Host "  back to retail pricing."
        # Deliberately not a pipeline failure: this is a known, human-resolvable
        # authorization boundary, and failing the monthly FOCUS run every time
        # would train everyone to ignore it. The message above is the signal.
        exit 0
    }
    $BillingAccountName = $enrollment.name
    Write-Host "Discovered enrollment $BillingAccountName ($($enrollment.displayName))."
}

$storageId = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/prod-example-westus3-rg/providers/Microsoft.Storage/storageAccounts/prodfinopswestus3sa"
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

# Azure validates timeframe against recurrence: MonthToDate belongs with a
# Daily schedule (that is why the FOCUS export works), while a Monthly
# schedule needs a completed-period timeframe. Both price sheet scripts
# copied MonthToDate onto a Monthly schedule, which the service rejects
# with "Invalid timeframe ... and schedule recurrence ... combination" --
# and because neither script had ever succeeded, nothing surfaced it.
# Candidates are ordered most- to least-preferred; the first accepted one
# wins, and the run prints which so this can be pinned later.
# The first entry is Microsoft's own documented billing-account price sheet
# example (ExportCreateOrUpdateByBillingAccountPricesheet): TheCurrentMonth
# with a Daily schedule. The generic cost-export pairings all fail for this
# export type -- MonthToDate/Daily works for FOCUS cost but is rejected for
# PriceSheet -- so the documented shape leads and the rest are fallbacks.
# Confirmed against the live enrollment on 2026-08-07. A PriceSheet export
# takes TheCurrentMonth with a Daily schedule and no dataset granularity.
# Every generic cost-export pairing is rejected, including MonthToDate/Daily
# which the FOCUS cost export uses successfully against this same API, and
# Microsoft's published example carries a granularity this api-version
# refuses -- so none of the obvious analogies hold. Kept as a one-entry list
# because the discovery cost four round trips; if Azure changes the contract
# again, add a candidate rather than editing a literal in place.
$candidates = @(
    @{ timeframe = "TheCurrentMonth"; recurrence = "Daily"; granularity = $null }
)

function New-ExportPayload {
    param([string]$Timeframe, [string]$Recurrence, [string]$Granularity)
    $dataSet = @{ configuration = @{ dataVersion = "2023-05-01" } }
    if ($Granularity) { $dataSet.granularity = $Granularity }
    @{
        properties = @{
            format                = "Csv"
            dataOverwriteBehavior = "OverwritePreviousReport"
            partitionData         = $true
            exportDescription     = "FluxFinOps enrollment price sheet"
            definition = @{
                type      = "PriceSheet"
                timeframe = $Timeframe
                dataSet   = $dataSet
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
                recurrence = $Recurrence
                recurrencePeriod = @{ from = $startDate; to = $endDate }
            }
        }
    } | ConvertTo-Json -Depth 20
}

Write-Host "CREATE billing-scope price sheet export on $BillingAccountName"

# Pass the body as a file rather than an argument. On Windows `az` is a
# batch file, so a multi-line JSON argument is mangled by cmd.exe and Azure
# receives an empty body -- "Invalid request payload: Unexpected end when
# reading JSON". The pipeline never hit this because it runs pscore on a
# Linux agent. UTF8 without BOM: a BOM breaks the JSON parser too.
$payloadPath = Join-Path ([System.IO.Path]::GetTempPath()) "flux-price-sheet-export.json"
$exit = 1
$text = ""
foreach ($candidate in $candidates) {
    $label = "$($candidate.timeframe)/$($candidate.recurrence)" +
        $(if ($candidate.granularity) { " granularity=$($candidate.granularity)" }
          else { " no granularity" })
    Write-Host "  trying timeframe $label"
    [System.IO.File]::WriteAllText(
        $payloadPath,
        (New-ExportPayload -Timeframe $candidate.timeframe `
            -Recurrence $candidate.recurrence -Granularity $candidate.granularity),
        (New-Object System.Text.UTF8Encoding($false))
    )
    try {
        $response = az rest --method PUT --uri $uri --body "@$payloadPath" 2>&1
        $exit = $LASTEXITCODE
    } finally {
        Remove-Item $payloadPath -ErrorAction SilentlyContinue
    }
    $text = ($response | Out-String)
    if ($exit -eq 0) {
        Write-Host "  accepted with $label"
        break
    }
    # Retry only payload-shape rejections, which is what the candidates vary.
    # Permissions, storage and scope errors repeat identically for every
    # candidate and should surface on the first attempt.
    if ($text -notmatch "Request properties validation failed") {
        break
    }
}

if ($exit -ne 0) {
    Write-Host $text.Trim()
    if ($text -match "Unauthorized|Forbidden|AuthorizationFailed|403|401") {
        Write-Host "ACTION REQUIRED: creation was refused at billing scope."
        Write-Host "  The identity reached the enrollment but cannot write to it."
        Write-Host "  An Enterprise Administrator must run this script; Azure"
        Write-Host "  assigns service principals read-only enrollment roles only."
        # An authorization boundary is a standing condition to report, not a
        # broken monthly pipeline.
        exit 0
    }
    # Anything else is a real fault worth failing on -- reporting a payload
    # or service error as "you lack permission" sent the last investigation
    # down the wrong path entirely.
    Write-Host "FAIL: the export request was rejected for the reason above."
    exit 1
}
Write-Host "Done. The flux-price-sheet job ingests it after the export's first run."
