from __future__ import annotations

from typing import Any


REPORT_CATALOG: dict[str, Any] = {
    "version": "2026-08-04.1",
    "contract": (
        "Read-only governed reporting metadata. The catalog exposes approved "
        "measures and dimensions; it does not expose arbitrary SQL."
    ),
    "reports": [
        {
            "id": "cost-summary",
            "name": "Cost summary",
            "maturity": "production",
            "endpoint": "/api/reports/cost",
            "grain": "Daily cost line aggregate",
            "measures": [
                "totalCost",
                "previousCost",
                "changeAmount",
                "changePercent",
                "averageDailyCost",
                "forecast",
            ],
            "dimensions": [
                "date",
                "subscription",
                "service",
                "resourceGroup",
                "resource",
                "region",
                "resourceType",
                "tagCoverage",
            ],
            "sources": ["Azure Cost Management", "Azure Resource Graph"],
            "filters": [
                "costType", "currency", "startDate", "endDate",
                "subscriptionId", "serviceName", "resourceId",
            ],
            "joins": [
                {
                    "from": "daily_cost_history.resource_id",
                    "to": "resources_current.resource_id",
                    "cardinality": "many-to-one",
                    "purpose": "Current resource identity only",
                }
            ],
        },
        {
            "id": "focus-cost-investigation",
            "name": "FOCUS cost investigation",
            "maturity": "preview",
            "endpoint": "/api/reports/focus-cost",
            "grain": "FOCUS v1.0 charge line",
            "measures": [
                "billedCost",
                "effectiveCost",
                "contractedCost",
                "listCost",
                "billedVsEffectiveDifference",
                "chargeCount",
                "resourceCount",
            ],
            "dimensions": [
                "date",
                "subscription",
                "service",
                "chargeCategory",
                "pricingCategory",
                "commitmentDiscountType",
                "resource",
                "resourceType",
                "resourceGroup",
                "region",
                "sku",
                "meter",
                "currency",
            ],
            "sources": ["Azure Cost Management FOCUS v1.0 exports"],
            "filters": [
                "currency",
                "startDate",
                "endDate",
                "subscriptionId",
                "serviceName",
                "resourceId",
                "chargeCategory",
                "pricingCategory",
                "commitmentDiscountType",
            ],
            "joins": [
                {
                    "from": "focus_cost_current.manifest_id",
                    "to": "focus_manifests_current.manifest_id",
                    "cardinality": "many-to-one",
                    "purpose": "Export coverage, version, and freshness lineage",
                }
            ],
        },
        {
            "id": "virtual-tag-showback",
            "name": "Virtual tag showback",
            "maturity": "production",
            "endpoint": "/api/reports/virtual-tags",
            "grain": "Monthly cost by resource and effective virtual-tag value",
            "measures": [
                "totalCost", "classifiedCost", "classifiedPercent",
                "resourceCount", "valueCount",
            ],
            "dimensions": [
                "virtualTagDimension", "virtualTagValue", "month",
                "resource", "subscription", "resourceGroup",
                "resourceType", "assignmentSource",
            ],
            "sources": [
                "Azure Cost Management daily history",
                "Azure Resource Graph current inventory",
                "Flux virtual-tag operational state",
            ],
            "filters": [
                "dimension", "value", "costType", "startDate", "endDate",
            ],
            "joins": [
                {
                    "from": "daily_cost_history.resource_id",
                    "to": "resources_current.resource_id",
                    "cardinality": "many-to-one",
                    "purpose": "Current effective virtual-tag classification",
                }
            ],
        },
        {
            "id": "workload-optimization",
            "name": "Workload optimization",
            "maturity": "production",
            "endpoint": "/api/reports/workload",
            "grain": "Current governed opportunity",
            "measures": [
                "opportunityCount",
                "monthlyGrossSavings",
                "monthlyRiskAdjustedSavings",
                "confidence",
                "ageDays",
                "telemetryCoverage",
            ],
            "dimensions": [
                "source",
                "category",
                "kind",
                "subscription",
                "resourceType",
                "confidence",
            ],
            "sources": [
                "Azure Advisor",
                "Flux Signals",
                "Azure Cost Management",
                "Azure Monitor",
                "LogicMonitor",
            ],
            "filters": [
                "source", "category", "kind", "subscriptionId",
                "resourceType", "confidence",
            ],
            "joins": [
                {
                    "from": "opportunity.resource_id",
                    "to": "telemetry/resource/valuation current views",
                    "cardinality": "one-to-zero-or-one per evidence source",
                    "purpose": "Governed confidence and valuation enrichment",
                }
            ],
        },
        {
            "id": "governance-posture",
            "name": "Governance posture",
            "maturity": "preview",
            "endpoint": "/api/reports/governance",
            "grain": "Policy assignment per subscription",
            "measures": [
                "evaluated",
                "compliant",
                "nonCompliant",
                "exempt",
                "compliancePercent",
            ],
            "dimensions": [
                "subscription", "policyAssignment", "policyDefinition",
                "resource", "complianceState", "exemption",
            ],
            "sources": ["Azure Resource Graph PolicyResources"],
            "filters": [
                "subscriptionId", "assignmentId", "complianceState",
            ],
            "joins": [
                {
                    "from": "policy resource assignment_id",
                    "to": "policy posture assignment_id",
                    "cardinality": "many-to-one",
                    "purpose": "Resource drilldown under assignment totals",
                }
            ],
        },
        {
            "id": "cost-anomalies",
            "name": "Cost anomalies",
            "maturity": "production",
            "endpoint": "/api/cost/anomalies",
            "grain": "Evaluation date, cost type, and evaluated scope",
            "measures": [
                "currentAmount",
                "baselineMedian",
                "absoluteChange",
                "percentChange",
                "kScore",
            ],
            "dimensions": [
                "costType",
                "scopeType",
                "subscription",
                "service",
                "resource",
                "severity",
                "reviewStatus",
            ],
            "sources": ["Azure Cost Management daily history"],
            "filters": [
                "costType", "scopeType", "subscriptionId", "serviceName",
                "severity", "status", "reviewStatus",
            ],
            "joins": [
                {
                    "from": "cost anomaly scope",
                    "to": "daily_cost_history governed scope",
                    "cardinality": "one-to-many",
                    "purpose": "Previous-period contributors",
                }
            ],
        },
    ],
    "guardrails": [
        "Currency is never summed across currencies without an explicit Mixed label.",
        "Actual and amortized cost are separate measures.",
        "Forecast output includes method, readiness, and confidence bounds.",
        "Opportunity value distinguishes gross and risk-adjusted estimates.",
        "Every report declares source lineage and limitations.",
    ],
}


