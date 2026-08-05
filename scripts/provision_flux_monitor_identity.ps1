<#
.SYNOPSIS
  Provision a permanent, non-interactive identity for the FluxFinOps agent.

.DESCRIPTION
  Creates a dedicated Entra app registration + service principal
  ("Flux-FinOps-Monitor") for monitoring Azure resources and Azure DevOps
  pipeline state without a personal interactive login or a stored secret.

  Default mode "cert" is the appropriate permanent non-interactive Entra
  credential for a LOCAL dev box (no OIDC issuer exists to federate). The
  public key is registered on the app via Microsoft Graph; the private key
  stays on this machine (outside the git workspace) and rotates yearly.
  No secret is stored in the repository.

  "ado" mode configures a workload-identity federated credential for an
  Azure DevOps service connection — the genuine WIF use case (an ADO
  pipeline issues the OIDC token Entra trusts).
#>
[CmdletBinding()]
param(
    [string]$AppName = "Flux-FinOps-Monitor",
    [string]$Subscription = "",
    [ValidateSet("ado", "cert")] [string]$Mode = "cert",
    [string]$AdoOrg = "",
    [string]$AdoProject = "IT Engineering",
    [string]$AdoServiceConnectionName = "flux-monitor",
    [string]$CertSubject = "CN=Flux-FinOps-Monitor",
    [int]$CertValidityYears = 1,
    [string]$OutDir = "D:\Codex\ssh\flux-monitor"
)

# Do not let benign native stderr (deprecation notices, etc.) kill the script.
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

az account set --subscription $Subscription
if ($LASTEXITCODE -ne 0) { throw "Cannot set subscription $Subscription" }
$subscriptionId = (az account show --query id -o tsv)
$tenantId = (az account show --query tenantId -o tsv)
Write-Host "Tenant: $tenantId  Subscription: $Subscription ($subscriptionId)"

# 1. App registration + service principal (idempotent).
$appId = (az ad app list --display-name $AppName --query "[0].appId" -o tsv 2>$null)
if ($appId) {
    Write-Host "Reusing existing app '$AppName' (appId=$appId)"
} else {
    $appId = (az ad app create --display-name $AppName --query appId -o tsv)
    if ($LASTEXITCODE -ne 0 -or -not $appId) { throw "Failed to create app" }
    Write-Host "Created app '$AppName' (appId=$appId)"
}
az ad sp create --id $appId --query id -o tsv 2>&1 | Out-Null
$spId = (az ad sp show --id $appId --query id -o tsv 2>$null)
Write-Host "Service principal objectId=$spId"

# 2. Azure RBAC: Reader on the target subscription (least privilege).
$scope = "/subscriptions/$subscriptionId"
$existing = az role assignment list --assignee-object-id $spId --assignee-principal-type ServicePrincipal --role Reader --scope $scope --query "[].id" -o tsv 2>$null
if (-not $existing) {
    Invoke-Az "Grant Reader on $Subscription" @(
        "role", "assignment", "create",
        "--assignee-object-id", $spId,
        "--assignee-principal-type", "ServicePrincipal",
        "--role", "Reader",
        "--scope", $scope
    )
} else {
    Write-Host "Reader role already assigned on '$Subscription'"
}

