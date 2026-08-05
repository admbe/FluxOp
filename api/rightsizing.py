from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any


METHOD_VERSION = "multi-source-rightsizing-v2"


def _number(value: float | Decimal | None) -> float | None:
    return float(value) if value is not None else None


def assess_resource(
    *,
    attempt_status: str,
    window_start: datetime | None,
    window_end: datetime | None,
    cpu_p95: float | Decimal | None,
    cpu_maximum: float | Decimal | None,
    cpu_coverage: float | Decimal | None,
    network_in_p95: float | Decimal | None,
    network_out_p95: float | Decimal | None,
    memory_p95: float | Decimal | None = None,
    source_disagreement: bool = False,
    advisor_target_sku: str = "",
    advisor_monthly_savings: float | Decimal | None = None,
    minimum_window_days: int = 14,
    minimum_coverage_percent: float = 70,
    idle_cpu_p95: float = 5,
    idle_cpu_maximum: float = 20,
    idle_network_p95_bytes: float = 52_428_800,
    review_cpu_p95: float = 30,
    memory_review_percent: float = 80,
) -> dict[str, Any]:
    cpu = _number(cpu_p95)
    cpu_max = _number(cpu_maximum)
    coverage = _number(cpu_coverage)
    memory = _number(memory_p95)
    network_in = _number(network_in_p95)
    network_out = _number(network_out_p95)
    window_days = (
        max(0, (window_end - window_start).days)
        if window_start and window_end
        else 0
    )

    base = {
        "kind": "",
        "status": "insufficient_telemetry",
        "coverageFlag": "none",
        "targetSku": "",
        "evidenceWindowDays": window_days,
        "cpuP95": cpu,
        "cpuMaximum": cpu_max,
        "memoryP95": memory,
        "networkInP95": network_in,
        "networkOutP95": network_out,
        "metricCoveragePercent": coverage,
        "advisorMonthlySavings": _number(advisor_monthly_savings),
        "reason": "No successful governed telemetry observation is available.",
        "methodVersion": METHOD_VERSION,
    }
    if attempt_status != "covered" or cpu is None:
        return base

    base["coverageFlag"] = "partial"
    if window_days < minimum_window_days or (coverage or 0) < minimum_coverage_percent:
        base["status"] = "warming_up"
        base["reason"] = (
            f"Telemetry has {window_days} days and {coverage or 0:.1f}% CPU "
            "coverage; more evidence is required."
        )
        return base
    if source_disagreement:
        base["status"] = "needs_review"
        base["reason"] = (
            "Telemetry sources materially disagree on CPU utilization. "
            "Review source lineage before taking action."
        )
        return base
    if network_in is None or network_out is None:
        base["status"] = "partial_telemetry"
        base["reason"] = (
            "CPU is covered, but both network metrics are required before "
            "classifying a VM as idle."
        )
        return base
    if memory is None:
        base["status"] = "partial_telemetry"
        base["reason"] = (
            "CPU and network are covered, but memory evidence is required "
            "before recommending an action."
        )
        return base

    base["coverageFlag"] = "covered"
    if memory >= memory_review_percent:
        base["status"] = "no_opportunity"
        base["reason"] = (
            f"Memory p95 is {memory:.1f}%, above the governed "
            f"{memory_review_percent:.1f}% review threshold."
        )
        return base
    idle = (
        cpu <= idle_cpu_p95
        and (cpu_max is None or cpu_max <= idle_cpu_maximum)
        and network_in <= idle_network_p95_bytes
        and network_out <= idle_network_p95_bytes
    )
    if idle:
        base["kind"] = "shutdown"
        base["status"] = "candidate"
        base["targetSku"] = "Deallocate VM"
        base["reason"] = (
            f"CPU p95 is {cpu:.1f}% (limit {idle_cpu_p95:.1f}%) and CPU "
            f"peak is {(cpu_max if cpu_max is not None else 0):.1f}% "
            f"(limit {idle_cpu_maximum:.1f}%). Network p95 is "
            f"{network_in / 1_048_576:.1f} MiB in and "
            f"{network_out / 1_048_576:.1f} MiB out (limit "
            f"{idle_network_p95_bytes / 1_048_576:.1f} MiB each), while "
            f"memory p95 is {memory:.1f}% (guardrail "
            f"{memory_review_percent:.1f}%). This remained true across "
            f"{window_days} days with {(coverage or 0):.1f}% CPU coverage."
        )
        return base

    if cpu <= review_cpu_p95 and advisor_target_sku:
        base["kind"] = "resize"
        base["status"] = "candidate"
        base["targetSku"] = advisor_target_sku
        base["reason"] = (
            f"CPU p95 is {cpu:.1f}%, at or below the governed "
            f"{review_cpu_p95:.1f}% resize-review limit; memory p95 is "
            f"{memory:.1f}%, below the {memory_review_percent:.1f}% "
            f"guardrail. The evidence covers {window_days} days with "
            f"{(coverage or 0):.1f}% CPU coverage, and Azure Advisor "
            f"identifies {advisor_target_sku} as the target SKU."
        )
        return base
    if cpu <= review_cpu_p95:
        base["kind"] = "rightsizing_review"
        base["status"] = "target_rate_unavailable"
        base["reason"] = (
            f"CPU p95 is {cpu:.1f}%, at or below the governed "
            f"{review_cpu_p95:.1f}% resize-review limit, and memory p95 is "
            f"{memory:.1f}%, below the {memory_review_percent:.1f}% "
            "guardrail. No governed target SKU is available, so Flux will "
            "not invent a resize target."
        )
        return base

    base["status"] = "no_opportunity"
    base["reason"] = (
        f"CPU p95 is {cpu:.1f}%, above the governed "
        f"{review_cpu_p95:.1f}% resize-review limit."
    )
    return base
