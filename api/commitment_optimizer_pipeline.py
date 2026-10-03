"""Azure Commitment Purchase Optimizer — orchestration pipeline.

Builds the hourly evidence cube from governed FOCUS charges, prices it
against the customer's negotiated price sheet (retail fallback is always
directional), simulates portfolios through the deterministic engine
(``api.commitment_optimizer``), enforces the purchase-readiness gates, and
persists versioned runs, scenarios, recommendations, and manifests.

Governed data sources:
* ``focus_cost_current`` — the only hourly source. Hourly rows are never
  fabricated from daily or monthly amounts.
* ``price_sheet_current`` — customer contracted PAYG, Savings Plan, and
  Reservation rates (the primary purchase basis).
* ``retail_prices_current`` — directional fallback only.
* ``reservation_inventory_current`` / ``savings_plans_current`` — existing
  commitments, applied before incremental purchases.
* Azure recommendation feeds — reconciliation benchmark, never summed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from . import commitment_optimizer as engine
from .config import settings
from .rightsizing_proposal import parse_sku

FLEXIBILITY_UNKNOWN = "unmapped"
SOFTWARE_METER_EXCLUSIONS = ("license", "software")

# FOCUS PricingCategory values that mean "paying the standard rate", and so
# are candidates for a commitment.
#
# Azure's FOCUS export writes "Standard" here, not "OnDemand" - "OnDemand"
# belongs to the Cost Management pricingModel vocabulary, which is a
# different column in a different table. Matching on "ondemand" therefore
# discarded every usage row and the optimizer reported zero eligible spend
# with no error to explain it. "on-demand" is accepted too because the FOCUS
# specification uses it and Azure may yet align.
#
# Deliberately excluded: "dynamic" (spot, which no commitment covers) and
# "committed" (already under a commitment; those rows are also caught by the
# CommitmentDiscountId check).
ON_DEMAND_PRICING_CATEGORIES = frozenset({"standard", "ondemand", "on-demand"})


# ---------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------


def _hours_in_uom(unit_of_measure: str) -> float:
    text = (unit_of_measure or "").lower().replace("1/", "")
    digits = "".join(ch for ch in text.split(" ")[0] if ch.isdigit() or ch == ".")
    if "hour" in text and digits:
        try:
            value = float(digits)
            return value if value > 0 else 1.0
        except ValueError:
            return 1.0
    return 1.0


def _term_key(term: str) -> str:
    value = (term or "").lower().replace(" ", "")
    if value in ("p1y", "1year", "1yr"):
        return "P1Y"
    if value in ("p3y", "3year", "3years", "3yr"):
        return "P3Y"
    return value.upper()


def build_price_book(database: Any) -> dict[str, Any]:
    """Contracted price book keyed by meter id, plus catalog metadata.

    ``source`` is "price_sheet" for negotiated rates and "retail" for the
    directional fallback; consumers must keep the two apart (baseline
    mixing is a blocking gate).
    """
    book: dict[str, Any] = {}
    sheet_currency = ""
    with database.connect(read_only=True) as db:
        rows = db.execute(
            """
            SELECT meter_id, price_type, term, unit_price, currency,
                   unit_of_measure, effective_date, sku_id, product
            FROM price_sheet_current
            WHERE unit_price IS NOT NULL
            """
        ).fetchall()
    weights: dict[str, float] = {}
    for (
        meter_id,
        price_type,
        term,
        unit_price,
        currency,
        uom,
        effective_date,
        sku_id,
        product,
    ) in rows:
        meter_id = str(meter_id or "").lower()
        if not meter_id:
            continue
        currency = str(currency or "").upper()
        weights[currency] = weights.get(currency, 0.0) + abs(float(unit_price))
        entry = book.setdefault(
            meter_id,
            {
                "meterId": meter_id,
                "currency": currency,
                "skuId": str(sku_id or ""),
                "product": str(product or ""),
                "effectiveDate": str(effective_date or ""),
                "consumption": None,
                "sp": {},
                "ri": {},
                "source": "price_sheet",
            },
        )
        hourly = float(unit_price) / _hours_in_uom(str(uom or ""))
        kind = str(price_type or "").lower()
        if "saving" in kind:
            entry["sp"][_term_key(str(term))] = hourly
        elif "reservation" in kind or "reserved" in kind:
            term_key = _term_key(str(term))
            term_hours = engine.TERM_HOURS.get(term_key, 8760.0)
            entry["ri"][term_key] = hourly / term_hours if term_hours else None
        else:
            entry["consumption"] = hourly
    # Distinct non-empty currencies — must fail closed, mirroring the FOCUS
    # manifest guard. Mixing EUR hourly rates into a USD baseline via
    # FX-naive division would understate commitment savings without tripping
    # the currency_match BLOCKED gate if the dominant currency happened to
    # match FOCUS currency.
    distinct = sorted(c for c in weights if c)
    if len(distinct) > 1:
        by_ccy = ", ".join(
            f"{ccy} ({weights[ccy]:,.2f} weight)" for ccy in distinct
        )
        raise ValueError(
            f"price_sheet_current contains {len(distinct)} distinct "
            f"currencies ({by_ccy}); refusing to pick a dominant currency. "
            "Re-ingest a single-currency price sheet or ingest per-currency."
        )
    sheet_currency = max(weights, key=weights.get) if weights else ""
    return {"meters": book, "currency": sheet_currency}


def retail_fallback_prices(database: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Directional retail rates keyed by (region, sku)."""
    prices: dict[tuple[str, str], dict[str, Any]] = {}
    with database.connect(read_only=True) as db:
        for region, sku, profile, currency, hourly, sp_1y, ri_1y, upfront in db.execute(
            """
            SELECT arm_region_name, arm_sku_name, price_profile, currency,
                   hourly_price, monthly_sp_1y, monthly_ri_1y, ri_1y_upfront
            FROM retail_prices_current
            """
        ).fetchall():
            prices[
                (str(region or "").lower(), str(sku or "").lower())
            ] = {
                "profile": profile,
                "currency": str(currency or "").upper(),
                "hourly": float(hourly) if hourly is not None else None,
                "spHourly1y": (
                    float(sp_1y) / settings.retail_prices_hours_per_month
                    if sp_1y is not None
                    else None
                ),
                "riHourly1y": (
                    float(ri_1y) / settings.retail_prices_hours_per_month
                    if ri_1y is not None
                    else None
                ),
                "ri1yUpfront": float(upfront) if upfront is not None else None,
            }
    return prices


# ---------------------------------------------------------------------------
# Hourly cube from governed FOCUS charges
# ---------------------------------------------------------------------------


def _detect_grain(
    database: Any, window_start: datetime, window_end: datetime
) -> dict[str, Any]:
    with database.connect(read_only=True) as db:
        interval = db.execute(
            """
            SELECT date_diff('minute', charge_period_start, charge_period_end)
                       AS minutes,
                   count(*) AS n
            FROM focus_cost_current
            WHERE charge_period_start >= ? AND charge_period_start < ?
              AND charge_period_end IS NOT NULL
            GROUP BY 1
            ORDER BY n DESC
            LIMIT 1
            """,
            [window_start, window_end],
        ).fetchone()
        hours = db.execute(
            """
            SELECT count(DISTINCT date_trunc('hour', charge_period_start))
            FROM focus_cost_current
            WHERE charge_period_start >= ? AND charge_period_start < ?
            """,
            [window_start, window_end],
        ).fetchone()
        duplicates = db.execute(
            """
            SELECT count(*) FROM (
                SELECT resource_id, meter_id, charge_period_start
                FROM focus_cost_current
                WHERE charge_period_start >= ? AND charge_period_start < ?
                  AND lower(charge_category) = 'usage'
                GROUP BY resource_id, meter_id, charge_period_start
                HAVING count(*) > 1
            )
            """,
            [window_start, window_end],
        ).fetchone()
    grain_minutes = int(interval[0]) if interval and interval[0] is not None else 0
    return {
        "grainMinutes": grain_minutes,
        "hourly": grain_minutes == 60,
        "observedHours": int(hours[0]) if hours else 0,
        "duplicateGroups": int(duplicates[0]) if duplicates else 0,
    }


