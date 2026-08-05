<#
.SYNOPSIS
  Apply approved DC2A worksheet metadata as NATIVE Azure resource tags,
  with an exact-state rollback file. Dry run by default.

.DESCRIPTION
  Reads Tagging-Effort/dc2a_enrichment_worksheet.csv and, for rows with
  ApprovalStatus=Approved, merges the proposed values as native Azure tags
  (application, application-owner, it-owner, department,
  region-classification, environment, migration-wave).

  Safety properties:
  - Dry run unless -Apply is passed; the dry run prints every change.
  - Before ANY change to a resource, its complete current tag set is
    appended to the rollback file (JSON lines). rollback_native_tags.ps1
    restores that exact state with az tag update --operation Replace.
  - Merge semantics only: no existing tag is removed or renamed.
  - Idempotent: a resource whose tags already carry the approved values is
    skipped, so reruns are safe.
  - Every attempt's outcome is written to an outcomes CSV.
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$Worksheet = "$PSScriptRoot/../Tagging-Effort/dc2a_enrichment_worksheet.csv",
    [string]$RollbackFile = "$PSScriptRoot/../Tagging-Effort/native_tag_rollback.jsonl",
    [string]$OutcomeFile = "$PSScriptRoot/../Tagging-Effort/native_tag_outcomes.csv"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Worksheet)) { throw "Worksheet not found: $Worksheet" }

$proposalToTag = [ordered]@{
    ProposedApplication          = "application"
    ProposedApplicationOwner     = "application-owner"
    ProposedITOwner              = "it-owner"
    ProposedDepartment           = "department"
    ProposedRegionClassification = "region-classification"
    ProposedEnvironment          = "environment"
    ProposedMigrationWave        = "migration-wave"
}

$rows = Import-Csv -Path $Worksheet | Where-Object {
    $_.ApprovalStatus -and $_.ApprovalStatus.Trim().ToLower() -eq "approved"
}
Write-Host "Approved rows: $($rows.Count). Mode: $(if ($Apply) { 'APPLY' } else { 'DRY RUN' })"

$outcomes = @()
foreach ($row in $rows) {
    $resourceId = $row.ResourceId
    if (-not $resourceId) { continue }
    $desired = @{}
    foreach ($column in $proposalToTag.Keys) {
        $value = ($row.$column ?? "").Trim()
        if ($value) { $desired[$proposalToTag[$column]] = $value }
    }
    if ($desired.Count -eq 0) { continue }

    $currentJson = az tag list --resource-id $resourceId -o json 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "SKIP (cannot read tags): $($row.ResourceName)"
        $outcomes += [pscustomobject]@{
            ResourceId = $resourceId; Result = "read-failed"; Detail = ""
        }
        continue
    }
    $current = ($currentJson | ConvertFrom-Json).properties.tags
    if ($null -eq $current) { $current = @{} }

    $changes = @{}
    foreach ($key in $desired.Keys) {
        $existing = $current.PSObject.Properties[$key]
        if (-not $existing -or $existing.Value -ne $desired[$key]) {
            $changes[$key] = $desired[$key]
        }
    }
    if ($changes.Count -eq 0) {
        Write-Host "OK (already tagged): $($row.ResourceName)"
        $outcomes += [pscustomobject]@{
            ResourceId = $resourceId; Result = "already-current"; Detail = ""
        }
        continue
    }
    $changeText = ($changes.Keys | ForEach-Object { "$_=$($changes[$_])" }) -join "; "
    if (-not $Apply) {
        Write-Host "WOULD TAG: $($row.ResourceName): $changeText"
        $outcomes += [pscustomobject]@{
            ResourceId = $resourceId; Result = "dry-run"; Detail = $changeText
        }
        continue
    }

    # Exact prior state first -- this line IS the rollback contract.
    @{ resourceId = $resourceId; previousTags = $current } |
        ConvertTo-Json -Compress -Depth 5 |
        Add-Content -Path $RollbackFile -Encoding utf8

    $tagArguments = $changes.Keys | ForEach-Object { "$_=$($changes[$_])" }
    az tag update --resource-id $resourceId --operation Merge --tags @tagArguments -o none
    if ($LASTEXITCODE -eq 0) {
        Write-Host "TAGGED: $($row.ResourceName): $changeText"
        $outcomes += [pscustomobject]@{
            ResourceId = $resourceId; Result = "applied"; Detail = $changeText
        }
    } else {
        Write-Host "FAILED: $($row.ResourceName)"
        $outcomes += [pscustomobject]@{
            ResourceId = $resourceId; Result = "failed"; Detail = $changeText
        }
    }
}

$outcomes | Export-Csv -Path $OutcomeFile -NoTypeInformation -Encoding utf8
Write-Host "Outcomes: $OutcomeFile"
if ($Apply) { Write-Host "Rollback state: $RollbackFile" }
