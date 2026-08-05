<#
.SYNOPSIS
  Safely deploy an approved Flux virtual-tag override payload, or roll it back.

.DESCRIPTION
  Dry-run is the default. Apply posts the payload in chunks below Flux's
  20,000-item API limit and writes one rollback record per applied override.
  Rollback restores prior values or deletes newly-created overrides. Rollback
  uses an expected-value guard and will not overwrite a newer production edit.

  This changes Flux metadata only; it does not write native Azure tags.
#>
[CmdletBinding(DefaultParameterSetName = "Deploy")]
param(
  [Parameter(ParameterSetName = "Deploy")]
  [string]$Payload = "$PSScriptRoot/../Tagging-Effort/dc2a_virtual_tag_overrides.json",
  [Parameter(ParameterSetName = "Deploy")]
  [switch]$Apply,
  [Parameter(ParameterSetName = "Rollback", Mandatory)]
  [switch]$Rollback,
  [string]$RollbackFile = "$PSScriptRoot/../Tagging-Effort/virtual-tag-rollback.jsonl",
  [string]$OutcomeFile = "$PSScriptRoot/../Tagging-Effort/virtual-tag-deploy-outcomes.csv",
  [string]$BaseUri = "",
  [string]$AccessToken = "",
  [ValidateRange(1, 20000)]
  [int]$ChunkSize = 5000
)

$ErrorActionPreference = "Stop"
$base = $BaseUri.TrimEnd("/")
if (-not $AccessToken) { $AccessToken = $env:FLUX_ACCESS_TOKEN }

function Invoke-FluxJson {
  param([string]$Path, [object]$Body)
  if (-not $AccessToken) { throw "Supply -AccessToken or FLUX_ACCESS_TOKEN." }
  $json = $Body | ConvertTo-Json -Depth 12 -Compress
  Invoke-RestMethod -Method Post -Uri "$base$Path" `
    -Headers @{ Authorization = "Bearer $AccessToken"; Accept = "application/json" } `
    -ContentType "application/json" -Body $json
}

function Confirm-FluxAdmin {
  if (-not $AccessToken) { throw "Supply -AccessToken or FLUX_ACCESS_TOKEN." }
  $session = Invoke-RestMethod -Uri "$base/api/session" `
    -Headers @{ Authorization = "Bearer $AccessToken" }
  if (-not $session.authenticated -or -not $session.permissions.canManageIntegrations) {
    throw "The token is not an authenticated Flux administrator."
  }
}

function Get-Chunks([object[]]$Items) {
  for ($offset = 0; $offset -lt $Items.Count; $offset += $ChunkSize) {
    $last = [Math]::Min($offset + $ChunkSize - 1, $Items.Count - 1)
    Write-Output (,@($Items[$offset..$last]))
  }
}

if ($Rollback) {
  Confirm-FluxAdmin
  if (-not (Test-Path $RollbackFile)) { throw "Rollback file not found: $RollbackFile" }
  $items = @(Get-Content $RollbackFile | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
  if (-not $items.Count) { throw "Rollback file is empty." }
  Write-Host "Rollback items: $($items.Count). Mode: APPLY"
  $results = @()
  foreach ($chunk in (Get-Chunks $items)) {
    $result = Invoke-FluxJson "/api/virtual-tags/overrides/rollback" @{ previous = @($chunk) }
    $results += [pscustomobject]@{
      Restored = $result.restored; Skipped = $result.skipped; Conflicts = $result.conflicts
    }
    if ($result.conflicts -gt 0) {
      Write-Warning "Rollback encountered $($result.conflicts) concurrency conflicts; review before retrying."
    }
  }
  $results | Export-Csv -Path $OutcomeFile -NoTypeInformation -Encoding utf8
  Write-Host "Rollback outcome: $OutcomeFile"
  exit 0
}

if (-not (Test-Path $Payload)) { throw "Payload not found: $Payload" }
$document = Get-Content $Payload -Raw | ConvertFrom-Json
$items = @($document.overrides)
if (-not $items.Count) { throw "Payload contains no overrides." }
if ($items.Count -gt 20000) { Write-Host "Payload exceeds one API request; chunking into $ChunkSize-item batches." }

$keys = @{}
foreach ($item in $items) {
  foreach ($required in @("resourceId", "tagKey", "tagValue")) {
    if (-not $item.$required) { throw "Payload item is missing $required." }
  }
  $key = "$($item.resourceId.ToLowerInvariant())|$($item.tagKey.ToLowerInvariant())"
  if ($keys.ContainsKey($key)) { throw "Duplicate resource/tag key in payload: $key" }
  $keys[$key] = $true
}
$resourceCount = @($items | ForEach-Object { $_.resourceId.ToLowerInvariant() } | Sort-Object -Unique).Count
Write-Host "Validated $($items.Count) overrides across $resourceCount resources."
Write-Host "Mode: $(if ($Apply) { 'APPLY' } else { 'DRY RUN' })"
if (-not $Apply) {
  $items | Group-Object tagKey | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0}: {1}" -f $_.Name, $_.Count)
  }
  Write-Host "No production changes made. Re-run with -Apply and an admin token to deploy."
  exit 0
}

Confirm-FluxAdmin
if (Test-Path $RollbackFile) { throw "Rollback file already exists; choose a new path to avoid mixing deployments." }
$outcomes = @()
$chunkNumber = 0
foreach ($chunk in (Get-Chunks $items)) {
  $chunkNumber++
  Write-Host "Applying chunk $chunkNumber ($($chunk.Count) overrides)..."
  $result = Invoke-FluxJson "/api/virtual-tags/overrides/import" @{ overrides = @($chunk) }
  if ($result.previous.Count -ne $result.applied) {
    throw "Chunk $chunkNumber returned an incomplete rollback record."
  }
  $byKey = @{}
  foreach ($item in $chunk) {
    $byKey["$($item.resourceId.ToLowerInvariant())|$($item.tagKey.ToLowerInvariant())"] = $item
  }
  foreach ($previous in $result.previous) {
    $key = "$($previous.resourceId.ToLowerInvariant())|$($previous.tagKey.ToLowerInvariant())"
    $desired = $byKey[$key]
    [pscustomobject]@{
      resourceId = $previous.resourceId
      tagKey = $previous.tagKey
      expectedValue = [string]$desired.tagValue
      previousValue = $previous.previousValue
      previousSource = $previous.previousSource
    } | ConvertTo-Json -Compress -Depth 5 | Add-Content -Path $RollbackFile -Encoding utf8
  }
  $outcomes += [pscustomobject]@{
    Chunk = $chunkNumber; Applied = $result.applied; RollbackItems = $result.previous.Count
  }
}
$outcomes | Export-Csv -Path $OutcomeFile -NoTypeInformation -Encoding utf8
Write-Host "Deployment complete. Rollback file: $RollbackFile"
Write-Host "Outcome file: $OutcomeFile"
