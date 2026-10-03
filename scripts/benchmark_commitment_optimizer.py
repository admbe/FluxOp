"""Performance benchmark for the Azure Commitment Purchase Optimizer.

Generates schema-faithful synthetic FOCUS hourly evidence (never fabricated
from daily or monthly amounts) plus a negotiated price sheet and retail
prices, then times the full optimization pipeline and the engine-level
simulation at 60-day and 365-day scales.

Usage:
    python -m scripts.benchmark_commitment_optimizer
    python -m scripts.benchmark_commitment_optimizer --days 60 --vms 200

Results are printed as a table suitable for pasting into
docs/COMMITMENT-OPTIMIZER.md.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from api import commitment_optimizer as engine
from api.commitment_optimizer_pipeline import run_commitment_optimization
from api.database import FluxDatabase, utc_now

SUB = "99999999-9999-9999-9999-999999999999"

PRICE_SHEET_CSV = """meterId,meterName,serviceFamily,product,skuId,unitOfMeasure,priceType,unitPrice,basePrice,marketPrice,currency,term
m-d4,D4s v5,Compute,Virtual Machines Dsv5,SKU-D4,1 Hour,Consumption,0.10,0.12,0.12,USD,
m-d4,D4s v5,Compute,Virtual Machines Dsv5,SKU-D4,1 Hour,SavingsPlan,0.065,0.12,0.12,USD,1 Year
m-d4,D4s v5,Compute,Virtual Machines Dsv5,SKU-D4,1 Year,Reservation,438.0,525.6,525.6,USD,1 Year
"""


def seed_focus(database: FluxDatabase, *, days: int, vms: int) -> None:
    manifest_id = f"manifest-{uuid4()}"
    end = (utc_now() - timedelta(days=1)).replace(
        minute=0, second=0, microsecond=0
    )
    start = end - timedelta(days=days)
    with database.connect() as db:
        db.execute(
            """
            INSERT INTO focus_export_manifests VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported',
                ?, ?, ?, ?, ?, ?, ''
            )
            """,
            [
                manifest_id,
                "run-bench",
                f"/{manifest_id}/manifest.json",
                "focus-export",
                "export-run-bench",
                SUB,
                "Benchmark subscription",
                start.date(),
                end.date(),
                end,
                end,
                "1",
                days * 24,
                1024,
                "USD",
                0.2 * days * 24 * vms,
                0.2 * days * 24 * vms,
            ],
        )
        db.execute(
            """
            INSERT INTO focus_cost_charges
            SELECT
                'charge-' || row_number() OVER (ORDER BY hour, vm),
                ?,
                hour,
                hour + INTERVAL '1' HOUR,
                date_trunc('month', hour),
                date_trunc('month', hour) + INTERVAL 1 MONTH,
                0.2, 0.2, 0.2, 0.24,
                'USD', 'Usage', 'Usage', 'Usage-based', 'D4s v5',
                'OnDemand', 1.0, '1 Hour', 1.0, '1 Hour', 0.10, 0.12,
                '', '', '', '', 'Compute', 'Virtual Machines',
                '/subscriptions/' || ? ||
                    '/resourceGroups/rg/providers/Microsoft.Compute'
                    '/virtualMachines/vm-' || vm,
                'vm-' || vm,
                'Microsoft.Compute/virtualMachines', 'rg-bench', ?,
                'Benchmark subscription', 'Microsoft.Compute', 'Microsoft',
                'eastus', 'SKU-D4', 'PRICE-D4', 'm-d4', 'D4s v5',
                'Virtual Machines', 'Dsv5', '{}', '{}'
            FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ, INTERVAL '1' HOUR)
                AS hours(hour),
                 range(?) AS vms(vm)
            """,
            [manifest_id, SUB, SUB, start, end, vms],
        )


def seed_price_sheet(database: FluxDatabase, root: Path) -> None:
    path = root / "pricesheet.csv"
    path.write_text(PRICE_SHEET_CSV, encoding="utf-8")
    database.store_price_sheet([path])


def seed_retail(database: FluxDatabase) -> None:
    with database.connect() as db:
        db.execute(
            """
            INSERT INTO retail_price_snapshots VALUES (
                ?, ?, 'eastus', 'Standard_D4s_v5', 'linux', '', 'linux',
                'USD', 'matched', 0.12, 87.6, 87.6, 0.0, 43.8, 525.6,
                47.45, 730.0, 'm-d4', 'D4s v5', 'Virtual Machines Dsv5',
                'D4s v5', '1 Hour', ?, 1, 'benchmark', '', '', '{}'
            )
            """,
            [
                f"retail-{uuid4()}",
                datetime.now(timezone.utc),
                datetime(2026, 6, 1, tzinfo=timezone.utc),
            ],
        )


def seed_rightsizing(database: FluxDatabase) -> None:
    with database.connect() as db:
        db.execute(
            """
            INSERT INTO rightsizing_recommendation_snapshots VALUES (
                ?, ?, 'vm-0', 'vm-0', ?, 'Benchmark subscription', 'rg-bench',
                'eastus', 'resize', 'candidate', 'Standard_D4s_v5',
                'Standard_D2s_v5', 30, 'covered', 'azure_monitor',
                3.0, 9.0, 40.0, 1000.0, 1000.0, 92.0, 20.0, 'USD',
                'retail_price_difference', 'Low CPU utilization.',
                '{}', 'benchmark-method'
            )
            """,
            [f"rr-{uuid4()}", datetime.now(timezone.utc), SUB],
        )


def benchmark_pipeline(days: int, vms: int) -> dict[str, float]:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        database = FluxDatabase(root / "bench.duckdb")
        database.init()
        started = time.perf_counter()
        seed_focus(database, days=days, vms=vms)
        seed_price_sheet(database, root)
        seed_retail(database)
        database.store_commitments(f"commitments-{uuid4()}", [], [])
        database.store_savings_plans(f"sp-{uuid4()}", [], [])
        seed_rightsizing(database)
        seed_seconds = time.perf_counter() - started
        started = time.perf_counter()
        result = run_commitment_optimization(
            database, lookback_days=days, requested_by="benchmark"
        )
        run_seconds = time.perf_counter() - started
    return {
        "days": days,
        "vms": vms,
        "charges": days * 24 * vms,
        "seedSeconds": seed_seconds,
        "runSeconds": run_seconds,
        "readiness": result["readiness"],
        "recommended": result["recommendedPortfolio"],
    }


def benchmark_engine_simulation(days: int, skus_per_hour: int) -> dict[str, float]:
    start_hour = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lines: list[engine.UsageLine] = []
    for hour_offset in range(days * 24):
        hour = start_hour + timedelta(hours=hour_offset)
        for sku_index in range(skus_per_hour):
            lines.append(
                engine.UsageLine(
                    hour=hour,
                    sku=f"sku-{sku_index}",
                    region="eastus",
                    flexibility_group=f"eastus:family-{sku_index % 25}",
                    quantity=1.0,
                    contracted_payg_rate=0.10,
                    list_payg_rate=0.12,
                    sp_rate=0.065,
                    ratio=1.0,
                )
            )
    reservations = [
        engine.Reservation(
            sku=f"sku-{index}",
            region="eastus",
            flexibility_group=f"eastus:family-{index % 25}",
            ratio=1.0,
            hourly_rate=0.05,
            quantity=1,
            existing=False,
        )
        for index in range(0, skus_per_hour, 4)
    ]
    plans = [engine.SavingsPlan(hourly_commitment=0.5 * skus_per_hour * 0.065, existing=False)]
    started = time.perf_counter()
    metrics = engine.simulate_portfolio(lines, reservations, plans)
    simulate_seconds = time.perf_counter() - started
    started = time.perf_counter()
    engine.sensitivity_report(lines, reservations, plans)
    sensitivity_seconds = time.perf_counter() - started
    return {
        "days": days,
        "rows": len(lines),
        "hours": days * 24,
        "skuRegionCombos": 25 * 1,
        "simulateSeconds": simulate_seconds,
        "sensitivitySeconds": sensitivity_seconds,
        "annualizedCost": metrics.annualized_cost,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--vms", type=int, default=200)
    parser.add_argument("--engine-days", type=int, default=365)
    parser.add_argument("--engine-skus", type=int, default=20)
    args = parser.parse_args()

    print("Commitment optimizer benchmark")
    print("=" * 72)

    pipeline = benchmark_pipeline(args.days, args.vms)
    print(
        f"Pipeline: {pipeline['days']}d x {pipeline['vms']} VMs "
        f"({pipeline['charges']:,} hourly charges) -> "
        f"{pipeline['readiness']} / {pipeline['recommended']} "
        f"in {pipeline['runSeconds']:.1f}s (seed {pipeline['seedSeconds']:.1f}s)"
    )

    engine_scale = benchmark_engine_simulation(args.engine_days, args.engine_skus)
    print(
        f"Engine: {engine_scale['days']}d x {engine_scale['rows']:,} usage rows "
        f"({engine_scale['hours']:,} hours, {engine_scale['skuRegionCombos']} "
        f"flexibility groups) -> simulate {engine_scale['simulateSeconds']:.1f}s, "
        f"sensitivity {engine_scale['sensitivitySeconds']:.1f}s"
    )


if __name__ == "__main__":
    main()
