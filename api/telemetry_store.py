"""Telemetry domain extracted from the god object (see #13).

FluxDatabase delegates to this mixin so the public surface
``from api.database import FluxDatabase`` is unchanged. Covers the
telemetry run/import lifecycle, raw samples + summaries, source matches,
LogicMonitor targets/checkpoints, fleet/resource views, FinOps Toolkit open-data,
and the telemetry chorus.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
from filelock import Timeout as FileLockTimeout


def _utc_now():
    from .database import utc_now
    return utc_now()

def _json_value(value):
    from .database import json_value
    return json_value(value)

_logger = logging.getLogger("flux.database")


class TelemetryMixin:


    def start_telemetry_run(self, source: str, trigger: str = "scheduled") -> str:  # type: ignore[no-redef]
        run_id = str(uuid4())
        with self.connect() as db:  # type: ignore[attr-defined]
            db.execute(
                "INSERT INTO telemetry_runs VALUES (?, ?, ?, ?, NULL, 'running', 0, '')",
                [run_id, source, trigger, _utc_now()],
            )
        return run_id

    def finish_telemetry_run(
        self,
        run_id: str,
        status: str,
        processed_count: int,
        message: str,
        completed_at: datetime | None = None,
    ) -> None:  # type: ignore[no-redef]
        with self.connect() as db:  # type: ignore[attr-defined]
            db.execute(
                """
                UPDATE telemetry_runs
                SET completed_at = ?, status = ?, processed_count = ?, message = ?
                WHERE id = ?
                """,
                [completed_at or _utc_now(), status, processed_count, message, run_id],
            )

    def start_telemetry_import(
        self,
        run_id: str,
        source: str,
        started_at: datetime,
    ) -> None:  # type: ignore[no-redef]
        """Start or idempotently replace one governed bootstrap import."""
        with self.connect() as db:  # type: ignore[attr-defined]
            db.execute("BEGIN TRANSACTION")
            try:
                db.execute(
                    "DELETE FROM telemetry_metric_summaries WHERE run_id = ?",
                    [run_id],
                )
                db.execute(
                    "DELETE FROM telemetry_resource_attempts WHERE run_id = ?",
                    [run_id],
                )
                db.execute(
                    "DELETE FROM resource_source_matches WHERE run_id = ?",
                    [run_id],
                )
                db.execute("DELETE FROM telemetry_runs WHERE id = ?", [run_id])
                db.execute(
                    """
                    INSERT INTO telemetry_runs VALUES (
                        ?, ?, 'bootstrap_import', ?, NULL, 'running', 0, ''
                    )
                    """,
                    [run_id, source, started_at],
                )
                db.execute("COMMIT")
            except (duckdb.Error, FileLockTimeout, OSError) as _error:
                _logger.warning("database transaction failed; rolling back", exc_info=_error)
                try:
                    db.execute("ROLLBACK")
                except Exception as _rollback_error:  # noqa: BLE001 - rollback must not mask original
                    _logger.warning("rollback also failed", exc_info=_rollback_error)
                raise RuntimeError("database transaction failed") from _error

    def telemetry_targets(self, limit: int = 200) -> list[dict[str, Any]]:  # type: ignore[no-redef]
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                """
                SELECT resource.resource_id, resource.name, resource.subscription_id,
                       resource.subscription_name, resource.resource_group,
                       resource.region, resource.raw_json,
                       attempt.observed_at AS last_attempt_at
                FROM resources_current AS resource
                LEFT JOIN telemetry_resource_attempts_current AS attempt
                  ON attempt.resource_id = lower(resource.resource_id)
                 AND attempt.source = 'azure_monitor'
                WHERE resource.resource_type = 'microsoft.compute/virtualmachines'
                ORDER BY last_attempt_at NULLS FIRST, resource.name
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [
            {
                "resourceId": row[0],
                "name": row[1],
                "subscriptionId": row[2],
                "subscriptionName": row[3],
                "resourceGroup": row[4],
                "region": row[5],
                "raw": json.loads(row[6] or "{}"),
            }
            for row in rows
        ]

    def logicmonitor_metric_targets(
        self,
        limit: int,
        *,
        initial_hours: int,
        maximum_window_hours: int,
    ) -> list[dict[str, Any]]:  # type: ignore[no-redef]
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                """
                SELECT match.source_resource_id, match.source_name,
                       match.resource_id,
                       coalesce(
                           json_extract_string(match.details_json, '$.platform'),
                           'Unknown'
                       ) AS platform,
                       checkpoint.collected_through
                FROM resource_source_matches_current AS match
                LEFT JOIN telemetry_collection_checkpoints AS checkpoint
                  ON checkpoint.source = 'logicmonitor'
                 AND checkpoint.source_resource_id = match.source_resource_id
                 AND checkpoint.stream = 'performance'
                JOIN resources_current AS resource
                  ON lower(resource.resource_id) = match.resource_id
                WHERE match.source = 'logicmonitor'
                  AND match.status = 'matched'
                  AND resource.resource_type =
                      'microsoft.compute/virtualmachines'
                ORDER BY checkpoint.collected_through NULLS FIRST,
                         match.source_name
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        now = _utc_now().replace(microsecond=0)
        values = []
        for row in rows:
            checkpoint = row[4]
            start = (
                checkpoint - timedelta(minutes=5)
                if checkpoint
                else now - timedelta(hours=initial_hours)
            )
            end = min(
                now,
                (checkpoint or start)
                + timedelta(hours=maximum_window_hours),
            )
            values.append(
                {
                    "sourceResourceId": row[0],
                    "sourceName": row[1],
                    "resourceId": row[2],
                    "platform": row[3],
                    "windowStart": start,
                    "windowEnd": end,
                }
            )
        return values

    def store_telemetry_samples(
        self,
        run_id: str,
        samples: list[dict[str, Any]],
        *,
        retention_days: int = 30,
    ) -> None:  # type: ignore[no-redef]
        """Upsert one target's samples.

        Deliberately does NOT prune here: this runs once per metric target,
        and the retention DELETE it used to carry was a full scan of the
        multi-million-row samples table inside the same transaction --
        dozens of times per run. Under the DuckDB memory cap that is what
        drove the LogicMonitor metrics job out of memory ("failed to pin
        block ... 1.4 GiB/1.4 GiB used", every run from mid-July to
        2026-08-02, leaving the source permanently degraded). Callers prune
        once per run via prune_telemetry_samples.
        """
        if not samples:
            return
        ingested_at = _utc_now()
        window_start = min(item["observedAt"] for item in samples)
        window_end = max(item["observedAt"] for item in samples)
        batch_scopes = sorted(
            {
                (item["source"], str(item["sourceResourceId"]))
                for item in samples
            }
        )
        with self.connect() as db:  # type: ignore[attr-defined]
            # Insert-heavy path against a large table under a hard memory
            # cap; insertion order carries no meaning for samples and
            # preserving it blocks spilling.
            db.execute("SET preserve_insertion_order = false")
            db.execute("BEGIN TRANSACTION")
            try:
                # Explicit idempotency instead of a PK upsert (see the
                # schema comment): re-collections replace their own
                # (source, resource, window) slice via a spillable scan.
                for source, source_resource_id in batch_scopes:
                    db.execute(
                        """
                        DELETE FROM telemetry_metric_samples
                        WHERE source = ? AND source_resource_id = ?
                          AND observed_at BETWEEN ? AND ?
                        """,
                        [source, source_resource_id, window_start, window_end],
                    )
                db.executemany(
                    """
                    INSERT INTO telemetry_metric_samples (
                        run_id, ingested_at, source, source_resource_id,
                        resource_id, metric, unit, observed_at, value,
                        lineage_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        [
                            run_id,
                            ingested_at,
                            item["source"],
                            str(item["sourceResourceId"]),
                            item["resourceId"].lower(),
                            item["metric"],
                            item.get("unit", ""),
                            item["observedAt"],
                            item["value"],
                            _json_value(item.get("lineage", {})),
                        ]
                        for item in samples
                    ],
                )
                db.execute("COMMIT")
            except (duckdb.Error, FileLockTimeout, OSError) as _error:
                _logger.warning("database transaction failed; rolling back", exc_info=_error)
                try:
                    db.execute("ROLLBACK")
                except Exception as _rollback_error:  # noqa: BLE001 - rollback must not mask original
                    _logger.warning("rollback also failed", exc_info=_rollback_error)
                raise RuntimeError("database transaction failed") from _error

    def prune_telemetry_samples(self, retention_days: int = 30) -> int:  # type: ignore[no-redef]
        """One retention pass over raw samples; call once per import run."""
        with self.connect() as db:  # type: ignore[attr-defined]
            return int(
                db.execute(
                    """
                    DELETE FROM telemetry_metric_samples
                    WHERE observed_at < now() - (? * INTERVAL '1 day')
                    """,
                    [max(1, retention_days)],
                ).fetchone()[0]
            )

    def update_telemetry_checkpoint(
        self,
        source_resource_id: str,
        collected_through: datetime,
        *,
        status: str,
        message: str,
    ) -> None:  # type: ignore[no-redef]
        with self.connect() as db:  # type: ignore[attr-defined]
            db.execute(
                """
                INSERT INTO telemetry_collection_checkpoints VALUES (
                    'logicmonitor', ?, 'performance', ?, ?, ?, ?
                )
                ON CONFLICT (source, source_resource_id, stream)
                DO UPDATE SET
                    collected_through = excluded.collected_through,
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    message = excluded.message
                """,
                [
                    str(source_resource_id),
                    collected_through,
                    _utc_now(),
                    status,
                    message[:500],
                ],
            )

    def summarize_logicmonitor_samples(
        self,
        run_id: str,
        resource_ids: list[str],
        *,
        history_days: int,
    ) -> int:  # type: ignore[no-redef]
        if not resource_ids:
            return 0
        normalized = sorted({item.lower() for item in resource_ids})
        placeholders = ", ".join("?" for _ in normalized)
        cutoff = _utc_now() - timedelta(days=history_days)
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                f"""
                SELECT resource_id, metric, any_value(unit),
                       min(observed_at), max(observed_at), count(*),
                       count(DISTINCT date_trunc('hour', observed_at)),
                       avg(value), quantile_cont(value, 0.95), max(value),
                       arg_max(value, observed_at),
                       arg_max(observed_at, observed_at),
                       any_value(source_resource_id)
                FROM telemetry_metric_samples
                WHERE source = 'logicmonitor'
                  AND observed_at >= ?
                  AND resource_id IN ({placeholders})
                GROUP BY resource_id, metric
                """,
                [cutoff, *normalized],
            ).fetchall()
        summaries = [
            {
                "resourceId": row[0],
                "source": "logicmonitor",
                "metric": row[1],
                "unit": row[2],
                "windowStart": row[3],
                "windowEnd": row[4],
                "sampleCount": row[5],
                "coveragePercent": min(
                    100.0,
                    row[6] / max(1, history_days * 24) * 100,
                ),
                "average": row[7],
                "p95": row[8],
                "maximum": row[9],
                "lastValue": row[10],
                "lastObservedAt": row[11],
                "aggregationMethod": (
                    f"Rolling {history_days}-day LogicMonitor samples; "
                    "hour-bucket coverage."
                ),
                "lineage": {
                    "sourceSystem": "LogicMonitor",
                    "deviceId": row[12],
                    "method": "checkpointed_incremental_v1",
                    "coverageSemantics": (
                        "Distinct observed hourly buckets divided by the "
                        "governed history window."
                    ),
                },
            }
            for row in rows
        ]
        self.store_telemetry_summaries(run_id, summaries)
        return len(summaries)

    def store_telemetry_summaries(
        self,
        run_id: str,
        summaries: list[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> None:  # type: ignore[no-redef]
        if not summaries:
            return
        observed_at = observed_at or _utc_now()
        rows = [
            [
                run_id,
                observed_at,
                item["resourceId"].lower(),
                item["source"],
                item["metric"],
                item.get("unit", ""),
                item["windowStart"],
                item["windowEnd"],
                item["sampleCount"],
                item["coveragePercent"],
                item.get("average"),
                item.get("p95"),
                item.get("maximum"),
                item.get("lastValue"),
                item.get("lastObservedAt"),
                item.get("aggregationMethod", ""),
                _json_value(item.get("lineage", {})),
            ]
            for item in summaries
        ]
        with self.connect() as db:  # type: ignore[attr-defined]
            db.executemany(
                """
                INSERT INTO telemetry_metric_summaries (
                    run_id, observed_at, resource_id, source, metric, unit,
                    window_start, window_end, sample_count, coverage_percent,
                    average, p95, maximum, last_value, last_observed_at,
                    aggregation_method, lineage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def store_telemetry_attempts(
        self,
        run_id: str,
        attempts: list[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> None:  # type: ignore[no-redef]
        if not attempts:
            return
        observed_at = observed_at or _utc_now()
        with self.connect() as db:  # type: ignore[attr-defined]
            db.executemany(
                "INSERT INTO telemetry_resource_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        run_id,
                        observed_at,
                        item["resourceId"].lower(),
                        item["source"],
                        item["status"],
                        item.get("metricCount", 0),
                        str(item.get("message", ""))[:500],
                    ]
                    for item in attempts
                ],
            )

    def store_source_matches(
        self,
        run_id: str,
        matches: list[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> None:  # type: ignore[no-redef]
        if not matches:
            return
        observed_at = observed_at or _utc_now()
        with self.connect() as db:  # type: ignore[attr-defined]
            db.executemany(
                "INSERT INTO resource_source_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        run_id,
                        observed_at,
                        item["source"],
                        item["sourceResourceId"],
                        item["sourceName"],
                        item["resourceId"].lower(),
                        item["status"],
                        item["method"],
                        item["confidence"],
                        _json_value(item.get("details")),
                    ]
                    for item in matches
                ],
            )

    def replace_finops_toolkit_open_data(
        self,
        files: dict[str, Path],
        datasets: dict[str, dict[str, str]],
    ) -> dict[str, int]:  # type: ignore[no-redef]
        """Load checksum-verified Microsoft FinOps Toolkit datasets."""
        imported_at = _utc_now()
        inserts = {
            "ResourceTypes": """
                INSERT INTO finops_toolkit_resource_types
                SELECT ?, ?, lower(coalesce("ResourceType", '')),
                    coalesce("SingularDisplayName", ''),
                    coalesce("PluralDisplayName", ''),
                    coalesce("LowerSingularDisplayName", ''),
                    coalesce("LowerPluralDisplayName", ''),
                    try_cast("IsPreview" AS BOOLEAN),
                    coalesce("Description", ''), coalesce("Icon", ''),
                    CASE WHEN trim(coalesce("Links", '')) = '' THEN '[]'
                        ELSE "Links" END::JSON
                FROM _finops_toolkit_source
            """,
            "Regions": """
                INSERT INTO finops_toolkit_regions
                SELECT ?, ?, lower(coalesce("OriginalValue", '')),
                    lower(coalesce("RegionId", '')),
                    coalesce("RegionName", '')
                FROM _finops_toolkit_source
            """,
            "Services": """
                INSERT INTO finops_toolkit_services
                SELECT ?, ?, lower(coalesce("ConsumedService", '')),
                    lower(coalesce("ResourceType", '')),
                    coalesce("ServiceName", ''),
                    coalesce("ServiceCategory", ''),
                    coalesce("ServiceSubcategory", ''),
                    coalesce("PublisherName", ''),
                    coalesce("PublisherType", ''),
                    coalesce("Environment", ''),
                    coalesce("ServiceModel", '')
                FROM _finops_toolkit_source
            """,
            "PricingUnits": """
                INSERT INTO finops_toolkit_pricing_units
                SELECT ?, ?, coalesce("UnitOfMeasure", ''),
                    coalesce("AccountTypes", ''),
                    try_cast("PricingBlockSize" AS DOUBLE),
                    coalesce("DistinctUnits", '')
                FROM _finops_toolkit_source
            """,
            "CommitmentDiscountEligibility": """
                INSERT INTO finops_toolkit_commitment_eligibility
                SELECT ?, ?, lower(coalesce("MeterId", '')),
                    coalesce("x_CommitmentDiscountSpendEligibility", ''),
                    coalesce("x_CommitmentDiscountUsageEligibility", '')
                FROM _finops_toolkit_source
            """,
        }
        target_tables = {
            "ResourceTypes": "finops_toolkit_resource_types",
            "Regions": "finops_toolkit_regions",
            "Services": "finops_toolkit_services",
            "PricingUnits": "finops_toolkit_pricing_units",
            "CommitmentDiscountEligibility": (
                "finops_toolkit_commitment_eligibility"
            ),
        }
        counts: dict[str, int] = {}
        with self.connect() as db:  # type: ignore[attr-defined]
            db.execute("BEGIN TRANSACTION")
            try:
                for dataset, path in files.items():
                    metadata = datasets[dataset]
                    version = metadata["toolkitVersion"]
                    relation = db.read_csv(
                        str(path),
                        header=True,
                        all_varchar=True,
                    )
                    relation.create_view(
                        "_finops_toolkit_source",
                        replace=True,
                    )
                    row_count = db.execute(
                        "SELECT count(*) FROM _finops_toolkit_source"
                    ).fetchone()[0]
                    db.execute(
                        f"DELETE FROM {target_tables[dataset]} "
                        "WHERE toolkit_version = ?",
                        [version],
                    )
                    db.execute(inserts[dataset], [version, imported_at])
                    db.execute(
                        """
                        DELETE FROM finops_toolkit_dataset_versions
                        WHERE dataset = ? AND toolkit_version = ?
                        """,
                        [dataset, version],
                    )
                    db.execute(
                        """
                        INSERT INTO finops_toolkit_dataset_versions VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        [
                            dataset,
                            version,
                            metadata["upstreamCommit"],
                            metadata["sourceUrl"],
                            metadata["sha256"],
                            imported_at,
                            row_count,
                            metadata["license"],
                        ],
                    )
                    counts[dataset] = row_count
                db.execute("COMMIT")
            except (duckdb.Error, FileLockTimeout, OSError) as _error:
                _logger.warning("database transaction failed; rolling back", exc_info=_error)
                try:
                    db.execute("ROLLBACK")
                except Exception as _rollback_error:  # noqa: BLE001 - rollback must not mask original
                    _logger.warning("rollback also failed", exc_info=_rollback_error)
                raise RuntimeError("database transaction failed") from _error
        return counts

    def finops_toolkit_status(self) -> dict[str, Any]:  # type: ignore[no-redef]
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                """
                SELECT dataset, toolkit_version, upstream_commit, source_url,
                       sha256, imported_at, row_count, license
                FROM finops_toolkit_dataset_versions
                QUALIFY row_number() OVER (
                    PARTITION BY dataset ORDER BY imported_at DESC
                ) = 1
                ORDER BY dataset
                """
            ).fetchall()
        return {
            "datasets": [
                {
                    "dataset": row[0],
                    "toolkitVersion": row[1],
                    "upstreamCommit": row[2],
                    "sourceUrl": row[3],
                    "sha256": row[4],
                    "importedAt": row[5].isoformat(),
                    "rowCount": row[6],
                    "license": row[7],
                }
                for row in rows
            ]
        }

    def telemetry_status(self) -> dict[str, Any]:  # type: ignore[no-redef]
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            runs = db.execute(
                """
                SELECT source, id, trigger, started_at, completed_at, status,
                       processed_count, message
                FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY source ORDER BY started_at DESC
                    ) AS rank
                    FROM telemetry_runs
                ) WHERE rank = 1 ORDER BY source
                """
            ).fetchall()
            vm_count = db.execute(
                "SELECT count(*) FROM resources_current WHERE resource_type = 'microsoft.compute/virtualmachines'"
            ).fetchone()[0]
            azure_count = db.execute(
                "SELECT count(DISTINCT resource_id) FROM telemetry_metric_summaries_current WHERE source = 'azure_monitor'"
            ).fetchone()[0]
            logicmonitor_metric_count = db.execute(
                """
                SELECT count(DISTINCT resource_id)
                FROM telemetry_metric_summaries_current
                WHERE source = 'logicmonitor'
                """
            ).fetchone()[0]
            checkpoint_status = db.execute(
                """
                SELECT count(*), min(collected_through),
                       max(collected_through)
                FROM telemetry_collection_checkpoints
                WHERE source = 'logicmonitor'
                  AND stream = 'performance'
                """
            ).fetchone()
            attempt_counts = dict(
                db.execute(
                    """
                    SELECT status, count(*) FROM telemetry_resource_attempts_current
                    WHERE source = 'azure_monitor' GROUP BY status
                    """
                ).fetchall()
            )
            match_counts = dict(
                db.execute(
                    """
                    SELECT status, count(*) FROM resource_source_matches_current
                    WHERE source = 'logicmonitor' GROUP BY status
                    """
                ).fetchall()
            )
            subscription_rows = db.execute(
                """
                WITH vms AS (
                    SELECT
                        lower(resource_id) AS resource_id,
                        subscription_id,
                        any_value(NULLIF(subscription_name, ''))
                            AS subscription_name
                    FROM resources_current
                    WHERE resource_type =
                        'microsoft.compute/virtualmachines'
                    GROUP BY resource_id, subscription_id
                ),
                azure_metrics AS (
                    SELECT DISTINCT resource_id
                    FROM telemetry_metric_summaries_current
                    WHERE source = 'azure_monitor'
                ),
                logicmonitor_metrics AS (
                    SELECT DISTINCT resource_id
                    FROM telemetry_metric_summaries_current
                    WHERE source = 'logicmonitor'
                ),
                logicmonitor_matches AS (
                    SELECT DISTINCT resource_id
                    FROM resource_source_matches_current
                    WHERE source = 'logicmonitor' AND status = 'matched'
                )
                SELECT
                    vm.subscription_id,
                    COALESCE(
                        any_value(vm.subscription_name),
                        vm.subscription_id
                    ) AS subscription_name,
                    count(*) AS virtual_machines,
                    count(attempt.resource_id) AS azure_attempted,
                    count(azure_metrics.resource_id) AS azure_covered,
                    count(*) FILTER (WHERE attempt.status = 'no_data')
                        AS azure_no_data,
                    count(*) FILTER (WHERE attempt.status = 'error')
                        AS azure_errors,
                    count(logicmonitor_matches.resource_id) AS lm_matched,
                    count(logicmonitor_metrics.resource_id) AS lm_covered,
                    count(*) FILTER (
                        WHERE rightsizing.status = 'candidate'
                    ) AS candidates,
                    count(*) FILTER (
                        WHERE rightsizing.status = 'warming_up'
                    ) AS warming_up,
                    count(*) FILTER (
                        WHERE rightsizing.status IN (
                            'insufficient_telemetry', 'partial_telemetry'
                        )
                    ) AS insufficient
                FROM vms AS vm
                LEFT JOIN telemetry_resource_attempts_current AS attempt
                  ON attempt.resource_id = vm.resource_id
                 AND attempt.source = 'azure_monitor'
                LEFT JOIN azure_metrics
                  ON azure_metrics.resource_id = vm.resource_id
                LEFT JOIN logicmonitor_matches
                  ON logicmonitor_matches.resource_id = vm.resource_id
                LEFT JOIN logicmonitor_metrics
                  ON logicmonitor_metrics.resource_id = vm.resource_id
                LEFT JOIN rightsizing_recommendations_current AS rightsizing
                  ON rightsizing.resource_id = vm.resource_id
                GROUP BY vm.subscription_id
                ORDER BY virtual_machines DESC, subscription_name
                """
            ).fetchall()
        return {
            "virtualMachineCount": vm_count,
            "azureMonitorCovered": azure_count,
            "azureMonitorAttempted": sum(attempt_counts.values()),
            "azureMonitorNoData": attempt_counts.get("no_data", 0),
            "azureMonitorErrors": attempt_counts.get("error", 0),
            "logicMonitorMatched": match_counts.get("matched", 0),
            "logicMonitorAmbiguous": match_counts.get("ambiguous", 0),
            "logicMonitorUnmatched": match_counts.get("unmatched", 0),
            "logicMonitorMetricCovered": logicmonitor_metric_count,
            "logicMonitorCheckpointed": checkpoint_status[0] or 0,
            "logicMonitorOldestCheckpoint": (
                checkpoint_status[1].isoformat()
                if checkpoint_status[1]
                else None
            ),
            "logicMonitorNewestCheckpoint": (
                checkpoint_status[2].isoformat()
                if checkpoint_status[2]
                else None
            ),
            "bySubscription": [
                {
                    "subscriptionId": row[0],
                    "subscriptionName": row[1],
                    "virtualMachines": row[2],
                    "azureMonitorAttempted": row[3],
                    "azureMonitorCovered": row[4],
                    "azureMonitorNoData": row[5],
                    "azureMonitorErrors": row[6],
                    "logicMonitorMatched": row[7],
                    "logicMonitorCovered": row[8],
                    "candidates": row[9],
                    "warmingUp": row[10],
                    "insufficient": row[11],
                }
                for row in subscription_rows
            ],
            "runs": [
                {
                    "source": row[0],
                    "id": row[1],
                    "trigger": row[2],
                    "startedAt": row[3].isoformat(),
                    "completedAt": row[4].isoformat() if row[4] else None,
                    "status": row[5],
                    "processedCount": row[6],
                    "message": row[7],
                }
                for row in runs
            ],
        }

    def fleet_telemetry(
        self,
        *,
        subscription_id: str = "",
        resource_type: str = "microsoft.compute/virtualmachines",
        region: str = "",
        search: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:  # type: ignore[no-redef]
        """Utilization summaries for many resources in one governed read.

        The per-resource ``resource_telemetry`` path costs one call per VM,
        which exhausts an assistant's tool budget on any fleet-scale
        question. This returns the same governed metric summaries pivoted to
        one row per resource, with cost and coverage attached, so fleet
        analysis fits in a single call.
        """
        limit = max(1, min(int(limit), 500))
        conditions = ["1 = 1"]
        params: list[Any] = []
        if subscription_id:
            conditions.append("resource.subscription_id = ?")
            params.append(subscription_id.lower())
        if resource_type:
            conditions.append("lower(resource.resource_type) = ?")
            params.append(resource_type.lower())
        if region:
            conditions.append("lower(resource.region) = ?")
            params.append(region.lower())
        if search:
            conditions.append(
                "(resource.name ILIKE ? OR resource.resource_group ILIKE ?)"
            )
            token = f"%{search}%"
            params.extend([token, token])
        where = " AND ".join(conditions)
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            rows = db.execute(
                f"""
                WITH resource_costs AS (
                    SELECT lower(resource_id) AS resource_id,
                           SUM(CASE WHEN cost_type = 'ActualCost'
                               THEN amount END) AS actual_cost,
                           any_value(currency) AS currency
                    FROM costs_current
                    GROUP BY lower(resource_id)
                ),
                metrics AS (
                    -- Metric names match the canonical governed names used by
                    -- compute_rightsizing_recommendations, so fleet analysis
                    -- and right-sizing read identical evidence.
                    SELECT
                        resource_id,
                        MAX(CASE WHEN lower(metric) = 'percentage cpu'
                            THEN average END) AS cpu_avg,
                        MAX(CASE WHEN lower(metric) = 'percentage cpu'
                            THEN p95 END) AS cpu_p95,
                        MAX(CASE WHEN lower(metric) = 'percentage cpu'
                            THEN maximum END) AS cpu_max,
                        MAX(CASE WHEN lower(metric) = 'memory used percentage'
                            THEN average END) AS memory_avg,
                        MAX(CASE WHEN lower(metric) = 'memory used percentage'
                            THEN p95 END) AS memory_p95,
                        MAX(CASE WHEN lower(metric) = 'network in total'
                            THEN p95 END) AS network_in_p95,
                        MAX(CASE WHEN lower(metric) = 'network out total'
                            THEN p95 END) AS network_out_p95,
                        MAX(coverage_percent) AS coverage_percent,
                        MAX(sample_count) AS sample_count,
                        MIN(window_start) AS window_start,
                        MAX(window_end) AS window_end,
                        string_agg(DISTINCT source, ',') AS sources
                    FROM telemetry_metric_summaries_current
                    GROUP BY resource_id
                )
                SELECT
                    resource.resource_id,
                    resource.name,
                    resource.resource_type,
                    resource.sku,
                    resource.region,
                    resource.subscription_id,
                    resource.subscription_name,
                    resource.resource_group,
                    metrics.cpu_avg, metrics.cpu_p95, metrics.cpu_max,
                    metrics.memory_avg, metrics.memory_p95,
                    metrics.network_in_p95, metrics.network_out_p95,
                    metrics.coverage_percent, metrics.sample_count,
                    metrics.window_start, metrics.window_end, metrics.sources,
                    cost.actual_cost, cost.currency
                FROM resources_current AS resource
                LEFT JOIN metrics
                  ON metrics.resource_id = lower(resource.resource_id)
                LEFT JOIN resource_costs AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                WHERE {where}
                ORDER BY cost.actual_cost DESC NULLS LAST, resource.name
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            total = db.execute(
                f"""
                SELECT count(*) FROM resources_current AS resource
                WHERE {where}
                """,
                params,
            ).fetchone()[0]
        def number(value: Any, digits: int = 1) -> float | None:
            return round(float(value), digits) if value is not None else None

        items = []
        covered = 0
        for row in rows:
            # CPU p95 is the metric right-sizing decisions rest on; a resource
            # with a coverage row but no CPU series is not usable evidence.
            has_cpu = row[9] is not None
            if has_cpu:
                covered += 1
            items.append(
                {
                    "resourceId": row[0],
                    "name": row[1],
                    "resourceType": row[2],
                    "sku": row[3] or "",
                    "region": row[4] or "",
                    "subscriptionId": row[5],
                    "subscriptionName": row[6] or "",
                    "resourceGroup": row[7] or "",
                    "cpuAverage": number(row[8]),
                    "cpuP95": number(row[9]),
                    "cpuMaximum": number(row[10]),
                    "memoryAverage": number(row[11]),
                    "memoryP95": number(row[12]),
                    "networkInP95Bytes": number(row[13]),
                    "networkOutP95Bytes": number(row[14]),
                    "coveragePercent": number(row[15]),
                    "sampleCount": int(row[16]) if row[16] is not None else 0,
                    "windowStart": row[17].isoformat() if row[17] else None,
                    "windowEnd": row[18].isoformat() if row[18] else None,
                    "telemetrySources": (
                        sorted(str(row[19]).split(",")) if row[19] else []
                    ),
                    "actualMonthlyCost": number(row[20], 2) if row[20] is not None else None,
                    "costCurrency": row[21] or "",
                    "costAvailable": row[20] is not None,
                    "costSource": ("costs_current" if row[20] is not None else "unavailable"),
                    "telemetryStatus": (
                        "covered" if has_cpu else "no_cpu_evidence"
                    ),
                }
            )
        return {
            "items": items,
            "returned": len(items),
            "matching": total,
            "truncated": total > len(items),
            "cpuEvidenceCount": covered,
            "filters": {
                "subscriptionId": subscription_id,
                "resourceType": resource_type,
                "region": region,
                "search": search,
                "limit": limit,
            },
            "limitations": (
                []
                if total <= len(items)
                else [
                    f"{total:,} resources match; the {len(items):,} highest-cost "
                    "are returned. Narrow by subscription or region for the rest."
                ]
            ),
        }

    def resource_telemetry(self, resource_id: str) -> dict[str, Any]:  # type: ignore[no-redef]
        normalized = resource_id.lower()
        with self.connect(read_only=True) as db:  # type: ignore[attr-defined]
            metrics = db.execute(
                """
                SELECT source, metric, unit, window_start, window_end,
                       sample_count, coverage_percent, average, p95, maximum,
                       last_value, last_observed_at, aggregation_method,
                       lineage_json
                FROM telemetry_metric_summaries_current
                WHERE resource_id = ?
                ORDER BY source, metric
                """,
                [normalized],
            ).fetchall()
            matches = db.execute(
                """
                SELECT source, source_resource_id, source_name, status, method,
                       confidence, observed_at
                FROM resource_source_matches_current
                WHERE resource_id = ?
                ORDER BY source, source_name
                """,
                [normalized],
            ).fetchall()
            azure_attempt = db.execute(
                """
                SELECT status, metric_count, message, observed_at
                FROM telemetry_resource_attempts_current
                WHERE resource_id = ? AND source = 'azure_monitor'
                """,
                [normalized],
            ).fetchone()
            logicmonitor_attempt = db.execute(
                """
                SELECT status, metric_count, message, observed_at
                FROM telemetry_resource_attempts_current
                WHERE resource_id = ? AND source = 'logicmonitor'
                """,
                [normalized],
            ).fetchone()
            rightsizing = db.execute(
                """
                SELECT computed_at, kind, status, current_sku, target_sku,
                       evidence_window_days, coverage_flag, telemetry_source,
                       cpu_p95, cpu_maximum, network_in_p95, network_out_p95,
                       metric_coverage_percent, estimated_monthly_saving,
                       currency, value_source, reason, evidence_json,
                       method_version
                FROM rightsizing_recommendations_current
                WHERE resource_id = ?
                """,
                [normalized],
            ).fetchone()
            cost_rows = db.execute(
                """
                SELECT usage_date, cost_type, sum(amount),
                       any_value(NULLIF(currency, ''))
                FROM daily_cost_history
                WHERE resource_id = ?
                  AND usage_date >= current_date - INTERVAL 35 DAY
                  AND cost_type IN ('ActualCost', 'AmortizedCost')
                GROUP BY usage_date, cost_type
                ORDER BY usage_date
                """,
                [normalized],
            ).fetchall()
            sample_rows = db.execute(
                """
                SELECT source, metric, unit,
                       date_trunc('hour', observed_at) AS bucket,
                       avg(value)
                FROM telemetry_metric_samples
                WHERE resource_id = ?
                  AND observed_at >= now() - INTERVAL 7 DAY
                GROUP BY source, metric, unit, bucket
                ORDER BY source, metric, bucket
                """,
                [normalized],
            ).fetchall()
        cost_daily: dict[str, dict[str, Any]] = {}
        for usage_date, cost_type, amount, cost_currency in cost_rows:
            entry = cost_daily.setdefault(
                usage_date.isoformat(),
                {
                    "date": usage_date.isoformat(),
                    "actual": None,
                    "amortized": None,
                    "currency": cost_currency or "",
                },
            )
            key = "actual" if cost_type == "ActualCost" else "amortized"
            entry[key] = round(float(amount or 0), 2)
        sample_series: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for source, metric, unit, bucket, value in sample_rows:
            sample_series.setdefault((source, metric, unit), []).append(
                {
                    "t": bucket.isoformat(),
                    "value": round(float(value or 0), 2),
                }
            )
        return {
            "resourceId": resource_id,
            "costDaily": list(cost_daily.values()),
            "sampleSeries": [
                {
                    "source": source,
                    "metric": metric,
                    "unit": unit,
                    "points": points,
                }
                for (source, metric, unit), points in sample_series.items()
            ],
            "metrics": [
                {
                    "source": row[0],
                    "metric": row[1],
                    "unit": row[2],
                    "windowStart": row[3].isoformat(),
                    "windowEnd": row[4].isoformat(),
                    "sampleCount": row[5],
                    "coveragePercent": round(row[6], 1),
                    "average": row[7],
                    "p95": row[8],
                    "maximum": row[9],
                    "lastValue": row[10],
                    "lastObservedAt": row[11].isoformat() if row[11] else None,
                    "aggregationMethod": row[12] or "",
                    "lineage": json.loads(row[13] or "{}"),
                }
                for row in metrics
            ],
            "matches": [
                {
                    "source": row[0],
                    "sourceResourceId": row[1],
                    "sourceName": row[2],
                    "status": row[3],
                    "method": row[4],
                    "confidence": row[5],
                    "observedAt": row[6].isoformat(),
                }
                for row in matches
            ],
            "azureMonitorAttempt": {
                "status": azure_attempt[0],
                "metricCount": azure_attempt[1],
                "message": azure_attempt[2],
                "observedAt": azure_attempt[3].isoformat(),
            } if azure_attempt else None,
            "logicMonitorAttempt": {
                "status": logicmonitor_attempt[0],
                "metricCount": logicmonitor_attempt[1],
                "message": logicmonitor_attempt[2],
                "observedAt": logicmonitor_attempt[3].isoformat(),
            } if logicmonitor_attempt else None,
            "rightsizingAssessment": {
                "computedAt": rightsizing[0].isoformat(),
                "kind": rightsizing[1],
                "status": rightsizing[2],
                "currentSku": rightsizing[3],
                "targetSku": rightsizing[4],
                "evidenceWindowDays": rightsizing[5],
                "coverageFlag": rightsizing[6],
                "telemetrySource": rightsizing[7],
                "cpuP95": rightsizing[8],
                "cpuMaximum": rightsizing[9],
                "networkInP95": rightsizing[10],
                "networkOutP95": rightsizing[11],
                "metricCoveragePercent": rightsizing[12],
                "estimatedMonthlySaving": rightsizing[13],
                "currency": rightsizing[14],
                "valueSource": rightsizing[15],
                "reason": rightsizing[16],
                "evidence": json.loads(rightsizing[17] or "{}"),
                "methodVersion": rightsizing[18],
            } if rightsizing else None,
        }
