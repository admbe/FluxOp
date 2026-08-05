from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .database import FluxDatabase


def _datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Telemetry import timestamp is missing.")
    normalized = text.replace("Z", "+00:00")
    normalized = re.sub(
        r"(\.\d{6})\d+(?=([+-]\d{2}:\d{2})?$)",
        r"\1",
        normalized,
    )
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        result = None
        for pattern in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
            try:
                result = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if result is None:
            raise ValueError(f"Unsupported telemetry timestamp: {text}")
    return (
        result.replace(tzinfo=timezone.utc)
        if result.tzinfo is None
        else result.astimezone(timezone.utc)
    )


def _number(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _integer(value: Any) -> int:
    number = _number(value)
    return max(0, int(number or 0))


def _coverage(sample_count: int, reference_count: int, fallback: float) -> float:
    if sample_count and reference_count:
        return round(min(100.0, sample_count / reference_count * fallback), 2)
    return round(max(0.0, min(100.0, fallback)), 2)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _summary(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _metric(
    *,
    resource_id: str,
    source: str,
    name: str,
    unit: str,
    start: datetime,
    end: datetime,
    sample_count: int,
    coverage: float,
    average: float | None,
    p95: float | None,
    maximum: float | None,
    aggregation: str,
    lineage: dict[str, Any],
) -> dict[str, Any] | None:
    if p95 is None and average is None and maximum is None:
        return None
    return {
        "resourceId": resource_id.lower(),
        "source": source,
        "metric": name,
        "unit": unit,
        "windowStart": start,
        "windowEnd": end,
        "sampleCount": sample_count,
        "coveragePercent": coverage,
        "average": average,
        "p95": p95,
        "maximum": maximum,
        "lastValue": None,
        "lastObservedAt": end,
        "aggregationMethod": aggregation,
        "lineage": lineage,
    }


def _logicmonitor(
    root: Path,
) -> tuple[str, datetime, datetime, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    coverage_path = root / "VM-Metric-Coverage.csv"
    matches_path = root / "LogicMonitor-Azure-Matches.csv"
    summary_path = root / "Run-Summary.json"
    run = _summary(summary_path)
    started = _datetime(run["StartedUtc"])
    completed = _datetime(run["CompletedUtc"])
    run_id = f"bootstrap-logicmonitor-{_fingerprint([coverage_path, matches_path, summary_path])}"

    match_rows = _rows(matches_path)
    resource_by_device = {
        str(row.get("LogicMonitorId") or "").strip(): str(
            row.get("AzureResourceId") or ""
        ).strip().lower()
        for row in match_rows
        if str(row.get("MatchStatus") or "").strip().lower() == "matched"
        and str(row.get("AzureResourceId") or "").strip()
    }
    matches = [
        {
            "source": "logicmonitor",
            "sourceResourceId": str(row.get("LogicMonitorId") or ""),
            "sourceName": str(
                row.get("LogicMonitorDisplayName")
                or row.get("LogicMonitorName")
                or row.get("LogicMonitorId")
                or ""
            ),
            "resourceId": resource_by_device[str(row.get("LogicMonitorId") or "").strip()],
            "status": "matched",
            "method": str(row.get("MatchMethod") or "bootstrap_import"),
            "confidence": "high",
            "details": {
                "importedFrom": matches_path.name,
                "platform": row.get("LogicMonitorPlatform") or "",
            },
        }
        for row in match_rows
        if str(row.get("LogicMonitorId") or "").strip() in resource_by_device
    ]

    summaries: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for row in _rows(coverage_path):
        device_id = str(row.get("LogicMonitorId") or "").strip()
        resource_id = resource_by_device.get(device_id)
        if not resource_id:
            continue
        end = _datetime(row.get("CollectionEndUtc") or completed)
        cpu_days = int(_number(row.get("RequestedHistoryDays")) or 30)
        disk_days = int(_number(row.get("DiskNetHistoryDays")) or 14)
        cpu_start = end - timedelta(days=cpu_days)
        disk_start = end - timedelta(days=disk_days)
        cpu_samples = _integer(row.get("CPUSampleCount"))
        memory_samples = _integer(row.get("MemorySampleCount"))
        cpu_coverage = _number(row.get("CPUCoveragePercent")) or 0
        memory_coverage = _coverage(memory_samples, cpu_samples, cpu_coverage)
        lineage = {
            "importedFrom": coverage_path.name,
            "sourceSystem": "LogicMonitor",
            "deviceId": device_id,
            "metricCoverage": row.get("MetricCoverage") or "",
            "cpuMemoryDays": cpu_days,
            "diskNetworkDays": disk_days,
            "semantics": "Guest and collector metric summaries from LM-Azure-Rightsizing-v2.2.",
        }

        candidates = [
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Percentage CPU", unit="Percent",
                start=cpu_start, end=end, sample_count=cpu_samples,
                coverage=cpu_coverage,
                average=_number(row.get("CPUAveragePercent")),
                p95=_number(row.get("CPUP95Percent")),
                maximum=_number(row.get("CPUMaxPercent")),
                aggregation="LogicMonitor guest series percentile",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Memory Used Percentage", unit="Percent",
                start=cpu_start, end=end, sample_count=memory_samples,
                coverage=memory_coverage, average=None,
                p95=_number(row.get("MemoryUsedP95Percent")),
                maximum=_number(row.get("MemoryUsedP99Percent")),
                aggregation="LogicMonitor guest series percentile",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Network In Total", unit="BytesPerSecond",
                start=disk_start, end=end, sample_count=0, coverage=0,
                average=None,
                p95=(
                    _number(row.get("NetworkInMbpsP95")) * 1_000_000 / 8
                    if _number(row.get("NetworkInMbpsP95")) is not None else None
                ),
                maximum=None,
                aggregation="LogicMonitor network throughput p95 converted from Mbps",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Network Out Total", unit="BytesPerSecond",
                start=disk_start, end=end, sample_count=0, coverage=0,
                average=None,
                p95=(
                    _number(row.get("NetworkOutMbpsP95")) * 1_000_000 / 8
                    if _number(row.get("NetworkOutMbpsP95")) is not None else None
                ),
                maximum=None,
                aggregation="LogicMonitor network throughput p95 converted from Mbps",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Disk Read Operations/Sec", unit="CountPerSecond",
                start=disk_start, end=end, sample_count=0, coverage=0,
                average=None, p95=_number(row.get("DiskReadIOPSP95")),
                maximum=None, aggregation="LogicMonitor disk IOPS p95",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Disk Write Operations/Sec", unit="CountPerSecond",
                start=disk_start, end=end, sample_count=0, coverage=0,
                average=None, p95=_number(row.get("DiskWriteIOPSP95")),
                maximum=None, aggregation="LogicMonitor disk IOPS p95",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Disk Read Bytes/Sec", unit="BytesPerSecond",
                start=disk_start, end=end, sample_count=0, coverage=0,
                average=None, p95=(
                    _number(row.get("DiskReadMiBpsP95")) * 1024 * 1024
                    if _number(row.get("DiskReadMiBpsP95")) is not None else None
                ),
                maximum=None,
                aggregation="LogicMonitor disk throughput p95 converted from MiB/s",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="logicmonitor",
                name="Disk Write Bytes/Sec", unit="BytesPerSecond",
                start=disk_start, end=end, sample_count=0, coverage=0,
                average=None, p95=(
                    _number(row.get("DiskWriteMiBpsP95")) * 1024 * 1024
                    if _number(row.get("DiskWriteMiBpsP95")) is not None else None
                ),
                maximum=None,
                aggregation="LogicMonitor disk throughput p95 converted from MiB/s",
                lineage=lineage,
            ),
        ]
        summaries.extend(item for item in candidates if item)
        attempts.append(
            {
                "resourceId": resource_id,
                "source": "logicmonitor",
                "status": "covered" if candidates[0] else "no_data",
                "metricCount": sum(item is not None for item in candidates),
                "message": "Imported governed LogicMonitor history from the completed v2.2 run.",
            }
        )
    return run_id, started, completed, summaries, attempts, matches


def _azure_monitor(
    root: Path,
) -> tuple[str, datetime, datetime, list[dict[str, Any]], list[dict[str, Any]]]:
    coverage_path = root / "VM-Metric-Coverage.csv"
    summary_path = root / "Run-Summary.json"
    run = _summary(summary_path)
    started = _datetime(run["StartedUtc"])
    completed = _datetime(run["CompletedUtc"])
    run_id = f"bootstrap-azure-monitor-{_fingerprint([coverage_path, summary_path])}"
    summaries: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for row in _rows(coverage_path):
        resource_id = str(row.get("AzureResourceId") or "").strip().lower()
        if not resource_id:
            continue
        end = _datetime(row.get("CollectionEndUtc") or completed)
        days = int(_number(row.get("RequestedHistoryDays")) or 90)
        start = end - timedelta(days=days)
        cpu_samples = _integer(row.get("CPUSampleCount"))
        memory_samples = _integer(row.get("MemorySampleCount"))
        cpu_coverage = _number(row.get("CPUCoveragePercent")) or 0
        memory_coverage = _coverage(memory_samples, cpu_samples, cpu_coverage)
        lineage = {
            "importedFrom": coverage_path.name,
            "sourceSystem": "Azure Monitor",
            "dataQuality": row.get("DataQuality") or "",
            "metricCoverage": row.get("MetricCoverage") or "",
            "timeGrain": row.get("TimeGrain") or run.get("TimeGrain") or "",
            "requestedDays": days,
            "semantics": (
                "CPU p95 is calculated from Azure Monitor hourly maximum "
                "series and intentionally skews conservative."
            ),
        }
        def mbps(field: str) -> float | None:
            value = _number(row.get(field))
            return value * 1_000_000 / 8 if value is not None else None

        def mibps(field: str) -> float | None:
            value = _number(row.get(field))
            return value * 1024 * 1024 if value is not None else None

        candidates = [
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Percentage CPU", unit="Percent", start=start, end=end,
                sample_count=cpu_samples, coverage=cpu_coverage,
                average=_number(row.get("CPUAveragePercent")),
                p95=_number(row.get("CPUP95Percent")),
                maximum=_number(row.get("CPUMaxPercent")),
                aggregation="p95 of PT1H maximum series",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Memory Used Percentage", unit="Percent", start=start, end=end,
                sample_count=memory_samples, coverage=memory_coverage,
                average=None, p95=_number(row.get("MemoryUsedP95Percent")),
                maximum=_number(row.get("MemoryUsedP99Percent")),
                aggregation="derived from Available Memory Bytes",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Network In Total", unit="BytesPerSecond", start=start, end=end,
                sample_count=0, coverage=0, average=None,
                p95=mbps("NetworkInMbpsP95"), maximum=None,
                aggregation="p95 of PT1H total converted from Mbps",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Network Out Total", unit="BytesPerSecond", start=start, end=end,
                sample_count=0, coverage=0, average=None,
                p95=mbps("NetworkOutMbpsP95"), maximum=None,
                aggregation="p95 of PT1H total converted from Mbps",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Disk Read Operations/Sec", unit="CountPerSecond",
                start=start, end=end, sample_count=0, coverage=0, average=None,
                p95=_number(row.get("DiskReadIOPSP95")), maximum=None,
                aggregation="p95 of PT1H disk IOPS series", lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Disk Write Operations/Sec", unit="CountPerSecond",
                start=start, end=end, sample_count=0, coverage=0, average=None,
                p95=_number(row.get("DiskWriteIOPSP95")), maximum=None,
                aggregation="p95 of PT1H disk IOPS series", lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Disk Read Bytes/Sec", unit="BytesPerSecond",
                start=start, end=end, sample_count=0, coverage=0, average=None,
                p95=mibps("DiskReadMiBpsP95"), maximum=None,
                aggregation="p95 of PT1H disk throughput converted from MiB/s",
                lineage=lineage,
            ),
            _metric(
                resource_id=resource_id, source="azure_monitor",
                name="Disk Write Bytes/Sec", unit="BytesPerSecond",
                start=start, end=end, sample_count=0, coverage=0, average=None,
                p95=mibps("DiskWriteMiBpsP95"), maximum=None,
                aggregation="p95 of PT1H disk throughput converted from MiB/s",
                lineage=lineage,
            ),
        ]
        summaries.extend(item for item in candidates if item)
        attempts.append(
            {
                "resourceId": resource_id,
                "source": "azure_monitor",
                "status": "covered" if candidates[0] else "no_data",
                "metricCount": sum(item is not None for item in candidates),
                "message": "Imported governed Azure Monitor host history.",
            }
        )
    return run_id, started, completed, summaries, attempts


def import_bootstrap(
    database: FluxDatabase,
    logicmonitor_root: Path,
    azure_monitor_root: Path,
    *,
    rightsizing_thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lm = _logicmonitor(logicmonitor_root)
    az = _azure_monitor(azure_monitor_root)

    lm_id, lm_started, lm_completed, lm_summaries, lm_attempts, matches = lm
    database.start_telemetry_import(lm_id, "logicmonitor", lm_started)
    database.store_telemetry_summaries(
        lm_id, lm_summaries, observed_at=lm_completed
    )
    database.store_telemetry_attempts(
        lm_id, lm_attempts, observed_at=lm_completed
    )
    database.store_source_matches(lm_id, matches, observed_at=lm_completed)
    database.finish_telemetry_run(
        lm_id,
        "succeeded",
        len(lm_attempts),
        f"Imported {len(lm_summaries):,} LogicMonitor metric summaries for {len(lm_attempts):,} linked VMs.",
        completed_at=lm_completed,
    )

    az_id, az_started, az_completed, az_summaries, az_attempts = az
    database.start_telemetry_import(az_id, "azure_monitor", az_started)
    database.store_telemetry_summaries(
        az_id, az_summaries, observed_at=az_completed
    )
    database.store_telemetry_attempts(
        az_id, az_attempts, observed_at=az_completed
    )
    database.finish_telemetry_run(
        az_id,
        "succeeded",
        len(az_attempts),
        f"Imported {len(az_summaries):,} Azure Monitor metric summaries for {len(az_attempts):,} VMs.",
        completed_at=az_completed,
    )

    recommendation_run_id = f"bootstrap-reconciliation-{max(lm_completed, az_completed).strftime('%Y%m%d%H%M%S')}"
    recommendation_count = database.compute_rightsizing_recommendations(
        recommendation_run_id,
        **(rightsizing_thresholds or {}),
    )
    return {
        "logicMonitorRunId": lm_id,
        "logicMonitorResources": len(lm_attempts),
        "logicMonitorMetrics": len(lm_summaries),
        "azureMonitorRunId": az_id,
        "azureMonitorResources": len(az_attempts),
        "azureMonitorMetrics": len(az_summaries),
        "recommendationRunId": recommendation_run_id,
        "recommendationCount": recommendation_count,
    }
