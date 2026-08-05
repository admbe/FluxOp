from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Mapping


METHOD_VERSION = "weekday-seasonal-forecast-v1"
FY_METHOD_VERSION = "post-migration-run-rate-v1"
FY_SEASONAL_METHOD_VERSION = "seasonal-yoy-comparison-v1"


def _month_add(month: date, count: int) -> date:
    total = month.year * 12 + (month.month - 1) + count
    return date(total // 12, total % 12 + 1, 1)


def fiscal_year_frame(
    as_of: date, fy_start_month: int
) -> tuple[date, date, str]:
    """The fiscal year containing as_of: (first month, last month, label).

    Fiscal years are named for the calendar year they end in, so a July-June
    year running 2026-07 through 2027-06 is FY2027. A January start names the
    year it covers.
    """
    start_year = (
        as_of.year if as_of.month >= fy_start_month else as_of.year - 1
    )
    fy_start = date(start_year, fy_start_month, 1)
    fy_last_month = _month_add(fy_start, 11)
    label = f"FY{fy_last_month.year}"
    return fy_start, fy_last_month, label


def forecast_fiscal_year(
    monthly: Mapping[date, float],
    *,
    as_of: date,
    fy_start_month: int = 7,
    growth_percent_monthly: float = 0.0,
    planned_savings_monthly: float = 0.0,
    savings_ramp_months: int = 3,
    current_month_estimate: float | None = None,
    method: str = FY_METHOD_VERSION,
) -> dict:
    """Return the governed post-migration forecast with an optional comparison.

    The primary forecast uses the trailing three complete months as the
    post-migration run-rate. The legacy same-month-last-year model remains
    available as ``seasonal-yoy-comparison-v1`` for comparison, but is never the default
    executive forecast.
    """
    if method == FY_SEASONAL_METHOD_VERSION:
        return _forecast_fiscal_year_impl(
            monthly,
            as_of=as_of,
            fy_start_month=fy_start_month,
            growth_percent_monthly=growth_percent_monthly,
            planned_savings_monthly=planned_savings_monthly,
            savings_ramp_months=savings_ramp_months,
            current_month_estimate=current_month_estimate,
            basis="seasonal",
        )
    if method != FY_METHOD_VERSION:
        raise ValueError(f"Unsupported fiscal-year forecast method: {method}")

    primary = _forecast_fiscal_year_impl(
        monthly,
        as_of=as_of,
        fy_start_month=fy_start_month,
        growth_percent_monthly=growth_percent_monthly,
        planned_savings_monthly=planned_savings_monthly,
        savings_ramp_months=savings_ramp_months,
        current_month_estimate=current_month_estimate,
        basis="run_rate",
    )
    comparison = _forecast_fiscal_year_impl(
        monthly,
        as_of=as_of,
        fy_start_month=fy_start_month,
        growth_percent_monthly=growth_percent_monthly,
        planned_savings_monthly=planned_savings_monthly,
        savings_ramp_months=savings_ramp_months,
        current_month_estimate=current_month_estimate,
        basis="seasonal",
    )
    primary["seasonalComparison"] = {
        "methodVersion": comparison["methodVersion"],
        "fyTotal": comparison["fyTotal"],
        "fyLower": comparison["fyLower"],
        "fyUpper": comparison["fyUpper"],
        "backtestMape": comparison["backtestMape"],
        "yoyFactor": comparison["yoyFactor"],
        "reason": comparison["reason"],
    }
    return primary


def _forecast_fiscal_year_impl(
    monthly: Mapping[date, float],
    *,
    as_of: date,
    fy_start_month: int = 7,
    growth_percent_monthly: float = 0.0,
    planned_savings_monthly: float = 0.0,
    savings_ramp_months: int = 3,
    current_month_estimate: float | None = None,
    basis: str = "run_rate",
) -> dict:
    """Project spend for the fiscal year containing as_of, at month grain.

    Complete historical months are used as-is. The primary basis is the
    trailing three-complete-month post-migration run-rate. The comparison
    basis uses the same-month-last-year seasonal method. Growth compounds
    monthly from the last complete month, planned savings ramp over the
    configured number of months, and confidence bands use backtest error for
    the selected basis.
    """
    fy_start, fy_last_month, fy_label = fiscal_year_frame(
        as_of, fy_start_month
    )
    current_month = as_of.replace(day=1)
    series = {
        month: max(float(amount or 0), 0.0)
        for month, amount in monthly.items()
    }
    complete = sorted(month for month in series if month < current_month)
    history_months = len(complete)

    trailing = [series[month] for month in complete[-3:]]
    trailing_mean = sum(trailing) / len(trailing) if trailing else 0.0

    yoy_factor = None
    recent = complete[-3:]
    overlap = [
        (series[month], series[_month_add(month, -12)])
        for month in recent
        if _month_add(month, -12) in series
    ]
    if overlap:
        prior_sum = sum(prior for _, prior in overlap)
        if prior_sum > 0:
            raw = sum(current for current, _ in overlap) / prior_sum
            yoy_factor = min(max(raw, 0.6), 1.6)

    def base_projection(month: date) -> tuple[float, bool]:
        if basis == "run_rate":
            return trailing_mean, False
        last_year = _month_add(month, -12)
        if yoy_factor is not None and last_year in series:
            return series[last_year] * yoy_factor, True
        return trailing_mean, False

    # Backtest the same rule on known months to size the confidence bands.
    errors: list[float] = []
    for index in range(4, len(complete)):
        target = complete[index]
        history = complete[:index]
        past = {month: series[month] for month in history}
        past_trailing = [past[month] for month in history[-3:]]
        past_mean = (
            sum(past_trailing) / len(past_trailing) if past_trailing else 0.0
        )
        last_year = _month_add(target, -12)
        if basis == "run_rate":
            predicted = past_mean
        else:
            predicted = past.get(last_year, past_mean)
        actual = series[target]
        if actual > 0:
            errors.append(abs(actual - predicted) / actual * 100)
    backtest_mape = round(sum(errors) / len(errors), 1) if errors else None
    band_ratio = max((backtest_mape or 0) / 100, 0.08)

    growth = growth_percent_monthly / 100
    ramp_months = max(int(savings_ramp_months), 0)
    months: list[dict] = []
    actual_to_date = 0.0
    projected = {"amount": 0.0, "lower": 0.0, "upper": 0.0}
    projection_index = 0
    for offset in range(12):
        month = _month_add(fy_start, offset)
        if month > fy_last_month:
            break
        if month < current_month and month in series:
            amount = round(series[month], 2)
            actual_to_date += amount
            months.append(
                {
                    "month": month.isoformat()[:7],
                    "status": "actual",
                    "amount": amount,
                    "lower": amount,
                    "upper": amount,
                    "seasonalBasis": False,
                }
            )
            continue
        projection_index += 1
        if month == current_month and current_month_estimate is not None:
            center = max(float(current_month_estimate), 0.0)
            seasonal = False
            status = "inProgress"
        else:
            center, seasonal = base_projection(month)
            status = "projected"
        steps = max(
            (month.year * 12 + month.month)
            - (current_month.year * 12 + current_month.month),
            0,
        )
        center *= (1 + growth) ** steps
        if planned_savings_monthly > 0 and month >= current_month:
            ramp = (
                1.0
                if ramp_months == 0
                else min(projection_index / ramp_months, 1.0)
            )
            center = max(center - planned_savings_monthly * ramp, 0.0)
        widening = 1.0 + 0.2 * max(projection_index - 1, 0)
        margin = center * min(band_ratio * widening, 0.6)
        entry = {
            "month": month.isoformat()[:7],
            "status": status,
            "amount": round(center, 2),
            "lower": round(max(center - margin, 0.0), 2),
            "upper": round(center + margin, 2),
            "seasonalBasis": seasonal,
        }
        months.append(entry)
        projected["amount"] += entry["amount"]
        projected["lower"] += entry["lower"]
        projected["upper"] += entry["upper"]

    status = "ready"
    if basis == "run_rate":
        reason = (
            "Post-migration monthly run-rate using the trailing three complete "
            "months, with compounding growth assumption and backtest-scaled "
            "bands. The pre-migration seasonal model is comparison-only."
        )
        method_version = FY_METHOD_VERSION
    else:
        reason = (
            "Optional seasonal comparison using the same month last year "
            "scaled by the trailing year-over-year factor, with trailing-mean "
            "fallback and backtest-scaled bands."
        )
        method_version = FY_SEASONAL_METHOD_VERSION
    if history_months == 0:
        status = "not_connected"
        reason = "No monthly cost history is available."
    elif history_months < 3:
        status = "limited"
        reason = (
            f"Only {history_months} complete months of history; the "
            "projection is a trailing-mean run-rate with wide bands."
        )

    return {
        "status": status,
        "fiscalYear": fy_label,
        "fyStartMonth": fy_start_month,
        "fyStart": fy_start.isoformat(),
        "fyEnd": _month_add(fy_last_month, 1).isoformat(),
        "months": months,
        "actualToDate": round(actual_to_date, 2),
        "projectedRemainder": {
            key: round(value, 2) for key, value in projected.items()
        },
        "fyTotal": round(actual_to_date + projected["amount"], 2),
        "fyLower": round(actual_to_date + projected["lower"], 2),
        "fyUpper": round(actual_to_date + projected["upper"], 2),
        "historyMonths": history_months,
        "yoyFactor": (
            round(yoy_factor, 3)
            if basis == "seasonal" and yoy_factor is not None
            else None
        ),
        "backtestMape": backtest_mape,
        "assumptions": {
            "growthPercentMonthly": growth_percent_monthly,
            "plannedSavingsMonthly": round(planned_savings_monthly, 2),
            "savingsRampMonths": ramp_months,
        },
        "methodVersion": method_version,
        "reason": reason,
    }


def _weekday_baseline(
    values: Mapping[date, float],
    target: date,
    *,
    weeks: int,
) -> tuple[float, float, int]:
    samples = [
        float(values[prior])
        for offset in range(1, weeks + 1)
        if (prior := target - timedelta(days=offset * 7)) in values
    ]
    if not samples:
        return 0.0, 0.0, 0
    center = float(median(samples))
    mad = float(median(abs(value - center) for value in samples))
    return center, mad, len(samples)


def forecast_daily_cost(
    amounts: Mapping[date, float],
    *,
    horizon_days: int = 30,
    minimum_history_days: int = 28,
    baseline_weeks: int = 8,
    latency_days: int = 2,
    as_of: date | None = None,
) -> dict:
    """Return a conservative weekday-seasonal cost forecast.

    A bounded recent-trend factor adjusts matching-weekday medians. Confidence
    bands use scaled median absolute deviation and widen with the horizon.
    """
    cutoff = (as_of or date.today()) - timedelta(days=max(latency_days, 0))
    series = {
        observed: max(float(amount or 0), 0.0)
        for observed, amount in amounts.items()
        if observed <= cutoff
    }
    if not series:
        return {
            "status": "not_connected",
            "historyDays": 0,
            "points": [],
            "forecastTotal": None,
            "lowerTotal": None,
            "upperTotal": None,
            "backtestMape": None,
            "backtestPoints": 0,
            "latencyDays": latency_days,
            "dataThrough": None,
            "monthly": [],
            "methodVersion": METHOD_VERSION,
            "reason": "No daily cost history is available.",
        }
    last_date = max(series)
    first_date = min(series)
    history_days = (last_date - first_date).days + 1
    if history_days < minimum_history_days:
        return {
            "status": "warming_up",
            "historyDays": history_days,
            "points": [],
            "forecastTotal": None,
            "lowerTotal": None,
            "upperTotal": None,
            "backtestMape": None,
            "backtestPoints": 0,
            "latencyDays": latency_days,
            "dataThrough": last_date.isoformat(),
            "monthly": [],
            "methodVersion": METHOD_VERSION,
            "reason": (
                f"Forecast needs {minimum_history_days} days; "
                f"{history_days} are available."
            ),
        }

    recent_dates = [
        last_date - timedelta(days=offset) for offset in range(14)
    ]
    prior_dates = [
        last_date - timedelta(days=offset) for offset in range(14, 28)
    ]
    recent_average = sum(series.get(day, 0) for day in recent_dates) / 14
    prior_average = sum(series.get(day, 0) for day in prior_dates) / 14
    trend_factor = (
        min(max(recent_average / prior_average, 0.5), 1.5)
        if prior_average > 0
        else 1.0
    )

    points = []
    for offset in range(1, max(horizon_days, 1) + 1):
        target = last_date + timedelta(days=offset)
        center, mad, baseline_points = _weekday_baseline(
            series,
            target,
            weeks=baseline_weeks,
        )
        # Decay the recent trend instead of extrapolating it indefinitely.
        decay = max(0.0, 1.0 - (offset - 1) / max(horizon_days, 1))
        adjusted_factor = 1.0 + (trend_factor - 1.0) * decay
        expected = max(center * adjusted_factor, 0.0)
        robust_sigma = 1.4826 * mad
        widening = 1.0 + offset / max(horizon_days, 1) * 0.35
        margin = max(robust_sigma * 1.96 * widening, expected * 0.05)
        points.append(
            {
                "date": target.isoformat(),
                "amount": round(expected, 2),
                "lower": round(max(expected - margin, 0.0), 2),
                "upper": round(expected + margin, 2),
                "baselinePoints": baseline_points,
            }
        )

    errors = []
    for offset in range(7):
        target = last_date - timedelta(days=offset)
        actual = series.get(target)
        center, _, count = _weekday_baseline(
            {
                day: value
                for day, value in series.items()
                if day < target
            },
            target,
            weeks=baseline_weeks,
        )
        if actual is not None and actual > 0 and count >= 4:
            errors.append(abs(actual - center) / actual * 100)

    monthly: dict[str, dict[str, float | str]] = {}
    for point in points:
        month = point["date"][:7]
        bucket = monthly.setdefault(
            month,
            {
                "month": month,
                "amount": 0.0,
                "lower": 0.0,
                "upper": 0.0,
            },
        )
        bucket["amount"] = float(bucket["amount"]) + point["amount"]
        bucket["lower"] = float(bucket["lower"]) + point["lower"]
        bucket["upper"] = float(bucket["upper"]) + point["upper"]

    return {
        "status": "ready",
        "historyDays": history_days,
        "points": points,
        "forecastTotal": round(sum(item["amount"] for item in points), 2),
        "lowerTotal": round(sum(item["lower"] for item in points), 2),
        "upperTotal": round(sum(item["upper"] for item in points), 2),
        "backtestMape": round(sum(errors) / len(errors), 1) if errors else None,
        "backtestPoints": len(errors),
        "latencyDays": latency_days,
        "dataThrough": last_date.isoformat(),
        "monthly": [
            {
                **bucket,
                "amount": round(float(bucket["amount"]), 2),
                "lower": round(float(bucket["lower"]), 2),
                "upper": round(float(bucket["upper"]), 2),
            }
            for bucket in monthly.values()
        ],
        "trendFactor": round(trend_factor, 3),
        "methodVersion": METHOD_VERSION,
        "reason": (
            "Matching-weekday median with bounded recent-trend adjustment "
            "and robust MAD confidence bands."
        ),
    }
