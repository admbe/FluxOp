# Grants the existing Flux-FinOps-Monitor service principal the additional
# read access an automation agent needs to verify production end to end, plus
# the Flux.Admin app role so it can call the governed admin API instead of
# handing every write back to a human.
#
# Deliberately reuses the identity that already exists (runbook section 8)
# rather than creating another one. It does NOT touch
# Flux-FinOps-Export-Provisioner: that identity is workload-identity-federated
# and the runbook's quarterly review explicitly checks that no certificate or
# secret was added to it as a fallback.
#
# Run as a user who can assign Azure RBAC on the storage account and edit the
# Flux Entra app registration. Steps are idempotent and print SKIP when the
# grant already exists. Nothing here grants write access to Azure resources,
# billing, or Key Vault.
#
#   pwsh -File scripts/provision_agent_access.ps1            # apply
#   pwsh -File scripts/provision_agent_access.ps1 -WhatIf    # show only

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Flux-FinOps-Monitor (runbook section 8).
    [string]$AgentAppId    = "f589122a-b275-49a5-a86c-fe69b44e859c",
    # The Easy Auth app registration in front of the Flux App Service.
    [string]$FluxAppId     = "8bb2139c-8875-4594-af15-d55d3d115c14",
    [string]$StorageId     = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/prod-example-westus3-rg/providers/Microsoft.Storage/storageAccounts/prodfinopswestus3sa",
    [string]$WebAppName    = "FluxFinOps",
    [string]$ResourceGroup = "prod-example-westus3-rg",
    [string]$BillingAccount = "74631236",
    [string]$PostgresServer = "pg-flux-prod"
)

$ErrorActionPreference = "Stop"
function Note($m) { Write-Host $m }

$agentSpId = az ad sp show --id $AgentAppId --query id -o tsv
if (-not $agentSpId) { throw "Agent service principal $AgentAppId not found." }
Note "Agent SP object id: $agentSpId"

# ---------------------------------------------------------------------------
# 1. Flux.Admin app role, assignable to applications
# ---------------------------------------------------------------------------
# The app role already exists for users (FLUX_ENTRA_ADMIN_ASSIGNMENTS is
# "Flux.Admin"), but a role only granted to users cannot be held by a daemon
# identity. allowedMemberTypes must include Application. No Flux code change
# is needed: api/auth.py maps the token's `roles` claim through the same
# assignment lists it already uses for people.
$app = az ad app show --id $FluxAppId -o json | ConvertFrom-Json
$adminRole = $app.appRoles | Where-Object { $_.value -eq "Flux.Admin" }
if (-not $adminRole) {
    throw "The Flux app has no Flux.Admin app role; create it before running this."
}
if ($adminRole.allowedMemberTypes -notcontains "Application") {
    if ($PSCmdlet.ShouldProcess("Flux.Admin", "allow Application member type")) {
        $roles = $app.appRoles
        foreach ($r in $roles) {
            if ($r.value -eq "Flux.Admin" -and $r.allowedMemberTypes -notcontains "Application") {
                $r.allowedMemberTypes = @("User", "Application")
            }
        }
        $tmp = New-TemporaryFile
        ($roles | ConvertTo-Json -Depth 10 -AsArray) | Set-Content -Path $tmp -Encoding utf8
        az ad app update --id $FluxAppId --app-roles "@$tmp" | Out-Null
        Remove-Item $tmp -ErrorAction SilentlyContinue
        Note "UPDATED Flux.Admin now assignable to applications"
        Start-Sleep -Seconds 10   # directory replication
        $app = az ad app show --id $FluxAppId -o json | ConvertFrom-Json
        $adminRole = $app.appRoles | Where-Object { $_.value -eq "Flux.Admin" }
    }
} else {
    Note "SKIP  Flux.Admin already assignable to applications"
}

# ---------------------------------------------------------------------------
# 2. Assign Flux.Admin to the agent SP
# ---------------------------------------------------------------------------
$fluxSpId = az ad sp show --id $FluxAppId --query id -o tsv
$existing = az rest --method GET `
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$agentSpId/appRoleAssignments" `
    -o json | ConvertFrom-Json
$already = $existing.value | Where-Object {
    $_.resourceId -eq $fluxSpId -and $_.appRoleId -eq $adminRole.id
}
if ($already) {
    Note "SKIP  agent already holds Flux.Admin"
} elseif ($PSCmdlet.ShouldProcess("agent SP", "assign Flux.Admin")) {
    $body = @{
        principalId = $agentSpId
        resourceId  = $fluxSpId
        appRoleId   = $adminRole.id
    } | ConvertTo-Json -Compress
    $tmp = New-TemporaryFile
    [System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding($false)))
    az rest --method POST `
        --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$agentSpId/appRoleAssignedTo" `
        --headers "Content-Type=application/json" --body "@$tmp" | Out-Null
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Note "GRANTED Flux.Admin to the agent SP"
}

