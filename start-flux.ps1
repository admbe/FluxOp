[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$Build
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$frontend = Join-Path $root "frontend"
$env:FLUX_PORT = "$Port"

if ($Build -or -not (Test-Path (Join-Path $frontend "dist\index.html"))) {
    Push-Location $frontend
    try {
        npm run build
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Starting Flux at http://127.0.0.1:$Port" -ForegroundColor Green
python (Join-Path $root "app.py")
