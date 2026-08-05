from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any


METHOD_VERSION = "opportunity-valuation-v2"

# These findings describe removal or full retirement of the billed resource.
# Modernization, tagging, and utilization rules are intentionally excluded
# until a governed target rate or sufficient telemetry exists.
FULL_COST_RULES = {
    "unattached_disk",
    "aged_snapshot",
    "snapshot_source_deleted",
    "public_ip_unattached",
    "public_ip_orphan_nic",
    "empty_standard_load_balancer",
    "empty_application_gateway",
    "vnet_gateway_no_connections",
    "empty_paid_app_service_plan",
}


def _number(value: float | Decimal | None) -> float | None:
    return float(value) if value is not None else None


def monthly_run_rate(
    amount: float | Decimal,
    period_start: date,
    period_end: date,
) -> float | None:
    observed_days = (period_end - period_start).days + 1
    if observed_days <= 0:
        return None
    month_days = calendar.monthrange(period_start.year, period_start.month)[1]
    return round(float(amount) / observed_days * month_days, 2)


def value_opportunity(
    *,
    source: str,
    rule_id: str,
    advisor_monthly: float | Decimal | None,
    advisor_annual: float | Decimal | None,
    cost_amount: float | Decimal | None,
    cost_type: str,
    period_start: date | None,
    period_end: date | None,
    confidence: float | Decimal | None,
    current_sku: str = "",
    target_sku: str = "",
    target_price_status: str = "",
    target_monthly_price: float | Decimal | None = None,
    target_price_currency: str = "",
    cost_currency: str = "",
) -> dict[str, Any]:
    gross: float | None = None
    current_monthly: float | None = None
    target_monthly = _number(target_monthly_price)
    status = "not_valued"
    value_source = ""
    basis = "No governed valuation method applies to this finding."

    if source == "azure_advisor":
        if cost_amount is not None and period_start and period_end:
            current_monthly = monthly_run_rate(
                cost_amount,
                period_start,
                period_end,
            )
        has_sku_pair = bool(current_sku and target_sku)
        currencies_match = bool(
            target_price_currency
            and cost_currency
            and target_price_currency.upper() == cost_currency.upper()
        )
        can_price_difference = (
            has_sku_pair
            and target_price_status == "matched"
            and current_monthly is not None
            and target_monthly is not None
            and currencies_match
        )
        if can_price_difference:
            difference = round(current_monthly - target_monthly, 2)
            basis = (
                f"Current {cost_type} month-to-date run rate minus the "
                "target SKU Microsoft retail hourly rate projected at the "
                "governed monthly-hour assumption."
            )
            value_source = (
                "amortized_cost_minus_retail_target"
                if cost_type == "AmortizedCost"
                else "actual_cost_minus_retail_target"
            )
            if difference > 0:
                gross = difference
                status = "valued"
            else:
                status = "target_not_cheaper"
                basis += " The modeled target is not cheaper than observed cost."
        else:
            gross = _number(advisor_monthly)
            if gross is None and advisor_annual is not None:
                gross = round(float(advisor_annual) / 12, 2)
                basis = "Azure Advisor annual savings estimate normalized to one month."
            elif gross is not None:
                gross = round(gross, 2)
                basis = "Azure Advisor monthly savings estimate."
        if gross is None:
            if status != "target_not_cheaper":
                status = (
                    target_price_status
                    if target_price_status in {
                        "ambiguous",
                        "not_found",
                        "unsupported_os",
                        "error",
                    }
                    else "no_advisor_estimate"
                )
                basis = (
                    "A governed SKU price difference could not be calculated "
                    f"({target_price_status or 'target rate not collected'}), "
                    "and Azure Advisor did not provide a savings amount."
                )
        elif not value_source:
            status = "valued"
            value_source = "azure_advisor"
    elif rule_id in FULL_COST_RULES:
        if cost_amount is None or not period_start or not period_end:
            status = "no_cost_data"
            basis = "No resource-level Cost Management amount is available."
        else:
            gross = monthly_run_rate(cost_amount, period_start, period_end)
            if gross is None:
                status = "no_cost_data"
                basis = "The Cost Management observation window is invalid."
            else:
                status = "valued"
                value_source = (
                    "amortized_cost_run_rate"
                    if cost_type == "AmortizedCost"
                    else "actual_cost_run_rate"
                )
                basis = (
                    f"Projected from month-to-date {cost_type} under a "
                    "full-resource retirement assumption."
                )

    confidence_value = _number(confidence)
    risk_adjusted = (
        round(gross * confidence_value, 2)
        if gross is not None and confidence_value is not None
        else None
    )
    if gross is not None and confidence_value is None:
        status = "valued_no_confidence"

    return {
        "status": status,
        "monthlyGross": gross,
        "monthlyRiskAdjusted": risk_adjusted,
        "currentMonthlyCost": current_monthly,
        "targetMonthlyCost": target_monthly,
        "valueSource": value_source,
        "basis": basis,
        "confidence": confidence_value,
        "methodVersion": METHOD_VERSION,
    }
