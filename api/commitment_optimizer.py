"""Azure Commitment Purchase Optimizer — deterministic economic engine.

Pure, side-effect-free simulation of Azure Reservations and Savings Plans
hour by hour, with benefits applied in the economically required order
(Reservation first, then Savings Plan, then pay-as-you-go), and purchase
portfolios selected along an explicit cost/risk frontier.

Design rules (see docs/COMMITMENT-OPTIMIZER.md):
* A Savings Plan is a fixed hourly monetary commitment, use-it-or-lose-it.
  Unused hourly commitment is waste and never rolls to another hour.
* Reservations are simulated in normalized instance-flexibility units.
* Savings Plan benefits are applied in highest-discount meter order.
* A usage unit is allocated at most once (RI xor SP xor PAYG): covered
  usage is never charged again on top of the commitment that covers it.
* The hourly total cost identity is:
      total = RI amortized cost + SP hourly commitment + PAYG residual
  so SP waste is part of the cost, not a footnote.
* Hourly data is never fabricated by dividing monthly or daily cost.

v2 corrects the v1 draft: overage is no longer double-counted in the PAYG
residual, the full SP commitment is charged (waste included), demand
normalization uses real flexibility ratios, and coverage is measured in
consistent dollar terms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

ALGORITHM_VERSION = "flux-commitment-optimizer-v2"

HOURS_PER_YEAR = 8760.0
EPSILON = 1e-9

PAYG = "payg"
EXISTING = "existing_commitments"
RI_ONLY = "ri_only"
SP_ONLY = "sp_only"
BLENDED = "blended"
PORTFOLIO_NAMES = (PAYG, EXISTING, RI_ONLY, SP_ONLY, BLENDED)

TERMS = ("P1Y", "P3Y")
TERM_HOURS = {"P1Y": 8760.0, "P3Y": 26280.0}

RISK_PROFILES: dict[str, dict[str, float]] = {
    "conservative": {
        "min_utilization": 0.9,
        "max_waste_pct": 0.05,
        "downside_weight": 0.6,
        "lock_in_weight": 0.3,
        "waste_weight": 0.5,
    },
    "balanced": {
        "min_utilization": 0.8,
        "max_waste_pct": 0.15,
        "downside_weight": 0.35,
        "lock_in_weight": 0.2,
        "waste_weight": 0.25,
    },
    "aggressive": {
        "min_utilization": 0.7,
        "max_waste_pct": 0.3,
        "downside_weight": 0.15,
        "lock_in_weight": 0.1,
        "waste_weight": 0.1,
    },
}


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageLine:
    """One SKU's concurrent usage within a single UTC hour.

    ``quantity`` is the physical instance count (or fractional part of an
    hour consumed); ``ratio`` converts physical units into normalized
    instance-flexibility units for the Reservation layer.
    """

    hour: datetime
    sku: str
    region: str
    flexibility_group: str
    quantity: float
    contracted_payg_rate: float
    list_payg_rate: float
    sp_rate: float
    ratio: float = 1.0
    ri_eligible: bool = True
    sp_eligible: bool = True
    subscription_id: str = ""
    resource_id: str = ""


@dataclass(frozen=True)
class Reservation:
    """One Reservation (existing or proposed) in normalized units."""

    sku: str
    region: str
    flexibility_group: str
    ratio: float
    hourly_rate: float
    quantity: int
    term: str = "P1Y"
    scope: str = "Shared"
    existing: bool = True


@dataclass(frozen=True)
class SavingsPlan:
    """A Savings Plan hourly monetary commitment."""

    hourly_commitment: float
    term: str = "P1Y"
    scope: str = "Shared"
    existing: bool = True


@dataclass(frozen=True)
class CatalogSku:
    """A currently purchasable Reservation catalog entry."""

    sku: str
    region: str
    flexibility_group: str
    ratio: float
    hourly_rate_p1y: float
    hourly_rate_p3y: float
    instance_flexibility: str = "Enabled"


@dataclass
class HourResult:
    hour: datetime
    demand_normalized: float = 0.0
    ri_capacity: float = 0.0
    ri_used: float = 0.0
    ri_unused: float = 0.0
    ri_covered_payg_equiv: float = 0.0
    ri_cost: float = 0.0
    sp_commitment: float = 0.0
    sp_used: float = 0.0
    sp_waste: float = 0.0
    sp_covered_payg_equiv: float = 0.0
    payg_cost: float = 0.0
    overage_cost: float = 0.0
    payg_equiv_cost: float = 0.0
    total_cost: float = 0.0


@dataclass
class PortfolioMetrics:
    portfolio: str = ""
    term: str = "P1Y"
    hours: int = 0
    payg_equiv_cost: float = 0.0
    list_equiv_cost: float = 0.0
    total_cost: float = 0.0
    ri_cost: float = 0.0
    sp_benefit_cost: float = 0.0
    payg_residual_cost: float = 0.0
    ri_capacity: float = 0.0
    ri_used: float = 0.0
    ri_unused: float = 0.0
    ri_utilization: float = 0.0
    ri_break_even_utilization: float = 0.0
    sp_used: float = 0.0
    sp_waste: float = 0.0
    sp_utilization: float = 0.0
    sp_util_p5: float = 0.0
    sp_util_p50: float = 0.0
    sp_util_p95: float = 0.0
    sp_break_even_hourly_spend: float = 0.0
    hours_with_waste: int = 0
    hours_with_overage: int = 0
    longest_waste_streak: int = 0
    coverage: float = 0.0
    savings_vs_payg: float = 0.0
    savings_vs_list: float = 0.0
    contracted_esr: float = 0.0
    list_esr: float = 0.0
    waste_pct: float = 0.0
    downside_savings: float = 0.0
    annualized_cost: float = 0.0
    annualized_savings: float = 0.0
    per_hour: list[HourResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Savings Plan layer
# ---------------------------------------------------------------------------


def sp_discount(line: UsageLine) -> float:
    if line.contracted_payg_rate <= 0:
        return 0.0
    discount = (
        (line.contracted_payg_rate - line.sp_rate) / line.contracted_payg_rate
    )
    # Azure-compatible lower-rate behavior: when the contracted PAYG rate is
    # at or below the Savings Plan rate, the plan manufactures no savings on
    # that meter; such lines still consume nothing from the commitment.
    return max(0.0, discount)


def simulate_savings_plan_hour(
    lines: Sequence[UsageLine], commitment: float
) -> tuple[float, float, float, float]:
    """Apply one hour of SP commitment to residual eligible lines.

    Returns ``(sp_used, waste, covered_payg_equiv, overage_payg_equiv)``.
    Lines are consumed in descending discount order; the marginal line may
    be partially covered, pro-rated by quantity. ``sp_used`` is the SP-rate
    cost of covered usage; the customer pays the full ``commitment`` either
    way, so ``waste = commitment - sp_used`` and is never carried over.
    """
    sp_used = 0.0
    covered_payg_equiv = 0.0
    overage_payg_equiv = 0.0
    remaining = max(0.0, commitment)
    ordered = sorted(
        (l for l in lines if l.sp_eligible and sp_discount(l) > 0),
        key=lambda l: (-sp_discount(l), l.sku, l.resource_id),
    )
    for line in ordered:
        line_sp_cost = line.quantity * line.sp_rate
        line_payg_equiv = line.quantity * line.contracted_payg_rate
        if remaining <= EPSILON or line_sp_cost <= 0:
            overage_payg_equiv += line_payg_equiv
            continue
        if line_sp_cost <= remaining + EPSILON:
            remaining -= line_sp_cost
            sp_used += line_sp_cost
            covered_payg_equiv += line_payg_equiv
        else:
            frac = remaining / line_sp_cost
            sp_used += remaining
            covered_payg_equiv += line_payg_equiv * frac
            overage_payg_equiv += line_payg_equiv * (1.0 - frac)
            remaining = 0.0
    waste = max(0.0, commitment - sp_used)
    return sp_used, waste, covered_payg_equiv, overage_payg_equiv


# ---------------------------------------------------------------------------
# Reservation layer + full portfolio simulation (RI -> SP -> PAYG)
# ---------------------------------------------------------------------------


def reservation_capacity(
    reservations: Sequence[Reservation],
) -> dict[str, float]:
    capacity: dict[str, float] = {}
    for r in reservations:
        if r.quantity <= 0 or r.ratio <= 0:
            continue
        capacity[r.flexibility_group] = (
            capacity.get(r.flexibility_group, 0.0) + r.quantity * r.ratio
        )
    return capacity


def reservation_hourly_cost(reservations: Sequence[Reservation]) -> float:
    return sum(
        max(0, r.quantity) * max(0.0, r.hourly_rate) for r in reservations
    )


def _scale_line(line: UsageLine, factor: float) -> UsageLine:
    return UsageLine(
        hour=line.hour,
        sku=line.sku,
        region=line.region,
        flexibility_group=line.flexibility_group,
        quantity=line.quantity * factor,
        contracted_payg_rate=line.contracted_payg_rate,
        list_payg_rate=line.list_payg_rate,
        sp_rate=line.sp_rate,
        ratio=line.ratio,
        ri_eligible=line.ri_eligible,
        sp_eligible=line.sp_eligible,
        subscription_id=line.subscription_id,
        resource_id=line.resource_id,
    )


def simulate_portfolio(
    lines: Sequence[UsageLine],
    reservations: Sequence[Reservation],
    savings_plans: Sequence[SavingsPlan],
    *,
    portfolio: str = "",
    term: str = "P1Y",
) -> PortfolioMetrics:
    """Simulate the RI -> SP -> PAYG benefit waterfall hour by hour.

    Every usage unit is allocated exactly once: covered by a Reservation
    (normalized units), covered by the Savings Plan commitment (dollars),
    or billed at the contracted PAYG rate. The SP commitment is charged in
    full each hour, so waste raises total cost instead of vanishing.
    """
    capacity = reservation_capacity(reservations)
    ri_cost_per_hour = reservation_hourly_cost(reservations)
    sp_commit_per_hour = sum(
        max(0.0, s.hourly_commitment) for s in savings_plans
    )

    by_hour: dict[datetime, list[UsageLine]] = {}
    for line in lines:
        by_hour.setdefault(line.hour, []).append(line)

    # Both commitment instruments bill on wall-clock time, not on usage: a
    # Savings Plan is a fixed hourly monetary commitment and a Reservation is
    # a fixed hourly capacity charge, each billed 24/7 for the whole term.
    # Iterating only the hours that produced usage rows therefore skipped the
    # charge for every idle hour -- a nightly dev shutdown, a scale-to-zero
    # window, or a FOCUS ingest gap -- so the commitment looked fully utilized
    # with zero waste and the run reported savings on a portfolio that loses
    # money. Densify across the observed window so idle hours are charged and
    # counted. The range is bounded by the observed data; no hours are
    # invented outside it.
    if by_hour:
        cursor = min(by_hour)
        last_hour = max(by_hour)
        while cursor < last_hour:
            cursor += timedelta(hours=1)
            by_hour.setdefault(cursor, [])

    metrics = PortfolioMetrics(portfolio=portfolio, term=term)
    util_samples: list[float] = []
    covered_weighted_discount = 0.0

    for hour in sorted(by_hour):
        hour_lines = by_hour[hour]
        hr = HourResult(hour=hour)
        hr.ri_capacity = sum(capacity.values())
        hr.ri_cost = ri_cost_per_hour
        hr.sp_commitment = sp_commit_per_hour

        residual_lines: list[UsageLine] = []
        group_remaining = dict(capacity)
        for line in hour_lines:
            if not line.ri_eligible or line.ratio <= 0:
                residual_lines.append(line)
                continue
            group = line.flexibility_group
            need_norm = line.quantity * line.ratio
            available = group_remaining.get(group, 0.0)
            covered_norm = min(need_norm, available)
            group_remaining[group] = available - covered_norm
            hr.ri_used += covered_norm
            if covered_norm >= need_norm - EPSILON:
                hr.ri_covered_payg_equiv += (
                    line.quantity * line.contracted_payg_rate
                )
            else:
                uncovered_fraction = (
                    (need_norm - covered_norm) / need_norm
                    if need_norm > 0
                    else 1.0
                )
                residual_lines.append(_scale_line(line, uncovered_fraction))
                hr.ri_covered_payg_equiv += (
                    line.quantity
                    * (1.0 - uncovered_fraction)
                    * line.contracted_payg_rate
                )
        hr.ri_unused = sum(
            max(0.0, remaining) for remaining in group_remaining.values()
        )
        hr.demand_normalized = sum(
            l.quantity * l.ratio for l in hour_lines if l.ri_eligible
        )

        sp_used, waste, covered_equiv, overage_equiv = (
            simulate_savings_plan_hour(residual_lines, sp_commit_per_hour)
        )
        hr.sp_used = sp_used
        hr.sp_waste = waste
        hr.sp_covered_payg_equiv = covered_equiv
        hr.overage_cost = overage_equiv

        payg_equiv_total = sum(
            l.quantity * l.contracted_payg_rate for l in residual_lines
        )
        hr.payg_equiv_cost = payg_equiv_total
        hr.payg_cost = payg_equiv_total - covered_equiv

        hr.total_cost = hr.ri_cost + hr.sp_commitment + hr.payg_cost
        metrics.per_hour.append(hr)

        metrics.ri_capacity += hr.ri_capacity
        metrics.ri_used += hr.ri_used
        metrics.ri_unused += hr.ri_unused
        metrics.sp_used += hr.sp_used
        metrics.sp_waste += hr.sp_waste
        metrics.payg_equiv_cost += payg_equiv_total
        metrics.list_equiv_cost += sum(
            l.quantity * l.list_payg_rate for l in hour_lines
        )
        metrics.total_cost += hr.total_cost
        metrics.ri_cost += hr.ri_cost
        metrics.sp_benefit_cost += hr.sp_commitment
        metrics.payg_residual_cost += hr.payg_cost
        metrics.hours_with_waste += 1 if waste > EPSILON else 0
        metrics.hours_with_overage += 1 if overage_equiv > EPSILON else 0
        if sp_commit_per_hour > 0:
            util_samples.append(sp_used / sp_commit_per_hour)
        if sp_used > EPSILON:
            discount = (covered_equiv - sp_used) / covered_equiv
            covered_weighted_discount += discount * sp_used

    metrics.hours = len(metrics.per_hour)
    _finalize(
        metrics,
        util_samples,
        sp_commit_per_hour,
        capacity,
        by_hour,
        covered_weighted_discount,
    )
    return metrics


def _percentile(samples: Sequence[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _finalize(
    m: PortfolioMetrics,
    util_samples: list[float],
    sp_commit: float,
    capacity: Mapping[str, float],
    by_hour: Mapping[datetime, list[UsageLine]],
    covered_weighted_discount: float,
) -> None:
    if m.hours:
        m.ri_utilization = (
            m.ri_used / (m.ri_capacity) if m.ri_capacity > 0 else 0.0
        )
    # Baseline is the contracted PAYG-equivalent of ALL usage: the residual
    # equivalent plus the equivalent covered by Reservations. SP-covered
    # usage is already inside the residual equivalent, so adding it again
    # would double-count.
    ri_covered_equiv = sum(h.ri_covered_payg_equiv for h in m.per_hour)
    covered_equiv = ri_covered_equiv + sum(
        h.sp_covered_payg_equiv for h in m.per_hour
    )
    baseline = m.payg_equiv_cost + ri_covered_equiv
    if baseline > 0:
        m.coverage = covered_equiv / baseline
    m.sp_utilization = (
        m.sp_used / (sp_commit * m.hours) if sp_commit * m.hours > 0 else 0.0
    )
    m.sp_util_p5 = _percentile(util_samples, 0.05)
    m.sp_util_p50 = _percentile(util_samples, 0.50)
    m.sp_util_p95 = _percentile(util_samples, 0.95)
    m.savings_vs_payg = baseline - m.total_cost
    m.contracted_esr = effective_savings_rate(baseline, m.total_cost)
    m.savings_vs_list = m.list_equiv_cost - m.total_cost
    m.list_esr = effective_savings_rate(m.list_equiv_cost, m.total_cost)
    m.waste_pct = (
        m.sp_waste / (sp_commit * m.hours) if sp_commit * m.hours > 0 else 0.0
    )
    if m.sp_used > EPSILON:
        avg_discount = covered_weighted_discount / m.sp_used
        if 0 < avg_discount < 1:
            m.sp_break_even_hourly_spend = sp_commit / (1.0 - avg_discount)
    # avg_unit_rate must be dollars per NORMALIZED unit, because ri_capacity
    # is normalized capacity. The numerator is therefore the extended PAYG
    # cost (rate x quantity) and the ratio belongs only in the denominator --
    # including it in both computed a ratio-weighted mean of the per-instance
    # rate and understated break-even by exactly `ratio`. parse_sku sets ratio
    # from the vCPU count, so ratio is essentially never 1 in production: a
    # reserved D8 (ratio 8) reported break-even at 7% when the truth was 60%.
    payg_cost_total = 0.0
    for hour_lines in by_hour.values():
        for line in hour_lines:
            if line.ri_eligible and line.ratio > 0:
                payg_cost_total += line.contracted_payg_rate * line.quantity
    demand_norm_total = sum(
        line.quantity * line.ratio
        for hour_lines in by_hour.values()
        for line in hour_lines
        if line.ri_eligible and line.ratio > 0
    )
    if demand_norm_total > 0 and m.ri_capacity > 0:
        avg_unit_rate = payg_cost_total / demand_norm_total
        breakeven_cost = m.ri_capacity / max(1, m.hours) * avg_unit_rate
        # Not clamped to 1.0: a value above 100% is the signal that these
        # reservations can never pay for themselves, and clamping presented
        # that as a benign "breaks even at full utilization".
        m.ri_break_even_utilization = (
            (m.ri_cost / max(1, m.hours)) / breakeven_cost
            if breakeven_cost > 0
            else 0.0
        )
    if m.hours:
        m.annualized_cost = m.total_cost / m.hours * HOURS_PER_YEAR
        m.annualized_savings = m.savings_vs_payg / m.hours * HOURS_PER_YEAR
    streak = best = 0
    for h in m.per_hour:
        if h.sp_waste > EPSILON:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    m.longest_waste_streak = best


# ---------------------------------------------------------------------------
# Exact purchase construction
# ---------------------------------------------------------------------------


def integer_decompose(
    target_normalized: float,
    catalog: Sequence[CatalogSku],
    term: str = "P1Y",
) -> tuple[list[dict[str, Any]], float]:
    """Deterministically decompose a normalized target into integer SKUs.

    Greedy by lowest amortized cost per normalized unit, then fills the
    residual with the smallest-overcoverage SKU. Returns the purchase lines
    and the explicit overcoverage in normalized units — rounding up is never
    silent.
    """
    if target_normalized <= EPSILON or not catalog:
        return [], 0.0
    priced = []
    for c in catalog:
        rate = c.hourly_rate_p1y if term == "P1Y" else c.hourly_rate_p3y
        if c.ratio <= 0 or rate < 0:
            continue
        priced.append((rate / c.ratio, c.sku, c))
    priced.sort(key=lambda t: (t[0], t[1]))
    remaining = target_normalized
    purchases: list[dict[str, Any]] = []
    for cost_per_unit, _sku, c in priced:
        if remaining <= EPSILON:
            break
        qty = int(remaining // c.ratio)
        if qty > 0:
            purchases.append(
                {
                    "sku": c.sku,
                    "region": c.region,
                    "flexibilityGroup": c.flexibility_group,
                    "quantity": qty,
                    "normalizedQuantity": round(qty * c.ratio, 6),
                    "ratio": c.ratio,
                    "instanceFlexibility": c.instance_flexibility,
                    # The rate this line was actually chosen on, so callers
                    # can cost the line instead of reusing the whole
                    # reservation's rate for every decomposed SKU.
                    "hourlyRate": cost_per_unit * c.ratio,
                }
            )
            remaining -= qty * c.ratio
    if remaining > EPSILON and priced:
        # Fill from the PRICED set, not the raw catalog: an entry filtered out
        # for having no usable rate on this term must not become the residual
        # filler (it would be costed at zero), and picking outside `priced`
        # could also leave `remaining` above the chosen ratio while
        # overcoverage still reported 0.0 -- an undercoverage shortfall
        # presented as an exact decomposition.
        smallest = min((c for _r, _s, c in priced), key=lambda c: c.ratio)
        smallest_rate = (
            smallest.hourly_rate_p1y if term == "P1Y" else smallest.hourly_rate_p3y
        )
        purchases.append(
            {
                "sku": smallest.sku,
                "region": smallest.region,
                "flexibilityGroup": smallest.flexibility_group,
                "quantity": 1,
                "normalizedQuantity": round(smallest.ratio, 6),
                "ratio": smallest.ratio,
                "instanceFlexibility": smallest.instance_flexibility,
                "hourlyRate": max(0.0, smallest_rate or 0.0),
            }
        )
        remaining -= smallest.ratio
    overcoverage = max(0.0, -remaining)
    return purchases, overcoverage


# ---------------------------------------------------------------------------
# Candidate generation + frontier optimization
# ---------------------------------------------------------------------------


def hourly_normalized_demand(
    lines: Iterable[UsageLine],
) -> dict[tuple[datetime, str], float]:
    demand: dict[tuple[datetime, str], float] = {}
    for line in lines:
        if not line.ri_eligible or line.ratio <= 0:
            continue
        key = (line.hour, line.flexibility_group)
        demand[key] = demand.get(key, 0.0) + line.quantity * line.ratio
    return demand


def ri_candidate_quantities(
    demand_by_hour: Sequence[float],
    existing: float = 0.0,
) -> list[int]:
    """Incremental RI quantities from demand percentiles, never negative."""
    if not demand_by_hour:
        return [0]
    candidates = {0}
    for pct in (0.5, 0.7, 0.8, 0.9, 0.95, 1.0):
        level = _percentile(list(demand_by_hour), pct)
        candidates.add(max(0, int(round(level - existing))))
    return sorted(candidates)


def sp_candidate_commitments(
    hourly_eligible_spend: Sequence[float],
    existing: float = 0.0,
    azure_recommended: float | None = None,
    increments: int = 12,
) -> list[float]:
    """Candidate hourly SP commitments at spend breakpoints.

    Breakpoints are drawn from the unique positive hourly eligible-spend
    levels; Azure's own recommended commitment and the existing commitment
    are always evaluated too.
    """
    candidates = {0.0, round(max(0.0, existing), 6)}
    if azure_recommended is not None and azure_recommended > 0:
        candidates.add(round(float(azure_recommended), 6))
    values = sorted({round(v, 6) for v in hourly_eligible_spend if v > EPSILON})
    if values:
        for i in range(1, increments + 1):
            idx = min(len(values) - 1, (i * len(values)) // (increments + 1))
            candidates.add(values[idx])
        candidates.add(values[-1])
    return sorted(candidates)


def objective(
    m: PortfolioMetrics,
    profile: str = "balanced",
) -> float:
    p = RISK_PROFILES[profile]
    waste_penalty = m.sp_waste * p["waste_weight"]
    downside_penalty = max(0.0, m.savings_vs_payg - m.downside_savings) * p[
        "downside_weight"
    ]
    lock_in_penalty = m.ri_unused * p["lock_in_weight"]
    return (
        m.annualized_cost + waste_penalty + downside_penalty + lock_in_penalty
    )


def feasible(
    m: PortfolioMetrics,
    profile: str = "balanced",
) -> bool:
    p = RISK_PROFILES[profile]
    if m.sp_benefit_cost <= 0 and m.ri_cost <= 0:
        return True
    # Gate each leg independently. max() let a strong Savings Plan carry a
    # badly over-bought reservation book through every risk profile, and
    # waste_pct is sp_waste / (sp_commit * hours) -- it re-measures the SP leg
    # and never sees RI waste at all, so it could not catch it either. A
    # portfolio with 100% SP utilization and 10% RI utilization, losing money,
    # passed even the conservative profile.
    floor = p["min_utilization"]
    sp_ok = m.sp_benefit_cost <= 0 or m.sp_utilization >= floor
    ri_ok = m.ri_cost <= 0 or m.ri_utilization >= floor
    # No separate RI waste term: ri_unused / ri_capacity is algebraically
    # 1 - ri_utilization, so adding it would silently raise the RI floor to
    # 1 - max_waste_pct (0.95 conservative) above the profile's stated
    # min_utilization and reject profitable portfolios.
    return sp_ok and ri_ok and m.waste_pct <= p["max_waste_pct"]


def select_portfolio(
    metrics_by_portfolio: Mapping[str, PortfolioMetrics],
    profile: str = "balanced",
) -> str:
    """Pick the feasible portfolio with the lowest objective value."""
    scored = [
        (objective(m, profile), name)
        for name, m in metrics_by_portfolio.items()
        if feasible(m, profile)
    ]
    if not scored:
        scored = [
            (objective(m, profile), name)
            for name, m in metrics_by_portfolio.items()
        ]
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][1]


def pareto_frontier(
    metrics_by_portfolio: Mapping[str, PortfolioMetrics],
) -> list[str]:
    """Non-dominated portfolios on (annualized cost, downside savings)."""
    names = sorted(metrics_by_portfolio)
    frontier: list[str] = []
    for name in names:
        m = metrics_by_portfolio[name]
        dominated = False
        for other_name in names:
            if other_name == name:
                continue
            o = metrics_by_portfolio[other_name]
            if (
                o.annualized_cost <= m.annualized_cost
                and o.downside_savings >= m.downside_savings
                and (
                    o.annualized_cost < m.annualized_cost
                    or o.downside_savings > m.downside_savings
                )
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(name)
    return frontier


# ---------------------------------------------------------------------------
# Effective savings rates
# ---------------------------------------------------------------------------


def effective_savings_rate(
    baseline_payg_equiv: float, actual_or_proposed: float
) -> float:
    if baseline_payg_equiv <= 0:
        return 0.0
    return (baseline_payg_equiv - actual_or_proposed) / baseline_payg_equiv


# ---------------------------------------------------------------------------
# Sensitivity + backtesting
# ---------------------------------------------------------------------------


def scale_demand(lines: Sequence[UsageLine], factor: float) -> list[UsageLine]:
    return [_scale_line(l, max(0.0, factor)) for l in lines]


def sensitivity_report(
    lines: Sequence[UsageLine],
    reservations: Sequence[Reservation],
    savings_plans: Sequence[SavingsPlan],
    factors: Sequence[float] = (1.0, 0.9, 0.8, 0.7),
) -> dict[str, Any]:
    """Downside economics under reduced-demand stress scenarios."""
    results = []
    baseline_savings = None
    for factor in factors:
        m = simulate_portfolio(
            scale_demand(lines, factor), reservations, savings_plans
        )
        if factor == 1.0:
            baseline_savings = m.savings_vs_payg
        results.append(
            {
                "demandFactor": factor,
                "savings": round(m.savings_vs_payg, 2),
                "riUtilization": round(m.ri_utilization, 4),
                "spUtilization": round(m.sp_utilization, 4),
                "waste": round(m.sp_waste, 2),
                "negative": m.savings_vs_payg < 0,
            }
        )
    downside = min((r["savings"] for r in results), default=0.0)
    return {
        "scenarios": results,
        "downsideSavings": downside,
        "baselineSavings": baseline_savings if baseline_savings is not None else 0.0,
        "negativeWindows": sum(1 for r in results if r["negative"]),
    }


def apply_downside(
    metrics: PortfolioMetrics,
    lines: Sequence[UsageLine],
    reservations: Sequence[Reservation],
    savings_plans: Sequence[SavingsPlan],
) -> None:
    report = sensitivity_report(lines, reservations, savings_plans)
    metrics.downside_savings = report["downsideSavings"]


def backtest(
    train: Sequence[UsageLine],
    holdout: Sequence[UsageLine],
    reservations: Sequence[Reservation],
    savings_plans: Sequence[SavingsPlan],
) -> dict[str, float]:
    in_sample = simulate_portfolio(train, reservations, savings_plans)
    out_sample = simulate_portfolio(holdout, reservations, savings_plans)
    return {
        "inSampleSavings": round(in_sample.savings_vs_payg, 2),
        "holdoutSavings": round(out_sample.savings_vs_payg, 2),
        "holdoutRiUtilization": round(out_sample.ri_utilization, 4),
        "holdoutSpUtilization": round(out_sample.sp_utilization, 4),
        "holdoutWaste": round(out_sample.sp_waste, 2),
        "inSampleSpUtilization": round(in_sample.sp_utilization, 4),
    }


# ---------------------------------------------------------------------------
# Data-quality gates -> purchase readiness
# ---------------------------------------------------------------------------

READINESS_LEVELS = (
    "PURCHASE_READY",
    "REVIEW_REQUIRED",
    "DIRECTIONAL_ONLY",
    "BLOCKED",
)


@dataclass
class GateResult:
    name: str
    passed: bool
    severity: str  # "block" | "review" | "directional"
    detail: str = ""


def classify_readiness(gates: Sequence[GateResult]) -> str:
    if any(g.severity == "block" and not g.passed for g in gates):
        return "BLOCKED"
    if any(g.severity == "directional" and not g.passed for g in gates):
        return "DIRECTIONAL_ONLY"
    if any(not g.passed for g in gates):
        return "REVIEW_REQUIRED"
    return "PURCHASE_READY"


def run_data_quality_gates(
    *,
    price_sheet_available: bool,
    currency_match: bool,
    price_join_unambiguous: bool,
    hourly_grain_explicit: bool,
    expected_hours_match: bool,
    no_duplicate_hours: bool,
    commitments_current: bool,
    flexibility_mappings_present: bool,
    rightsizing_confident: bool,
    usage_recent: bool,
    no_focus_double_count: bool,
    software_excluded: bool,
    baselines_not_mixed: bool,
    catalog_purchasable: bool,
    price_dates_cover_purchase: bool = True,
    positive_after_thresholds: bool = True,
) -> list[GateResult]:
    g = lambda name, passed, sev, detail="": GateResult(
        name, passed, sev, detail
    )
    return [
        g(
            "price_sheet_available",
            price_sheet_available,
            "block",
            "Customer price sheet is required for purchase-ready pricing.",
        ),
        g(
            "currency_match",
            currency_match,
            "block",
            "Price sheet currency must match the billing currency.",
        ),
        g(
            "price_join_unambiguous",
            price_join_unambiguous,
            "block",
            "Consumption, RI, and SP prices must join unambiguously.",
        ),
        g(
            "hourly_grain_explicit",
            hourly_grain_explicit,
            "directional",
            "Hourly series must come from an explicit hourly source.",
        ),
        g(
            "expected_hours_match",
            expected_hours_match,
            "directional",
            "Observed UTC hour count must match the interval.",
        ),
        g(
            "no_duplicate_hours",
            no_duplicate_hours,
            "directional",
            "Duplicate or out-of-order hours were detected.",
        ),
        g(
            "commitments_current",
            commitments_current,
            "review",
            "Existing commitment inventory must be current.",
        ),
        g(
            "flexibility_mappings_present",
            flexibility_mappings_present,
            "review",
            "Instance-flexibility mapping missing for affected SKUs.",
        ),
        g(
            "rightsizing_confident",
            rightsizing_confident,
            "review",
            "Rightsizing evidence below the governed confidence rule.",
        ),
        g(
            "usage_recent",
            usage_recent,
            "review",
            "Usage data is stale for the selected strategy.",
        ),
        g(
            "no_focus_double_count",
            no_focus_double_count,
            "block",
            "Usage must not be counted in both FOCUS and Azure feeds.",
        ),
        g(
            "software_excluded",
            software_excluded,
            "block",
            "Software charges must stay outside the RI hardware benefit.",
        ),
        g(
            "baselines_not_mixed",
            baselines_not_mixed,
            "block",
            "List and contracted baselines must not be mixed.",
        ),
        g(
            "catalog_purchasable",
            catalog_purchasable,
            "block",
            "Purchase lines must map to purchasable catalog items.",
        ),
        g(
            "price_dates_cover_purchase",
            price_dates_cover_purchase,
            "review",
            "Price effective dates should cover the purchase date.",
        ),
        g(
            "positive_after_thresholds",
            positive_after_thresholds,
            "review",
            "Incremental savings must clear minimum thresholds.",
        ),
    ]


# ---------------------------------------------------------------------------
# Reconciliation with Azure's own recommendations
# ---------------------------------------------------------------------------


def reconcile_with_azure(
    *,
    flux_commitment: float | None = None,
    azure_commitment: float | None = None,
    flux_quantity: float | None = None,
    azure_quantity: float | None = None,
    flux_savings: float | None = None,
    azure_savings: float | None = None,
    tolerance_pct: float = 10.0,
) -> dict[str, Any]:
    """Compare Flux outputs against Azure recommendation evidence.

    Each paired metric is checked against the documented tolerance; missing
    Azure evidence is reported as unevaluated rather than passed.
    """

    def check(
        flux: float | None, azure: float | None
    ) -> tuple[str, float | None]:
        if flux is None or azure is None or azure == 0:
            return ("unevaluated", None)
        variance = abs(flux - azure) / abs(azure) * 100.0
        return ("within_tolerance" if variance <= tolerance_pct else "variance", variance)

    checks = {}
    for name, flux, azure in (
        ("commitment", flux_commitment, azure_commitment),
        ("quantity", flux_quantity, azure_quantity),
        ("savings", flux_savings, azure_savings),
    ):
        status, variance = check(flux, azure)
        checks[name] = {
            "flux": flux,
            "azure": azure,
            "status": status,
            "variancePercent": round(variance, 2) if variance is not None else None,
        }
    evaluated = [c for c in checks.values() if c["status"] != "unevaluated"]
    return {
        "tolerancePercent": tolerance_pct,
        "checks": checks,
        "overall": (
            "reconciled"
            if evaluated and all(c["status"] == "within_tolerance" for c in evaluated)
            else "variance" if evaluated else "unevaluated"
        ),
    }
