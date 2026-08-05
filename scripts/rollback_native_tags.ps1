<#
.SYNOPSIS
  Restore the exact pre-change native tag state recorded by
  apply_native_tags.ps1.

.DESCRIPTION
  Replays native_tag_rollback.jsonl newest-entry-last: for each recorded
  resource, az tag update --operation Replace restores the COMPLETE tag
  set captured before the change -- added keys disappear, modified keys
  revert, and keys that never changed remain identical. Idempotent and
  safe to rerun.
#>
[CmdletBinding()]
param(
    [string]$RollbackFile = "$PSScriptRoot/../Tagging-Effort/native_tag_rollback.jsonl",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $RollbackFile)) { throw "Rollback file not found: $RollbackFile" }

# Last capture per resource wins (the earliest state if a resource was
# recorded once; reruns never re-record, so first == pristine).
$byResource = [ordered]@{}
Get-Content $RollbackFile | ForEach-Object {
    $entry = $_ | ConvertFrom-Json
    if (-not $byResource.Contains($entry.resourceId)) {
        $byResource[$entry.resourceId] = $entry.previousTags
    }
}
Write-Host "Resources to restore: $($byResource.Count). Mode: $(if ($Apply) { 'APPLY' } else { 'DRY RUN' })"

foreach ($resourceId in $byResource.Keys) {
    $tags = $byResource[$resourceId]
    $pairs = @()
    if ($tags) {
        foreach ($property in $tags.PSObject.Properties) {
            $pairs += "$($property.Name)=$($property.Value)"
        }
    }
    if (-not $Apply) {
        Write-Host "WOULD RESTORE: $resourceId -> $($pairs -join '; ')"
        continue
    }
    if ($pairs.Count -gt 0) {
        az tag update --resource-id $resourceId --operation Replace --tags @pairs -o none
    } else {
        az tag delete --resource-id $resourceId --yes -o none 2>$null
    }
    if ($LASTEXITCODE -eq 0) {
        Write-Host "RESTORED: $resourceId"
    } else {
        Write-Host "FAILED: $resourceId"
    }
}
