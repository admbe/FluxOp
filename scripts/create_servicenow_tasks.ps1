<#
.SYNOPSIS
Out-of-band ServiceNow filing for Flux planned remediation tasks.

Reads a flux-servicenow-tasks.csv (Opportunities -> ServiceNow tasks) and
creates one planned_task record per row via the ServiceNow Table API.

Safety model (matches apply_native_tags.ps1):
  - DRY-RUN BY DEFAULT: without -Execute nothing is created. The dry run
    still authenticates, verifies table API access, resolves the
    assignment group, and reports per-row what would happen -- so it
    doubles as the permissions probe.
  - Duplicate-proof: each row's correlation ID is searched in existing
    records first; matches are skipped. Safe to re-run after a partial
    failure.
  - No credential storage: pass -Credential (Get-Credential) for basic
    auth or -BearerToken for OAuth; nothing is written to disk.
  - Reconcile output: writes <csv-folder>\servicenow-reconcile.json
    mapping correlation IDs to created TASK numbers, ready for
    POST /api/remediation/reconcile so Flux locks the lifecycles.

.EXAMPLE
# Probe + preview (creates nothing):
./create_servicenow_tasks.ps1 -CsvPath ~\Downloads\flux-servicenow-tasks.csv -Credential (Get-Credential)

.EXAMPLE
# File everything >= $5/month:
./create_servicenow_tasks.ps1 -CsvPath ~\Downloads\flux-servicenow-tasks.csv -Credential (Get-Credential) -MinMonthlyCost 5 -Execute

.NOTES
If the dry run fails with 401/403 your role has form access but not API
access -- use the pre-filled form links in the CSV, or the console batch
script (Opportunities -> ServiceNow batch), which rides the browser
session instead.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$CsvPath,

    [string]$InstanceUrl = "",
    [string]$Table = "planned_task",
    [string]$AssignmentGroup = "AzureCloud_CF",
    [string]$Priority = "3",
    [string]$ConfigurationItem = "Azure AD (Entra)",
    [int]$DueDays = 30,

    # Client-side filter on top of whatever the CSV already contains.
    [double]$MinMonthlyCost = 0,

    [pscredential]$Credential,
    [string]$BearerToken,

    # Without this switch the script only probes and previews.
    [switch]$Execute,

    [string]$ReconcileOutPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InstanceUrl = $InstanceUrl.TrimEnd('/')

if (-not $Credential -and -not $BearerToken) {
    throw "Provide -Credential (Get-Credential) for basic auth or -BearerToken for OAuth."
}
if (-not (Test-Path $CsvPath)) {
    throw "CSV not found: $CsvPath"
}
if (-not $ReconcileOutPath) {
    $ReconcileOutPath = Join-Path (Split-Path -Parent (Resolve-Path $CsvPath)) 'servicenow-reconcile.json'
}

function Invoke-SnowApi {
    param(
        [Parameter(Mandatory)] [string]$Method,
        [Parameter(Mandatory)] [string]$Path,
        $Body
    )
    $call = @{
        Method      = $Method
        Uri         = "$InstanceUrl$Path"
        ContentType = 'application/json'
        Headers     = @{ Accept = 'application/json' }
    }
    if ($BearerToken) {
        $call.Headers.Authorization = "Bearer $BearerToken"
    }
    else {
        $call.Authentication = 'Basic'
        $call.Credential = $Credential
    }
    if ($null -ne $Body) {
        $call.Body = $Body | ConvertTo-Json -Depth 6
    }
    Invoke-RestMethod @call
}

# ---------------------------------------------------------------------------
# 1. Probe: does this identity have table API access at all?
# ---------------------------------------------------------------------------
try {
    $null = Invoke-SnowApi -Method GET -Path "/api/now/table/$Table`?sysparm_limit=1&sysparm_fields=number"
    Write-Host "API access confirmed: $Table is readable as this identity." -ForegroundColor Green
}
catch {
    $status = $_.Exception.Response.StatusCode.value__ 2>$null
    throw ("Table API probe failed (HTTP $status). Your account cannot use the REST API " +
        "with this auth method -- likely SSO without a local password, or an ACL. " +
        "Fall back to the CSV's pre-filled form links or the console batch script. ($_)")
}

# ---------------------------------------------------------------------------
# 2. Resolve the assignment group name to its sys_id.
# ---------------------------------------------------------------------------
$groupQuery = [uri]::EscapeDataString("name=$AssignmentGroup")
$groupResult = Invoke-SnowApi -Method GET -Path "/api/now/table/sys_user_group?sysparm_fields=sys_id,name&sysparm_limit=1&sysparm_query=$groupQuery"
$groupId = ''
if ($groupResult.result -and $groupResult.result.Count -gt 0) {
    $groupId = $groupResult.result[0].sys_id
    Write-Host "Assignment group '$AssignmentGroup' -> $groupId" -ForegroundColor Green
}
else {
    Write-Warning "Assignment group '$AssignmentGroup' not found or not readable; records will be created unassigned."
}