def build_hourly_usage(
    database: Any,
    window_start: datetime,
    window_end: datetime,
    price_book: dict[str, Any],
    currency: str,
    excluded_resources: set[str],
    term: str = "P1Y",
) -> dict[str, Any]:
    """Aggregate FOCUS usage into engine UsageLines.

    SP layer: compute OnDemand usage not already covered by an existing
    commitment discount, priced at contracted rates. RI layer: Virtual
    Machines usage-based charges only — software meters never enter the
    Reservation hardware benefit.
    """
    meters = price_book["meters"]
    excluded = sorted(excluded_resources)[:200]
    exclusion_clause = ""
    if excluded:
        placeholders = ", ".join("?" for _ in excluded)
        exclusion_clause = f"AND lower(resource_id) NOT IN ({placeholders})"
    with database.connect(read_only=True) as db:
        rows = db.execute(
            f"""
            SELECT date_trunc('hour', charge_period_start) AS hour,
                   meter_id, meter_name, sku_id, region_name,
                   lower(service_category) AS service_category,
                   lower(meter_category) AS meter_category,
                   lower(pricing_category) AS pricing_category,
                   lower(charge_category) AS charge_category,
                   commitment_discount_id,
                   sum(COALESCE(pricing_quantity, 0)) AS quantity,
                   sum(COALESCE(contracted_cost, billed_cost)) AS contracted,
                   sum(COALESCE(list_cost, contracted_cost, billed_cost))
                       AS list_cost,
                   avg(contracted_unit_price) AS contracted_unit_price,
                   avg(list_unit_price) AS list_unit_price
            FROM focus_cost_current
            WHERE charge_period_start >= ? AND charge_period_start < ?
              AND lower(charge_category) = 'usage'
              AND billing_currency = ?
              {exclusion_clause}
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
            """,
            [window_start, window_end, currency, *excluded],
        ).fetchall()

    lines: list[engine.UsageLine] = []
    stats = {
        "rows": 0,
        "spEligibleRows": 0,
        "riEligibleRows": 0,
        "unpricedContractedCost": 0.0,
        "coveredByExistingCommitment": 0.0,
        # Every other way a row can leave this loop is counted. This one was
        # not, so a vocabulary mismatch that rejected all 17,360 rows looked
        # indistinguishable from having no compute usage at all.
        "skippedNotOnDemandCompute": 0,
        "directionalPricing": False,
    }
    for (
        hour,
        meter_id,
        meter_name,
        sku_id,
        region,
        service_category,
        meter_category,
        pricing_category,
        charge_category,
        commitment_discount_id,
        quantity,
        contracted,
        list_cost,
        contracted_unit_price,
        list_unit_price,
    ) in rows:
        stats["rows"] += 1
        if commitment_discount_id:
            stats["coveredByExistingCommitment"] += float(contracted or 0.0)
            continue
        if (
            service_category != "compute"
            or pricing_category not in ON_DEMAND_PRICING_CATEGORIES
        ):
            stats["skippedNotOnDemandCompute"] += 1
            continue
        meter_key = str(meter_id or "").lower()
        price = meters.get(meter_key)
        physical_qty = float(quantity or 0.0)
        is_vm = meter_category == "virtual machines" and not any(
            marker in str(meter_name or "").lower()
            for marker in SOFTWARE_METER_EXCLUSIONS
        )
        if is_vm and physical_qty > 0:
            units = physical_qty
            # Missing cost is unknown, not $0 — explicit None check
            if contracted is None:
                group_contracted = None  # type: ignore[assignment]
            else:
                group_contracted = float(contracted) / physical_qty
            raw_list = list_cost if list_cost is not None else contracted
            if raw_list is None:
                group_list = None  # type: ignore[assignment]
            else:
                group_list = float(raw_list) / physical_qty
        else:
            units = 1.0
            group_contracted = None if contracted is None else float(contracted)  # type: ignore[assignment]
            raw_list2 = list_cost if list_cost is not None else contracted
            group_list = None if raw_list2 is None else float(raw_list2)  # type: ignore[assignment]
        payg_rate = None
        sp_rate = None
        if price and price.get("consumption") is not None:
            payg_rate = price["consumption"]
        elif contracted_unit_price is not None and float(contracted_unit_price) > 0:
            payg_rate = float(contracted_unit_price)
        elif group_contracted is not None and group_contracted > 0:
            payg_rate = group_contracted / units if units else 0.0
        if payg_rate is None or payg_rate <= 0:
            # Missing/unknown cost is not $0 — exclude from totals and surface
            # as "price unavailable" (caller counts via unpricedContractedCost).
            # Only add a numeric cost when we have one; None stays unknown.
            if contracted is not None:
                stats["unpricedContractedCost"] += float(contracted)
            else:
                # No cost at all — still counts as unpriced but without a $ figure
                stats["unpricedContractedCost"] += 0.0
                stats.setdefault("unpricedRowsWithUnknownCost", 0)
                stats["unpricedRowsWithUnknownCost"] += 1  # type: ignore[operator]
            continue
        if price and price.get("sp"):
            # The requested term only. build_price_book keys these by term
            # and the source query has no ORDER BY, so next(iter(...)) took
            # whichever row DuckDB happened to return first -- a P1Y run
            # could be priced entirely at P3Y rates, and P1Y and P3Y runs
            # produced identical SP economics. A missing term is unpriced
            # (directional), never silently substituted from the other term.
            sp_rate = price["sp"].get(_term_key(term))
        if sp_rate is None:
            stats["directionalPricing"] = True
        lines.append(
            engine.UsageLine(
                hour=hour,
                sku=str(sku_id or meter_key),
                region=str(region or ""),
                flexibility_group=_flexibility_group(
                    str(meter_name or ""), str(sku_id or ""), str(region or "")
                ),
                quantity=units if is_vm else 1.0,
                contracted_payg_rate=group_contracted if not is_vm else payg_rate,  # type: ignore[arg-type]
                list_payg_rate=(  # type: ignore[arg-type]
                    group_list
                    if (not is_vm or (group_list is not None and group_list > 0))
                    else payg_rate * 1.2
                ),
                sp_rate=float(sp_rate) if sp_rate is not None else payg_rate,
                ratio=_flexibility_ratio(str(meter_name or ""), str(sku_id or "")),
                ri_eligible=is_vm,
                sp_eligible=sp_rate is not None and is_vm,
            )
        )
        if sp_rate is not None:
            stats["spEligibleRows"] += 1
        if is_vm:
            stats["riEligibleRows"] += 1
    return {
        "lines": lines,
        "stats": stats,
        "unmappedGroups": sorted(
            {l.flexibility_group for l in lines if l.flexibility_group == FLEXIBILITY_UNKNOWN}
        ),
    }


def _normalize_sku_name(value: str) -> str:
    """Normalize SKU-like labels to the ``standard_<family><size>_v<n>`` form.

    FOCUS meter names ("D4s v5"), ARM SKU names ("Standard_D4s_v5"), and
    Advisor targets all describe the same VM; the flexibility parser speaks
    only the ARM form.
    """
    text = (value or "").strip().lower()
    if not text:
        return ""
    if " " in text and "_" not in text:
        parts = text.split()
        text = parts[0] + ("_" + "_".join(parts[1:]) if len(parts) > 1 else "")
        text = text.replace(" ", "")
    if not text.startswith("standard_"):
        text = f"standard_{text}"
    return text


def _flexibility_group(meter_name: str, sku_id: str, region: str) -> str:
    parsed = parse_sku(_normalize_sku_name(meter_name)) or parse_sku(
        _normalize_sku_name(sku_id)
    )
    if not parsed:
        return FLEXIBILITY_UNKNOWN
    norm_region = str(region or "").lower().replace(" ", "").replace("-", "")
    return f"{norm_region}:{parsed['family']}"


def _flexibility_ratio(meter_name: str, sku_id: str) -> float:
    parsed = parse_sku(_normalize_sku_name(meter_name)) or parse_sku(
        _normalize_sku_name(sku_id)
    )
    return float(parsed["ratio"]) if parsed else 1.0


# ---------------------------------------------------------------------------
# Existing commitments + catalog
# ---------------------------------------------------------------------------


def load_existing_commitments(
    database: Any,
    price_book: dict[str, Any],
    retail_prices: dict[tuple[str, str], dict[str, Any]],
    term: str,
) -> dict[str, Any]:
    reservations: list[engine.Reservation] = []
    savings_plans: list[engine.SavingsPlan] = []
    catalog: dict[str, engine.CatalogSku] = {}
    reservation_details: list[dict[str, Any]] = []
    savings_plan_details: list[dict[str, Any]] = []
    # Set when an owned commitment had to be priced on a term other than the
    # one being modelled; the run is directional rather than purchase-ready.
    directional_terms = False
    with database.connect(read_only=True) as db:
        for sku, region, quantity, state in db.execute(
            """
            SELECT sku, region, sum(quantity), any_value(state)
            FROM reservation_inventory_current
            WHERE quantity > 0
            GROUP BY sku, region
            """
        ).fetchall():
            parsed = parse_sku(str(sku or ""))
            if not parsed or not quantity:
                continue
            norm_region = str(region or "").lower().replace(" ", "").replace("-", "")
            group = f"{norm_region}:{parsed['family']}"
            ratio = float(parsed["ratio"])
            hourly_rate = _ri_hourly_rate(
                price_book, retail_prices, str(region), str(sku), term
            )
            if hourly_rate is None:
                # An owned reservation must never vanish from the baseline.
                # Dropping it would erase real existing commitment: the
                # EXISTING portfolio collapses toward PAYG, and
                # _hourly_eligible_sp_spend then sees hours that are already
                # RI-covered as uncovered and oversizes the Savings Plan on
                # top of capacity you already bought. Price it on the
                # 1-year rate and flag the run directional instead.
                hourly_rate = _ri_hourly_rate(
                    price_book, retail_prices, str(region), str(sku), "P1Y"
                )
                if hourly_rate is None:
                    continue
                directional_terms = True
            reservations.append(
                engine.Reservation(
                    sku=str(sku),
                    region=str(region),
                    flexibility_group=group,
                    ratio=ratio,
                    hourly_rate=hourly_rate,
                    quantity=int(quantity),
                    term=term,
                    existing=True,
                )
            )
        for (
            reservation_id,
            sku,
            region,
            quantity,
            expiry_date,
            utilization_30d,
        ) in db.execute(
            """
            SELECT reservation_id, sku, region, quantity, expiry_date,
                   utilization_30d
            FROM reservation_inventory_current
            WHERE quantity > 0
            """
        ).fetchall():
            reservation_details.append(
                {
                    "commitmentId": str(reservation_id or ""),
                    "sku": str(sku or ""),
                    "region": str(region or ""),
                    "quantity": int(quantity or 0),
                    "expiryDate": str(expiry_date) if expiry_date else "",
                    "utilization30d": (
                        float(utilization_30d)
                        if utilization_30d is not None
                        else None
                    ),
                }
            )
        for hourly in db.execute(
            """
            SELECT sum(hourly_commitment)
            FROM savings_plans_current
            WHERE hourly_commitment IS NOT NULL
              AND (expiry_date IS NULL OR expiry_date >= CURRENT_DATE)
            """
        ).fetchone():
            if hourly:
                savings_plans.append(
                    engine.SavingsPlan(
                        hourly_commitment=float(hourly), term=term, existing=True
                    )
                )
        for (
            savings_plan_id,
            hourly_commitment,
            expiry_date,
            utilization_30d,
        ) in db.execute(
            """
            SELECT savings_plan_id, hourly_commitment, expiry_date,
                   utilization_30d
            FROM savings_plans_current
            WHERE hourly_commitment IS NOT NULL
              AND (expiry_date IS NULL OR expiry_date >= CURRENT_DATE)
            """
        ).fetchall():
            savings_plan_details.append(
                {
                    "commitmentId": str(savings_plan_id or ""),
                    "hourlyCommitment": float(hourly_commitment or 0.0),
                    "expiryDate": str(expiry_date) if expiry_date else "",
                    "utilization30d": (
                        float(utilization_30d)
                        if utilization_30d is not None
                        else None
                    ),
                }
            )
    for (region, sku), price in retail_prices.items():
        parsed = parse_sku(sku)
        if not parsed or price.get("riHourly1y") is None:
            continue
        group = f"{region}:{parsed['family']}"
        key = f"{region}:{sku}"
        # Only a 1-year reservation rate is collected (api/pricing.py filters
        # reservationTerm eq '1 year', and retail_prices_current has no
        # 3-year column). The previous `riHourly1y * 0.7` invented one: the
        # real 1yr->3yr step varies materially by family and region, and that
        # fabricated number decided expectedCost, the P1Y-vs-P3Y term
        # comparison, and integer_decompose's SKU ranking. A SKU with no real
        # 3-year rate is now simply not purchasable on a 3-year term, which
        # lets the catalog_purchasable data-quality gate fire instead.
        three_year = price.get("riHourly3y")
        catalog[key] = engine.CatalogSku(
            sku=sku,
            region=region,
            flexibility_group=group,
            ratio=float(parsed["ratio"]),
            hourly_rate_p1y=price["riHourly1y"],
            hourly_rate_p3y=(
                float(three_year) if three_year is not None else -1.0
            ),
        )
    return {
        "reservations": reservations,
        "savingsPlans": savings_plans,
        "catalog": list(catalog.values()),
        "reservationDetails": reservation_details,
        "savingsPlanDetails": savings_plan_details,
        "directionalTerms": directional_terms,
    }


