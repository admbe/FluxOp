from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Mapping


METHOD_VERSION = "cost-seasonal-mad-v1"


def evaluate_cost_series(
    amounts: Mapping[date, float],
    evaluation_date: date,
    *,
    minimum_history_days: int = 28,
    minimum_baseline_points: int = 4,
    baseline_weeks: int = 8,
    threshold_k: float = 3.5,
    minimum_increase: float = 10.0,
) -> dict[str, float | int | str | None]:
    """Evaluate a daily cost series against prior matching weekdays.

    Missing days after a scope first appears are treated as zero. This makes the
    comparison seasonal without pretending that a newly observed scope has a
    mature baseline.
    """
    normalized = {
        observed_date: float(amount or 0)
        for observed_date, amount in amounts.items()
        if observed_date <= evaluation_date
    }
    current = normalized.get(evaluation_date, 0.0)
    first_seen = min(normalized) if normalized else evaluation_date
    history_days = max((evaluation_date - first_seen).days, 0)
    baseline_dates = [
        evaluation_date - timedelta(days=7 * offset)
        for offset in range(1, max(baseline_weeks, 1) + 1)
        if evaluation_date - timedelta(days=7 * offset) >= first_seen
    ]
    baseline_values = [normalized.get(day, 0.0) for day in baseline_dates]
    previous_week = (
        normalized.get(evaluation_date - timedelta(days=7))
        if evaluation_date - timedelta(days=7) >= first_seen
        else None
    )

    result: dict[str, float | int | str | None] = {
        "status": "warming_up",
        "severity": "none",
        "currentAmount": current,
        "baselinePoints": len(baseline_values),
        "baselineMedian": None,
        "mad": None,
        "kScore": None,
        "previousWeekAmount": previous_week,
        "absoluteChange": None,
        "percentChange": None,
        "reason": (
            f"Baseline warming up: {history_days} of "
            f"{minimum_history_days} required history days."
        ),
        "methodVersion": METHOD_VERSION,
    }
    if (
        history_days < minimum_history_days
        or len(baseline_values) < minimum_baseline_points
    ):
        return result

    center = float(median(baseline_values))
    deviations = [abs(value - center) for value in baseline_values]
    mad = float(median(deviations))
    change = current - center
    percent_change = (change / center * 100) if center else None
    if mad > 0:
        k_score = 0.6745 * change / mad
    elif change > 0:
        k_score = threshold_k * 2
    else:
        k_score = 0.0

    anomalous = (
        current > 0
        and change >= minimum_increase
        and k_score >= threshold_k
        and (center == 0 or percent_change is None or percent_change >= 25)
    )
    severity = "none"
    if anomalous:
        severity = (
            "high"
            if k_score >= threshold_k * 2
            or change >= max(100.0, center)
            else "medium"
        )
    result.update(
        {
            "status": "anomalous" if anomalous else "normal",
            "severity": severity,
            "baselineMedian": center,
            "mad": mad,
            "kScore": k_score,
            "absoluteChange": change,
            "percentChange": percent_change,
            "reason": (
                (
                    f"Daily cost is {change:,.2f} above the "
                    f"{len(baseline_values)}-point matching-weekday median."
                )
                if anomalous
                else (
                    f"Daily cost is within the governed "
                    f"{len(baseline_values)}-point matching-weekday baseline."
                )
            ),
        }
    )
    return result