# ---------------------------------------------------------------------------
# 2b. Resolve the configuration item name to its sys_id (optional).
# ---------------------------------------------------------------------------
$ciId = ''
if ($ConfigurationItem) {
    $ciQuery = [uri]::EscapeDataString("name=$ConfigurationItem")
    $ciResult = Invoke-SnowApi -Method GET -Path "/api/now/table/cmdb_ci?sysparm_fields=sys_id,name&sysparm_limit=1&sysparm_query=$ciQuery"
    if ($ciResult.result -and $ciResult.result.Count -gt 0) {
        $ciId = $ciResult.result[0].sys_id
        Write-Host "Configuration item '$ConfigurationItem' -> $ciId" -ForegroundColor Green
    }
    else {
        Write-Warning "Configuration item '$ConfigurationItem' not found; records will be created without a CI."
    }
}
$dueDate = (Get-Date).AddDays($DueDays).ToString('yyyy-MM-dd') + ' 09:00:00'

# ---------------------------------------------------------------------------
# 3. Load and filter the Flux package rows.
# ---------------------------------------------------------------------------
$rows = Import-Csv -Path $CsvPath
$selected = @($rows | Where-Object {
        $monthly = 0.0
        $raw = if ($_.estimated_monthly_savings) { $_.estimated_monthly_savings } else { $_.current_monthly_cost }
        [double]::TryParse($raw, [ref]$monthly) | Out-Null
        $monthly -ge $MinMonthlyCost
    })
Write-Host ("{0} of {1} CSV rows selected (MinMonthlyCost={2})." -f $selected.Count, @($rows).Count, $MinMonthlyCost)
if ($selected.Count -eq 0) { return }

# ---------------------------------------------------------------------------
# 4. Per row: skip if the correlation ID already exists, else create.
# ---------------------------------------------------------------------------
$results = foreach ($row in $selected) {
    $key = $row.correlation_id
    $dupeQuery = [uri]::EscapeDataString("descriptionLIKE$key")
    $existing = Invoke-SnowApi -Method GET -Path "/api/now/table/$Table`?sysparm_fields=number&sysparm_limit=1&sysparm_query=$dupeQuery"
    if ($existing.result -and $existing.result.Count -gt 0) {
        [pscustomobject]@{
            correlationKey = $key
            disk           = $row.disk_name
            taskNumber     = $existing.result[0].number
            outcome        = 'already-exists'
        }
        continue
    }

    # Full description body, correlation ID included so dedupe and
    # reconcile can find the record again by content.
    $description = @(
        $row.description
        ''
        "Correlation ID: $key"
        "Validation: $($row.validation_steps)"
        "Remediation: $($row.remediation_steps)"
        "Risks: $($row.risks)"
        "Rollback: $($row.rollback_plan)"
        "Flux: $($row.flux_link)"
    ) -join "`n"

    if (-not $Execute) {
        [pscustomobject]@{
            correlationKey = $key
            disk           = $row.disk_name
            taskNumber     = '(dry run)'
            outcome        = 'would-create'
        }
        continue
    }

    try {
        $created = Invoke-SnowApi -Method POST -Path "/api/now/table/$Table" -Body @{
            short_description = $row.short_description
            description       = $description
            assignment_group  = $groupId
            priority          = $Priority
            cmdb_ci           = $ciId
            due_date          = $dueDate
        }
        [pscustomobject]@{
            correlationKey = $key
            disk           = $row.disk_name
            taskNumber     = $created.result.number
            outcome        = 'created'
        }
    }
    catch {
        Write-Warning "Create failed for $($row.disk_name): $_"
        [pscustomobject]@{
            correlationKey = $key
            disk           = $row.disk_name
            taskNumber     = ''
            outcome        = 'failed'
        }
    }
}

$results | Format-Table -AutoSize

# ---------------------------------------------------------------------------
# 5. Reconcile payload for Flux (created + already-existing records).
# ---------------------------------------------------------------------------
$updates = @($results | Where-Object { $_.taskNumber -and $_.outcome -in @('created', 'already-exists') } |
    ForEach-Object {
        @{ correlationKey = $_.correlationKey; taskNumber = $_.taskNumber; status = 'submitted' }
    })
if ($updates.Count -gt 0) {
    @{ updates = $updates } | ConvertTo-Json -Depth 4 | Set-Content -Path $ReconcileOutPath -Encoding utf8
    Write-Host "`nReconcile payload written to $ReconcileOutPath" -ForegroundColor Green
    Write-Host "Apply it so Flux locks these lifecycles: POST /api/remediation/reconcile with that JSON body (admin)."
}
if (-not $Execute) {
    Write-Host "`nDry run only -- re-run with -Execute to create the records above." -ForegroundColor Yellow
}
