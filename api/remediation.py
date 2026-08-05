"""ServiceNow-ready planned remediation task packages from Flux Signals.

The minimum viable integration deliberately requires nothing from
ServiceNow: Flux builds complete, reviewable task packages (JSON and a
CSV shaped for import sets or manual form entry) for an allowlisted set
of high-confidence signals -- initially unattached managed disks -- and
tracks each task's lifecycle in the operational store so the same
resource and signal can never produce duplicate tasks while a
remediation is active. A reconcile import maps ServiceNow task numbers
and statuses back onto the correlation keys when (or if) they can be
read or exported from the tenant.

Nothing here deletes anything. The generated task content itself walks
the approver through validation, snapshot, retention, and separately
approved deletion.
"""
from __future__ import annotations

import hashlib
from typing import Any

# Lifecycle statuses that count as an active remediation: while a task for
# (signal, resource) sits in one of these, no new task is generated.
ACTIVE_STATUSES = (
    "exported",
    "submitted",
    "awaiting_validation",
    "approved",
    "snapshot_taken",
    "retention_wait",
)
TERMINAL_STATUSES = ("completed", "cancelled", "rejected")
# 'exported' means "handed to a human in a package", not "filed in
# ServiceNow" -- re-exporting the same correlation key is idempotent, so
# package regeneration only suppresses tasks that were actually filed.
SUPPRESS_STATUSES = tuple(s for s in ACTIVE_STATUSES if s != "exported")


def correlation_key(signal_kind: str, resource_id: str) -> str:
    """Stable identity for one remediation lifecycle of one resource."""
    digest = hashlib.sha256(
        f"{signal_kind}|{resource_id.lower()}".encode("utf-8")
    ).hexdigest()[:20]
    return f"flux-{signal_kind.replace('_', '-')}-{digest}"