def report_catalog() -> dict[str, Any]:
    return REPORT_CATALOG


def validate_report_request(payload: dict[str, Any]) -> dict[str, Any]:
    if any(key.lower() in {"sql", "query", "statement"} for key in payload):
        raise ValueError("Arbitrary SQL or query text is not accepted.")
    report = next(
        (
            item for item in REPORT_CATALOG["reports"]
            if item["id"] == payload.get("reportId")
        ),
        None,
    )
    if not report:
        raise ValueError("Unknown governed report.")
    requested_measures = payload.get("measures") or report["measures"]
    requested_dimensions = payload.get("dimensions") or report["dimensions"]
    requested_filters = payload.get("filters") or {}
    invalid_measures = sorted(set(requested_measures) - set(report["measures"]))
    invalid_dimensions = sorted(
        set(requested_dimensions) - set(report["dimensions"])
    )
    invalid_filters = sorted(set(requested_filters) - set(report["filters"]))
    if invalid_measures or invalid_dimensions or invalid_filters:
        raise ValueError(
            "Request contains undeclared catalog fields: "
            f"measures={invalid_measures}, dimensions={invalid_dimensions}, "
            f"filters={invalid_filters}."
        )
    return {
        "status": "approved",
        "reportId": report["id"],
        "endpoint": report["endpoint"],
        "measures": requested_measures,
        "dimensions": requested_dimensions,
        "filters": requested_filters,
        "contractVersion": REPORT_CATALOG["version"],
    }