# ---------------------------------------------------------------------------
# 3. Let Easy Auth accept tokens from the agent SP
# ---------------------------------------------------------------------------
# authV2 currently pins allowed_client_applications to the Flux app itself, so
# a token issued to any other client is rejected before Flux sees it.
$authUri = "https://management.azure.com/subscriptions/$((az account show --query id -o tsv))/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$WebAppName/config/authsettingsV2?api-version=2023-01-01"
$auth = az rest --method GET --uri $authUri -o json | ConvertFrom-Json
# The validation branch is absent entirely while the portal is set to "allow
# requests only from this application itself", so every level has to be
# created before the list can be written. Reading straight through the path
# throws on a fresh app.
$aad = $auth.properties.identityProviders.azureActiveDirectory
if (-not $aad) { throw "No Microsoft identity provider is configured on $WebAppName." }
if (-not $aad.PSObject.Properties['validation'] -or -not $aad.validation) {
    $aad | Add-Member -NotePropertyName validation -NotePropertyValue ([pscustomobject]@{}) -Force
}
if (-not $aad.validation.PSObject.Properties['defaultAuthorizationPolicy'] -or
    -not $aad.validation.defaultAuthorizationPolicy) {
    $aad.validation | Add-Member -NotePropertyName defaultAuthorizationPolicy `
        -NotePropertyValue ([pscustomobject]@{}) -Force
}
$policy = $aad.validation.defaultAuthorizationPolicy
if (-not $policy.PSObject.Properties['allowedApplications'] -or -not $policy.allowedApplications) {
    # Switching away from "this application itself" drops the implicit
    # self-allow, so the Flux app must be listed explicitly or every browser
    # sign-in starts failing.
    $policy | Add-Member -NotePropertyName allowedApplications `
        -NotePropertyValue @($FluxAppId) -Force
}
$claims = $policy.allowedApplications
if ($claims -contains $AgentAppId) {
    Note "SKIP  agent already in allowedApplications"
} elseif ($PSCmdlet.ShouldProcess("Easy Auth", "allow the agent client application")) {
    $updated = @($FluxAppId) + @($claims) + $AgentAppId | Where-Object { $_ } | Select-Object -Unique
    $policy.allowedApplications = $updated
    Note ("  allowedApplications will be: " + ($updated -join ', '))
    $tmp = New-TemporaryFile
    [System.IO.File]::WriteAllText(
        $tmp, ($auth | ConvertTo-Json -Depth 30 -Compress),
        (New-Object System.Text.UTF8Encoding($false))
    )
    az rest --method PUT --uri $authUri --headers "Content-Type=application/json" --body "@$tmp" | Out-Null
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Note "UPDATED Easy Auth allowedApplications now includes the agent"
}

# ---------------------------------------------------------------------------
# 4. Storage Blob Data Reader on the cost-export account
# ---------------------------------------------------------------------------
# Lets the agent confirm whether an export actually delivered a file instead of
# inferring it from job logs, which cost several five-minute round trips.
$hasBlob = az role assignment list --assignee $AgentAppId --scope $StorageId `
    --role "Storage Blob Data Reader" --query "[0].id" -o tsv 2>$null
if ($hasBlob) {
    Note "SKIP  Storage Blob Data Reader already assigned"
} elseif ($PSCmdlet.ShouldProcess($StorageId, "assign Storage Blob Data Reader")) {
    az role assignment create --assignee-object-id $agentSpId `
        --assignee-principal-type ServicePrincipal `
        --role "Storage Blob Data Reader" --scope $StorageId | Out-Null
    Note "GRANTED Storage Blob Data Reader"
}

# ---------------------------------------------------------------------------
# 5 and 6: the two grants this script cannot make
# ---------------------------------------------------------------------------
Note ""
Note "REMAINING - these cannot be automated from Azure RBAC:"
Note ""
Note "  EnrollmentReader on billing account $BillingAccount"
Note "    Azure assigns service principals read-only enrollment roles only, and"
Note "    the assignment is a billing-plane operation an Enterprise Administrator"
Note "    must make. Portal: Cost Management + Billing > enrollment > Access"
Note "    control > Add > Enrollment Reader > search the app by name."
Note "    Gains: read export definitions and run history, so a delivery failure"
Note "    is diagnosable without a human running az rest."
Note ""
Note "  Read-only PostgreSQL role on $PostgresServer"
Note "    Connect as the admin (credential in kv-flux-prod, secret"
Note "    operational-database-url) and run:"
Note "      CREATE ROLE flux_agent_ro LOGIN PASSWORD '<generated>';"
Note "      GRANT CONNECT ON DATABASE <db> TO flux_agent_ro;"
Note "      GRANT USAGE ON SCHEMA public TO flux_agent_ro;"
Note "      GRANT SELECT ON ALL TABLES IN SCHEMA public TO flux_agent_ro;"
Note "      ALTER DEFAULT PRIVILEGES IN SCHEMA public"
Note "        GRANT SELECT ON TABLES TO flux_agent_ro;"
Note "    Then add a firewall rule for the workstation egress IP and store the"
Note "    password in the same Key Vault. SELECT only - no writes."
Note ""
Note "Verify the app-role path once directory replication settles:"
Note "  az login --service-principal --username $AgentAppId ``"
Note "    --certificate <flux-monitor-key.pem> --tenant <tenant>"
Note "  TOKEN=`$(az account get-access-token --resource api://$FluxAppId --query accessToken -o tsv)"
Note "  curl -H `"Authorization: Bearer `$TOKEN`" https://flux.example.com/api/session"
Note "  Expect authenticated=true with the admin role."
