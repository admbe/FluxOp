[CmdletBinding()]
param(
    [string]$BaseUri = "",
    [string]$AccessToken = "",
    [ValidateSet("reader", "admin")]
    [string]$ExpectedRole = "reader"
)

$ErrorActionPreference = "Stop"
$base = $BaseUri.TrimEnd("/")

if (-not $AccessToken) {
    $response = Invoke-WebRequest `
        -Uri "$base/api/health" `
        -MaximumRedirection 0 `
        -SkipHttpErrorCheck `
        -ErrorAction SilentlyContinue
    if ([int]$response.StatusCode -notin @(302, 401)) {
        throw "Expected the Entra boundary, received HTTP $([int]$response.StatusCode)."
    }
    Write-Host "PASS: Flux is running behind the Microsoft Entra boundary."
    Write-Host "Supply -AccessToken to validate authenticated roles and governed APIs."
    exit 0
}

$headers = @{ Authorization = "Bearer $AccessToken" }
$session = Invoke-RestMethod -Uri "$base/api/session" -Headers $headers
if (-not $session.authenticated -or -not $session.permissions.canRead) {
    throw "The supplied identity is not an authenticated Flux reader."
}
if ($ExpectedRole -eq "admin" -and -not $session.permissions.canManageIntegrations) {
    throw "The supplied identity does not have the expected Flux administrator role."
}

$checks = @(
    "/api/health",
    "/api/overview",
    "/api/recommendations/quality"
)
if ($ExpectedRole -eq "admin") {
    $checks += @(
        "/api/operations/health",
        "/api/integrations/azure",
        "/api/integrations/cost-reconciliation"
    )
}
foreach ($path in $checks) {
    Invoke-RestMethod -Uri "$base$path" -Headers $headers | Out-Null
    Write-Host "PASS: $path"
}

Write-Host "PASS: authenticated $ExpectedRole production smoke test completed."