def _ri_hourly_rate(
    price_book: dict[str, Any],
    retail_prices: dict[tuple[str, str], dict[str, Any]],
    region: str,
    sku: str,
    term: str,
) -> float | None:
    # Term-strict. Falling back to whichever term the price sheet happened to
    # list -- or to the 1-year retail rate for a 3-year reservation -- prices
    # an existing commitment at a rate nobody is paying, and that rate enters
    # the portfolio's total cost and therefore its savings.
    wanted = _term_key(term)
    for meter in price_book["meters"].values():
        if str(meter.get("skuId") or "").lower() == sku.lower() and meter.get("ri"):
            rate = meter["ri"].get(wanted)
            if rate is not None:
                return rate
    retail = retail_prices.get((region.lower(), sku.lower()))
    retail_key = f"riHourly{wanted[1:].lower()}"  # P1Y -> riHourly1y
    if retail and retail.get(retail_key) is not None:
        return retail[retail_key]
    return None


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------


def run_commitment_optimization(
    database: Any,
    *,
    lookback_days: int | None = None,
    risk_profile: str | None = None,
    term: str | None = None,
    requested_by: str = "",
) -> dict[str, Any]:
    """Execute one versioned optimization run and persist every result.

    Reruns never overwrite: each run receives a new id and immutable input
    records. The latest good run stays available while a new run is in
    progress.
    """
    from .database import utc_now

    lookback_days = lookback_days or settings.commitment_optimizer_lookback_days
    risk_profile = (risk_profile or settings.commitment_optimizer_risk_profile)
    term = (term or settings.commitment_optimizer_default_term).upper()
    if term not in engine.TERMS:
        term = "P1Y"
    if risk_profile not in engine.RISK_PROFILES:
        risk_profile = "balanced"
    run_id = f"opt-{uuid4()}"
    started = utc_now()
    _insert_run(
        database,
        run_id=run_id,
        requested_by=requested_by,
        started_at=started,
        status="running",
        risk_profile=risk_profile,
        term=term,
        lookback_days=lookback_days,
    )

    try:
        result = _optimize(
            database,
            run_id=run_id,
            lookback_days=lookback_days,
            risk_profile=risk_profile,
            term=term,
        )
    except Exception as error:
        _complete_run(
            database,
            run_id,
            status="failed",
            readiness="BLOCKED",
            summary={},
            gates=[],
            error=str(error),
        )
        raise

    _complete_run(
        database,
        run_id,
        status="completed",
        readiness=result["readiness"],
        summary=result["summary"],
        gates=result["gates"],
        error="",
        currency=result["currency"],
        recommended_portfolio=result["recommendedPortfolio"],
    )
    _insert_scenarios(database, run_id, result["scenarios"])
    _insert_recommendations(database, run_id, result["recommendations"])
    if result.get("hourlyEvidence"):
        database.store_optimizer_hourly(run_id, result["hourlyEvidence"])
    result["runId"] = run_id
    result["status"] = "completed"
    return result


def _optimize(
    database: Any, *, run_id: str, lookback_days: int, risk_profile: str, term: str
) -> dict[str, Any]:
    from .database import utc_now

    now = utc_now()
    with database.connect(read_only=True) as db:
        bounds = db.execute(
            """
            SELECT max(charge_period_start) FROM focus_cost_current
            WHERE lower(charge_category) = 'usage'
            """
        ).fetchone()
        currency_row = db.execute(
            """
            SELECT billing_currency FROM focus_cost_current
            WHERE lower(charge_category) = 'usage'
            GROUP BY billing_currency
            ORDER BY sum(abs(billed_cost)) DESC
            LIMIT 1
            """
        ).fetchone()
    if not bounds or not bounds[0] or not currency_row:
        gates = engine.run_data_quality_gates(
            price_sheet_available=False,
            currency_match=False,
            price_join_unambiguous=False,
            hourly_grain_explicit=False,
            expected_hours_match=False,
            no_duplicate_hours=True,
            commitments_current=False,
            flexibility_mappings_present=False,
            rightsizing_confident=False,
            usage_recent=False,
            no_focus_double_count=True,
            software_excluded=True,
            baselines_not_mixed=True,
            catalog_purchasable=False,
        )
        return {
            "readiness": engine.classify_readiness(gates),
            "gates": [_gate_dict(g) for g in gates],
            "summary": {
                "error": "No governed FOCUS usage charges are available yet.",
            },
            "scenarios": [],
            "recommendations": [],
            "currency": "",
            "recommendedPortfolio": "",
            "hourlyEvidence": [],
        }

    window_end = bounds[0] + timedelta(hours=1)
    window_start = window_end - timedelta(days=lookback_days)
    currency = str(currency_row[0]).upper()

    price_book = build_price_book(database)
    retail_prices = retail_fallback_prices(database)
    grain = _detect_grain(database, window_start, window_end)
    excluded = _excluded_resources(database)
    usage = build_hourly_usage(
        database, window_start, window_end, price_book, currency, excluded,
        term=term,
    )
    lines: list[engine.UsageLine] = usage["lines"]
    commitments = load_existing_commitments(
        database, price_book, retail_prices, term
    )

    freshness = _source_freshness(database)
    # P1-10a: unify gate + banner definition — available means row_count>0 AND not stale per source_freshness health
    try:
        health_items = database.source_freshness()
        ps = next((h for h in health_items if h.get("source") == "PriceSheet"), None)
        ps_healthy = ps is not None and not ps.get("stale") and (ps.get("rowCount") or 0) > 0
        price_sheet_available = ps_healthy and freshness.get("PriceSheet", 0) > 0
    except Exception:
        price_sheet_available = freshness.get("PriceSheet", 0) > 0
    currency_match = (
        not price_sheet_available
        or price_book["currency"] == ""
        or price_book["currency"] == currency
    )
    # Extended cost, not a sum of rates. contracted_payg_rate is a per
    # instance-hour rate and quantity is the instance count for the hourly
    # group, while unpricedContractedCost accumulates extended dollars -- so
    # omitting quantity compared incompatible units and understated the
    # healthy denominator by roughly the mean instance count, blocking runs
    # with only a small genuinely-unpriced tail. This gate is severity
    # "block": tripping it drops every recommendation to review.
    eligible_spend = sum(
        l.quantity * l.contracted_payg_rate for l in lines if l.sp_eligible
    )
    unpriced_spend = usage["stats"]["unpricedContractedCost"]
    price_join_unambiguous = bool(lines) and unpriced_spend <= 0.5 * max(
        eligible_spend + unpriced_spend, 1e-9
    )
    expected_hours = lookback_days * 24
    expected_hours_match = grain["observedHours"] >= 0.9 * expected_hours
    usage_recent = (now - window_end) <= timedelta(days=3)
    ri_demand = [
        l.quantity * l.ratio for l in lines if l.ri_eligible
    ]
    flexibility_mappings_present = bool(ri_demand) and sum(
        l.quantity * l.ratio
        for l in lines
        if l.ri_eligible and l.flexibility_group != FLEXIBILITY_UNKNOWN
    ) >= 0.8 * sum(ri_demand)
    rightsizing_confident = _rightsizing_confident(database)
    commitments_current = (
        "Commitments" in freshness and "SavingsPlans" in freshness
    )

    payg = engine.simulate_portfolio(lines, [], [], portfolio=engine.PAYG, term=term)
    existing = engine.simulate_portfolio(
        lines,
        commitments["reservations"],
        commitments["savingsPlans"],
        portfolio=engine.EXISTING,
        term=term,
    )

    best_ri, ri_scenarios = _optimize_reservations(
        lines, commitments, term, risk_profile
    )
    best_sp_only, sp_scenarios = _optimize_savings_plans(
        lines, commitments, database, term, risk_profile,
        ri_reservations=None, portfolio_label=engine.SP_ONLY,
    )
    best_blended, blended_scenarios = _optimize_savings_plans(
        lines, commitments, database, term, risk_profile,
        ri_reservations=best_ri["reservations"],
        portfolio_label=engine.BLENDED,
    )

    portfolios: dict[str, engine.PortfolioMetrics] = {
        engine.PAYG: payg,
        engine.EXISTING: existing,
        engine.RI_ONLY: best_ri["metrics"],
        engine.SP_ONLY: best_sp_only["metrics"],
        engine.BLENDED: best_blended["metrics"],
    }
    compositions: dict[str, tuple[list[engine.Reservation], list[engine.SavingsPlan]]] = {}
    for name, metric in portfolios.items():
        metric.portfolio = name
        reservations = []
        plans = []
        if name in (engine.EXISTING, engine.RI_ONLY, engine.BLENDED):
            reservations = (
                commitments["reservations"] + best_ri["incremental"]
                if name != engine.EXISTING
                else commitments["reservations"]
            )
        if name in (engine.EXISTING, engine.SP_ONLY, engine.BLENDED):
            plans = list(commitments["savingsPlans"])
            if name == engine.SP_ONLY:
                plans += best_sp_only["incremental"]
            elif name == engine.BLENDED:
                plans += best_blended["incremental"]
        compositions[name] = (list(reservations), list(plans))
        engine.apply_downside(metric, lines, reservations, plans)

    for metric in portfolios.values():
        if expected_hours > 0:
            metric.annualized_cost = metric.total_cost / expected_hours * engine.HOURS_PER_YEAR
            metric.annualized_savings = metric.savings_vs_payg / expected_hours * engine.HOURS_PER_YEAR

    scenarios = _scenario_rows(portfolios) + ri_scenarios + sp_scenarios + blended_scenarios
    recommended = engine.select_portfolio(portfolios, risk_profile)
    if portfolios[recommended].savings_vs_payg <= 0 and recommended != engine.PAYG:
        recommended = engine.PAYG

    gates = engine.run_data_quality_gates(
        price_sheet_available=price_sheet_available,
        currency_match=currency_match,
        price_join_unambiguous=bool(lines) and price_join_unambiguous,
        hourly_grain_explicit=grain["hourly"],
        expected_hours_match=expected_hours_match,
        no_duplicate_hours=grain["duplicateGroups"] == 0,
        commitments_current=commitments_current,
        flexibility_mappings_present=flexibility_mappings_present,
        rightsizing_confident=rightsizing_confident,
        usage_recent=usage_recent,
        no_focus_double_count=True,
        software_excluded=True,
        baselines_not_mixed=True,
        # Term-aware: a catalog entry only counts as purchasable if it has a
        # real rate for the requested term. Checking bool(catalog) alone let
        # a 3-year run pass this block gate while silently producing no
        # reservation recommendations at all -- no 3-year retail rate is
        # collected, and one is no longer fabricated as 0.7 x the 1-year.
        catalog_purchasable=(
            # No reservation-eligible usage at all: nothing to purchase.
            not ri_demand
            # Empty or stale catalog: the pre-existing behaviour. This is a
            # separate condition (source_freshness covers it), and blocking
            # here would take a Savings-Plan-only recommendation down with it.
            or not commitments["catalog"]
            # There IS demand and there IS a catalog, so at least one entry
            # must be purchasable on the requested term. Keying this on
            # best_ri["incremental"] would let it pass exactly when the
            # missing term rate is what emptied that list.
            or any(
                (c.hourly_rate_p1y if term == "P1Y" else c.hourly_rate_p3y) >= 0
                for c in commitments["catalog"]
            )
        ),
        positive_after_thresholds=(
            portfolios[recommended].savings_vs_payg
            >= settings.commitment_optimizer_min_monthly_savings
            * max(1, len(portfolios[recommended].per_hour) or 1)
            / 730.0
            if recommended != engine.PAYG
            else True
        ),
    )
    readiness = engine.classify_readiness(gates)

    recommendations = _recommendations(
        database,
        run_id=run_id,
        recommended=recommended,
        portfolios=portfolios,
        commitments=commitments,
        best_ri=best_ri,
        best_sp=best_blended if recommended == engine.BLENDED else best_sp_only,
        readiness=readiness,
        term=term,
        risk_profile=risk_profile,
        hours=len(payg.per_hour),
        rightsizing_confident=rightsizing_confident,
        now=now,
    )

    backtest_report = None
    if recommended != engine.PAYG:
        bt_reservations, bt_plans = compositions.get(recommended, ([], []))
        backtest_report = _backtest_portfolio(
            lines, bt_reservations, bt_plans, lookback_days
        )

    review_events = _review_events(
        database,
        recommended=recommended,
        portfolios=portfolios,
        commitments=commitments,
        usage_recent=usage_recent,
        readiness=readiness,
        now=now,
    )

    hourly_evidence = [
        {
            "hour": h.hour,
            "demandNormalized": h.demand_normalized,
            "riCapacity": h.ri_capacity,
            "riUsed": h.ri_used,
            "riUnused": h.ri_unused,
            "riCoveredCost": h.ri_covered_payg_equiv,
            "spCommitment": h.sp_commitment,
            "spUsed": h.sp_used,
            "spWaste": h.sp_waste,
            "spCoveredCost": h.sp_covered_payg_equiv,
            "paygCost": h.payg_cost,
            "overageCost": h.overage_cost,
            "totalCost": h.total_cost,
        }
        for h in portfolios[recommended].per_hour
    ]

    reconciliation = _reconcile(database, portfolios, best_blended, term)

    summary = {
        "windowStart": window_start.isoformat(),
        "windowEnd": window_end.isoformat(),
        "lookbackDays": lookback_days,
        "grain": grain,
        "currency": currency,
        "priceSheetCurrency": price_book["currency"],
        "priceSheetAvailable": price_sheet_available,
        "directionalPricing": usage["stats"]["directionalPricing"]
        or not price_sheet_available
        # An owned commitment priced on a term other than the one modelled.
        or bool(commitments.get("directionalTerms")),
        "usageStats": usage["stats"],
        "existingCommitments": {
            "reservations": len(commitments["reservations"]),
            "savingsPlans": len(commitments["savingsPlans"]),
        },
        "portfolios": {
            name: _portfolio_summary(metric) for name, metric in portfolios.items()
        },
        "recommendedPortfolio": recommended,
        "riskProfile": risk_profile,
        "frontier": engine.pareto_frontier(portfolios),
        "reconciliation": reconciliation,
        "backtest": backtest_report,
        "reviewEvents": review_events,
        "algorithmVersion": engine.ALGORITHM_VERSION,
    }

    return {
        "readiness": readiness,
        "gates": [_gate_dict(g) for g in gates],
        "summary": summary,
        "scenarios": scenarios,
        "recommendations": recommendations,
        "currency": currency,
        "recommendedPortfolio": recommended,
        "hourlyEvidence": hourly_evidence,
    }