def disk_task(
    finding: dict[str, Any],
    virtual_tags: dict[str, dict[str, str]],
    base_url: str,
) -> dict[str, Any]:
    """One complete planned-remediation task for an unattached disk."""
    resource_id = str(finding.get("resourceId") or "")
    name = str(finding.get("resourceName") or resource_id.rsplit("/", 1)[-1])
    monthly_cost = finding.get("actualMonthlyCost")
    # Retiring an unattached disk saves its full running cost, so the
    # disk's own cost is the savings estimate when none is provided.
    monthly = finding.get("estimatedMonthlySavings")
    if monthly is None:
        monthly = monthly_cost
    annual = round(float(monthly) * 12, 2) if monthly is not None else None
    tag = lambda key: (virtual_tags.get(key) or {}).get("value", "")
    key = correlation_key("unattached_disk", resource_id)
    return {
        "correlationKey": key,
        "signalKind": "unattached_disk",
        "title": f"Planned remediation: unattached managed disk {name}",
        "shortDescription": (
            f"Validate and retire unattached Azure disk {name} "
            f"({finding.get('subscriptionName') or ''})"
        ),
        "description": (
            f"Flux Signals identified managed disk {name} as unattached "
            f"with {finding.get('confidence') or ''} confidence "
            f"(score {finding.get('confidenceScore')}), first seen "
            f"{finding.get('firstSeen') or 'unknown'}, continuously for "
            f"{finding.get('ageDays') if finding.get('ageDays') is not None else '?'} days. "
            f"Reason: {finding.get('reason') or ''}"
        ),
        "signal": {
            "source": finding.get("source"),
            "kind": finding.get("kind"),
            "confidence": finding.get("confidence"),
            "confidenceScore": finding.get("confidenceScore"),
            "firstSeen": finding.get("firstSeen"),
            "lastSeen": finding.get("lastSeen"),
            "ageDays": finding.get("ageDays"),
            "consecutiveObservations": finding.get("consecutiveCount"),
            "evidence": finding.get("confidenceFactors"),
        },
        "resource": {
            "subscriptionId": finding.get("subscriptionId"),
            "subscriptionName": finding.get("subscriptionName"),
            "resourceGroup": finding.get("resourceGroup"),
            "diskName": name,
            "resourceId": resource_id,
            "region": finding.get("region"),
            "sku": finding.get("currentSku") or "",
            "relatedVm": finding.get("relatedResourceId") or "",
        },
        "financials": {
            "currentMonthlyCost": monthly_cost,
            "estimatedMonthlySavings": monthly,
            "estimatedAnnualSavings": annual,
            "currency": finding.get("savingsCurrency") or "USD",
        },
        "ownership": {
            "applicationOwner": tag("application-owner"),
            "itOwner": tag("it-owner"),
            "application": tag("application"),
            "department": tag("department"),
            "environment": tag("environment"),
        },
        "validationSteps": [
            "Confirm the disk still reports diskState Unattached in the "
            "Azure portal.",
            "Check the Azure activity log for attach/detach events in the "
            "last 30 days.",
            "Search backup, DR, ASR, and infrastructure-as-code "
            "repositories for references to the disk name or resource ID.",
            "Confirm with the owner above (or the subscription owner if "
            "blank) that the disk is not reserved for recovery.",
        ],
        "remediationSteps": [
            "Obtain owner sign-off on this task.",
            "Take a snapshot of the disk (Standard storage) and record the "
            "snapshot resource ID in this task.",
            "Wait the agreed retention period (default 30 days) with the "
            "disk unchanged.",
            "Delete the disk through the separately approved change "
            "process, recording the outcome here.",
        ],
        "risks": (
            "The disk may hold data referenced by a recovery, forensic, or "
            "seasonal process that does not show as an attachment. The "
            "snapshot plus retention window mitigates this; deletion "
            "without the snapshot step is not part of this plan."
        ),
        "approvalRequirements": (
            "Named owner (or subscription owner) approval plus standard "
            "change approval before the deletion step. This task plans and "
            "tracks; it does not authorize deletion by itself."
        ),
        "rollbackPlan": (
            "Before deletion: close this task with no action. After "
            "deletion: recreate the disk from the recorded snapshot "
            "(az disk create --source <snapshot-id>) and reattach if "
            "required. The snapshot must not be deleted until the "
            "retention window after disk deletion has passed."
        ),
        "links": {
            "fluxResource": f"{base_url}/#/inventory?search={name}",
            "fluxSignal": f"{base_url}/#/opportunities?search={name}",
            "azureResource": (
                f"https://portal.azure.com/#@/resource{resource_id}"
            ),
        },
    }


def task_description(task: dict[str, Any]) -> str:
    """The full ServiceNow description body, correlation ID included, so
    reconcile can find the record again by content."""
    return (
        task["description"]
        + f"\n\nCorrelation ID: {task['correlationKey']}"
        + "\nValidation: " + " | ".join(task["validationSteps"])
        + "\nRemediation: " + " | ".join(task["remediationSteps"])
        + f"\nRollback: {task['rollbackPlan']}"
        + f"\nFlux: {task['links']['fluxSignal']}"
    )


def servicenow_form_url(
    task: dict[str, Any],
    instance_url: str,
    table: str = "planned_task",
) -> str:
    """A pre-filled New Record link for the discovered planned_task form.

    Classic-UI forms accept sysparm_query field prefills; the interactive
    session is redirected into the normal navigation shell. Only plain
    text fields prefill reliably (reference fields such as assignment
    group and configuration item need internal sys_ids), so those stay a
    manual pick on the form. '^' is the sysparm_query field separator and
    must not appear inside values; the description is trimmed to keep the
    URL inside common length limits -- the CSV carries the full text.
    """
    from urllib.parse import quote

    def clean(value: str, limit: int) -> str:
        return value.replace("^", "-")[:limit]

    description = clean(task_description(task), 1800)
    query = (
        f"short_description={clean(task['shortDescription'], 150)}"
        f"^description={description}"
    )
    return (
        f"{instance_url}/{table}.do?sys_id=-1&sysparm_query="
        + quote(query, safe="")
    )


