<#
.SYNOPSIS
  Register (or replace) the federated credential that lets the
  flux-focus-export-provisioner ADO service connection authenticate as the
  Flux-FinOps-Export-Provisioner app, using the exact Subject identifier ADO
  generated for that connection.

.DESCRIPTION
  Run this AFTER creating the ADO service connection shell (Project settings
  -> Service connections -> New -> Azure Resource Manager -> App
  registration or managed identity (manual)), not before. That connection
  type generates its own opaque per-connection Subject identifier
  (/eid1/c/pub/t/.../a/.../sc/.../...) -- it cannot be predicted in advance,
  which is why provision_focus_export_identity.ps1 no longer attempts to
  create this credential itself.

  The Issuer is not connection-specific -- it is always
  https://login.microsoftonline.com/{tenantId}/v2.0 for this connection type
  -- so it is derived automatically rather than taken as a parameter.

  Safe to re-run: deletes any existing credential of the same name first, so
  re-running after recreating the ADO connection (which generates a new
  Subject) correctly replaces the stale one instead of erroring or leaving
  both in place.

.PARAMETER Subject
  The exact "Subject identifier" string shown in the ADO service connection
  dialog, e.g. /eid1/c/pub/t/{hash}/a/{hash}/sc/{guid}/{guid}.

.EXAMPLE
  .\scripts\register_focus_export_federation.ps1 -Subject "/eid1/c/pub/t/{tenant-hash}/a/{app-hash}/sc/00000000-0000-0000-0000-000000000000/00000000-0000-0000-0000-000000000001"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Subject,
    [string]$AppName = "Flux-FinOps-Export-Provisioner",
    [string]$CredentialName = "flux-focus-export-provisioner-ado",
    [string]$StorageSubscription = ""
)

$ErrorActionPreference = "Continue"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    $env:PATH = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;$env:PATH"
}

function Invoke-Az {
    param([string]$Description, [string[]]$ArgumentList)
    Write-Host "-> $Description"
    & az @ArgumentList 2>&1 | ForEach-Object { Write-Host "   $_" }
    if ($LASTEXITCODE -ne 0) { throw "az failed (exit $LASTEXITCODE): $Description" }
}

az account set --subscription $StorageSubscription
if ($LASTEXITCODE -ne 0) { throw "Cannot set subscription $StorageSubscription" }
$tenantId = (az account show --query tenantId -o tsv)
$issuer = "https://login.microsoftonline.com/$tenantId/v2.0"

$appId = (az ad app list --display-name $AppName --query "[0].appId" -o tsv 2>$null)
if (-not $appId) { throw "App '$AppName' not found -- run provision_focus_export_identity.ps1 first" }
Write-Host "App: $AppName (appId=$appId)"
Write-Host "Issuer : $issuer"
Write-Host "Subject: $Subject"

$existingId = az ad app federated-credential list --id $appId --query "[?name=='$CredentialName'].id" -o tsv 2>$null
if ($existingId) {
    Invoke-Az "Delete existing federated credential (will be replaced)" @(
        "ad", "app", "federated-credential", "delete",
        "--id", $appId,
        "--federated-credential-id", $CredentialName
    )
}

$fcPayload = [ordered]@{
    name        = $CredentialName
    issuer      = $issuer
    subject     = $Subject
    description = "FluxFinOps FOCUS export provisioning pipeline"
    audiences   = @("api://AzureADTokenExchange")
} | ConvertTo-Json -Depth 5
$fcFile = New-TemporaryFile
try {
    Set-Content -Path $fcFile -Value $fcPayload -Encoding utf8NoBOM -NoNewline
    Invoke-Az "Create federated credential" @(
        "ad", "app", "federated-credential", "create",
        "--id", $appId,
        "--parameters", "@$fcFile"
    )
} finally {
    Remove-Item $fcFile -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Done. Go back to the ADO service connection dialog and click 'Verify and save'."