# 3. Credential.
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
switch ($Mode) {
    "ado" {
        $issuer = "https://vstoken.dev.azure.com/$AdoOrg"
        $subject = "sc://$AdoOrg/$AdoProject/$AdoServiceConnectionName"
        $fcId = az ad app federated-credential list --id $appId --query "[?name=='flux-monitor-ado'].id" -o tsv 2>$null
        if (-not $fcId) {
            Invoke-Az "Create federated credential" @(
                "ad", "app", "federated-credential", "create",
                "--id", $appId,
                "--federated-credential-configuration-name", "flux-monitor-ado",
                "--issuer", $issuer,
                "--subject", $subject,
                "--audiences", "api://AzureADTokenExchange"
            )
        } else {
            Write-Host "Federated credential already exists"
        }
        Write-Host ""
        Write-Host "NEXT (manual): create the ADO Workload Identity Federation service connection"
        Write-Host "  Org=$AdoOrg Project=$AdoProject Name=$AdoServiceConnectionName -> app $appId"
        Write-Host "  The pipeline then logs in via that service connection."
    }
    "cert" {
        $openssl = (Get-Command openssl -ErrorAction SilentlyContinue).Source
        if (-not $openssl) {
            $openssl = Get-ChildItem "C:\cygwin64\bin\openssl.exe","C:\Program Files\Git\usr\bin\openssl.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
        }
        if (-not $openssl) { throw "openssl not found. Install Git for Windows or cygwin, or use -Mode ado." }

        $notAfter = (Get-Date).AddYears($CertValidityYears)
        $cert = New-SelfSignedCertificate `
            -Subject $CertSubject `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -NotAfter $notAfter `
            -KeyExportPolicy Exportable `
            -KeySpec Signature
        if (-not $cert) { throw "Failed to create self-signed certificate" }
        $cerPath = Join-Path $OutDir "flux-monitor-cert.cer"
        $pfxPath = Join-Path $OutDir "flux-monitor-cert.pfx"
        $pemPath = Join-Path $OutDir "flux-monitor-key.pem"
        Export-Certificate -Cert $cert -FilePath $cerPath -Force | Out-Null
        $pfxPassword = -join ((1..32) | ForEach-Object { [char](Get-Random -Minimum 65 -Maximum 90) })
        $pfxSec = ConvertTo-SecureString $pfxPassword -AsPlainText -Force
        Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $pfxSec -Force | Out-Null

        # PFX -> PEM (private key + public cert) via openssl. az login reads
        # the private key; the public cert is uploaded to the app below.
        & $openssl pkcs12 -in $pfxPath -out $pemPath -nodes -password pass:$pfxPassword 2>&1 | ForEach-Object { Write-Host "   $_" }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pemPath)) { throw "openssl failed to produce PEM" }
        Remove-Item $pfxPath -ErrorAction SilentlyContinue

        # Register the public key on the app via Microsoft Graph using
        # Invoke-RestMethod (az rest's --headers arg breaks az.cmd on Windows).
        $graphUri = "https://graph.microsoft.com/v1.0/applications(appId='$appId')"
        $token = az account get-access-token --resource "https://graph.microsoft.com" --query accessToken -o tsv 2>$null
        if (-not $token) { throw "Could not obtain Graph access token" }
        $existing = Invoke-RestMethod -Method Get -Uri $graphUri -Headers @{ Authorization = "Bearer $token" }
        $keys = @($existing.keyCredentials)
        $newKey = [pscustomobject]@{
            displayName   = "flux-monitor-cert"
            type          = "AsymmetricX509Cert"
            usage         = "Verify"
            key           = [Convert]::ToBase64String([IO.File]::ReadAllBytes($cerPath))
            startDateTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            endDateTime   = $notAfter.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        }
        $keys = @($keys) + @($newKey)
        $bodyJson = @{ keyCredentials = $keys } | ConvertTo-Json -Depth 6
        Invoke-RestMethod -Method Patch -Uri $graphUri -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } -Body $bodyJson | Out-Null
        Write-Host "Public cert registered on app via Graph; private key at $pemPath"
        Write-Host ""
        Write-Host "Agent login (run in the agent shell):"
        Write-Host "  az login --service-principal --username $appId --certificate `"$pemPath`" --tenant $tenantId"
    }
}

Write-Host ""
Write-Host "=== summary ==="
Write-Host "AppName      : $AppName"
Write-Host "AppId        : $appId"
Write-Host "ObjectId     : $spId"
Write-Host "TenantId     : $tenantId"
Write-Host "Subscription : $Subscription ($subscriptionId)"
Write-Host "Mode         : $Mode"
Write-Host "OutDir       : $OutDir"
Write-Host ""
Write-Host "Remaining manual step: grant Reader on the 'IT Engineering' DevOps project"
Write-Host "to this service principal (see runbook agent-identity-setup.md step 3)."
