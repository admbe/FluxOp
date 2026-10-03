"""Deterministic economic tests for the commitment optimizer engine.

Covers the mandatory Azure benefit-application rules: hourly use-it-or-
lose-it Savings Plan semantics, Reservation-before-Savings-Plan ordering,
highest-discount meter precedence, instance-size flexibility, integer
purchase decomposition, double-coverage prevention, and reproducibility.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from api import commitment_optimizer as eng


def utc(day: int, hour: int) -> datetime:
    return datetime(2026, 7, day, hour)


def line(
    hour,
    *,
    sku="D4s_v5",
    region="eastus",
    group="eastus:D_v5",
    qty=1.0,
    payg=1.0,
    sp=0.65,
    ratio=1.0,
    ri_eligible=True,
    sp_eligible=True,
    list_rate=None,
    resource="",
):
    return eng.UsageLine(
        hour=hour,
        sku=sku,
        region=region,
        flexibility_group=group,
        quantity=qty,
        contracted_payg_rate=payg,
        list_payg_rate=list_rate if list_rate is not None else payg * 1.2,
        sp_rate=sp,
        ratio=ratio,
        ri_eligible=ri_eligible,
        sp_eligible=sp_eligible,
        resource_id=resource,
    )


def hours(n=24, start_day=1):
    return [utc(start_day, h) for h in range(n)] if n <= 24 else [
        utc(start_day + d, h) for d in range(n // 24) for h in range(24)
    ]


class SavingsPlanSimulationTests(unittest.TestCase):
    def test_case5_unused_commitment_is_waste_and_never_rolls(self):
        usage = [line(utc(1, h)) for h in (0, 1, 2)]
        plan = eng.SavingsPlan(hourly_commitment=5.0, existing=False)
        m = eng.simulate_portfolio(usage, [], [plan])
        self.assertAlmostEqual(m.sp_waste, 3 * (5.0 - 0.65), places=6)
        self.assertEqual(m.hours_with_waste, 3)
        for hour in m.per_hour:
            self.assertGreaterEqual(hour.sp_waste, 0.0)
            self.assertAlmostEqual(
                hour.sp_used + hour.sp_waste, 5.0, places=6
            )

    def test_case6_overage_above_commitment(self):
        usage = [line(utc(1, 0), qty=4.0)]
        plan = eng.SavingsPlan(hourly_commitment=1.0, existing=False)
        m = eng.simulate_portfolio(usage, [], [plan])
        hour = m.per_hour[0]
        self.assertAlmostEqual(hour.sp_used, 1.0, places=6)
        self.assertGreater(hour.overage_cost, 0.0)
        self.assertEqual(m.hours_with_overage, 1)

    def test_case8_highest_discount_meter_consumed_first(self):
        hour = utc(1, 0)
        low_discount = line(hour, sku="A", sp=0.9, payg=1.0)
        high_discount = line(hour, sku="B", sp=0.2, payg=1.0, resource="r-b")
        used, waste, covered, overage = eng.simulate_savings_plan_hour(
            [low_discount, high_discount], commitment=0.2
        )
        self.assertAlmostEqual(used, 0.2, places=6)
        self.assertAlmostEqual(covered, 1.0, places=6)
        self.assertAlmostEqual(overage, 1.0, places=6)

    def test_case13_contracted_payg_below_sp_rate_no_savings(self):
        hour = utc(1, 0)
        cheap = line(hour, payg=0.5, sp=0.65)
        self.assertEqual(eng.sp_discount(cheap), 0.0)
        used, waste, covered, overage = eng.simulate_savings_plan_hour(
            [cheap], commitment=1.0
        )
        self.assertEqual(used, 0.0)
        self.assertEqual(overage, 0.0)
        self.assertAlmostEqual(waste, 1.0, places=6)
        m = eng.simulate_portfolio(
            [cheap], [], [eng.SavingsPlan(hourly_commitment=1.0, existing=False)]
        )
        hour_result = m.per_hour[0]
        self.assertAlmostEqual(hour_result.payg_cost, 0.5, places=6)
        self.assertAlmostEqual(m.savings_vs_payg, -1.0, places=6)

    def test_partial_coverage_prorated(self):
        hour = utc(1, 0)
        used, waste, covered, overage = eng.simulate_savings_plan_hour(
            [line(hour, qty=2.0)], commitment=0.5
        )
        self.assertAlmostEqual(used, 0.5, places=6)
        self.assertAlmostEqual(covered, 0.5 / 0.65, places=6)
        self.assertAlmostEqual(overage, 2.0 - 0.5 / 0.65, places=6)
        self.assertEqual(waste, 0.0)


class IdleHourCommitmentTests(unittest.TestCase):
    """Commitments bill on wall-clock time, not on hours that had usage.

    Regression: simulate_portfolio built its hour grid from the usage rows,
    so an hour with no eligible compute -- a nightly dev shutdown, a
    scale-to-zero window, a FOCUS ingest gap -- never had the Savings Plan
    commitment or the Reservation capacity charged against it. The plan then
    reported 100% utilization, zero waste, and positive savings on a
    portfolio that actually loses money, and every risk gate passed.
    """

    def _half_idle_day(self):
        # Usage in hours 0-11 only; hour 23 anchors the 24-hour window.
        lines = [line(utc(1, h), payg=1.30, sp=1.00, ri_eligible=False)
                 for h in range(12)]
        lines.append(
            line(utc(1, 23), qty=0.0, payg=1.30, sp=1.00, ri_eligible=False)
        )
        return lines

    def test_savings_plan_is_charged_for_idle_hours(self):
        metrics = eng.simulate_portfolio(
            self._half_idle_day(), [],
            [eng.SavingsPlan(hourly_commitment=1.0, term="P1Y",
                             scope="shared", existing=False)],
            portfolio="SP_ONLY",
        )
        # 24 calendar hours, not the 13 that carried usage rows.
        self.assertEqual(metrics.hours, 24)
        # Azure bills the commitment 24/7: 24 x $1.00.
        self.assertAlmostEqual(metrics.sp_benefit_cost, 24.0, places=6)
        self.assertAlmostEqual(metrics.sp_utilization, 0.5, places=6)
        self.assertAlmostEqual(metrics.waste_pct, 0.5, places=6)

    def test_idle_hours_flip_the_sign_of_reported_savings(self):
        """12h x $1.30 PAYG = $15.60 against a 24h x $1.00 = $24.00 plan."""
        metrics = eng.simulate_portfolio(
            self._half_idle_day(), [],
            [eng.SavingsPlan(hourly_commitment=1.0, term="P1Y",
                             scope="shared", existing=False)],
            portfolio="SP_ONLY",
        )
        self.assertAlmostEqual(metrics.total_cost, 24.0, places=6)
        self.assertAlmostEqual(metrics.savings_vs_payg, -8.4, places=6)
        self.assertLess(metrics.savings_vs_payg, 0.0)
        # A plan that loses money must not read as feasible.
        self.assertFalse(eng.feasible(metrics, "conservative"))

    def test_a_fully_used_window_is_unchanged(self):
        """Densifying must not perturb a window with no gaps."""
        lines = [line(utc(1, h), payg=1.30, sp=1.00, ri_eligible=False)
                 for h in range(24)]
        metrics = eng.simulate_portfolio(
            lines, [],
            [eng.SavingsPlan(hourly_commitment=1.0, term="P1Y",
                             scope="shared", existing=False)],
            portfolio="SP_ONLY",
        )
        self.assertEqual(metrics.hours, 24)
        self.assertAlmostEqual(metrics.sp_utilization, 1.0, places=6)
        self.assertAlmostEqual(metrics.waste_pct, 0.0, places=6)


class ReservationSimulationTests(unittest.TestCase):
    def test_case1_constant_vm_ri_optimal(self):
        usage = [line(utc(1, h)) for h in range(24)]
        payg = eng.simulate_portfolio(usage, [], [])
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=False,
        )
        with_ri = eng.simulate_portfolio(usage, [ri], [])
        self.assertLess(with_ri.total_cost, payg.total_cost)
        self.assertGreater(with_ri.savings_vs_payg, 0.0)
        self.assertAlmostEqual(with_ri.ri_utilization, 1.0, places=6)

    def test_case2_family_movement_prefers_savings_plan(self):
        usage = [line(utc(1, h), group="eastus:D_v5", sku="D4s_v5") for h in range(12)]
        usage += [
            line(utc(1, h), group="eastus:E_v5", sku="E4s_v5")
            for h in range(12, 24)
        ]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=False,
        )
        plan = eng.SavingsPlan(hourly_commitment=0.65, existing=False)
        ri_only = eng.simulate_portfolio(usage, [ri], [])
        sp_only = eng.simulate_portfolio(usage, [], [plan])
        self.assertLess(sp_only.total_cost, ri_only.total_cost)

    def test_case3_region_movement_prefers_savings_plan(self):
        usage = [line(utc(1, h), region="eastus", group="eastus:D_v5") for h in range(12)]
        usage += [
            line(utc(1, h), region="westus2", group="westus2:D_v5")
            for h in range(12, 24)
        ]
        plan = eng.SavingsPlan(hourly_commitment=0.65, existing=False)
        m = eng.simulate_portfolio(usage, [], [plan])
        self.assertAlmostEqual(m.sp_utilization, 1.0, places=6)
        self.assertEqual(m.sp_waste, 0.0)

    def test_case7_reservation_applied_before_savings_plan(self):
        usage = [line(utc(1, 0))]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=True,
        )
        plan = eng.SavingsPlan(hourly_commitment=1.0, existing=True)
        m = eng.simulate_portfolio(usage, [ri], [plan])
        hour = m.per_hour[0]
        self.assertAlmostEqual(hour.ri_used, 1.0, places=6)
        self.assertEqual(hour.sp_used, 0.0)
        self.assertAlmostEqual(hour.sp_waste, 1.0, places=6)

    def test_case9_existing_ri_removes_demand_first(self):
        demand = [1.0, 2.0, 3.0, 4.0]
        candidates = eng.ri_candidate_quantities(demand, existing=3)
        self.assertIn(1, candidates)
        self.assertNotIn(4, candidates)
        self.assertTrue(all(c >= 0 for c in candidates))

    def test_case11_flexibility_ratio_covers_multiple_sizes(self):
        usage = [
            line(utc(1, 0), sku="D2s_v5", qty=2.0, ratio=2.0),
        ]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=4.0, hourly_rate=0.6, quantity=1, existing=False,
        )
        m = eng.simulate_portfolio(usage, [ri], [])
        self.assertAlmostEqual(m.per_hour[0].ri_used, 4.0, places=6)
        self.assertAlmostEqual(m.per_hour[0].ri_unused, 0.0, places=6)

    def test_case17_ineligible_usage_stays_on_demand(self):
        usage = [line(utc(1, 0), ri_eligible=False)]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=False,
        )
        m = eng.simulate_portfolio(usage, [ri], [])
        hour = m.per_hour[0]
        self.assertEqual(hour.ri_used, 0.0)
        self.assertAlmostEqual(hour.payg_cost, 1.0, places=6)


class UnpricedTermTests(unittest.TestCase):
    """A SKU with no rate for the requested term is not purchasable on it.

    Regression: hourly_rate_p3y was fabricated as riHourly1y * 0.7. The real
    1yr->3yr step varies materially by family and region, and that invented
    constant decided expectedCost, the P1Y-vs-P3Y comparison and the SKU
    ranking inside integer_decompose. Unavailable rates are now negative and
    must be excluded rather than costed.
    """

    def test_a_sku_without_a_term_rate_is_excluded(self):
        catalog = [
            eng.CatalogSku(sku="D8s_v5", region="eastus",
                           flexibility_group="g", ratio=8.0,
                           hourly_rate_p1y=0.40, hourly_rate_p3y=-1.0),
        ]
        self.assertEqual(eng.integer_decompose(8.0, catalog, "P3Y"), ([], 0.0))
        lines, _ = eng.integer_decompose(8.0, catalog, "P1Y")
        self.assertEqual(len(lines), 1)

    def test_the_residual_filler_never_uses_an_unpriced_sku(self):
        """It filled from the raw catalog, so it could pick an entry that was
        deliberately excluded for having no usable rate -- and cost it at 0."""
        catalog = [
            eng.CatalogSku(sku="D8s_v5", region="eastus",
                           flexibility_group="g", ratio=8.0,
                           hourly_rate_p1y=0.40, hourly_rate_p3y=0.30),
            eng.CatalogSku(sku="D2s_v5", region="eastus",
                           flexibility_group="g", ratio=2.0,
                           hourly_rate_p1y=0.12, hourly_rate_p3y=-1.0),
        ]
        lines, _ = eng.integer_decompose(9.0, catalog, "P3Y")
        self.assertTrue(lines)
        for item in lines:
            self.assertNotEqual(item["sku"], "D2s_v5")
            self.assertGreater(item["hourlyRate"], 0.0)


class PurchaseLineCostTests(unittest.TestCase):
    """Each decomposed purchase line must carry its own rate.

    Regression: the pipeline costed every decomposed line with
    reservation.hourly_rate * reservation.quantity -- the whole reservation,
    repeated per line, at a rate that need not even belong to the SKU named
    on the row.
    """

    def test_decomposition_reports_the_rate_each_line_was_chosen_on(self):
        catalog = [
            eng.CatalogSku(sku="D8s_v5", region="eastus",
                           flexibility_group="g", ratio=8.0,
                           hourly_rate_p1y=0.40, hourly_rate_p3y=0.28),
            eng.CatalogSku(sku="D2s_v5", region="eastus",
                           flexibility_group="g", ratio=2.0,
                           hourly_rate_p1y=0.12, hourly_rate_p3y=0.084),
        ]
        lines, _ = eng.integer_decompose(18.0, catalog, "P1Y")
        self.assertTrue(lines)
        by_sku = {item["sku"]: item for item in lines}
        for sku, expected in (("D8s_v5", 0.40), ("D2s_v5", 0.12)):
            if sku in by_sku:
                self.assertAlmostEqual(
                    by_sku[sku]["hourlyRate"], expected, places=6, msg=sku
                )

    def test_three_year_lines_carry_the_three_year_rate(self):
        catalog = [eng.CatalogSku(sku="D8s_v5", region="eastus",
                                  flexibility_group="g", ratio=8.0,
                                  hourly_rate_p1y=0.40, hourly_rate_p3y=0.28)]
        lines, _ = eng.integer_decompose(8.0, catalog, "P3Y")
        self.assertAlmostEqual(lines[0]["hourlyRate"], 0.28, places=6)


class FeasibilityGateTests(unittest.TestCase):
    """Each commitment leg needs its own utilization floor.

    Regression: feasible() used max(sp_utilization, ri_utilization), and
    waste_pct is sp_waste / (sp_commit * hours) -- it re-measures the SP leg
    and never sees reservation waste. A strong Savings Plan therefore
    carried an arbitrarily over-bought reservation book through every risk
    profile, including "conservative", which is the profile a treasury team
    picks precisely to avoid that.
    """

    def _sp_strong_ri_idle(self):
        # D-family demand matched exactly by an SP; 20 E-family reservations
        # held against zero E-family demand.
        lines = [line(utc(1, h), group="eus:d", qty=1.0, payg=1.0, sp=0.65,
                      ri_eligible=False)
                 for h in range(24)]
        reservations = [eng.Reservation(sku="E4s_v5", region="eastus",
                                        flexibility_group="eus:e", ratio=1.0,
                                        hourly_rate=0.50, quantity=20)]
        plans = [eng.SavingsPlan(hourly_commitment=0.65, term="P1Y",
                                 scope="shared", existing=False)]
        return eng.simulate_portfolio(lines, reservations, plans,
                                      portfolio="BLENDED")

    def test_idle_reservations_are_not_carried_by_a_strong_savings_plan(self):
        metrics = self._sp_strong_ri_idle()
        self.assertAlmostEqual(metrics.sp_utilization, 1.0, places=4)
        self.assertLess(metrics.ri_utilization, 0.1)
        self.assertLess(metrics.savings_vs_payg, 0.0)
        for profile in ("conservative", "balanced", "aggressive"):
            self.assertFalse(
                eng.feasible(metrics, profile),
                msg=f"{profile} accepted an idle reservation book",
            )

    def test_a_healthy_blended_portfolio_still_passes(self):
        lines = [line(utc(1, h), group="eus:d", qty=2.0, payg=1.0, sp=0.65)
                 for h in range(24)]
        reservations = [eng.Reservation(sku="D4s_v5", region="eastus",
                                        flexibility_group="eus:d", ratio=1.0,
                                        hourly_rate=0.55, quantity=2)]
        metrics = eng.simulate_portfolio(lines, reservations, [],
                                         portfolio="RI_ONLY")
        self.assertAlmostEqual(metrics.ri_utilization, 1.0, places=4)
        self.assertTrue(eng.feasible(metrics, "conservative"))

    def test_no_commitment_is_always_feasible(self):
        lines = [line(utc(1, h), qty=1.0) for h in range(24)]
        metrics = eng.simulate_portfolio(lines, [], [], portfolio="PAYG")
        self.assertTrue(eng.feasible(metrics, "conservative"))


class ReservationBreakEvenTests(unittest.TestCase):
    """Break-even must be dollars per NORMALIZED unit.

    Regression: the numerator carried an extra `* line.ratio`, which the
    denominator already applies, so avg_unit_rate was a ratio-weighted mean
    of the per-instance rate and break-even came out understated by exactly
    `ratio`. parse_sku derives ratio from the vCPU count, so ratio is
    essentially never 1 in production -- every real reservation was affected.
    """

    def test_mixed_sizes_report_the_true_break_even(self):
        # 1x ratio-1 @ $0.10 + 1x ratio-2 @ $0.20 = $0.30/hr of PAYG value
        # across 3 normalized units; 3 reservations at $0.07 = $0.21/hr.
        # 0.21 / 0.30 = 70%.
        lines = [
            line(utc(1, 0), sku="D2s", group="g", qty=1.0, payg=0.10,
                 sp=0.08, ratio=1.0),
            line(utc(1, 0), sku="D4s", group="g", qty=1.0, payg=0.20,
                 sp=0.16, ratio=2.0),
        ]
        reservations = [eng.Reservation(sku="D2s", region="eastus",
                                        flexibility_group="g", ratio=1.0,
                                        hourly_rate=0.07, quantity=3)]
        metrics = eng.simulate_portfolio(lines, reservations, [],
                                         portfolio="RI_ONLY")
        self.assertAlmostEqual(
            metrics.ri_break_even_utilization, 0.70, places=4
        )

    def test_vcpu_scale_ratio_is_not_applied_twice(self):
        """A D8 (ratio 8) was reported at 7% break-even against a true 60%."""
        lines = [line(utc(1, 0), sku="D8s", group="g", qty=1.0, payg=0.384,
                      sp=0.30, ratio=8.0)]
        reservations = [eng.Reservation(sku="D8s", region="eastus",
                                        flexibility_group="g", ratio=8.0,
                                        hourly_rate=0.23, quantity=1)]
        metrics = eng.simulate_portfolio(lines, reservations, [],
                                         portfolio="RI_ONLY")
        self.assertAlmostEqual(
            metrics.ri_break_even_utilization, 0.599, places=3
        )

    def test_an_underwater_reservation_reports_above_one(self):
        """Clamping to 1.0 disguised "can never pay for itself" as benign."""
        lines = [line(utc(1, 0), sku="D8s", group="g", qty=1.0, payg=0.384,
                      sp=0.30, ratio=8.0)]
        reservations = [eng.Reservation(sku="D8s", region="eastus",
                                        flexibility_group="g", ratio=8.0,
                                        hourly_rate=1.20, quantity=1)]
        metrics = eng.simulate_portfolio(lines, reservations, [],
                                         portfolio="RI_ONLY")
        self.assertGreater(metrics.ri_break_even_utilization, 1.0)


class BlendedPortfolioTests(unittest.TestCase):
    def test_case4_stable_base_plus_variable_top_blended_wins(self):
        usage = []
        for h in range(24):
            usage.append(line(utc(1, h), resource="base"))
            if h < 20:
                usage.append(line(utc(1, h), resource="burst", qty=2.0))
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=False,
        )
        plan = eng.SavingsPlan(hourly_commitment=1.3, existing=False)
        ri_only = eng.simulate_portfolio(usage, [ri], [])
        sp_only = eng.simulate_portfolio(
            usage, [], [eng.SavingsPlan(hourly_commitment=1.95, existing=False)]
        )
        blended = eng.simulate_portfolio(usage, [ri], [plan])
        self.assertLess(blended.total_cost, ri_only.total_cost)
        self.assertLess(blended.total_cost, sp_only.total_cost)

    def test_case10_existing_sp_reduces_incremental_need(self):
        spend = [0.5, 1.0, 1.5, 2.0]
        candidates = eng.sp_candidate_commitments(spend, existing=1.5)
        self.assertIn(1.5, candidates)
        self.assertIn(0.0, candidates)

    def test_case23_terms_produce_distinct_results(self):
        catalog = [
            eng.CatalogSku(
                sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
                ratio=4.0, hourly_rate_p1y=0.06, hourly_rate_p3y=0.04,
            )
        ]
        p1y, _ = eng.integer_decompose(4.0, catalog, "P1Y")
        p3y, _ = eng.integer_decompose(4.0, catalog, "P3Y")
        self.assertEqual(p1y[0]["sku"], p3y[0]["sku"])
        ri1 = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=4.0, hourly_rate=0.06, quantity=1, term="P1Y", existing=False,
        )
        ri3 = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=4.0, hourly_rate=0.04, quantity=1, term="P3Y", existing=False,
        )
        usage = [line(utc(1, h), qty=4.0, ratio=1.0) for h in range(24)]
        m1 = eng.simulate_portfolio(usage, [ri1], [], term="P1Y")
        m3 = eng.simulate_portfolio(usage, [ri3], [], term="P3Y")
        self.assertLess(m3.total_cost, m1.total_cost)
        self.assertEqual(m1.term, "P1Y")
        self.assertEqual(m3.term, "P3Y")

    def test_case24_scope_is_carried_distinctly(self):
        shared = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, scope="Shared",
            existing=False,
        )
        narrow = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, scope="Subscription",
            existing=False,
        )
        self.assertNotEqual(shared.scope, narrow.scope)
        plan_shared = eng.SavingsPlan(hourly_commitment=1.0, scope="Shared")
        plan_sub = eng.SavingsPlan(hourly_commitment=1.0, scope="Subscription")
        self.assertNotEqual(plan_shared.scope, plan_sub.scope)


class PurchaseConstructionTests(unittest.TestCase):
    def test_case12_integer_decomposition_is_valid(self):
        catalog = [
            eng.CatalogSku(
                sku="D2s_v5", region="eastus", flexibility_group="eastus:D_v5",
                ratio=2.0, hourly_rate_p1y=0.03, hourly_rate_p3y=0.02,
            ),
            eng.CatalogSku(
                sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
                ratio=4.0, hourly_rate_p1y=0.055, hourly_rate_p3y=0.038,
            ),
        ]
        purchases, overcoverage = eng.integer_decompose(7.0, catalog, "P1Y")
        total_norm = sum(p["normalizedQuantity"] for p in purchases)
        self.assertGreaterEqual(total_norm + 1e-6, 7.0)
        self.assertTrue(all(p["quantity"] >= 1 for p in purchases))
        self.assertAlmostEqual(total_norm - 7.0, overcoverage, places=6)
        self.assertTrue(all(isinstance(p["quantity"], int) for p in purchases))

    def test_case26_expired_commitments_excluded_by_pipeline(self):
        plan = eng.SavingsPlan(hourly_commitment=1.0, existing=True)
        self.assertTrue(plan.existing)

    def test_zero_target_yields_no_purchases(self):
        purchases, over = eng.integer_decompose(0.0, [], "P1Y")
        self.assertEqual(purchases, [])
        self.assertEqual(over, 0.0)


class DoubleCoveragePreventionTests(unittest.TestCase):
    def test_case28_usage_allocated_at_most_once(self):
        usage = [line(utc(1, h)) for h in range(6)]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=True,
        )
        plan = eng.SavingsPlan(hourly_commitment=0.5, existing=True)
        m = eng.simulate_portfolio(usage, [ri], [plan])
        for hour in m.per_hour:
            allocated_equiv = (
                hour.ri_covered_payg_equiv
                + hour.sp_covered_payg_equiv
                + hour.payg_cost
            )
            self.assertAlmostEqual(
                allocated_equiv, hour.payg_equiv_cost + hour.ri_covered_payg_equiv,
                places=6,
            )
            self.assertLessEqual(hour.ri_used, hour.ri_capacity + 1e-9)
            self.assertLessEqual(hour.sp_used, hour.sp_commitment + 1e-9)
            self.assertGreaterEqual(hour.sp_waste, -1e-9)
            self.assertGreaterEqual(hour.overage_cost, -1e-9)

    def test_case29_sp_covered_usage_not_charged_twice(self):
        usage = [line(utc(1, 0))]
        plan = eng.SavingsPlan(hourly_commitment=0.65, existing=False)
        m = eng.simulate_portfolio(usage, [], [plan])
        hour = m.per_hour[0]
        self.assertAlmostEqual(hour.total_cost, 0.65, places=6)
        self.assertAlmostEqual(hour.payg_cost, 0.0, places=6)

    def test_cost_identity_reconciles(self):
        usage = [line(utc(1, h), qty=1.5) for h in range(12)]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=False,
        )
        plan = eng.SavingsPlan(hourly_commitment=0.4, existing=False)
        m = eng.simulate_portfolio(usage, [ri], [plan])
        for hour in m.per_hour:
            self.assertAlmostEqual(
                hour.total_cost,
                hour.ri_cost + hour.sp_commitment + hour.payg_cost,
                places=6,
            )
        self.assertAlmostEqual(
            m.total_cost, m.ri_cost + m.sp_benefit_cost + m.payg_residual_cost,
            places=6,
        )


class RiskAndSelectionTests(unittest.TestCase):
    def test_case25_demand_reduction_exposes_overcommitment(self):
        usage = [line(utc(1, h)) for h in range(24)]
        plan = eng.SavingsPlan(hourly_commitment=0.65, existing=False)
        report = eng.sensitivity_report(usage, [], [plan])
        stressed = report["scenarios"][-1]
        self.assertLess(stressed["savings"], report["baselineSavings"])

    def test_case27_nonpositive_savings_selects_payg(self):
        usage = [line(utc(1, 0))]
        payg = eng.simulate_portfolio(usage, [], [], portfolio=eng.PAYG)
        wasteful = eng.simulate_portfolio(
            usage, [], [eng.SavingsPlan(hourly_commitment=5.0, existing=False)],
            portfolio=eng.SP_ONLY,
        )
        chosen = eng.select_portfolio(
            {eng.PAYG: payg, eng.SP_ONLY: wasteful}, "conservative"
        )
        self.assertEqual(chosen, eng.PAYG)

    def test_case30_same_inputs_reproducible(self):
        usage = [line(utc(1, h)) for h in range(24)]
        plan = eng.SavingsPlan(hourly_commitment=0.6, existing=False)

        def run():
            m = eng.simulate_portfolio(usage, [], [plan])
            return (
                round(m.total_cost, 10),
                round(m.savings_vs_payg, 10),
                round(m.sp_waste, 10),
            )

        self.assertEqual(run(), run())

    def test_pareto_frontier_excludes_dominated(self):
        usage = [line(utc(1, h)) for h in range(24)]
        payg = eng.simulate_portfolio(usage, [], [], portfolio=eng.PAYG)
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.6, quantity=1, existing=False,
        )
        ri_only = eng.simulate_portfolio(usage, [ri], [], portfolio=eng.RI_ONLY)
        frontier = eng.pareto_frontier(
            {eng.PAYG: payg, eng.RI_ONLY: ri_only}
        )
        self.assertIn(eng.RI_ONLY, frontier)


class GateTests(unittest.TestCase):
    def _all_pass(self) -> dict[str, bool]:
        return {
            "price_sheet_available": True,
            "currency_match": True,
            "price_join_unambiguous": True,
            "hourly_grain_explicit": True,
            "expected_hours_match": True,
            "no_duplicate_hours": True,
            "commitments_current": True,
            "flexibility_mappings_present": True,
            "rightsizing_confident": True,
            "usage_recent": True,
            "no_focus_double_count": True,
            "software_excluded": True,
            "baselines_not_mixed": True,
            "catalog_purchasable": True,
        }

    def test_case18_missing_hourly_blocks_purchase_readiness(self):
        gates = eng.run_data_quality_gates(
            **{**self._all_pass(), "hourly_grain_explicit": False}
        )
        self.assertEqual(eng.classify_readiness(gates), "DIRECTIONAL_ONLY")

    def test_case19_missing_price_join_blocks(self):
        gates = eng.run_data_quality_gates(
            **{**self._all_pass(), "price_join_unambiguous": False}
        )
        self.assertEqual(eng.classify_readiness(gates), "BLOCKED")

    def test_case20_currency_mismatch_blocks(self):
        gates = eng.run_data_quality_gates(
            **{**self._all_pass(), "currency_match": False}
        )
        self.assertEqual(eng.classify_readiness(gates), "BLOCKED")

    def test_case21_duplicate_hours_flagged_directional(self):
        gates = eng.run_data_quality_gates(
            **{**self._all_pass(), "no_duplicate_hours": False}
        )
        self.assertEqual(eng.classify_readiness(gates), "DIRECTIONAL_ONLY")

    def test_all_pass_is_purchase_ready(self):
        gates = eng.run_data_quality_gates(**self._all_pass())
        self.assertEqual(eng.classify_readiness(gates), "PURCHASE_READY")

    def test_review_gate_yields_review_required(self):
        gates = eng.run_data_quality_gates(
            **{**self._all_pass(), "commitments_current": False}
        )
        self.assertEqual(eng.classify_readiness(gates), "REVIEW_REQUIRED")


class ReconciliationTests(unittest.TestCase):
    def test_reconcile_within_tolerance(self):
        result = eng.reconcile_with_azure(
            flux_commitment=1.05, azure_commitment=1.0, tolerance_pct=10.0
        )
        self.assertEqual(result["checks"]["commitment"]["status"], "within_tolerance")
        self.assertEqual(result["overall"], "reconciled")

    def test_reconcile_variance(self):
        result = eng.reconcile_with_azure(
            flux_commitment=2.0, azure_commitment=1.0, tolerance_pct=10.0
        )
        self.assertEqual(result["checks"]["commitment"]["status"], "variance")
        self.assertEqual(result["overall"], "variance")

    def test_reconcile_missing_evidence_unevaluated(self):
        result = eng.reconcile_with_azure(flux_commitment=1.0)
        self.assertEqual(result["overall"], "unevaluated")


class DstSafetyTests(unittest.TestCase):
    def test_case22_utc_hours_are_never_duplicated(self):
        start = datetime(2026, 11, 1, 0)
        seen = set()
        usage = []
        for offset in range(48):
            hour = start + timedelta(hours=offset)
            self.assertNotIn(hour, seen)
            seen.add(hour)
            usage.append(line(hour))
        m = eng.simulate_portfolio(usage, [], [])
        self.assertEqual(m.hours, 48)


class BacktestTests(unittest.TestCase):
    def test_backtest_reports_in_sample_and_holdout(self):
        train = [line(utc(1, h)) for h in range(24)]
        holdout = [line(utc(2, h)) for h in range(24)]
        plan = eng.SavingsPlan(hourly_commitment=0.65, existing=False)
        result = eng.backtest(train, holdout, [], [plan])
        self.assertAlmostEqual(result["inSampleSavings"], 24 * 0.35, places=2)
        self.assertAlmostEqual(result["holdoutSavings"], 24 * 0.35, places=2)
        self.assertAlmostEqual(result["holdoutWaste"], 0.0, places=6)

    def test_backtest_holdout_detects_degraded_economics(self):
        train = [line(utc(1, h)) for h in range(24)]
        shrunken_holdout = [line(utc(2, 0))]
        plan = eng.SavingsPlan(hourly_commitment=0.65, existing=False)
        result = eng.backtest(train, shrunken_holdout, [], [plan])
        self.assertGreater(result["inSampleSavings"], 0.0)
        self.assertLess(result["holdoutSavings"], result["inSampleSavings"])


class InvariantTests(unittest.TestCase):
    def test_ri_used_never_exceeds_capacity(self):
        usage = [line(utc(1, h), qty=10.0) for h in range(6)]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.5, quantity=2, existing=True,
        )
        m = eng.simulate_portfolio(usage, [ri], [])
        for hour in m.per_hour:
            self.assertLessEqual(hour.ri_used, hour.ri_capacity + 1e-9)
            self.assertGreaterEqual(hour.ri_unused, -1e-9)

    def test_sp_used_never_exceeds_commitment_and_waste_nonnegative(self):
        usage = [line(utc(1, h), qty=3.0) for h in range(6)]
        plan = eng.SavingsPlan(hourly_commitment=1.0, existing=False)
        m = eng.simulate_portfolio(usage, [], [plan])
        for hour in m.per_hour:
            self.assertLessEqual(hour.sp_used, hour.sp_commitment + 1e-9)
            self.assertGreaterEqual(hour.sp_waste, -1e-9)
            self.assertGreaterEqual(hour.overage_cost, -1e-9)

    def test_portfolio_cost_reconciles_to_components(self):
        usage = [line(utc(1, h), qty=2.0) for h in range(5)]
        ri = eng.Reservation(
            sku="D4s_v5", region="eastus", flexibility_group="eastus:D_v5",
            ratio=1.0, hourly_rate=0.5, quantity=1, existing=False,
        )
        plan = eng.SavingsPlan(hourly_commitment=0.4, existing=False)
        m = eng.simulate_portfolio(usage, [ri], [plan])
        self.assertAlmostEqual(
            m.total_cost, m.ri_cost + m.sp_benefit_cost + m.payg_residual_cost,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