def package_console_script(
    tasks: list[dict[str, Any]],
    assignment_group: str,
    table: str = "planned_task",
    priority: str = "3",
    configuration_item: str = "",
    due_days: int = 30,
    limit: int = 0,
) -> str:
    """A self-contained batch-create script for the browser DevTools
    console of a signed-in ServiceNow tab.

    Same-origin fetch with the session's X-UserToken (window.g_ck) means
    no credentials ever leave the browser and every record is created as
    the signed-in user. The script confirms the batch size before
    creating anything, skips any task whose correlation ID already
    appears in an existing record (safe to re-run), and prints the
    correlation-to-TASK-number mapping as a ready-to-paste Flux
    reconcile payload.
    """
    import json as json_module

    selected = tasks[:limit] if limit else tasks
    payload = [
        {
            "correlationKey": task["correlationKey"],
            "shortDescription": task["shortDescription"],
            "description": task_description(task),
        }
        for task in selected
    ]
    tasks_json = json_module.dumps(payload, indent=1)
    group_json = json_module.dumps(assignment_group)
    table_json = json_module.dumps(table)
    priority_json = json_module.dumps(str(priority))
    ci_json = json_module.dumps(configuration_item)
    due_days_json = json_module.dumps(int(due_days))
    return (
        "// Flux ServiceNow batch: paste into the DevTools console of a\n"
        "// signed-in tab on your ServiceNow instance (any page).\n"
        "(async () => {\n"
        f"  const TABLE = {table_json};\n"
        f"  const GROUP = {group_json};\n"
        f"  const PRIORITY = {priority_json};\n"
        f"  const CI_NAME = {ci_json};\n"
        f"  const DUE_DAYS = {due_days_json};\n"
        f"  const TASKS = {tasks_json};\n"
        "  if (!location.hostname.endsWith('service-now.com')) {\n"
        "    console.error('Run this in a ServiceNow tab.'); return;\n"
        "  }\n"
        "  const token = window.g_ck\n"
        "    || document.querySelector('iframe#gsft_main')"
        "?.contentWindow?.g_ck;\n"
        "  if (!token) {\n"
        "    console.error('No session token found -- open any classic "
        "form (e.g. ' + TABLE + '.do) first, then re-run.'); return;\n"
        "  }\n"
        "  const headers = { 'Content-Type': 'application/json',\n"
        "    'Accept': 'application/json', 'X-UserToken': token };\n"
        "  const get = async (url) => (await fetch(url, "
        "{ headers })).json();\n"
        "  const groups = await get('/api/now/table/sys_user_group"
        "?sysparm_fields=sys_id&sysparm_limit=1&sysparm_query=name='\n"
        "    + encodeURIComponent(GROUP));\n"
        "  const groupId = groups.result?.[0]?.sys_id || '';\n"
        "  if (!groupId) console.warn('Assignment group not resolved; "
        "records will be created unassigned -- set it on the form.');\n"
        "  let ciId = '';\n"
        "  if (CI_NAME) {\n"
        "    const cis = await get('/api/now/table/cmdb_ci"
        "?sysparm_fields=sys_id,name&sysparm_limit=1&sysparm_query=name='\n"
        "      + encodeURIComponent(CI_NAME));\n"
        "    ciId = cis.result?.[0]?.sys_id || '';\n"
        "    if (!ciId) console.warn('Configuration item \"' + CI_NAME\n"
        "      + '\" not resolved; records will be created without a CI.');\n"
        "  }\n"
        "  const due = new Date(Date.now() + DUE_DAYS * 86400000)\n"
        "    .toISOString().slice(0, 10);\n"
        "  if (!confirm('Create up to ' + TASKS.length + ' ' + TABLE\n"
        "      + ' records assigned to ' + GROUP + ' (priority '\n"
        "      + PRIORITY + ', due ' + due + ')?')) return;\n"
        "  const results = [];\n"
        "  for (const task of TASKS) {\n"
        "    const existing = await get('/api/now/table/' + TABLE\n"
        "      + '?sysparm_fields=number&sysparm_limit=1"
        "&sysparm_query=descriptionLIKE'\n"
        "      + encodeURIComponent(task.correlationKey));\n"
        "    if (existing.result?.length) {\n"
        "      results.push({ correlationKey: task.correlationKey,\n"
        "        taskNumber: existing.result[0].number, "
        "outcome: 'already-exists' });\n"
        "      continue;\n"
        "    }\n"
        "    const response = await fetch('/api/now/table/' + TABLE, {\n"
        "      method: 'POST', headers,\n"
        "      body: JSON.stringify({\n"
        "        short_description: task.shortDescription,\n"
        "        description: task.description,\n"
        "        assignment_group: groupId, priority: PRIORITY,\n"
        "        cmdb_ci: ciId, due_date: due + ' 09:00:00',\n"
        "      }),\n"
        "    });\n"
        "    const body = await response.json().catch(() => ({}));\n"
        "    results.push({ correlationKey: task.correlationKey,\n"
        "      taskNumber: body.result?.number || ('HTTP ' + "
        "response.status),\n"
        "      outcome: response.ok ? 'created' : 'failed' });\n"
        "  }\n"
        "  console.table(results);\n"
        "  const reconcile = { updates: results\n"
        "    .filter((r) => r.outcome !== 'failed')\n"
        "    .map((r) => ({ correlationKey: r.correlationKey,\n"
        "      taskNumber: r.taskNumber, status: 'submitted' })) };\n"
        "  console.log('Flux reconcile payload:');\n"
        "  console.log(JSON.stringify(reconcile, null, 2));\n"
        "  console.log('To lock these lifecycles, open the Flux site, "
        "press F12, and paste the line below into ITS console:');\n"
        "  console.log(\"await fetch('/api/remediation/reconcile', "
        "{ method: 'POST', credentials: 'include', headers: "
        "{ 'Content-Type': 'application/json' }, body: JSON.stringify(\"\n"
        "    + JSON.stringify(reconcile)\n"
        "    + \") }).then(r => r.json())\");\n"
        "})();\n"
    )


