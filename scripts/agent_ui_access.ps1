# Opens the Flux front end to the operator workstation so an automation agent
# can drive the real UI and catch render faults before a human does.
#
# READ THIS FIRST. This is deliberate weakening of a live site:
#
#   * Access restrictions deny every source except the addresses you allow,
#     so ANY OTHER USER OF FLUX IS LOCKED OUT while this is enabled. If people
#     other than the operator use flux.example.com, use -Slot instead and run
#     it against a deployment slot.
#   * -DisableAuth turns off Entra sign-in and puts the app in mock-auth mode,
#     where every request is a local administrator. The IP allowlist becomes
#     the only control. Never leave it on unattended.
#
# The safer default is IP restriction WITHOUT -DisableAuth: pair it with the
# Flux.Admin app role from provision_agent_access.ps1 and the agent can call
# every API with a bearer token while humans still sign in normally. Only add
# -DisableAuth when browser rendering itself has to be verified.
#
#   pwsh -File scripts/agent_ui_access.ps1 -Enable                  # IP allowlist only
#   pwsh -File scripts/agent_ui_access.ps1 -Enable -DisableAuth     # + mock auth
#   pwsh -File scripts/agent_ui_access.ps1 -Disable                 # revert everything

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Enable,
    [switch]$Disable,
    [switch]$DisableAuth,
    [string[]]$AllowIp,
    [string]$WebAppName    = "FluxFinOps",
    [string]$ResourceGroup = "prod-example-westus3-rg",
    [string]$Slot,
    [int]$ExpireHours = 8
)

$ErrorActionPreference = "Stop"
$RuleName = "agent-operator-workstation"
$slotArgs = if ($Slot) { @("--slot", $Slot) } else { @() }
$target = if ($Slot) { "$WebAppName/$Slot" } else { $WebAppName }

if (-not ($Enable -xor $Disable)) { throw "Pass exactly one of -Enable or -Disable." }

if ($Enable) {
    if (-not $AllowIp) {
        # Whatever Azure sees is what the restriction must name; a NAT or VPN
        # egress is rarely the address the workstation reports locally.
        $ip = (Invoke-RestMethod -Uri "https://api.ipify.org?format=json").ip
        $AllowIp = @("$ip/32")
        Write-Host "Detected egress address $ip"
    }

    if (-not $Slot) {
        Write-Warning "Applying to the live site. Every other user will be blocked until you run -Disable."
    }
    if ($DisableAuth -and -not $PSCmdlet.ShouldProcess($target, "DISABLE Entra sign-in (mock auth)")) { return }

    $priority = 100
    foreach ($cidr in $AllowIp) {
        $name = "$RuleName-$priority"
        if ($PSCmdlet.ShouldProcess($target, "allow $cidr")) {
            az webapp config access-restriction add -g $ResourceGroup -n $WebAppName @slotArgs `
                --rule-name $name --action Allow --ip-address $cidr --priority $priority | Out-Null
            Write-Host "ALLOW $cidr (priority $priority)"
        }
        $priority += 10
    }
    # An explicit allow rule makes App Service deny everything else implicitly;
    # the SCM site keeps its own rules so Kudu and the pipeline still work.

    if ($DisableAuth) {
        az webapp config appsettings set -g $ResourceGroup -n $WebAppName @slotArgs `
            --settings FLUX_AUTH_MODE=mock FLUX_ALLOW_MOCK_AUTH=1 `
            "FLUX_AGENT_UI_WINDOW_EXPIRES=$((Get-Date).ToUniversalTime().AddHours($ExpireHours).ToString('o'))" | Out-Null
        Write-Host "AUTH DISABLED - mock mode. Expires by convention at +$ExpireHours h; run -Disable to restore."
        Write-Warning "Every request is now a local administrator from the allowed addresses."
    } else {
        Write-Host "Auth left ON. Use a bearer token for api://<flux-app-id> (see provision_agent_access.ps1)."
    }
    Write-Host ""
    Write-Host "Revert with: pwsh -File scripts/agent_ui_access.ps1 -Disable" + $(if ($Slot) { " -Slot $Slot" } else { "" })
}

if ($Disable) {
    $existing = az webapp config access-restriction show -g $ResourceGroup -n $WebAppName @slotArgs -o json | ConvertFrom-Json
    foreach ($rule in $existing.ipSecurityRestrictions) {
        if ($rule.name -like "$RuleName*") {
            if ($PSCmdlet.ShouldProcess($target, "remove $($rule.name)")) {
                az webapp config access-restriction remove -g $ResourceGroup -n $WebAppName @slotArgs `
                    --rule-name $rule.name | Out-Null
                Write-Host "REMOVED $($rule.name)"
            }
        }
    }
    $mode = az webapp config appsettings list -g $ResourceGroup -n $WebAppName @slotArgs `
        --query "[?name=='FLUX_AUTH_MODE'].value" -o tsv
    if ($mode -eq "mock") {
        az webapp config appsettings set -g $ResourceGroup -n $WebAppName @slotArgs `
            --settings FLUX_AUTH_MODE=entra | Out-Null
        Write-Host "AUTH RESTORED - entra"
    }
    # FLUX_ALLOW_MOCK_AUTH is the unlock that makes mock mode serve at all;
    # clearing it is what actually re-closes the door if FLUX_AUTH_MODE is
    # ever left behind.
    az webapp config appsettings delete -g $ResourceGroup -n $WebAppName @slotArgs `
        --setting-names FLUX_ALLOW_MOCK_AUTH 2>$null | Out-Null
    az webapp config appsettings delete -g $ResourceGroup -n $WebAppName @slotArgs `
        --setting-names FLUX_AGENT_UI_WINDOW_EXPIRES 2>$null | Out-Null
    Write-Host "Reverted. Confirm sign-in works before walking away."
}
