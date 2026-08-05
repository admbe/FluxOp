from __future__ import annotations

from datetime import datetime
from typing import Any


METHOD_VERSION = "opportunity-confidence-v1"
BASE_WEIGHTS = {
    "persistence": 0.35,
    "corroboration": 0.25,
    "sourceEvidence": 0.25,
    "freshness": 0.15,
}
UTILIZATION_WEIGHTS = {
    "persistence": 0.25,
    "corroboration": 0.20,
    "sourceEvidence": 0.20,
    "freshness": 0.10,
    "telemetry": 0.25,
}


def _freshness(last_seen: datetime, computed_at: datetime) -> float:
    age_days = max(0, (computed_at - last_seen).days)
    if age_days <= 2:
        return 1.0
    if age_days <= 7:
        return 0.7
    if age_days <= 30:
        return 0.4
    return 0.2


def _telemetry(status: str) -> float:
    return {
        "covered": 1.0,
        "no_data": 0.25,
        "error": 0.0,
    }.get(status, 0.1)


def confidence_score(
    *,
    family: str,
    consecutive_count: int,
    source_count: int,
    source_evidence: float,
    last_seen: datetime,
    computed_at: datetime,
    telemetry_status: str = "",
) -> dict[str, Any]:
    # DuckDB may return fixed-point aggregates as Decimal in production even
    # when test fixtures use Python floats. Normalize at this pure boundary so
    # persisted history can always be rescored during startup or migration.
    evidence = float(source_evidence)
    utilization_dependent = family == "compute_shutdown"
    factors = {
        "persistence": min(1.0, 0.2 * max(1, consecutive_count)),
        "corroboration": 1.0 if source_count > 1 else 0.5,
        "sourceEvidence": max(0.0, min(1.0, evidence)),
        "freshness": _freshness(last_seen, computed_at),
    }
    weights = (
        dict(UTILIZATION_WEIGHTS)
        if utilization_dependent
        else dict(BASE_WEIGHTS)
    )
    if utilization_dependent:
        factors["telemetry"] = _telemetry(telemetry_status)
    contributions = {
        name: round(factors[name] * weight, 4)
        for name, weight in weights.items()
    }
    score = round(sum(contributions.values()), 4)
    label = "High" if score >= 0.75 else "Medium" if score >= 0.5 else "Review"
    return {
        "score": score,
        "label": label,
        "factors": factors,
        "weights": weights,
        "contributions": contributions,
        "telemetryApplicable": utilization_dependent,
        "methodVersion": METHOD_VERSION,
    }