PRIORITY_LABELS = {
    "1": "1 - Critical",
    "2": "2 - High",
    "3": "3 - Moderate",
    "4": "4 - Low",
    "5": "5 - Planning",
}


def package_csv(
    tasks: list[dict[str, Any]],
    assignment_group: str = "",
    priority: str = "3",
) -> str:
    """Flatten tasks into a ServiceNow-import-friendly CSV."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "correlation_id", "short_description", "description",
            "category", "subcategory", "priority", "assignment_group",
            "subscription_id", "subscription_name", "resource_group",
            "disk_name", "resource_id", "region", "sku",
            "current_monthly_cost", "estimated_monthly_savings",
            "estimated_annual_savings", "currency", "confidence",
            "first_detected", "application", "application_owner",
            "it_owner", "department", "environment",
            "validation_steps", "remediation_steps", "risks",
            "approval_requirements", "rollback_plan", "flux_link",
            "servicenow_prefilled_form_url",
        ]
    )
    for task in tasks:
        resource = task["resource"]
        financials = task["financials"]
        ownership = task["ownership"]
        writer.writerow(
            [
                task["correlationKey"],
                task["shortDescription"],
                task["description"],
                "Cloud Cost Optimization", "Azure Storage",
                PRIORITY_LABELS.get(str(priority), str(priority)),
                assignment_group,
                resource["subscriptionId"], resource["subscriptionName"],
                resource["resourceGroup"], resource["diskName"],
                resource["resourceId"], resource["region"], resource["sku"],
                financials["currentMonthlyCost"],
                financials["estimatedMonthlySavings"],
                financials["estimatedAnnualSavings"],
                financials["currency"],
                task["signal"]["confidence"],
                task["signal"]["firstSeen"],
                ownership["application"], ownership["applicationOwner"],
                ownership["itOwner"], ownership["department"],
                ownership["environment"],
                " | ".join(task["validationSteps"]),
                " | ".join(task["remediationSteps"]),
                task["risks"], task["approvalRequirements"],
                task["rollbackPlan"], task["links"]["fluxSignal"],
                task.get("servicenowFormUrl", ""),
            ]
        )
    return buffer.getvalue()
