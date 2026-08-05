from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


def opportunity_evidence(
    opportunity: dict[str, Any],
    telemetry: dict[str, Any] | None,
    rightsizing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    freshness = opportunity.get("observedAt")
    rationale = (
        rightsizing.get("reason")
        if rightsizing and rightsizing.get("reason")
        else opportunity.get("reason")
        or "No governed rationale was available."
    )
    return {
        "schemaVersion": "flux-evidence-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "evidenceType": "opportunity",
        "decisionStatus": "review_required",
        "opportunity": opportunity,
        "telemetry": telemetry or {
            "resourceId": opportunity.get("resourceId", ""),
            "metrics": [],
        },
        "decisionRationale": rationale,
        "rightSizing": rightsizing,
        "sourceFreshness": {
            "observedAt": freshness,
            "valuationSnapshot": opportunity.get("valuationCostSnapshotId", ""),
            "targetPriceSnapshot": opportunity.get("targetPriceSnapshotId", ""),
        },
        "uncertainty": {
            "confidence": opportunity.get("confidence") or "Review",
            "grossMonthlyValue": opportunity.get("monthlyGrossSavings"),
            "riskAdjustedMonthlyValue": opportunity.get(
                "monthlyRiskAdjustedSavings"
            ),
            "requiresOwnerValidation": True,
        },
        "changeRequest": {
            "title": f"Review {opportunity.get('title') or 'Flux opportunity'}",
            "requestedAction": (
                "Validate the recommendation and prepare an approved change; "
                "this draft does not authorize implementation."
            ),
            "implementationChecklist": [
                "Confirm resource owner and business service.",
                "Confirm maintenance window and dependent resources.",
                "Capture current configuration and recovery point.",
                "Apply only the separately approved change.",
            ],
            "validationChecklist": [
                "Validate service health and monitoring after the change.",
                "Confirm cost and telemetry evidence in the next Flux cycles.",
            ],
            "rollbackPlan": (
                "Restore the captured configuration or resource sizing if "
                "technical validation fails."
            ),
        },
        "guardrails": [
            "This package is decision support and does not authorize remediation.",
            "Validate ownership, recovery, contractual, and workload requirements.",
            "Savings are estimates; gross and risk-adjusted values are distinct.",
        ],
        "lineage": {
            "source": opportunity.get("source", ""),
            "observedAt": opportunity.get("observedAt"),
            "confidenceMethod": opportunity.get("confidenceMethodVersion", ""),
            "valuationMethod": opportunity.get("valuationMethodVersion", ""),
            "costSnapshot": opportunity.get("valuationCostSnapshotId", ""),
            "targetPriceSnapshot": opportunity.get("targetPriceSnapshotId", ""),
        },
    }


def anomaly_evidence(anomaly: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "flux-evidence-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "evidenceType": "cost_anomaly",
        "decisionStatus": "investigation_required",
        "anomaly": anomaly,
        "methodology": {
            "methodVersion": anomaly.get("methodVersion", ""),
            "baseline": "Matching-weekday median and median absolute deviation",
            "baselinePoints": anomaly.get("baselinePoints"),
            "robustScore": anomaly.get("kScore"),
        },
        "sourceFreshness": {
            "evaluatedAt": anomaly.get("evaluatedAt"),
            "evaluationDate": anomaly.get("evaluationDate"),
        },
        "uncertainty": {
            "signalOnly": True,
            "billingLatencyPossible": True,
            "creditsAndPurchasesNotDecomposed": True,
        },
        "changeRequest": {
            "title": (
                f"Investigate {anomaly.get('scopeType')} cost anomaly "
                f"{anomaly.get('scopeId')}"
            ),
            "requestedAction": (
                "Investigate the billed-cost change and document its owner, "
                "business cause, and disposition."
            ),
            "implementationChecklist": [
                "Confirm billing period completeness and currency.",
                "Compare the matching prior period and contributing services.",
                "Contact the resource or subscription owner.",
                "Record the investigation status in Flux.",
            ],
            "validationChecklist": [
                "Confirm whether spend returns to baseline.",
                "Validate any approved corrective action independently.",
            ],
            "rollbackPlan": (
                "No Azure mutation is proposed by this investigation draft."
            ),
        },
        "guardrails": [
            "A cost anomaly is a statistical signal, not proof of waste.",
            "Confirm billing latency, credits, purchases, and workload changes.",
            "Actual and amortized cost must be investigated independently.",
        ],
    }


def evidence_markdown(pack: dict[str, Any]) -> str:
    if pack["evidenceType"] == "opportunity":
        item = pack["opportunity"]
        lines = [
            "# Flux change-request draft — opportunity",
            "",
            f"- Evidence ID: `{item.get('id', '')}`",
            f"- Generated: {pack['generatedAt']}",
            f"- Resource: {item.get('resourceName') or item.get('resourceId')}",
            f"- Subscription: {item.get('subscriptionName') or item.get('subscriptionId')}",
            f"- Signal: {item.get('title', '')}",
            f"- Confidence: {item.get('confidence') or 'Review'}",
            f"- Source: {item.get('source', '')}",
            "",
            "## Finding",
            "",
            str(item.get("reason") or ""),
            "",
            "## Decision rationale",
            "",
            str(pack.get("decisionRationale") or ""),
            "",
            "## Valuation",
            "",
            f"- Gross monthly value: {item.get('monthlyGrossSavings')}",
            f"- Risk-adjusted monthly value: {item.get('monthlyRiskAdjustedSavings')}",
            f"- Currency: {item.get('valuationCurrency') or item.get('savingsCurrency')}",
            f"- Basis: {item.get('valuationBasis') or 'Not valued'}",
            "",
            "## Telemetry",
            "",
            f"- Governed metric summaries: {len((pack.get('telemetry') or {}).get('metrics', []))}",
            "",
            "## Proposed implementation checklist",
            "",
            *[
                f"- {value}"
                for value in pack["changeRequest"]["implementationChecklist"]
            ],
            "",
            "## Validation checklist",
            "",
            *[
                f"- {value}"
                for value in pack["changeRequest"]["validationChecklist"]
            ],
            "",
            "## Rollback",
            "",
            str(pack["changeRequest"]["rollbackPlan"]),
            "",
        ]
    else:
        item = pack["anomaly"]
        lines = [
            "# Flux change-request draft — cost investigation",
            "",
            f"- Generated: {pack['generatedAt']}",
            f"- Evaluation date: {item.get('evaluationDate')}",
            f"- Scope: {item.get('scopeType')} / {item.get('scopeId')}",
            f"- Cost type: {item.get('costType')}",
            f"- Severity: {item.get('severity')}",
            "",
            "## Signal",
            "",
            f"- Current amount: {item.get('currentAmount')} {item.get('currency')}",
            f"- Seasonal baseline: {item.get('baselineMedian')} {item.get('currency')}",
            f"- Absolute change: {item.get('absoluteChange')} {item.get('currency')}",
            f"- Percent change: {item.get('percentChange')}",
            f"- Robust score: {item.get('kScore')}",
            f"- Baseline points: {item.get('baselinePoints')}",
            "",
            str(item.get("reason") or ""),
            "",
            "## Top contributors",
            "",
            *[
                f"- {value.get('name')}: {value.get('current')} "
                f"{value.get('currency')} ({value.get('change')} change)"
                for value in pack.get("contributors", [])
            ],
            "",
            "## Investigation checklist",
            "",
            *[
                f"- {value}"
                for value in pack["changeRequest"]["implementationChecklist"]
            ],
            "",
            "## Validation checklist",
            "",
            *[
                f"- {value}"
                for value in pack["changeRequest"]["validationChecklist"]
            ],
            "",
        ]
    lines.extend(["## Guardrails", ""])
    lines.extend(f"- {value}" for value in pack["guardrails"])
    lines.extend(
        [
            "",
            "## Machine-readable payload",
            "",
            "```json",
            json.dumps(pack, indent=2, default=str),
            "```",
            "",
        ]
    )
    return "\n".join(lines)