# ---------------------------------------------------------------------------
# Portfolio search
# ---------------------------------------------------------------------------


def _optimize_reservations(
    lines: list[engine.UsageLine],
    commitments: dict[str, Any],
    term: str,
    risk_profile: str,
) -> dict[str, Any]:
    demand = engine.hourly_normalized_demand(lines)
    by_group: dict[str, list[float]] = {}
    for (_hour, group), value in demand.items():
        by_group.setdefault(group, []).append(value)
    existing_by_group = engine.reservation_capacity(commitments["reservations"])
    catalog_by_group: dict[str, list[engine.CatalogSku]] = {}
    for sku in commitments["catalog"]:
        catalog_by_group.setdefault(sku.flexibility_group, []).append(sku)

    breakpoints = (0.5, 0.7, 0.8, 0.9, 0.95, 1.0)
    scenarios: list[dict[str, Any]] = []
    best = {"metrics": None, "reservations": [], "incremental": [], "level": 0.0}
    best_objective = None
    for level in breakpoints:
        incremental: list[engine.Reservation] = []
        for group, series in sorted(by_group.items()):
            target = engine._percentile(series, level)
            existing_norm = existing_by_group.get(group, 0.0)
            needed = target - existing_norm
            if needed <= engine.EPSILON or group not in catalog_by_group:
                continue
            # A SKU with no real rate for the requested term is not
            # purchasable on that term (see the catalog build: no 3-year
            # retail rate is collected, and one is no longer fabricated).
            candidates = [
                c
                for c in catalog_by_group[group]
                if (c.hourly_rate_p1y if term == "P1Y" else c.hourly_rate_p3y) >= 0
            ]
            if not candidates:
                continue
            # Rank on the term's own rate. Ranking on the 1-year rate was
            # equivalent only while the 3-year rate was a fixed 0.7 multiple
            # of it; with real per-family 3-year rates it would pick the
            # wrong SKU.
            sku = min(
                candidates,
                key=lambda c: (
                    (
                        (c.hourly_rate_p1y if term == "P1Y" else c.hourly_rate_p3y)
                        / c.ratio
                    )
                    if c.ratio
                    else 1e9,
                    c.sku,
                ),
            )
            quantity = max(1, int(round(needed / sku.ratio))) if sku.ratio else 0
            if quantity <= 0:
                continue
            rate = (
                sku.hourly_rate_p1y if term == "P1Y" else sku.hourly_rate_p3y
            )
            incremental.append(
                engine.Reservation(
                    sku=sku.sku,
                    region=sku.region,
                    flexibility_group=group,
                    ratio=sku.ratio,
                    hourly_rate=rate,
                    quantity=quantity,
                    term=term,
                    existing=False,
                )
            )
        reservations = commitments["reservations"] + incremental
        metrics = engine.simulate_portfolio(
            lines, reservations, commitments["savingsPlans"],
            portfolio=engine.RI_ONLY, term=term,
        )
        engine.apply_downside(metrics, lines, reservations, commitments["savingsPlans"])
        scenarios.append(_scenario_row(f"ri-p{int(level * 100)}", metrics))
        score = engine.objective(metrics, risk_profile)
        if incremental and (best_objective is None or score < best_objective):
            best_objective = score
            best = {
                "metrics": metrics,
                "reservations": reservations,
                "incremental": incremental,
                "level": level,
            }
    if best["metrics"] is None:
        base = engine.simulate_portfolio(
            lines, commitments["reservations"], commitments["savingsPlans"],
            portfolio=engine.RI_ONLY, term=term,
        )
        best = {
            "metrics": base,
            "reservations": commitments["reservations"],
            "incremental": [],
            "level": 0.0,
        }
    return best, scenarios


def _optimize_savings_plans(
    lines: list[engine.UsageLine],
    commitments: dict[str, Any],
    database: Any,
    term: str,
    risk_profile: str,
    ri_reservations: list[engine.Reservation] | None,
    portfolio_label: str = engine.SP_ONLY,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    reservations = (
        ri_reservations if ri_reservations is not None else commitments["reservations"]
    )
    existing_commit = sum(
        s.hourly_commitment for s in commitments["savingsPlans"]
    )
    hourly_eligible_spend = _hourly_eligible_sp_spend(lines, reservations)
    azure_recommended = _azure_sp_recommendation(database)
    candidates = engine.sp_candidate_commitments(
        hourly_eligible_spend,
        existing=existing_commit,
        azure_recommended=azure_recommended,
    )
    scenarios: list[dict[str, Any]] = []
    best = {
        "metrics": None,
        "incremental": [],
        "commitment": existing_commit,
    }
    best_objective = None
    for candidate in candidates:
        incremental_commit = max(0.0, candidate - existing_commit)
        plans = list(commitments["savingsPlans"])
        if incremental_commit > engine.EPSILON:
            plans.append(
                engine.SavingsPlan(
                    hourly_commitment=incremental_commit, term=term, existing=False
                )
            )
        metrics = engine.simulate_portfolio(
            lines, reservations, plans, portfolio=portfolio_label, term=term
        )
        engine.apply_downside(metrics, lines, reservations, plans)
        scenarios.append(_scenario_row(f"sp-{candidate:.2f}", metrics))
        score = engine.objective(metrics, risk_profile)
        if best_objective is None or score < best_objective:
            best_objective = score
            best = {
                "metrics": metrics,
                "incremental": [
                    p for p in plans if not p.existing
                ],
                "commitment": candidate,
            }
    return best, scenarios


def _hourly_eligible_sp_spend(
    lines: list[engine.UsageLine],
    reservations: list[engine.Reservation],
) -> list[float]:
    residual = engine.simulate_portfolio(lines, reservations, [])
    return [
        hour.sp_used + hour.overage_cost for hour in residual.per_hour
    ]


def _azure_sp_recommendation(database: Any) -> float | None:
    with database.connect(read_only=True) as db:
        row = db.execute(
            """
            SELECT recommended_commitment
            FROM savings_plan_recommendations_current
            WHERE recommended_commitment IS NOT NULL
            ORDER BY observed_at DESC
            LIMIT 1
            """
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# Recommendations, scenarios, reconciliation
# ---------------------------------------------------------------------------


def apportion_expected_savings(
    recommendations: list[dict[str, Any]],
    monthly_savings: float,
) -> None:
    """Split a portfolio's monthly savings across its purchase lines.

    Each line previously reported the WHOLE portfolio's savings, so a plan
    with N purchase lines summed to N times the true figure. These rows are
    rendered as a per-row column in the purchase-plan table and written
    per-row into the approved purchase-manifest CSV, where a human totals the
    column.

    Attribution is proportional to each line's own committed cost, which is
    deterministic and sums back to the portfolio total. Informational,
    deferred and zero-cost rows are left at 0.0.
    """
    purchase_lines = [
        item
        for item in recommendations
        if item.get("commitmentType") in ("reservation", "savings_plan")
        and item.get("status") != "deferred"
        and float(item.get("expectedCost") or 0.0) > 0
    ]
    committed_total = sum(float(item["expectedCost"]) for item in purchase_lines)
    if committed_total <= 0 or monthly_savings <= 0:
        return
    for item in purchase_lines:
        item["expectedSavings"] = round(
            monthly_savings * (float(item["expectedCost"]) / committed_total), 2
        )


def _recommendations(
    database: Any,
    *,
    run_id: str,
    recommended: str,
    portfolios: dict[str, engine.PortfolioMetrics],
    commitments: dict[str, Any],
    best_ri: dict[str, Any],
    best_sp: dict[str, Any],
    readiness: str,
    term: str,
    risk_profile: str,
    hours: int,
    rightsizing_confident: bool,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    from .database import utc_now

    now = now or utc_now()
    recommendations: list[dict[str, Any]] = []
    purchase_ready = readiness == "PURCHASE_READY"
    status = "purchase_ready" if purchase_ready else "review_required"

    if recommended in (engine.RI_ONLY, engine.BLENDED):
        for reservation in best_ri["incremental"]:
            lines, overcoverage = engine.integer_decompose(
                reservation.quantity * reservation.ratio,
                [
                    c
                    for c in commitments["catalog"]
                    if c.flexibility_group == reservation.flexibility_group
                ]
                or commitments["catalog"],
                term,
            )
            for line in lines:
                recommendations.append(
                    {
                        "recommendationId": f"{run_id}-ri-{len(recommendations)}",
                        "runId": run_id,
                        "scenarioId": recommended,
                        "action": "buy_now" if purchase_ready else "review",
                        "commitmentType": "reservation",
                        "sku": line["sku"],
                        "region": line["region"],
                        "flexibilityGroup": reservation.flexibility_group,
                        "quantity": line["quantity"],
                        "normalizedQuantity": line["normalizedQuantity"],
                        "hourlyCommitment": 0.0,
                        "term": term,
                        "scope": "Shared",
                        # This line's own cost: its quantity at the rate the
                        # decomposition actually selected it on. Using
                        # reservation.quantity repeated the whole
                        # reservation's cost on every decomposed SKU, and
                        # reservation.hourly_rate is not even the rate of the
                        # SKU named on the row.
                        "expectedCost": round(
                            line.get("hourlyRate", reservation.hourly_rate)
                            * line["quantity"]
                            * engine.TERM_HOURS.get(term, 8760.0),
                            2,
                        ),
                        # Apportioned below, once every line's cost is known.
                        "expectedSavings": 0.0,
                        "utilization": round(
                            portfolios[recommended].ri_utilization, 4
                        ),
                        "waste": round(portfolios[recommended].sp_waste, 2),
                        "coverage": round(portfolios[recommended].coverage, 4),
                        "confidence": "high" if purchase_ready else "directional",
                        "rationale": (
                            f"Hourly normalized demand at the selected "
                            f"percentile exceeds existing reservation capacity "
                            f"in {reservation.flexibility_group}; overcoverage "
                            f"{overcoverage:.2f} normalized units is explicit."
                        ),
                        "evidence": {"overcoverageNormalized": overcoverage},
                        "status": status,
                    }
                )
    if recommended in (engine.SP_ONLY, engine.BLENDED):
        for plan in best_sp["incremental"]:
            metrics = portfolios[recommended]
            recommendations.append(
                {
                    "recommendationId": f"{run_id}-sp-{len(recommendations)}",
                    "runId": run_id,
                    "scenarioId": recommended,
                    "action": "buy_now" if purchase_ready else "review",
                    "commitmentType": "savings_plan",
                    "sku": "",
                    "region": "",
                    "flexibilityGroup": "",
                    "quantity": 0.0,
                    "normalizedQuantity": 0.0,
                    "hourlyCommitment": round(plan.hourly_commitment, 4),
                    "term": term,
                    "scope": "Shared",
                    "expectedCost": round(
                        plan.hourly_commitment * engine.TERM_HOURS.get(term, 8760.0),
                        2,
                    ),
                    # Apportioned below, once every line's cost is known.
                    "expectedSavings": 0.0,
                    "utilization": round(metrics.sp_utilization, 4),
                    "waste": round(metrics.sp_waste, 2),
                    "coverage": round(metrics.coverage, 4),
                    "confidence": "high" if purchase_ready else "directional",
                    "rationale": (
                        "Hourly eligible spend breakpoint with the lowest "
                        "objective value; unused commitment is reported as "
                        "waste and never carried between hours."
                    ),
                    "evidence": {
                        "spUtilP5": metrics.sp_util_p5,
                        "spUtilP50": metrics.sp_util_p50,
                        "spUtilP95": metrics.sp_util_p95,
                        "hoursWithWaste": metrics.hours_with_waste,
                    },
                    "status": status,
                }
            )
    payg_metrics = portfolios[engine.PAYG]
    recommendations.append(
        {
            "recommendationId": f"{run_id}-payg-{len(recommendations)}",
            "runId": run_id,
            "scenarioId": engine.PAYG,
            "action": "no_action",
            "commitmentType": "payg_remainder",
            "sku": "",
            "region": "",
            "flexibilityGroup": "",
            "quantity": 0.0,
            "normalizedQuantity": 0.0,
            "hourlyCommitment": 0.0,
            "term": "",
            "scope": "",
            "expectedCost": round(
                portfolios[recommended].payg_residual_cost / max(1, hours) * 730, 2
            ),
            "expectedSavings": 0.0,
            "utilization": 0.0,
            "waste": 0.0,
            "coverage": round(portfolios[recommended].coverage, 4),
            "confidence": "high",
            "rationale": (
                "Residual usage stays on demand: volatility, lifecycle risk, "
                "or missing hourly evidence make commitment uneconomic or "
                "ungoverned. Reconsider when coverage data improves."
            ),
            "evidence": {
                "paygResidualMonthly": round(
                    payg_metrics.payg_equiv_cost / max(1, hours) * 730, 2
                )
            },
            "status": "informational",
        }
    )
    if not rightsizing_confident and recommended != engine.PAYG:
        review_date = (now + timedelta(days=30)).date().isoformat()
        recommendations.append(
            {
                "recommendationId": f"{run_id}-defer-{len(recommendations)}",
                "runId": run_id,
                "scenarioId": recommended,
                "action": "defer",
                "commitmentType": "reservation",
                "sku": "",
                "region": "",
                "flexibilityGroup": "",
                "quantity": 0.0,
                "normalizedQuantity": 0.0,
                "hourlyCommitment": 0.0,
                "term": term,
                "scope": "Shared",
                "expectedCost": 0.0,
                "expectedSavings": 0.0,
                "utilization": 0.0,
                "waste": 0.0,
                "coverage": 0.0,
                "confidence": "directional",
                "rationale": (
                    "Deferred until rightsizing evidence is confident: "
                    "buying before governed rightsizing completes risks "
                    "immediate overcoverage."
                ),
                "evidence": {
                    "trigger": "rightsizing_confident",
                    "proposedDate": review_date,
                    "benefitOfWaiting": (
                        "Avoids purchasing capacity that governed "
                        "rightsizing is about to remove."
                    ),
                    "costOfWaiting": (
                        "Eligible usage stays at PAYG rates until the "
                        "purchase is re-evaluated."
                    ),
                    "requiredEvent": (
                        "Governed rightsizing recommendations reach "
                        "confident coverage, or an administrator approves "
                        "buying the current-state baseline."
                    ),
                },
                "status": "deferred",
            }
        )
    # ------------------------------------------------------------------
    # Apportion the portfolio's savings across the purchase lines.
    #
    # Every line previously reported portfolios[recommended].savings_vs_payg
    # -- the WHOLE portfolio's monthly savings -- so a plan with N purchase
    # lines showed N times the true figure. These rows render as a per-row
    # column in the "Purchase plan" table and are written per-row into the
    # approved purchase-manifest CSV, where a human totals the column. A
    # 4-line blended plan saving $40,000/month summed to $160,000/month.
    #
    # Savings are attributed in proportion to each line's own committed cost,
    # which is deterministic and sums back to the portfolio total.
    # ------------------------------------------------------------------
    apportion_expected_savings(
        recommendations,
        max(0.0, portfolios[recommended].savings_vs_payg / max(1, hours) * 730),
    )

    recommendations.extend(
        _laddered_recommendations(
            run_id=run_id, commitments=commitments, term=term, now=now
        )
    )
    return recommendations


def _laddered_recommendations(
    *,
    run_id: str,
    commitments: dict[str, Any],
    term: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Renewal-aligned and allow-expiry recommendations (spec section 17).

    Expiring commitments surface as explicit ``renew`` review lines aligned
    to their expiry date, and underutilized commitments surface as
    ``allow_expiry`` so an expiring portfolio is a deliberate choice rather
    than a surprise. Future prices are never assumed to hold.
    """
    ladder: list[dict[str, Any]] = []
    horizon = now + timedelta(
        days=settings.commitment_optimizer_expiry_review_days
    )
    allow_expiry_utilization = (
        settings.commitment_optimizer_allow_expiry_utilization
    )
    for detail in commitments.get("reservationDetails", []):
        expiry = _parse_expiry(detail.get("expiryDate"))
        if not expiry or not (now <= expiry <= horizon):
            continue
        utilization = detail.get("utilization30d")
        underutilized = (
            utilization is not None
            and utilization < allow_expiry_utilization
        )
        action = "allow_expiry" if underutilized else "renew"
        rationale = (
            f"Reservation {detail.get('sku', '')} "
            f"({detail.get('quantity', 0)} units, "
            f"{detail.get('region', '')}) expires on "
            f"{expiry.date().isoformat()}."
        )
        if underutilized:
            rationale += (
                f" 30-day utilization {utilization:.0%} is below the "
                f"{allow_expiry_utilization:.0%} allow-expiry threshold; "
                "letting it lapse is modeled as cheaper than renewal."
            )
        else:
            rationale += (
                " Re-evaluate the increment against the latest hourly "
                "evidence before renewing; future prices are not assumed "
                "to match today's."
            )
        ladder.append(
            {
                "recommendationId": f"{run_id}-ladder-{len(ladder)}",
                "runId": run_id,
                "scenarioId": engine.EXISTING,
                "action": action,
                "commitmentType": "reservation",
                "sku": detail.get("sku", ""),
                "region": detail.get("region", ""),
                "flexibilityGroup": "",
                "quantity": float(detail.get("quantity", 0)),
                "normalizedQuantity": 0.0,
                "hourlyCommitment": 0.0,
                "term": term,
                "scope": "Shared",
                "expectedCost": 0.0,
                "expectedSavings": 0.0,
                "utilization": round(utilization, 4)
                if utilization is not None
                else 0.0,
                "waste": 0.0,
                "coverage": 0.0,
                "confidence": "directional",
                "rationale": rationale,
                "evidence": {
                    "trigger": "commitment_expiry",
                    "proposedDate": expiry.date().isoformat(),
                    "commitmentId": detail.get("commitmentId", ""),
                },
                "status": "deferred",
            }
        )
    for detail in commitments.get("savingsPlanDetails", []):
        expiry = _parse_expiry(detail.get("expiryDate"))
        if not expiry or not (now <= expiry <= horizon):
            continue
        utilization = detail.get("utilization30d")
        underutilized = (
            utilization is not None
            and utilization < allow_expiry_utilization
        )
        action = "allow_expiry" if underutilized else "renew"
        hourly = float(detail.get("hourlyCommitment", 0.0))
        rationale = (
            f"Savings Plan ({hourly:.4f}/hour) expires on "
            f"{expiry.date().isoformat()}."
        )
        if underutilized:
            rationale += (
                f" 30-day utilization {utilization:.0%} is below the "
                f"{allow_expiry_utilization:.0%} allow-expiry threshold; "
                "re-commit only the modeled stable spend, if any."
            )
        else:
            rationale += (
                " Re-derive the hourly commitment from the latest hourly "
                "eligible spend before renewing."
            )
        ladder.append(
            {
                "recommendationId": f"{run_id}-ladder-{len(ladder)}",
                "runId": run_id,
                "scenarioId": engine.EXISTING,
                "action": action,
                "commitmentType": "savings_plan",
                "sku": "",
                "region": "",
                "flexibilityGroup": "",
                "quantity": 0.0,
                "normalizedQuantity": 0.0,
                "hourlyCommitment": round(hourly, 4),
                "term": term,
                "scope": "Shared",
                "expectedCost": 0.0,
                "expectedSavings": 0.0,
                "utilization": round(utilization, 4)
                if utilization is not None
                else 0.0,
                "waste": 0.0,
                "coverage": 0.0,
                "confidence": "directional",
                "rationale": rationale,
                "evidence": {
                    "trigger": "commitment_expiry",
                    "proposedDate": expiry.date().isoformat(),
                    "commitmentId": detail.get("commitmentId", ""),
                },
                "status": "deferred",
            }
        )
    return ladder


def _scenario_rows(
    portfolios: dict[str, engine.PortfolioMetrics],
) -> list[dict[str, Any]]:
    return [_scenario_row(name, metric) for name, metric in sorted(portfolios.items())]


def _scenario_row(name: str, metric: engine.PortfolioMetrics) -> dict[str, Any]:
    return {
        "scenarioId": f"{metric.portfolio or name}-{name}",
        "name": name,
        "portfolio": metric.portfolio or name,
        "term": metric.term,
        "scope": "Shared",
        "demandFactor": 1.0,
        "totalCost": round(metric.total_cost, 2),
        "paygEquivCost": round(metric.payg_equiv_cost, 2),
        "savings": round(metric.savings_vs_payg, 2),
        "contractedEsr": round(metric.contracted_esr, 4),
        "listEsr": round(metric.list_esr, 4),
        "coverage": round(metric.coverage, 4),
        "riUtilization": round(metric.ri_utilization, 4),
        "spUtilization": round(metric.sp_utilization, 4),
        "waste": round(metric.sp_waste, 2),
        "overage": round(sum(h.overage_cost for h in metric.per_hour), 2),
        "downsideSavings": round(metric.downside_savings, 2),
        "metrics": {
            "hours": metric.hours,
            "annualizedCost": round(metric.annualized_cost, 2),
            "annualizedSavings": round(metric.annualized_savings, 2),
            "wastePercent": round(metric.waste_pct, 4),
            "hoursWithWaste": metric.hours_with_waste,
            "hoursWithOverage": metric.hours_with_overage,
            "longestWasteStreak": metric.longest_waste_streak,
            "spUtilP5": round(metric.sp_util_p5, 4),
            "spUtilP50": round(metric.sp_util_p50, 4),
            "spUtilP95": round(metric.sp_util_p95, 4),
            "riBreakEvenUtilization": round(metric.ri_break_even_utilization, 4),
        },
    }


def _portfolio_summary(metric: engine.PortfolioMetrics) -> dict[str, Any]:
    return {
        "hours": metric.hours,
        "totalCost": round(metric.total_cost, 2),
        "paygEquivCost": round(metric.payg_equiv_cost, 2),
        "listEquivCost": round(metric.list_equiv_cost, 2),
        "savings": round(metric.savings_vs_payg, 2),
        "contractedEsr": round(metric.contracted_esr, 4),
        "listEsr": round(metric.list_esr, 4),
        "coverage": round(metric.coverage, 4),
        "riUtilization": round(metric.ri_utilization, 4),
        "spUtilization": round(metric.sp_utilization, 4),
        "waste": round(metric.sp_waste, 2),
        "wastePercent": round(metric.waste_pct, 4),
        "downsideSavings": round(metric.downside_savings, 2),
        "annualizedCost": round(metric.annualized_cost, 2),
        "annualizedSavings": round(metric.annualized_savings, 2),
    }


def _reconcile(
    database: Any,
    portfolios: dict[str, engine.PortfolioMetrics],
    best_blended: dict[str, Any],
    term: str,
) -> dict[str, Any]:
    with database.connect(read_only=True) as db:
        sp_row = db.execute(
            """
            SELECT recommended_commitment, savings_amount
            FROM savings_plan_recommendations_current
            WHERE recommended_commitment IS NOT NULL
            ORDER BY observed_at DESC
            LIMIT 1
            """
        ).fetchone()
        ri_row = db.execute(
            """
            SELECT sum(recommended_quantity), sum(net_savings)
            FROM reservation_recommendations_current
            """
        ).fetchone()
    flux_commitment = best_blended["commitment"] if best_blended else None
    return engine.reconcile_with_azure(
        flux_commitment=flux_commitment,
        azure_commitment=float(sp_row[0]) if sp_row and sp_row[0] is not None else None,
        flux_savings=(
            portfolios[engine.BLENDED].savings_vs_payg if portfolios else None
        ),
        azure_savings=(
            float(sp_row[1])
            if sp_row and sp_row[1] is not None
            else (float(ri_row[1]) if ri_row and ri_row[1] is not None else None)
        ),
        tolerance_pct=settings.commitment_reconciliation_tolerance_pct,
    )


# ---------------------------------------------------------------------------
# Operational persistence + status
# ---------------------------------------------------------------------------


def _gate_dict(gate: engine.GateResult) -> dict[str, Any]:
    return {
        "name": gate.name,
        "passed": gate.passed,
        "severity": gate.severity,
        "detail": gate.detail,
    }


def _insert_run(
    database: Any,
    *,
    run_id: str,
    requested_by: str,
    started_at: Any,
    status: str,
    risk_profile: str,
    term: str,
    lookback_days: int,
) -> None:
    with database.operational_connect() as db:
        db.execute(
            """
            INSERT INTO commitment_optimization_runs (
                run_id, requested_by, started_at, status, risk_profile, term,
                lookback_days, algorithm_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                requested_by,
                started_at,
                status,
                risk_profile,
                term,
                lookback_days,
                engine.ALGORITHM_VERSION,
            ],
        )


def _complete_run(
    database: Any,
    run_id: str,
    *,
    status: str,
    readiness: str,
    summary: dict[str, Any],
    gates: list[dict[str, Any]],
    error: str,
    currency: str = "",
    recommended_portfolio: str = "",
) -> None:
    from .database import utc_now

    inputs_hash = hashlib.sha256(
        json.dumps(summary, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    with database.operational_connect() as db:
        db.execute(
            """
            UPDATE commitment_optimization_runs
            SET completed_at = ?, status = ?, readiness = ?,
                data_quality_json = ?, inputs_hash = ?,
                recommended_portfolio = ?, summary_json = ?, currency = ?,
                error = ?
            WHERE run_id = ?
            """,
            [
                utc_now(),
                status,
                readiness,
                json.dumps(gates, default=str),
                inputs_hash,
                recommended_portfolio,
                json.dumps(summary, default=str),
                currency,
                error,
                run_id,
            ],
        )


def _insert_scenarios(
    database: Any, run_id: str, scenarios: list[dict[str, Any]]
) -> None:
    if not scenarios:
        return
    with database.operational_connect() as db:
        db.executemany(
            """
            INSERT INTO commitment_optimizer_scenarios (
                run_id, scenario_id, name, portfolio, term, scope,
                demand_factor, total_cost, payg_equiv_cost, savings,
                contracted_esr, list_esr, coverage, ri_utilization,
                sp_utilization, waste, overage, downside_savings, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                [
                    run_id,
                    scenario["scenarioId"][:200],
                    scenario["name"][:200],
                    scenario["portfolio"],
                    scenario["term"],
                    scenario["scope"],
                    scenario["demandFactor"],
                    scenario["totalCost"],
                    scenario["paygEquivCost"],
                    scenario["savings"],
                    scenario["contractedEsr"],
                    scenario["listEsr"],
                    scenario["coverage"],
                    scenario["riUtilization"],
                    scenario["spUtilization"],
                    scenario["waste"],
                    scenario["overage"],
                    scenario["downsideSavings"],
                    json.dumps(scenario.get("metrics") or {}, default=str),
                ]
                for scenario in scenarios
            ],
        )


def _insert_recommendations(
    database: Any, run_id: str, recommendations: list[dict[str, Any]]
) -> None:
    if not recommendations:
        return
    with database.operational_connect() as db:
        db.executemany(
            """
            INSERT INTO commitment_optimizer_recommendations (
                recommendation_id, run_id, scenario_id, action,
                commitment_type, sku, region, flexibility_group, quantity,
                normalized_quantity, hourly_commitment, term, scope,
                expected_cost, expected_savings, utilization, waste, coverage,
                confidence, rationale, evidence_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?)
            """,
            [
                [
                    rec["recommendationId"],
                    run_id,
                    rec["scenarioId"],
                    rec["action"],
                    rec["commitmentType"],
                    rec["sku"],
                    rec["region"],
                    rec["flexibilityGroup"],
                    rec["quantity"],
                    rec["normalizedQuantity"],
                    rec["hourlyCommitment"],
                    rec["term"],
                    rec["scope"],
                    rec["expectedCost"],
                    rec["expectedSavings"],
                    rec["utilization"],
                    rec["waste"],
                    rec["coverage"],
                    rec["confidence"],
                    rec["rationale"],
                    json.dumps(rec.get("evidence") or {}, default=str),
                    rec["status"],
                ]
                for rec in recommendations
            ],
        )


def _excluded_resources(database: Any) -> set[str]:
    try:
        with database.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT target_id FROM commitment_optimizer_overrides
                WHERE override_type = 'exclude' AND target_type = 'resource'
                  AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)
                """
            ).fetchall()
        return {str(row[0]).lower() for row in rows}
    except Exception:
        return set()


def _source_freshness(database: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        with database.connect(read_only=True) as db:
            for source, row_count in db.execute(
                """
                SELECT source, COALESCE(sum(row_count), 0)
                FROM source_sync_state
                GROUP BY source
                """
            ).fetchall():
                counts[str(source)] = int(row_count or 0)
    except Exception:
        pass
    return counts


def _rightsizing_confident(database: Any) -> bool:
    try:
        with database.connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT count(*),
                       count(*) FILTER (WHERE status = 'candidate')
                FROM rightsizing_recommendations_current
                """
            ).fetchone()
        if not row or not row[0]:
            return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Backtesting, review events, laddering evidence
# ---------------------------------------------------------------------------


def _split_train_holdout(
    lines: Sequence[engine.UsageLine], holdout_days: int = 7
) -> tuple[list[engine.UsageLine], list[engine.UsageLine]]:
    """Chronological train/holdout split; the holdout is the final window."""
    if not lines:
        return [], []
    hours = sorted({line.hour for line in lines})
    cutoff = hours[-1] - timedelta(hours=holdout_days * 24) + timedelta(hours=1)
    if cutoff <= hours[0]:
        return [], list(lines)
    train = [line for line in lines if line.hour < cutoff]
    holdout = [line for line in lines if line.hour >= cutoff]
    return train, holdout


def _backtest_portfolio(
    lines: Sequence[engine.UsageLine],
    reservations: Sequence[engine.Reservation],
    savings_plans: Sequence[engine.SavingsPlan],
    lookback_days: int,
) -> dict[str, Any] | None:
    """Rolling train/holdout backtest of the selected portfolio.

    Requires at least 14 days of hourly evidence so the holdout window is
    never the entire sample. Shorter lookbacks report no backtest rather
    than an overstated one.
    """
    if lookback_days < 14:
        return None
    train, holdout = _split_train_holdout(lines)
    if not train or not holdout:
        return None
    result = engine.backtest(train, holdout, reservations, savings_plans)
    result["trainHours"] = len({line.hour for line in train})
    result["holdoutHours"] = len({line.hour for line in holdout})
    result["stable"] = (
        result["holdoutSavings"] >= 0
        and result["holdoutSavings"] >= 0.25 * result["inSampleSavings"]
        if result["inSampleSavings"] > 0
        else result["holdoutSavings"] >= 0
    )
    return result


def _commitment_expiry_events(
    commitments: dict[str, Any], *, within_days: int, now: datetime
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    horizon = now + timedelta(days=within_days)
    for detail in commitments.get("reservationDetails", []):
        expiry = _parse_expiry(detail.get("expiryDate"))
        if expiry and now <= expiry <= horizon:
            events.append(
                {
                    "type": "commitment_expiring",
                    "commitmentType": "reservation",
                    "commitmentId": detail.get("commitmentId", ""),
                    "detail": (
                        f"Reservation {detail.get('sku', '')} "
                        f"({detail.get('quantity', 0)} units, "
                        f"{detail.get('region', '')}) expires on "
                        f"{expiry.date().isoformat()}; plan renewal or "
                        "deliberate expiry before that date."
                    ),
                    "expiryDate": expiry.date().isoformat(),
                }
            )
    for detail in commitments.get("savingsPlanDetails", []):
        expiry = _parse_expiry(detail.get("expiryDate"))
        if expiry and now <= expiry <= horizon:
            events.append(
                {
                    "type": "commitment_expiring",
                    "commitmentType": "savings_plan",
                    "commitmentId": detail.get("commitmentId", ""),
                    "detail": (
                        f"Savings Plan "
                        f"({detail.get('hourlyCommitment', 0):.4f}/hour) "
                        f"expires on {expiry.date().isoformat()}; plan "
                        "renewal or deliberate expiry before that date."
                    ),
                    "expiryDate": expiry.date().isoformat(),
                }
            )
    return events


def _parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _review_events(
    database: Any,
    *,
    recommended: str,
    portfolios: dict[str, engine.PortfolioMetrics],
    commitments: dict[str, Any],
    usage_recent: bool,
    readiness: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Continuous re-optimization review events (spec section 21).

    Events are advisory: they flag thresholds crossed since the last run so
    a human can re-evaluate. They never mutate recommendations.
    """
    events: list[dict[str, Any]] = []
    threshold = settings.commitment_optimizer_utilization_review_threshold
    metrics = portfolios.get(recommended)
    if metrics is not None and recommended != engine.PAYG:
        if metrics.ri_capacity > 0 and metrics.ri_utilization < threshold:
            events.append(
                {
                    "type": "ri_utilization_below_threshold",
                    "detail": (
                        f"Modeled Reservation utilization "
                        f"{metrics.ri_utilization:.0%} is below the "
                        f"{threshold:.0%} review threshold."
                    ),
                }
            )
        if metrics.sp_utilization < threshold and (
            metrics.sp_used > 0 or metrics.sp_waste > 0
        ):
            events.append(
                {
                    "type": "sp_utilization_below_threshold",
                    "detail": (
                        f"Modeled Savings Plan utilization "
                        f"{metrics.sp_utilization:.0%} is below the "
                        f"{threshold:.0%} review threshold."
                    ),
                }
            )
    if not usage_recent:
        events.append(
            {
                "type": "usage_data_stale",
                "detail": (
                    "The newest governed FOCUS evidence is older than three "
                    "days; refresh cost ingestion before acting on this run."
                ),
            }
        )
    if readiness in ("BLOCKED", "DIRECTIONAL_ONLY"):
        events.append(
            {
                "type": "data_quality_degraded",
                "detail": (
                    f"Run readiness is {readiness}; purchase manifests are "
                    "refused until the blocking data-quality gates pass."
                ),
            }
        )
    events.extend(
        _commitment_expiry_events(
            commitments,
            within_days=settings.commitment_optimizer_expiry_review_days,
            now=now,
        )
    )
    previous = _previous_completed_run(database)
    if previous and metrics is not None:
        materiality = settings.commitment_optimizer_change_materiality_pct
        if previous["recommendedPortfolio"] and (
            previous["recommendedPortfolio"] != recommended
        ):
            events.append(
                {
                    "type": "recommendation_changed",
                    "detail": (
                        "Recommended portfolio changed from "
                        f"{previous['recommendedPortfolio']} to "
                        f"{recommended} since the previous completed run."
                    ),
                }
            )
        else:
            previous_summary = previous.get("summary") or {}
            previous_portfolios = previous_summary.get("portfolios") or {}
            previous_metrics = previous_portfolios.get(recommended) or {}
            previous_savings = float(previous_metrics.get("annualizedSavings") or 0)
            current_savings = metrics.annualized_savings
            base = max(abs(previous_savings), 1.0)
            drift_pct = abs(current_savings - previous_savings) / base * 100
            if previous_savings != 0 and drift_pct >= materiality:
                events.append(
                    {
                        "type": "recommendation_changed",
                        "detail": (
                            "Annualized savings for the recommended portfolio "
                            f"moved {drift_pct:.1f}% since the previous "
                            "completed run (materiality threshold "
                            f"{materiality:.0f}%)."
                        ),
                    }
                )
    return events


def _previous_completed_run(database: Any) -> dict[str, Any] | None:
    try:
        with database.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT run_id, recommended_portfolio, summary_json
                FROM commitment_optimization_runs
                WHERE status = 'completed'
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        summary = json.loads(row[2] or "{}")
    except json.JSONDecodeError:
        summary = {}
    return {
        "runId": row[0],
        "recommendedPortfolio": row[1] or "",
        "summary": summary,
    }


def list_optimizer_runs(database: Any, limit: int = 25) -> list[dict[str, Any]]:
    with database.operational_connect(read_only=True) as db:
        rows = db.execute(
            """
            SELECT run_id, requested_by, started_at, completed_at, status,
                   risk_profile, term, lookback_days, algorithm_version,
                   readiness, currency, recommended_portfolio, error
            FROM commitment_optimization_runs
            ORDER BY started_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    return [
        {
            "runId": row[0],
            "requestedBy": row[1],
            "startedAt": row[2].isoformat() if row[2] else None,
            "completedAt": row[3].isoformat() if row[3] else None,
            "status": row[4],
            "riskProfile": row[5],
            "term": row[6],
            "lookbackDays": row[7],
            "algorithmVersion": row[8],
            "readiness": row[9],
            "currency": row[10],
            "recommendedPortfolio": row[11],
            "error": row[12],
        }
        for row in rows
    ]


def optimizer_run_detail(database: Any, run_id: str) -> dict[str, Any]:
    with database.operational_connect(read_only=True) as db:
        run = db.execute(
            """
            SELECT run_id, requested_by, started_at, completed_at, status,
                   risk_profile, term, lookback_days, algorithm_version,
                   readiness, currency, recommended_portfolio,
                   data_quality_json, summary_json, error
            FROM commitment_optimization_runs
            WHERE run_id = ?
            """,
            [run_id],
        ).fetchone()
        if not run:
            raise KeyError(run_id)
        scenarios = db.execute(
            """
            SELECT scenario_id, name, portfolio, term, scope, demand_factor,
                   total_cost, payg_equiv_cost, savings, contracted_esr,
                   list_esr, coverage, ri_utilization, sp_utilization, waste,
                   overage, downside_savings, metrics_json
            FROM commitment_optimizer_scenarios
            WHERE run_id = ?
            ORDER BY portfolio, scenario_id
            """,
            [run_id],
        ).fetchall()
        recommendations = db.execute(
            """
            SELECT recommendation_id, scenario_id, action, commitment_type,
                   sku, region, flexibility_group, quantity,
                   normalized_quantity, hourly_commitment, term, scope,
                   expected_cost, expected_savings, utilization, waste,
                   coverage, confidence, rationale, evidence_json, status,
                   decision_by, decision_at, decision_note
            FROM commitment_optimizer_recommendations
            WHERE run_id = ?
            ORDER BY action, commitment_type, sku
            """,
            [run_id],
        ).fetchall()
    return {
        "run": {
            "runId": run[0],
            "requestedBy": run[1],
            "startedAt": run[2].isoformat() if run[2] else None,
            "completedAt": run[3].isoformat() if run[3] else None,
            "status": run[4],
            "riskProfile": run[5],
            "term": run[6],
            "lookbackDays": run[7],
            "algorithmVersion": run[8],
            "readiness": run[9],
            "currency": run[10],
            "recommendedPortfolio": run[11],
            "gates": json.loads(run[12] or "[]"),
            "summary": json.loads(run[13] or "{}"),
            "error": run[14],
        },
        "scenarios": [
            {
                "scenarioId": row[0],
                "name": row[1],
                "portfolio": row[2],
                "term": row[3],
                "scope": row[4],
                "demandFactor": row[5],
                "totalCost": row[6],
                "paygEquivCost": row[7],
                "savings": row[8],
                "contractedEsr": row[9],
                "listEsr": row[10],
                "coverage": row[11],
                "riUtilization": row[12],
                "spUtilization": row[13],
                "waste": row[14],
                "overage": row[15],
                "downsideSavings": row[16],
                "metrics": json.loads(row[17] or "{}"),
            }
            for row in scenarios
        ],
        "recommendations": [
            {
                "recommendationId": row[0],
                "scenarioId": row[1],
                "action": row[2],
                "commitmentType": row[3],
                "sku": row[4],
                "region": row[5],
                "flexibilityGroup": row[6],
                "quantity": row[7],
                "normalizedQuantity": row[8],
                "hourlyCommitment": row[9],
                "term": row[10],
                "scope": row[11],
                "expectedCost": row[12],
                "expectedSavings": row[13],
                "utilization": row[14],
                "waste": row[15],
                "coverage": row[16],
                "confidence": row[17],
                "rationale": row[18],
                "evidence": json.loads(row[19] or "{}"),
                "status": row[20],
                "decisionBy": row[21],
                "decisionAt": row[22].isoformat() if row[22] else None,
                "decisionNote": row[23],
            }
            for row in recommendations
        ],
    }


def optimizer_status(database: Any) -> dict[str, Any]:
    runs = list_optimizer_runs(database, limit=1)
    latest = runs[0] if runs else None
    return {
        "enabled": settings.commitment_optimizer_enabled,
        "algorithmVersion": engine.ALGORITHM_VERSION,
        "latestRun": latest,
        "readinessLevels": list(engine.READINESS_LEVELS),
        "riskProfiles": sorted(engine.RISK_PROFILES),
    }


def compare_optimizer_runs(
    database: Any, base_run_id: str, compare_run_id: str
) -> dict[str, Any]:
    """Side-by-side delta of two completed optimization runs.

    Runs are immutable, so the comparison is reproducible: identical run
    pairs always yield identical deltas.
    """

    def _projection(detail: dict[str, Any]) -> dict[str, Any]:
        run = detail["run"]
        summary = run.get("summary") or {}
        portfolios = summary.get("portfolios") or {}
        recommended = run.get("recommendedPortfolio") or ""
        metrics = portfolios.get(recommended) or {}
        purchases = [
            rec
            for rec in detail["recommendations"]
            if rec["action"] in ("buy_now", "review")
            and rec["commitmentType"] in ("reservation", "savings_plan")
        ]
        return {
            "runId": run["runId"],
            "startedAt": run.get("startedAt"),
            "readiness": run.get("readiness") or "",
            "riskProfile": run.get("riskProfile") or "",
            "term": run.get("term") or "",
            "lookbackDays": run.get("lookbackDays"),
            "algorithmVersion": run.get("algorithmVersion") or "",
            "recommendedPortfolio": recommended,
            "annualizedCost": float(metrics.get("annualizedCost") or 0.0),
            "annualizedSavings": float(metrics.get("annualizedSavings") or 0.0),
            "contractedEsr": float(metrics.get("contractedEsr") or 0.0),
            "coverage": float(metrics.get("coverage") or 0.0),
            "purchaseLines": len(purchases),
            "currency": run.get("currency") or "",
        }

    base = _projection(optimizer_run_detail(database, base_run_id))
    compare = _projection(optimizer_run_detail(database, compare_run_id))

    def _delta(key: str) -> float:
        return round(compare[key] - base[key], 6)

    recommendation_changed = (
        base["recommendedPortfolio"] != compare["recommendedPortfolio"]
    )
    savings_base = max(abs(base["annualizedSavings"]), 1.0)
    savings_drift_pct = round(
        abs(compare["annualizedSavings"] - base["annualizedSavings"])
        / savings_base
        * 100,
        2,
    )
    return {
        "base": base,
        "compare": compare,
        "deltas": {
            "annualizedCost": _delta("annualizedCost"),
            "annualizedSavings": _delta("annualizedSavings"),
            "contractedEsr": _delta("contractedEsr"),
            "coverage": _delta("coverage"),
            "purchaseLines": compare["purchaseLines"] - base["purchaseLines"],
        },
        "recommendationChanged": recommendation_changed,
        "savingsDriftPercent": savings_drift_pct,
        "materialityThresholdPercent": (
            settings.commitment_optimizer_change_materiality_pct
        ),
        "materialChange": (
            recommendation_changed
            or savings_drift_pct
            >= settings.commitment_optimizer_change_materiality_pct
        ),
    }


def generate_purchase_manifest(
    database: Any, run_id: str, actor: str
) -> dict[str, Any]:
    """Versioned purchase manifest for a completed run.

    Blocked or directional-only runs cannot produce a purchase manifest;
    the refusal is explicit rather than a qualified estimate.
    """
    from .database import utc_now

    detail = optimizer_run_detail(database, run_id)
    run = detail["run"]
    if run["status"] != "completed":
        raise ValueError("The optimization run has not completed.")
    if run["readiness"] in ("BLOCKED", "DIRECTIONAL_ONLY"):
        raise ValueError(
            f"Run readiness is {run['readiness']}; a purchase manifest "
            "requires PURCHASE_READY or REVIEW_REQUIRED evidence."
        )
    purchase_lines = [
        rec
        for rec in detail["recommendations"]
        if rec["action"] in ("buy_now", "review")
        and rec["commitmentType"] in ("reservation", "savings_plan")
    ]
    manifest_body = {
        "runId": run_id,
        "generatedAt": utc_now().isoformat(),
        "generatedBy": actor,
        "readiness": run["readiness"],
        "currency": run["currency"],
        "algorithmVersion": run["algorithmVersion"],
        "pricingBasis": "contracted price sheet; retail fallback directional",
        "purchases": purchase_lines,
        "note": (
            "This manifest is purchase-ready evidence, not an executed "
            "purchase. Flux never buys commitments automatically."
        ),
    }
    payload = json.dumps(manifest_body, default=str, sort_keys=True)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with database.operational_connect() as db:
        existing = db.execute(
            """
            SELECT COALESCE(max(version), 0) FROM commitment_purchase_manifests
            WHERE run_id = ?
            """,
            [run_id],
        ).fetchone()[0]
        version = int(existing) + 1
        manifest_id = f"manifest-{uuid4()}"
        db.execute(
            """
            INSERT INTO commitment_purchase_manifests (
                manifest_id, run_id, scenario_id, version, generated_at,
                generated_by, pricing_at, readiness, manifest_json,
                content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                manifest_id,
                run_id,
                run["recommendedPortfolio"],
                version,
                utc_now(),
                actor,
                utc_now(),
                run["readiness"],
                payload,
                content_hash,
            ],
        )
    csv_lines = [
        "action,commitmentType,sku,region,quantity,normalizedQuantity,"
        "hourlyCommitment,term,scope,expectedCost,expectedSavings"
    ]
    for rec in purchase_lines:
        csv_lines.append(
            ",".join(
                [
                    rec["action"],
                    rec["commitmentType"],
                    rec["sku"],
                    rec["region"],
                    str(rec["quantity"]),
                    str(rec["normalizedQuantity"]),
                    str(rec["hourlyCommitment"]),
                    rec["term"],
                    rec["scope"],
                    str(rec["expectedCost"]),
                    str(rec["expectedSavings"]),
                ]
            )
        )
    return {
        "manifestId": manifest_id,
        "version": version,
        "hash": content_hash,
        "json": manifest_body,
        "csv": "\n".join(csv_lines) + "\n",
    }


def record_recommendation_decision(
    database: Any,
    recommendation_id: str,
    *,
    decision: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    from .database import utc_now

    if decision not in ("approved", "rejected"):
        raise ValueError("Decision must be 'approved' or 'rejected'.")
    with database.operational_connect() as db:
        found = db.execute(
            """
            SELECT recommendation_id FROM commitment_optimizer_recommendations
            WHERE recommendation_id = ?
            """,
            [recommendation_id],
        ).fetchone()
        if not found:
            raise KeyError(recommendation_id)
        db.execute(
            """
            UPDATE commitment_optimizer_recommendations
            SET status = ?, decision_by = ?, decision_at = ?, decision_note = ?
            WHERE recommendation_id = ?
            """,
            [decision, actor, utc_now(), note, recommendation_id],
        )
    return {
        "recommendationId": recommendation_id,
        "decision": decision,
        "decisionBy": actor,
    }


def save_optimizer_override(
    database: Any,
    *,
    target_type: str,
    target_id: str,
    override_type: str,
    value: str = "",
    reason: str = "",
    actor: str = "",
    expires_at: Any = None,
) -> dict[str, Any]:
    from .database import utc_now

    if target_type not in ("resource", "resource_group", "subscription"):
        raise ValueError("Unsupported override target type.")
    if override_type not in ("exclude", "include", "force_demand_factor"):
        raise ValueError("Unsupported override type.")
    override_id = f"override-{uuid4()}"
    with database.operational_connect() as db:
        db.execute(
            """
            INSERT INTO commitment_optimizer_overrides (
                override_id, target_type, target_id, override_type, value,
                reason, created_by, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                override_id,
                target_type,
                target_id,
                override_type,
                value,
                reason,
                actor,
                utc_now(),
                expires_at,
            ],
        )
    return {"overrideId": override_id}
