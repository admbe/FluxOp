"""Rightsizing / plan-board domain extracted from the god object (see #13).

FluxDatabase delegates to this mixin so the public surface
``from api.database import FluxDatabase`` is unchanged. Covers the
rightsizing kanban boards/buckets/plan lifecycle (``rightsizing_boards``,
``rightsizing_plan_board``, ``replace_flux_proposal_board``, duplicate/save/
delete/assign, ``compute/ensure_rightsizing_recommendations``, dossier, etc.)
plus the two small helpers the board code owns: ``_plan_evidence_fingerprint``
(invalidated on snapshot swap) and ``planned_rightsizing_monthly_savings``
(scoped to the primary board for the fiscal outlook).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from .pricing import price_profile
from .rightsizing import (
    METHOD_VERSION as RIGHTSIZING_METHOD_VERSION,
    assess_resource,
)
from .valuation import monthly_run_rate

_logger = logging.getLogger("flux.database")


def _utc_now():
    from .database import utc_now as _orig_utc_now
    return _orig_utc_now()


# Legacy direct name used by the extracted methods (kept as module-level alias
# so the verbatim method bodies keep calling utc_now() without churn).
def utc_now():  # type: ignore[no-redef]
    return _utc_now()


def _json_value(value: Any) -> str:
    from .database import json_value
    return json_value(value)


# json_value alias for the one call-site that uses the bare name
def json_value(value: Any) -> str:  # type: ignore[no-redef]
    return _json_value(value)

class RightsizingMixin:

    # Keep bucket constants next to the mixin that owns them.
    RIGHTSIZING_SPECIAL_BUCKETS = (
        "__unassigned__",
        "__nodata__",
        "__review__",
        "__savingsplan__",
        "__decommission__",
        "__excluded__",
    )

    FLUX_PROPOSAL_ACTOR = "flux"

    def _plan_evidence_fingerprint(self) -> tuple[Any, ...]:
        """Cheap generation marker for the plan's analytical evidence.

        Counts and high-water marks over the four inputs the evidence query
        reads. Sub-millisecond against either the snapshot or the mutable
        file, and any data landing moves at least one component.
        """
        with self.connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT
                    (SELECT count(*) FROM focus_export_manifests),
                    (SELECT coalesce(CAST(max(imported_at) AS VARCHAR), '')
                       FROM focus_export_manifests),
                    (SELECT count(*) FROM resources_current),
                    (SELECT count(*) FROM rightsizing_recommendations_current),
                    (SELECT count(*)
                       FROM telemetry_metric_summaries_current),
                    (SELECT count(*) FROM price_sheet_current)
                """
            ).fetchone()
        return tuple(row) + (str(self._read_snapshot_path or ""),)

    @property
    def planned_rightsizing_monthly_savings(self) -> float:
        """Sum of planner-entered bucket reference savings, for the outlook.

        Scoped to the primary board only: a scratch or exploration board
        (see rightsizing_boards) must never silently inflate the fiscal
        outlook just by existing.
        """
        with self.operational_connect(read_only=True) as db:
            row = db.execute(
                """
                SELECT coalesce(sum(bucket.ref_monthly_savings), 0)
                FROM rightsizing_plan_buckets AS bucket
                JOIN rightsizing_boards AS board
                  ON board.id = bucket.board_id
                WHERE board.is_primary = TRUE
                """
            ).fetchone()
        return float(row[0] or 0) if row else 0.0

    @staticmethod
    def _resolve_rightsizing_board_readonly(
        db: Any, board_id: str
    ) -> str | None:
        """Resolve a board id using only reads; None means "would need to
        write" (no board_id given and either no boards exist yet, or none
        is flagged primary).

        DuckDB's development (non-PostgreSQL) operational store opens a
        fresh connection per call with no locking shared with the
        analytical store's connection lock, so a writable connection
        racing a concurrent read-only one against the same file raises
        "different configuration" errors -- reproduced live pairing this
        with the Overview page's concurrent polling. Callers that only
        ever need to read (the plan board, the log) must stay read-only
        in the overwhelmingly common case where a board already exists;
        only the one-time cold-start/repair path below escalates.
        """
        if board_id:
            return board_id
        row = db.execute(
            "SELECT id FROM rightsizing_boards WHERE is_primary = TRUE "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            return str(row[0])
        row = db.execute(
            "SELECT id FROM rightsizing_boards ORDER BY created_at LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None

    def _resolve_rightsizing_board(self, db: Any, board_id: str) -> str:
        """Resolve a possibly-blank board id to a real one.

        A blank id -- every pre-boards caller, and a switcher that hasn't
        loaded yet -- means "the primary board", lazily created here on
        first use so a fresh install needs no separate seed step. Takes an
        already-open, writable operational connection; the caller commits.
        Prefer _resolve_rightsizing_board_readonly when the caller doesn't
        otherwise need a writable connection (see its docstring for why).
        """
        if board_id:
            return board_id
        row = db.execute(
            "SELECT id FROM rightsizing_boards WHERE is_primary = TRUE "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            return str(row[0])
        row = db.execute(
            "SELECT id FROM rightsizing_boards ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            # A board exists but somehow none is flagged primary; promote
            # the oldest rather than creating a second board.
            board_id = str(row[0])
            db.execute(
                "UPDATE rightsizing_boards SET is_primary = TRUE WHERE id = ?",
                [board_id],
            )
            return board_id
        new_id = str(uuid4())
        now = utc_now()
        db.execute(
            """
            INSERT INTO rightsizing_boards (
                id, name, description, is_primary, created_by,
                created_at, updated_at
            ) VALUES (?, 'Default', '', TRUE, 'system', ?, ?)
            """,
            [new_id, now, now],
        )
        return new_id

    def _ensure_rightsizing_board(self, board_id: str) -> str:
        """Writable fallback for the cold-start/repair case only -- opens
        its own operational connection, so callers only pay for it when
        _resolve_rightsizing_board_readonly returned None."""
        with self.operational_connect() as db:
            resolved = self._resolve_rightsizing_board(db, board_id)
            db.commit()
        return resolved

    @staticmethod
    def _rightsizing_board_row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "name": str(row[1]),
            "description": str(row[2] or ""),
            "isPrimary": bool(row[3]),
            "createdBy": str(row[4] or ""),
            "createdAt": row[5].isoformat() if row[5] else None,
            "updatedAt": row[6].isoformat() if row[6] else None,
        }

    def rightsizing_boards(self) -> list[dict[str, Any]]:
        """Every board, with lightweight per-board counts for a switcher."""
        with self.operational_connect(read_only=True) as db:
            any_board = db.execute(
                "SELECT 1 FROM rightsizing_boards LIMIT 1"
            ).fetchone()
            rows = (
                db.execute(
                    "SELECT id, name, description, is_primary, created_by, "
                    "created_at, updated_at FROM rightsizing_boards "
                    "ORDER BY is_primary DESC, created_at"
                ).fetchall()
                if any_board
                else []
            )
            bucket_counts = dict(
                db.execute(
                    "SELECT board_id, count(*) FROM rightsizing_plan_buckets "
                    "GROUP BY board_id"
                ).fetchall()
            )
            assigned_counts = dict(
                db.execute(
                    """
                    SELECT board_id, count(*)
                    FROM rightsizing_plan_assignments
                    WHERE bucket_key NOT IN ('__unassigned__', '__nodata__')
                    GROUP BY board_id
                    """
                ).fetchall()
            )
        if not any_board:
            # Cold start only: bootstrap the Default board, then this is a
            # single, cheap re-query (see rightsizing_plan_board's
            # docstring for why the writable path stays isolated).
            self._ensure_rightsizing_board("")
            with self.operational_connect(read_only=True) as db:
                rows = db.execute(
                    "SELECT id, name, description, is_primary, created_by, "
                    "created_at, updated_at FROM rightsizing_boards "
                    "ORDER BY is_primary DESC, created_at"
                ).fetchall()
        boards = [self._rightsizing_board_row(row) for row in rows]
        for board in boards:
            board["bucketCount"] = int(bucket_counts.get(board["id"], 0))
            board["assignedCount"] = int(assigned_counts.get(board["id"], 0))
        return boards

    def create_rightsizing_board(
        self, name: str, description: str = "", actor: str = ""
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("A board name is required.")
        description = description.strip()
        now = utc_now()
        board_id = str(uuid4())
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO rightsizing_boards (
                    id, name, description, is_primary, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, FALSE, ?, ?, ?)
                """,
                [board_id, name, description, actor, now, now],
            )
            db.commit()
        return {
            "id": board_id,
            "name": name,
            "description": description,
            "isPrimary": False,
            "createdBy": actor,
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "bucketCount": 0,
            "assignedCount": 0,
        }

    def rename_rightsizing_board(
        self, board_id: str, name: str, description: str = "",
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("A board name is required.")
        description = description.strip()
        now = utc_now()
        with self.operational_connect() as db:
            self._assert_editable_board(db, board_id)
            existing = db.execute(
                "SELECT id FROM rightsizing_boards WHERE id = ?", [board_id]
            ).fetchone()
            if not existing:
                raise ValueError(f"Board {board_id!r} does not exist.")
            db.execute(
                "UPDATE rightsizing_boards SET name = ?, description = ?, "
                "updated_at = ? WHERE id = ?",
                [name, description, now, board_id],
            )
            db.commit()
        return {"id": board_id, "name": name, "description": description}

    def set_primary_rightsizing_board(self, board_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            self._assert_editable_board(db, board_id)
            existing = db.execute(
                "SELECT id FROM rightsizing_boards WHERE id = ?", [board_id]
            ).fetchone()
            if not existing:
                raise ValueError(f"Board {board_id!r} does not exist.")
            db.execute(
                "UPDATE rightsizing_boards SET is_primary = FALSE, "
                "updated_at = ? WHERE is_primary = TRUE",
                [now],
            )
            db.execute(
                "UPDATE rightsizing_boards SET is_primary = TRUE, "
                "updated_at = ? WHERE id = ?",
                [now, board_id],
            )
            db.commit()
        return {"id": board_id, "isPrimary": True}

    def delete_rightsizing_board(self, board_id: str) -> dict[str, Any]:
        """Delete a board and everything on it.

        Refuses to remove the primary board: the fiscal outlook and the
        resource evidence dossier always read the primary board, so leaving
        none flagged primary would silently zero them out rather than
        raising. Promote another board first.
        """
        with self.operational_connect() as db:
            self._assert_editable_board(db, board_id)
            row = db.execute(
                "SELECT is_primary FROM rightsizing_boards WHERE id = ?",
                [board_id],
            ).fetchone()
            if not row:
                raise ValueError(f"Board {board_id!r} does not exist.")
            if bool(row[0]):
                raise PermissionError(
                    "Set another board as primary before deleting this "
                    "one -- the fiscal outlook and AI evidence always "
                    "track the primary board."
                )
            buckets_removed = db.execute(
                "SELECT count(*) FROM rightsizing_plan_buckets "
                "WHERE board_id = ?",
                [board_id],
            ).fetchone()[0]
            assignments_removed = db.execute(
                "SELECT count(*) FROM rightsizing_plan_assignments "
                "WHERE board_id = ?",
                [board_id],
            ).fetchone()[0]
            db.execute(
                "DELETE FROM rightsizing_plan_log WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_assignments WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_boards WHERE id = ?", [board_id]
            )
            db.commit()
        return {
            "removed": board_id,
            "bucketsRemoved": int(buckets_removed),
            "assignmentsRemoved": int(assignments_removed),
        }

    @staticmethod
    def _rightsizing_bucket_row(row: Any) -> dict[str, Any]:
        return {
            "bucketKey": str(row[0]),
            "boardId": str(row[1]),
            "region": str(row[2]),
            "sku": str(row[3]),
            "strategy": str(row[4] or ""),
            "source": str(row[5] or ""),
            "refQuantity": int(row[6]) if row[6] is not None else None,
            "refMonthlyPayg": float(row[7]) if row[7] is not None else None,
            "refMonthlyRi1y": float(row[8]) if row[8] is not None else None,
            "refRi1yUpfront": float(row[9]) if row[9] is not None else None,
            "refMonthlySp1y": float(row[10]) if row[10] is not None else None,
            "refMonthlySavings": float(row[11]) if row[11] is not None else None,
            "refReservationCheck": str(row[12] or ""),
            "note": str(row[13] or ""),
            "createdBy": str(row[14] or ""),
            "createdAt": row[15].isoformat() if row[15] else None,
            "updatedAt": row[16].isoformat() if row[16] else None,
        }

    def rightsizing_plan_board(self, board_id: str = "") -> dict[str, Any]:
        """The full planning board: live VM seed joined with plan state.

        The VM list always comes from current inventory (snapshot reads on
        the web), never from a frozen export -- a plan drawn over stale VMs
        quietly plans machines that no longer exist. Plan state lives in the
        operational store so every planner sees the same board. A blank
        board_id resolves to the primary board (see rightsizing_boards).
        """
        # The analytical evidence below is the expensive part of this
        # endpoint (~19s TTFB measured in production): a multi-CTE scan
        # over the FOCUS charge history on every page open. The evidence
        # only changes when new data lands, so memoize the assembled
        # payload against a cheap data fingerprint.
        fingerprint = self._plan_evidence_fingerprint()
        cached = self._plan_vms_cache
        cache_hit = cached is not None and cached[0] == fingerprint
        vm_rows: list[Any] = []
        if not cache_hit:
            with self.connect(read_only=True) as db:
                vm_rows = db.execute(
                    """
                    WITH cpu AS (
                        SELECT resource_id, p95, coverage_percent, source,
                               window_days
                        FROM (
                            SELECT resource_id, p95, coverage_percent, source,
                                greatest(
                                    date_diff('day', window_start, window_end) + 1,
                                    1
                                ) AS window_days,
                                row_number() OVER (
                                    PARTITION BY resource_id
                                    ORDER BY CASE source
                                        WHEN 'azure_monitor' THEN 1 ELSE 2 END,
                                        observed_at DESC
                                ) AS source_rank
                            FROM telemetry_metric_summaries_current
                            WHERE lower(metric) = 'percentage cpu'
                        )
                        WHERE source_rank = 1
                    ), focus_manifest_rank AS (
                        SELECT manifest_id,
                               row_number() OVER (
                                   PARTITION BY subscription_id,
                                       date_trunc('month', period_start)
                                   ORDER BY period_end DESC, imported_at DESC
                               ) AS manifest_rank
                        FROM focus_export_manifests
                        WHERE status = 'imported'
                    ), focus_vm_charges AS (
                        SELECT cost.*
                        FROM focus_cost_charges AS cost
                        JOIN focus_manifest_rank AS manifest
                          ON manifest.manifest_id = cost.manifest_id
                         AND manifest.manifest_rank = 1
                        WHERE lower(cost.service_name) = 'virtual machines'
                          AND lower(cost.meter_category) = 'virtual machines'
                          AND lower(cost.service_category) = 'compute'
                          AND cost.billing_currency = (
                              SELECT billing_currency
                              FROM focus_cost_charges
                              WHERE lower(service_name) = 'virtual machines'
                                AND lower(meter_category) = 'virtual machines'
                                AND lower(service_category) = 'compute'
                              GROUP BY billing_currency
                              ORDER BY sum(abs(COALESCE(list_cost, billed_cost))) DESC
                              LIMIT 1
                          )
                    ), focus_bounds AS (
                        SELECT
                            greatest(
                                max(CAST(charge_period_start AS DATE))
                                    - INTERVAL 29 DAY,
                                min(CAST(charge_period_start AS DATE))
                            ) AS window_start,
                            max(CAST(charge_period_start AS DATE)) AS window_end
                        FROM focus_vm_charges
                    ), ps_hourly AS (
                        -- Price-sheet on-demand hourly equivalent per meter: the
                        -- only valid baseline for Committed (RI-covered) rows,
                        -- whose list AND contracted costs both export as zero.
                        SELECT meter_id,
                               arg_max(
                                   unit_price / NULLIF(CAST(
                                       regexp_extract(unit_of_measure, '(\\d+)', 1)
                                       AS DOUBLE), 0),
                                   try_strptime(effective_date,
                                                '%m/%d/%Y %H:%M:%S')
                               ) AS on_demand_hourly
                        FROM price_sheet_current
                        WHERE price_type = 'Consumption'
                          AND unit_of_measure ILIKE '%hour%'
                        GROUP BY meter_id
                    ), focus AS (
                        -- Baseline ladder. CSP billing exports list_cost = 0 on
                        -- 83-96% of production rows while contracted_cost is
                        -- populated on ~95% (and equals list in this tenant), so
                        -- a bare list_cost baseline starves reconciliation:
                        -- list > contracted > derived list > derived contracted >
                        -- price-sheet on-demand (Committed rows) > billed.
                        SELECT lower(cost.resource_id) AS resource_id,
                               mode(
                                   CASE
                                       WHEN cost.list_cost IS NOT NULL
                                        AND abs(cost.list_cost) > 0.000000001
                                       THEN 'list'
                                       WHEN cost.contracted_cost IS NOT NULL
                                        AND abs(cost.contracted_cost) > 0.000000001
                                       THEN 'contracted'
                                       WHEN cost.pricing_quantity IS NOT NULL
                                        AND cost.list_unit_price IS NOT NULL
                                        AND cost.list_unit_price > 0
                                       THEN 'derived_list'
                                       WHEN cost.pricing_quantity IS NOT NULL
                                        AND cost.contracted_unit_price IS NOT NULL
                                        AND cost.contracted_unit_price > 0
                                       THEN 'derived_contracted'
                                       WHEN lower(cost.pricing_category)
                                            = 'committed'
                                        AND cost.pricing_quantity IS NOT NULL
                                        AND ps.on_demand_hourly IS NOT NULL
                                       THEN 'price_sheet_on_demand'
                                       ELSE 'billed'
                                   END
                               ) AS baseline_source,
                               sum(
                                   CASE
                                       WHEN cost.list_cost IS NOT NULL
                                        AND abs(cost.list_cost) > 0.000000001
                                       THEN cost.list_cost
                                       WHEN cost.contracted_cost IS NOT NULL
                                        AND abs(cost.contracted_cost) > 0.000000001
                                       THEN cost.contracted_cost
                                       WHEN cost.pricing_quantity IS NOT NULL
                                        AND cost.list_unit_price IS NOT NULL
                                        AND cost.list_unit_price > 0
                                       THEN cost.pricing_quantity
                                            * cost.list_unit_price
                                       WHEN cost.pricing_quantity IS NOT NULL
                                        AND cost.contracted_unit_price IS NOT NULL
                                        AND cost.contracted_unit_price > 0
                                       THEN cost.pricing_quantity
                                            * cost.contracted_unit_price
                                       WHEN lower(cost.pricing_category)
                                            = 'committed'
                                        AND cost.pricing_quantity IS NOT NULL
                                        AND ps.on_demand_hourly IS NOT NULL
                                       THEN cost.pricing_quantity
                                            * ps.on_demand_hourly
                                       ELSE cost.billed_cost
                                   END
                               )
                                   * 30.0 / nullif(
                                       date_diff(
                                           'day', bounds.window_start,
                                           bounds.window_end
                                       ) + 1,
                                       0
                                   ) AS monthly_list_cost,
                               sum(cost.effective_cost) * 30.0 / nullif(
                                   date_diff(
                                       'day', bounds.window_start,
                                       bounds.window_end
                                   ) + 1,
                                   0
                               ) AS monthly_effective_cost,
                               date_diff(
                                   'day', bounds.window_start,
                                   bounds.window_end
                               ) + 1 AS window_days,
                               any_value(cost.billing_currency) AS billing_currency
                        FROM focus_vm_charges AS cost
                        CROSS JOIN focus_bounds AS bounds
                        LEFT JOIN ps_hourly AS ps
                          ON ps.meter_id = cost.meter_id
                        WHERE cost.resource_id <> ''
                          AND CAST(cost.charge_period_start AS DATE)
                              BETWEEN bounds.window_start AND bounds.window_end
                        GROUP BY lower(cost.resource_id), bounds.window_start,
                                 bounds.window_end
                    )
                    SELECT
                        lower(resource.resource_id),
                        resource.name,
                        resource.subscription_name,
                        resource.resource_group,
                        resource.region,
                        resource.sku,
                        resource.estimated_monthly_cost,
                        rec.kind,
                        rec.current_sku,
                        rec.target_sku,
                        rec.cpu_p95,
                        rec.metric_coverage_percent,
                        rec.evidence_window_days,
                        rec.estimated_monthly_saving,
                        rec.reason,
                        cpu.p95,
                        cpu.coverage_percent,
                        cpu.source,
                        cpu.window_days,
                        COALESCE(
                            json_extract_string(resource.raw_json, '$.osType'),
                            ''
                        ),
                        COALESCE(
                            json_extract_string(
                                resource.raw_json, '$.licenseType'
                            ),
                            ''
                        ),
                        focus.monthly_list_cost,
                        focus.monthly_effective_cost,
                        focus.window_days,
                        focus.billing_currency,
                        focus.baseline_source
                    FROM resources_current AS resource
                    LEFT JOIN rightsizing_recommendations_current AS rec
                      ON lower(rec.resource_id) = lower(resource.resource_id)
                    LEFT JOIN cpu
                      ON cpu.resource_id = lower(resource.resource_id)
                    LEFT JOIN focus
                      ON focus.resource_id = lower(resource.resource_id)
                    WHERE lower(resource.resource_type)
                        = 'microsoft.compute/virtualmachines'
                    ORDER BY resource.name
                    """
                ).fetchall()
        vms = []
        for row in vm_rows:
            cpu_p95 = row[10] if row[10] is not None else row[15]
            profile, license_model = price_profile(row[19], row[20])
            vms.append(
                {
                    "vmKey": str(row[0]),
                    "name": str(row[1]),
                    "subscriptionName": str(row[2] or ""),
                    "resourceGroup": str(row[3] or ""),
                    "region": str(row[4] or ""),
                    "sku": str(row[5] or row[8] or ""),
                    "estimatedMonthlyCost": (
                        float(row[21]) if row[21] is not None
                        else float(row[6]) if row[6] is not None else None
                    ),
                    "observedMonthlyListCost": (
                        float(row[21]) if row[21] is not None else None
                    ),
                    "observedMonthlyEffectiveCost": (
                        float(row[22]) if row[22] is not None else None
                    ),
                    "costWindowDays": (
                        int(row[23]) if row[23] is not None else None
                    ),
                    "costCurrency": str(row[24] or ""),
                    "baselineSource": str(row[25] or ""),
                    "operatingSystem": str(row[19] or "").lower(),
                    "licenseModel": license_model,
                    "priceProfile": profile,
                    "action": str(row[7] or "none"),
                    "targetSku": str(row[9] or ""),
                    "cpuP95": float(cpu_p95) if cpu_p95 is not None else None,
                    "coveragePercent": (
                        float(row[11] if row[11] is not None else row[16])
                        if (row[11] is not None or row[16] is not None)
                        else None
                    ),
                    "windowDays": (
                        int(row[12]) if row[12] is not None
                        else int(row[18]) if row[18] is not None
                        else None
                    ),
                    "estimatedMonthlySaving": (
                        float(row[13]) if row[13] is not None else None
                    ),
                    "reason": str(row[14] or ""),
                    "telemetrySource": str(row[17] or ""),
                    "noData": cpu_p95 is None and row[7] is None,
                }
            )
        if cache_hit:
            vms = list(cached[1])
        else:
            with self._plan_vms_lock:
                self._plan_vms_cache = (fingerprint, list(vms))
        with self.operational_connect(read_only=True) as db:
            resolved_board_id = self._resolve_rightsizing_board_readonly(
                db, board_id
            )
            board_row, bucket_rows, assignment_rows = None, [], []
            if resolved_board_id:
                board_row = db.execute(
                    "SELECT name, description FROM rightsizing_boards WHERE id = ?",
                    [resolved_board_id],
                ).fetchone()
                bucket_rows = db.execute(
                    """
                    SELECT bucket_key, board_id, region, sku, strategy,
                           source, ref_quantity, ref_monthly_payg,
                           ref_monthly_ri_1y, ref_ri_1y_upfront,
                           ref_monthly_sp_1y, ref_monthly_savings,
                           ref_reservation_check, note, created_by,
                           created_at, updated_at
                    FROM rightsizing_plan_buckets
                    WHERE board_id = ?
                    ORDER BY region, sku
                    """,
                    [resolved_board_id],
                ).fetchall()
                assignment_rows = db.execute(
                    """
                    SELECT vm_key, vm_name, bucket_key, decision, note,
                           ref_monthly_payg, ref_monthly_commitment,
                           ref_monthly_savings, economics_status,
                           updated_by, updated_at
                    FROM rightsizing_plan_assignments
                    WHERE board_id = ?
                    """,
                    [resolved_board_id],
                ).fetchall()
        # Cold start (no board exists anywhere yet) or repair (a board
        # exists but none is primary): the rare path that needs a write,
        # isolated so the common case above never opens a writable
        # connection at all. A newly bootstrapped board is always named
        # "Default" (see _resolve_rightsizing_board), so no re-fetch needed.
        if not resolved_board_id:
            board_id = self._ensure_rightsizing_board(board_id)
            board_row = ("Default", "")
        else:
            board_id = resolved_board_id
        buckets = [self._rightsizing_bucket_row(row) for row in bucket_rows]
        seed_keys = {vm["vmKey"] for vm in vms}
        assignments: dict[str, dict[str, Any]] = {}
        imported_unmatched = []
        for row in assignment_rows:
            entry = {
                "bucketKey": str(row[2]),
                "decision": str(row[3] or ""),
                "note": str(row[4] or ""),
                "refMonthlyPayg": (
                    float(row[5]) if row[5] is not None else None
                ),
                "refMonthlyCommitment": (
                    float(row[6]) if row[6] is not None else None
                ),
                "refMonthlySavings": (
                    float(row[7]) if row[7] is not None else None
                ),
                "economicsStatus": str(row[8] or ""),
                "updatedBy": str(row[9] or ""),
                "updatedAt": row[10].isoformat() if row[10] else None,
            }
            key = str(row[0])
            if key in seed_keys:
                assignments[key] = entry
            else:
                # Preserved from an import but absent from live inventory
                # (decommissioned since, or never an Azure VM). Shown
                # separately rather than silently dropped.
                imported_unmatched.append({**entry, "vmKey": key, "vmName": str(row[1] or "")})
        assigned = sum(
            1
            for value in assignments.values()
            if value["bucketKey"] not in ("__unassigned__", "__nodata__")
        )
        # Savings are grouped by the vehicle that delivers them, because a
        # reservation, a savings plan and a decommission are three different
        # commitments with different risk and approval paths. A bucket's
        # strategy decides its group: ten of these buckets are savings plans
        # because Azure publishes no reservation rate for their SKUs.
        def is_savings_plan(strategy: str) -> bool:
            return "savings" in strategy.lower()

        reservation_bucket_savings = sum(
            bucket["refMonthlySavings"] or 0
            for bucket in buckets
            if not is_savings_plan(bucket["strategy"])
        )
        savings_plan_bucket_savings = sum(
            bucket["refMonthlySavings"] or 0
            for bucket in buckets
            if is_savings_plan(bucket["strategy"])
        )
        bucket_savings = reservation_bucket_savings + savings_plan_bucket_savings
        # Bucket savings cover every member the plan names, whether or not the
        # member resolved to live inventory. Per-VM savings are counted the
        # same way for consistency: excluding unmatched rows here while
        # including them there understated decommissions by exactly the rows
        # that had already left inventory. The unmatched figure is still
        # reported separately as a data-quality caveat.
        vm_rows = list(assignments.values()) + list(imported_unmatched)
        savings_plan_vm_savings = sum(
            row.get("refMonthlySavings") or 0
            for row in vm_rows
            if row["bucketKey"] == "__savingsplan__"
        )
        savings_plan_savings = (
            savings_plan_bucket_savings + savings_plan_vm_savings
        )
        # Decommissioning eliminates the whole run rate, so its saving lives
        # on the VM rather than in any commitment bucket. Counted from the
        # Excluded column, where only genuine decommissions carry economics;
        # out-of-scope VMs leave them null and contribute nothing. Bucket
        # members never appear here, so nothing is double counted.
        decommission_savings = sum(
            row.get("refMonthlySavings") or 0
            for row in vm_rows
            if row["bucketKey"] == "__decommission__"
        )
        planned_savings = (
            reservation_bucket_savings
            + savings_plan_savings
            + decommission_savings
        )
        # Savings attached to imported rows that no longer match live
        # inventory are excluded from the plan total (the machine is gone, or
        # its name never resolved) but must still be visible, or a naming
        # mismatch would quietly delete value from the plan.
        unmatched_savings = sum(
            entry.get("refMonthlySavings") or 0 for entry in imported_unmatched
        )
        modeled_reservation_buckets = sum(
            bucket["refMonthlySavings"] is not None
            and not is_savings_plan(bucket["strategy"])
            for bucket in buckets
        )
        modeled_savings_plan_buckets = sum(
            bucket["refMonthlySavings"] is not None
            and is_savings_plan(bucket["strategy"])
            for bucket in buckets
        )
        reservation_bucket_count = sum(
            not is_savings_plan(bucket["strategy"]) for bucket in buckets
        )
        savings_plan_bucket_count = sum(
            is_savings_plan(bucket["strategy"]) for bucket in buckets
        )
        savings_plan_assignments = [
            assignment for assignment in assignments.values()
            if assignment["bucketKey"] == "__savingsplan__"
        ]
        return {
            "boardId": board_id,
            "boardName": str(board_row[0]) if board_row else "",
            "boardDescription": str(board_row[1] or "") if board_row else "",
            "vms": vms,
            "buckets": buckets,
            "assignments": assignments,
            "importedUnmatched": imported_unmatched,
            "summary": {
                "totalVms": len(vms),
                "assigned": assigned,
                "noData": sum(1 for vm in vms if vm["noData"]),
                "bucketCount": len(buckets),
                "plannedMonthlySavings": round(planned_savings, 2),
                "commitmentMonthlySavings": round(
                    reservation_bucket_savings + savings_plan_savings, 2
                ),
                "reservationMonthlySavings": round(
                    reservation_bucket_savings, 2
                ),
                "savingsPlanMonthlySavings": round(savings_plan_savings, 2),
                "decommissionMonthlySavings": round(decommission_savings, 2),
                "reservationBucketCount": reservation_bucket_count,
                "savingsPlanBucketCount": savings_plan_bucket_count,
                "modeledSavingsPlanBuckets": modeled_savings_plan_buckets,
                "unmatchedMonthlySavings": round(unmatched_savings, 2),
                "modeledReservationBuckets": modeled_reservation_buckets,
                "savingsPlanCandidates": len(savings_plan_assignments),
                "modeledSavingsPlanCandidates": sum(
                    assignment.get("refMonthlySavings") is not None
                    for assignment in savings_plan_assignments
                ),
            },
        }

    FLUX_PROPOSAL_ACTOR = "flux"

    def _assert_editable_board(self, db: Any, board_id: str) -> None:
        row = db.execute(
            "SELECT created_by FROM rightsizing_boards WHERE id = ?",
            [board_id],
        ).fetchone()
        if row and str(row[0]) == self.FLUX_PROPOSAL_ACTOR:
            raise PermissionError(
                "The Flux proposal board is read-only. Copy it to an "
                "editable board to make changes."
            )

    def replace_flux_proposal_board(
        self,
        *,
        board_name: str,
        description: str,
        actor: str,
        buckets: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
        summary_note: str,
    ) -> str:
        """Create or wholesale-refresh the system proposal board.

        The proposal is regenerated, never edited: contents are replaced in
        one transaction and the decision log keeps exactly one entry per
        refresh instead of a synthetic move history.
        """
        now = utc_now()
        specials = set(self.RIGHTSIZING_SPECIAL_BUCKETS)
        with self.operational_connect() as db:
            row = db.execute(
                "SELECT id FROM rightsizing_boards "
                "WHERE created_by = ? AND name = ?",
                [actor, board_name],
            ).fetchone()
            if row:
                board_id = str(row[0])
                db.execute(
                    "UPDATE rightsizing_boards SET description = ?, "
                    "updated_at = ? WHERE id = ?",
                    [description, now, board_id],
                )
            else:
                board_id = str(uuid4())
                db.execute(
                    """
                    INSERT INTO rightsizing_boards (
                        id, name, description, is_primary, created_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, FALSE, ?, ?, ?)
                    """,
                    [board_id, board_name, description, actor, now, now],
                )
            db.execute(
                "DELETE FROM rightsizing_plan_assignments WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE board_id = ?",
                [board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_log WHERE board_id = ?",
                [board_id],
            )
            for bucket in buckets:
                key = f"{board_id}:{bucket['region']}|{bucket['sku']}"
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_buckets (
                        bucket_key, board_id, region, sku, strategy, source,
                        ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                        ref_ri_1y_upfront, ref_monthly_sp_1y,
                        ref_monthly_savings, ref_reservation_check, note,
                        created_by, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, 'flux-proposal', ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        key,
                        board_id,
                        bucket["region"],
                        bucket["sku"],
                        str(bucket.get("strategy") or ""),
                        (
                            int(bucket["refQuantity"])
                            if bucket.get("refQuantity") is not None
                            else None
                        ),
                        bucket.get("refMonthlyPayg"),
                        bucket.get("refMonthlyRi1y"),
                        bucket.get("refRi1yUpfront"),
                        bucket.get("refMonthlySp1y"),
                        bucket.get("refMonthlySavings"),
                        str(bucket.get("refReservationCheck") or ""),
                        str(bucket.get("note") or ""),
                        actor,
                        now,
                        now,
                    ],
                )
            for assignment_index, assignment in enumerate(assignments):
                bucket_key = str(assignment["bucketKey"])
                if bucket_key not in specials:
                    bucket_key = f"{board_id}:{bucket_key}"
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, ref_monthly_payg,
                        ref_monthly_commitment, ref_monthly_savings,
                        economics_status, source, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'flux-proposal', ?, ?)
                    """,
                    [
                        board_id,
                        assignment["vmKey"],
                        assignment.get("vmName") or "",
                        assignment.get("subscriptionName") or "",
                        bucket_key,
                        str(assignment.get("decision") or "Pending"),
                        str(assignment.get("note") or ""),
                        assignment.get("refMonthlyPayg"),
                        assignment.get("refMonthlyCommitment"),
                        assignment.get("refMonthlySavings"),
                        str(assignment.get("economicsStatus") or ""),
                        actor,
                        now,
                    ],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_log (
                        id, board_id, ts, actor, vm_key, vm_name,
                        from_label, to_label, decision, note
                    ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                    """,
                    [
                        str(uuid4()),
                        board_id,
                        now + timedelta(microseconds=assignment_index),
                        actor,
                        assignment["vmKey"],
                        assignment.get("vmName") or "",
                        bucket_key,
                        str(assignment.get("decision") or "Pending"),
                        str(assignment.get("note") or ""),
                    ],
                )
            db.execute(
                """
                INSERT INTO rightsizing_plan_log (
                    id, board_id, ts, actor, vm_key, vm_name, from_label,
                    to_label, decision, note
                ) VALUES (?, ?, ?, ?, '', '', '', '', '', ?)
                """,
                [
                    str(uuid4()), board_id,
                    now + timedelta(microseconds=len(assignments) + 1),
                    actor, summary_note,
                ],
            )
            db.commit()
        return board_id

    def duplicate_rightsizing_board(
        self, source_board_id: str, name: str, actor: str = ""
    ) -> dict[str, Any]:
        """Copy a board's buckets and placements into a new editable board."""
        name = name.strip()
        if not name:
            raise ValueError("A name for the copied board is required.")
        now = utc_now()
        new_id = str(uuid4())
        specials = set(self.RIGHTSIZING_SPECIAL_BUCKETS)
        with self.operational_connect() as db:
            source = db.execute(
                "SELECT name FROM rightsizing_boards WHERE id = ?",
                [source_board_id],
            ).fetchone()
            if not source:
                raise ValueError("The source board does not exist.")
            db.execute(
                """
                INSERT INTO rightsizing_boards (
                    id, name, description, is_primary, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, FALSE, ?, ?, ?)
                """,
                [
                    new_id,
                    name,
                    f"Copied from {source[0]} on {now.date().isoformat()}.",
                    actor,
                    now,
                    now,
                ],
            )
            bucket_rows = db.execute(
                """
                SELECT bucket_key, region, sku, strategy, source,
                       ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                       ref_ri_1y_upfront, ref_monthly_sp_1y,
                       ref_monthly_savings, ref_reservation_check, note
                FROM rightsizing_plan_buckets WHERE board_id = ?
                """,
                [source_board_id],
            ).fetchall()
            key_map: dict[str, str] = {}
            for row in bucket_rows:
                suffix = str(row[0]).split(":", 1)[-1]
                new_key = f"{new_id}:{suffix}"
                key_map[str(row[0])] = new_key
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_buckets (
                        bucket_key, board_id, region, sku, strategy, source,
                        ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                        ref_ri_1y_upfront, ref_monthly_sp_1y,
                        ref_monthly_savings, ref_reservation_check, note,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [new_key, new_id, *row[1:13], actor, now, now],
                )
            assignment_rows = db.execute(
                """
                SELECT vm_key, vm_name, subscription_name, bucket_key,
                       decision, note, ref_monthly_payg,
                       ref_monthly_commitment, ref_monthly_savings,
                       economics_status
                FROM rightsizing_plan_assignments WHERE board_id = ?
                """,
                [source_board_id],
            ).fetchall()
            for row in assignment_rows:
                bucket_key = str(row[3])
                if bucket_key not in specials:
                    bucket_key = key_map.get(
                        bucket_key, f"{new_id}:{bucket_key.split(':', 1)[-1]}"
                    )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, ref_monthly_payg,
                        ref_monthly_commitment, ref_monthly_savings,
                        economics_status, source, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'copy', ?, ?)
                    """,
                    [new_id, row[0], row[1], row[2], bucket_key, row[4],
                     row[5], row[6], row[7], row[8], row[9], actor, now],
                )
            db.execute(
                """
                INSERT INTO rightsizing_plan_log (
                    id, board_id, ts, actor, vm_key, vm_name, from_label,
                    to_label, decision, note
                ) VALUES (?, ?, ?, ?, '', '', '', '', '', ?)
                """,
                [
                    str(uuid4()),
                    new_id,
                    now,
                    actor,
                    f"Board copied from {source[0]} "
                    f"({len(bucket_rows)} buckets, "
                    f"{len(assignment_rows)} placements).",
                ],
            )
            db.commit()
        return {"id": new_id, "name": name}

    def save_rightsizing_bucket(
        self, payload: dict[str, Any], updated_by: str = ""
    ) -> dict[str, Any]:
        region = str(payload.get("region") or "").strip()
        sku = str(payload.get("sku") or "").strip()
        now = utc_now()

        def number(name: str) -> float | None:
            value = payload.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                # The standalone tool serialized economics as strings and
                # left unpriced fields as "" -- blank means absent, not zero.
                return None
            return float(value)

        with self.operational_connect() as db:
            board_id = self._resolve_rightsizing_board(
                db, str(payload.get("boardId") or "")
            )
            self._assert_editable_board(db, board_id)
            key = f"{board_id}:{region}|{sku}"
            existing = db.execute(
                "SELECT created_by, created_at FROM rightsizing_plan_buckets "
                "WHERE bucket_key = ?",
                [key],
            ).fetchone()
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE bucket_key = ?",
                [key],
            )
            db.execute(
                """
                INSERT INTO rightsizing_plan_buckets (
                    bucket_key, board_id, region, sku, strategy, source,
                    ref_quantity, ref_monthly_payg, ref_monthly_ri_1y,
                    ref_ri_1y_upfront, ref_monthly_sp_1y,
                    ref_monthly_savings, ref_reservation_check, note,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    key,
                    board_id,
                    region,
                    sku,
                    str(payload.get("strategy") or "").strip(),
                    str(payload.get("source") or "ui"),
                    (
                        int(float(payload["refQuantity"]))
                        if payload.get("refQuantity") not in (None, "")
                        else None
                    ),
                    number("refMonthlyPayg"),
                    number("refMonthlyRi1y"),
                    number("refRi1yUpfront"),
                    number("refMonthlySp1y"),
                    number("refMonthlySavings"),
                    str(payload.get("refReservationCheck") or "").strip(),
                    str(payload.get("note") or "").strip(),
                    existing[0] if existing else updated_by,
                    existing[1] if existing else now,
                    now,
                ],
            )
            db.commit()
        return {"bucketKey": key, "boardId": board_id}

    def delete_rightsizing_bucket(
        self, bucket_key: str, actor: str = ""
    ) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            bucket_row = db.execute(
                "SELECT board_id FROM rightsizing_plan_buckets "
                "WHERE bucket_key = ?",
                [bucket_key],
            ).fetchone()
            if not bucket_row:
                raise ValueError(f"Bucket {bucket_key!r} does not exist.")
            board_id = str(bucket_row[0])
            self._assert_editable_board(db, board_id)
            moved = db.execute(
                "SELECT count(*) FROM rightsizing_plan_assignments "
                "WHERE bucket_key = ? AND board_id = ?",
                [bucket_key, board_id],
            ).fetchone()[0]
            db.execute(
                "UPDATE rightsizing_plan_assignments "
                "SET bucket_key = '__unassigned__', updated_by = ?, "
                "updated_at = ? WHERE bucket_key = ? AND board_id = ?",
                [actor, now, bucket_key, board_id],
            )
            db.execute(
                "DELETE FROM rightsizing_plan_buckets WHERE bucket_key = ?",
                [bucket_key],
            )
            db.execute(
                """
                INSERT INTO rightsizing_plan_log (
                    id, board_id, ts, actor, vm_key, vm_name, from_label,
                    to_label, decision, note
                ) VALUES (?, ?, ?, ?, '', '', ?, 'Unassigned', '', ?)
                """,
                [
                    str(uuid4()),
                    board_id,
                    now,
                    actor,
                    bucket_key,
                    f"Bucket removed; {int(moved)} VM(s) returned to Unassigned.",
                ],
            )
            db.commit()
        return {"removed": bucket_key, "movedToUnassigned": int(moved)}

    def assign_rightsizing_vms(
        self,
        moves: list[dict[str, Any]],
        board_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.operational_connect() as db:
            board_id = self._resolve_rightsizing_board(db, board_id)
            self._assert_editable_board(db, board_id)
            valid_buckets = {r[0] for r in db.execute("SELECT bucket_key FROM rightsizing_plan_buckets WHERE board_id = ?", [board_id]).fetchall()}
            valid_buckets.update(self.RIGHTSIZING_SPECIAL_BUCKETS)
            for move in moves:
                vm_key = str(move.get("vmKey") or "").strip().lower()
                bucket_key = str(move.get("bucketKey") or "__unassigned__")
                if bucket_key not in valid_buckets:
                    raise ValueError(f"Bucket {bucket_key!r} does not exist on board {board_id!r}.")
                previous = db.execute(
                    "SELECT bucket_key, decision, note "
                    "FROM rightsizing_plan_assignments "
                    "WHERE board_id = ? AND vm_key = ?",
                    [board_id, vm_key],
                ).fetchone()
                decision = str(
                    move.get("decision")
                    if move.get("decision") is not None
                    else (previous[1] if previous else "Pending")
                )
                note = str(
                    move.get("note")
                    if move.get("note") is not None
                    else (previous[2] if previous else "")
                )
                db.execute(
                    "DELETE FROM rightsizing_plan_assignments "
                    "WHERE board_id = ? AND vm_key = ?",
                    [board_id, vm_key],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, source, updated_by,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ui', ?, ?)
                    """,
                    [
                        board_id,
                        vm_key,
                        str(move.get("vmName") or ""),
                        str(move.get("subscriptionName") or ""),
                        bucket_key,
                        decision,
                        note,
                        actor,
                        now,
                    ],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_log (
                        id, board_id, ts, actor, vm_key, vm_name,
                        from_label, to_label, decision, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(uuid4()),
                        board_id,
                        now,
                        actor,
                        vm_key,
                        str(move.get("vmName") or ""),
                        str(previous[0]) if previous else "__unassigned__",
                        bucket_key,
                        decision,
                        note,
                    ],
                )
            db.commit()
        return {"moved": len(moves), "boardId": board_id}

    def rightsizing_plan_log(
        self, board_id: str = "", limit: int = 250
    ) -> list[dict[str, Any]]:
        with self.operational_connect(read_only=True) as db:
            resolved_board_id = self._resolve_rightsizing_board_readonly(
                db, board_id
            )
            rows = (
                db.execute(
                    """
                    SELECT ts, actor, vm_key, vm_name, from_label, to_label,
                           decision, note
                    FROM rightsizing_plan_log
                    WHERE board_id = ?
                    ORDER BY ts DESC
                    LIMIT ?
                    """,
                    [resolved_board_id, int(limit)],
                ).fetchall()
                if resolved_board_id
                else []
            )
        return [
            {
                "ts": row[0].isoformat() if row[0] else None,
                "actor": str(row[1] or ""),
                "vmKey": str(row[2] or ""),
                "vmName": str(row[3] or ""),
                "fromLabel": str(row[4] or ""),
                "toLabel": str(row[5] or ""),
                "decision": str(row[6] or ""),
                "note": str(row[7] or ""),
            }
            for row in rows
        ]

    # Bucket economics fields the importer can set, keyed by Flux's own
    # names; used identically for diffing (dry run) and writing (apply).
    _RIGHTSIZING_BUCKET_FIELDS = (
        "strategy", "refQuantity", "refMonthlyPayg", "refMonthlyRi1y",
        "refRi1yUpfront", "refMonthlySp1y", "refMonthlySavings",
        "refReservationCheck", "note",
    )

    @staticmethod
    def _normalize_rightsizing_bucket_field(name: str, value: Any) -> Any:
        if name in ("strategy", "refReservationCheck", "note"):
            return str(value or "").strip()
        if name == "refQuantity":
            return int(float(value)) if value not in (None, "") else None
        # Economics fields: blank string means absent, not zero -- the same
        # rule save_rightsizing_bucket applies when it writes.
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return round(float(value), 2)

    def import_rightsizing_plan(
        self,
        payload: dict[str, Any],
        *,
        board_id: str = "",
        new_board_name: str = "",
        dry_run: bool = False,
        actor: str = "import",
    ) -> dict[str, Any]:
        """Import (or preview importing) the standalone board's files.

        The standalone tool identified VMs by LogicMonitor id (``lm-1402``),
        which means nothing to Flux. Matching goes through VM name plus
        subscription against live inventory; a VM that no longer exists is
        kept under an ``import:`` key rather than dropped, because a decision
        about a machine that was since decommissioned is still part of the
        plan's history.

        The standalone tool stays in use alongside Flux, so re-importing a
        refreshed export is routine, not one-time -- every import can run as
        a dry run first (``dry_run=True``): it resolves VMs and classifies
        every bucket and assignment as added/changed/unchanged against the
        target board and returns that classification without writing
        anything, so a planner can see exactly what an updated file would
        change before committing to it. ``new_board_name`` targets a fresh
        board instead of an existing one (created for real only when
        ``dry_run`` is false); leaving both ``board_id`` and
        ``new_board_name`` blank targets the primary board.
        """
        new_board_name = new_board_name.strip()
        target_is_new = bool(new_board_name) and not board_id
        vms = payload.get("vms") or []
        by_id = {
            str(vm.get("id")): vm for vm in vms if vm.get("id") is not None
        }
        with self.connect(read_only=True) as db:
            inventory = db.execute(
                """
                SELECT lower(resource_id), lower(name),
                       lower(coalesce(subscription_name, '')),
                       lower(coalesce(resource_group, '')),
                       lower(coalesce(region, ''))
                FROM resources_current
                WHERE lower(resource_type) = 'microsoft.compute/virtualmachines'
                """
            ).fetchall()
        by_name: dict[str, list[tuple[str, ...]]] = {}
        by_short: dict[str, list[tuple[str, ...]]] = {}
        inventory_names: list[str] = []
        for row in inventory:
            entry = tuple(str(value or "") for value in row)
            inventory_names.append(entry[1])
            by_name.setdefault(entry[1], []).append(entry)
            short = entry[1].split(".", 1)[0]
            if short != entry[1]:
                by_short.setdefault(short, []).append(entry)

        def narrow(
            candidates: list[tuple[str, ...]], vm: dict[str, Any]
        ) -> list[tuple[str, ...]]:
            # Tighten same-name candidates by subscription, then resource
            # group, then region, stopping as soon as one remains. A filter
            # that eliminates everything is ignored: the tool's labels come
            # from LogicMonitor and may disagree with inventory.
            for field, index in (
                ("subscriptionName", 2),
                ("resourceGroup", 3),
                ("region", 4),
            ):
                if len(candidates) == 1:
                    return candidates
                value = str(vm.get(field) or "").strip().lower()
                if value:
                    narrowed = [c for c in candidates if c[index] == value]
                    if narrowed:
                        candidates = narrowed
            return candidates

        unmatched_names: list[str] = []

        def resolve(lm_id: str) -> tuple[str, str, bool]:
            vm = by_id.get(lm_id) or {}
            display = str(vm.get("vmName") or "").strip()
            # The tool's vmName is the LogicMonitor device name, which may be
            # a FQDN or the guest hostname rather than the Azure resource
            # name; computerName is the guest hostname when known. Try each
            # as given and as a domain-stripped short name, both directions.
            for raw in (vm.get("vmName"), vm.get("computerName")):
                name = str(raw or "").strip().lower()
                if not name:
                    continue
                short = name.split(".", 1)[0]
                candidates = (
                    by_name.get(name)
                    or by_name.get(short)
                    or by_short.get(short)
                    or []
                )
                candidates = narrow(candidates, vm)
                if len(candidates) == 1:
                    return candidates[0][0], display or name, True
            return f"import:{lm_id}", display or lm_id, False

        buckets = payload.get("buckets") or {}
        if isinstance(buckets, dict):
            bucket_pairs = [
                (str(key), value) for key, value in buckets.items()
            ]
        else:
            bucket_pairs = [
                (str(value.get("key") or ""), value) for value in buckets
            ]

        # Human labels for the diff preview -- the file's own bucket keys
        # (bare "region|sku") and the four fixed pseudo-buckets, keyed by
        # the exact string that appears in an incoming assignment.
        bucket_label_by_raw_key: dict[str, str] = {
            "__unassigned__": "Unassigned",
            "__nodata__": "No monitoring data",
            "__review__": "Keep on demand",
            "__savingsplan__": "Savings plan",
            "__excluded__": "Excluded",
        }
        for key, bucket in bucket_pairs:
            bucket_key_in = str(bucket.get("key") or key)
            region = str(bucket.get("region") or "").strip()
            sku = str(bucket.get("sku") or "").strip()
            if (
                bucket_key_in in self.RIGHTSIZING_SPECIAL_BUCKETS
                or not region
                or not sku
            ):
                continue
            bucket_label_by_raw_key[f"{region}|{sku}"] = f"{sku} — {region}"

        def bare_bucket_ref(key: str) -> str:
            # An assignment's bucket key arrives bare ("region|sku") from
            # the standalone tool, but re-importing Flux's own "Export"
            # backup carries the previous board-prefixed key instead
            # ("<board-id>:region|sku", possibly a different board's id).
            # Stripping down to the part after the last ":" normalizes
            # either shape to the bare form before it's re-prefixed or
            # used to look up a label -- region/sku names never contain
            # ":" themselves, so this is unambiguous.
            if key in self.RIGHTSIZING_SPECIAL_BUCKETS:
                return key
            return key.rsplit(":", 1)[-1]

        # Resolve every incoming assignment/vmMeta key to a live (or
        # preserved) vm_key up front, once -- the diff and the write both
        # need the identical resolution.
        assignments_in = payload.get("assignments") or {}
        vm_meta = payload.get("vmMeta") or payload.get("vm_meta") or {}
        resolved_moves: list[dict[str, Any]] = []
        matched = unmatched = 0
        for lm_id in set(assignments_in) | set(vm_meta):
            bucket_key = str(assignments_in.get(lm_id) or "__unassigned__")
            meta = vm_meta.get(lm_id) or {}
            decision = str(meta.get("decision") or "Pending")
            note = str(meta.get("note") or "")

            def economics(name: str) -> float | None:
                value = meta.get(name)
                if value is None or (
                    isinstance(value, str) and not value.strip()
                ):
                    return None
                return float(value)

            if bucket_key == "__unassigned__" and not (
                note or decision not in ("", "Pending")
            ):
                # Default state with nothing attached carries no signal.
                continue
            vm_key, vm_name, resolved = resolve(str(lm_id))
            matched += 1 if resolved else 0
            unmatched += 0 if resolved else 1
            if not resolved:
                unmatched_names.append(vm_name)
            vm = by_id.get(str(lm_id)) or {}
            resolved_moves.append(
                {
                    "lmId": str(lm_id),
                    "bucketLabel": bucket_label_by_raw_key.get(
                        bare_bucket_ref(bucket_key), bucket_key
                    ),
                    "vmKey": vm_key,
                    "vmName": vm_name,
                    "subscriptionName": str(vm.get("subscriptionName") or ""),
                    "bucketKey": bucket_key,
                    "decision": decision,
                    "note": note,
                    # Per-VM economics let an imported plan carry savings for
                    # placements that have no bucket to hold them -- savings
                    # plan candidates and decommissions, whose whole value is
                    # the eliminated run rate.
                    "refMonthlyPayg": economics("refMonthlyPayg"),
                    "refMonthlyCommitment": economics("refMonthlyCommitment"),
                    "refMonthlySavings": economics("refMonthlySavings"),
                    "economicsStatus": str(meta.get("economicsStatus") or ""),
                    "resolved": resolved,
                }
            )

        with self.operational_connect() as db:
            resolved_board_id = (
                None if target_is_new
                else self._resolve_rightsizing_board(db, board_id)
            )
            if resolved_board_id:
                self._assert_editable_board(db, resolved_board_id)
            existing_buckets: dict[str, tuple[Any, ...]] = {}
            existing_assignments: dict[str, tuple[Any, ...]] = {}
            existing_import_log_count = 0
            if resolved_board_id:
                existing_buckets = {
                    str(row[0]): row
                    for row in db.execute(
                        """
                        SELECT bucket_key, region, sku, strategy,
                               ref_quantity, ref_monthly_payg,
                               ref_monthly_ri_1y, ref_ri_1y_upfront,
                               ref_monthly_sp_1y, ref_monthly_savings,
                               ref_reservation_check, note
                        FROM rightsizing_plan_buckets WHERE board_id = ?
                        """,
                        [resolved_board_id],
                    ).fetchall()
                }
                existing_assignments = {
                    str(row[0]): row
                    for row in db.execute(
                        "SELECT vm_key, bucket_key, decision, note "
                        "FROM rightsizing_plan_assignments "
                        "WHERE board_id = ?",
                        [resolved_board_id],
                    ).fetchall()
                }
                existing_import_log_count = db.execute(
                    "SELECT count(*) FROM rightsizing_plan_log "
                    "WHERE board_id = ? AND actor LIKE 'import%'",
                    [resolved_board_id],
                ).fetchone()[0]
            db.commit()

        if resolved_board_id:
            # The file's bucket keys are usually bare "region|sku" strings,
            # but re-importing Flux's own export carries an already
            # board-prefixed key (see save_rightsizing_bucket); bare_bucket_ref
            # normalizes either shape before re-prefixing onto the target
            # board so a re-import never double-prefixes. Special
            # pseudo-buckets carry no region/sku and are never prefixed.
            for move in resolved_moves:
                if move["bucketKey"] not in self.RIGHTSIZING_SPECIAL_BUCKETS:
                    move["bucketKey"] = (
                        f"{resolved_board_id}:"
                        f"{bare_bucket_ref(move['bucketKey'])}"
                    )

        # ---- Classify buckets: added / changed / unchanged / skipped ----
        normalize = self._normalize_rightsizing_bucket_field
        buckets_added: list[dict[str, Any]] = []
        buckets_changed: list[dict[str, Any]] = []
        buckets_unchanged = 0
        buckets_skipped = 0
        bucket_writes: list[dict[str, Any]] = []
        for key, bucket in bucket_pairs:
            bucket_key_in = str(bucket.get("key") or key)
            region = str(bucket.get("region") or "").strip()
            sku = str(bucket.get("sku") or "").strip()
            if (
                bucket_key_in in self.RIGHTSIZING_SPECIAL_BUCKETS
                or not region
                or not sku
            ):
                # The tool serializes its fixed pseudo-columns (savings plan,
                # excluded, ...) alongside real buckets. Those columns always
                # exist on the board; importing one as a regular bucket
                # produced a duplicate "Savings Plan (all eligible VMs)"
                # column.
                buckets_skipped += 1
                continue
            incoming_raw = {
                "strategy": bucket.get("strategy"),
                "refQuantity": bucket.get("refQuantity"),
                "refMonthlyPayg": bucket.get("refMonthlyPaygBaseline"),
                "refMonthlyRi1y": bucket.get("refMonthlyRi1YearCost"),
                "refRi1yUpfront": bucket.get("refRi1YearUpfrontTotal"),
                "refMonthlySp1y": bucket.get("refMonthlySp1YearCost"),
                "refMonthlySavings": bucket.get("refMonthlySavingsVsPayg"),
                "refReservationCheck": bucket.get(
                    "refExistingReservationCheck"
                ),
                "note": bucket.get("note"),
            }
            bucket_writes.append(
                {"region": region, "sku": sku, "raw": incoming_raw}
            )
            full_key = (
                f"{resolved_board_id}:{region}|{sku}"
                if resolved_board_id else ""
            )
            label = f"{sku} — {region}"
            existing_row = existing_buckets.get(full_key)
            if existing_row is None:
                buckets_added.append(
                    {"label": label, "region": region, "sku": sku}
                )
                continue
            stored_raw = {
                "strategy": existing_row[3],
                "refQuantity": existing_row[4],
                "refMonthlyPayg": existing_row[5],
                "refMonthlyRi1y": existing_row[6],
                "refRi1yUpfront": existing_row[7],
                "refMonthlySp1y": existing_row[8],
                "refMonthlySavings": existing_row[9],
                "refReservationCheck": existing_row[10],
                "note": existing_row[11],
            }
            changed_fields = [
                {
                    "field": name,
                    "before": normalize(name, stored_raw[name]),
                    "after": normalize(name, incoming_raw[name]),
                }
                for name in self._RIGHTSIZING_BUCKET_FIELDS
                if normalize(name, stored_raw[name])
                != normalize(name, incoming_raw[name])
            ]
            if changed_fields:
                buckets_changed.append(
                    {
                        "label": label, "region": region, "sku": sku,
                        "fields": changed_fields,
                    }
                )
            else:
                buckets_unchanged += 1

        # Labels for buckets already on the board, keyed by their real
        # (board-prefixed) bucket_key -- for the "before" side of a changed
        # assignment, which stores the real key, not the file's bare one.
        bucket_label_by_full_key: dict[str, str] = {
            "__unassigned__": "Unassigned",
            "__nodata__": "No monitoring data",
            "__review__": "Keep on demand",
            "__savingsplan__": "Savings plan",
            "__excluded__": "Excluded",
        }
        for full_key, row in existing_buckets.items():
            bucket_label_by_full_key[full_key] = f"{row[2]} — {row[1]}"

        # ---- Classify assignments: added / changed / unchanged ----
        assignments_added: list[dict[str, Any]] = []
        assignments_changed: list[dict[str, Any]] = []
        assignments_unchanged = 0
        for move in resolved_moves:
            existing_row = existing_assignments.get(move["vmKey"])
            if existing_row is None:
                assignments_added.append(
                    {
                        "vmKey": move["vmKey"],
                        "vmName": move["vmName"],
                        "bucketKey": move["bucketKey"],
                        "bucketLabel": move["bucketLabel"],
                        "decision": move["decision"],
                        "note": move["note"],
                        "resolved": move["resolved"],
                    }
                )
                continue
            before = {
                "bucketKey": str(existing_row[1]),
                "decision": str(existing_row[2] or ""),
                "note": str(existing_row[3] or ""),
            }
            after = {
                "bucketKey": move["bucketKey"],
                "decision": move["decision"],
                "note": move["note"],
            }
            if before != after:
                assignments_changed.append(
                    {
                        "vmKey": move["vmKey"], "vmName": move["vmName"],
                        "before": {
                            **before,
                            "bucketLabel": bucket_label_by_full_key.get(
                                before["bucketKey"], before["bucketKey"]
                            ),
                        },
                        "after": {
                            **after, "bucketLabel": move["bucketLabel"],
                        },
                    }
                )
            else:
                assignments_unchanged += 1

        incoming_log = payload.get("log") or []

        if dry_run:
            return {
                "dryRun": True,
                "boardId": resolved_board_id,
                "newBoardName": new_board_name or None,
                "buckets": {
                    "added": buckets_added,
                    "changed": buckets_changed,
                    "unchanged": buckets_unchanged,
                    "skipped": buckets_skipped,
                },
                "assignments": {
                    "added": assignments_added,
                    "changed": assignments_changed,
                    "unchanged": assignments_unchanged,
                },
                "logEntriesReplaced": existing_import_log_count,
                "logEntriesIncoming": len(incoming_log),
                "matched": matched,
                "unmatched": unmatched,
                "unmatchedSamples": unmatched_names[:5],
                "inventorySample": sorted(inventory_names)[:5],
                "inventoryVmCount": len(inventory_names),
            }

        # ---- Real apply, reusing the resolution and diff work above ----
        if target_is_new:
            board = self.create_rightsizing_board(new_board_name, actor=actor)
            resolved_board_id = board["id"]
            # A new board has no id until now, so the prefixing above was
            # skipped for it. Without this, every non-special assignment keeps
            # a bare "region|sku" key while its bucket row is written as
            # "<board-id>:region|sku" -- the keys never meet, and each of
            # those VMs renders in no column at all. Only the special
            # pseudo-columns survived, which looked like most of the fleet
            # had silently vanished on import.
            for move in resolved_moves:
                if move["bucketKey"] not in self.RIGHTSIZING_SPECIAL_BUCKETS:
                    move["bucketKey"] = (
                        f"{resolved_board_id}:"
                        f"{bare_bucket_ref(move['bucketKey'])}"
                    )

        buckets_imported = 0
        for write in bucket_writes:
            buckets_imported += 1
            raw = write["raw"]
            self.save_rightsizing_bucket(
                {
                    "boardId": resolved_board_id,
                    "region": write["region"],
                    "sku": write["sku"],
                    "strategy": raw["strategy"],
                    "source": "import",
                    "refQuantity": raw["refQuantity"],
                    "refMonthlyPayg": raw["refMonthlyPayg"],
                    "refMonthlyRi1y": raw["refMonthlyRi1y"],
                    "refRi1yUpfront": raw["refRi1yUpfront"],
                    "refMonthlySp1y": raw["refMonthlySp1y"],
                    "refMonthlySavings": raw["refMonthlySavings"],
                    "refReservationCheck": raw["refReservationCheck"],
                    "note": raw["note"],
                },
                updated_by=actor,
            )

        now = utc_now()
        imported = 0
        with self.operational_connect() as db:
            for move in resolved_moves:
                lm_id = move["lmId"]
                vm_key = move["vmKey"]
                if move["resolved"]:
                    # A previous import may have preserved this VM under its
                    # import: key before matching worked; promote it.
                    db.execute(
                        "DELETE FROM rightsizing_plan_assignments "
                        "WHERE board_id = ? AND vm_key = ?",
                        [resolved_board_id, f"import:{lm_id}"],
                    )
                db.execute(
                    "DELETE FROM rightsizing_plan_assignments "
                    "WHERE board_id = ? AND vm_key = ?",
                    [resolved_board_id, vm_key],
                )
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_assignments (
                        board_id, vm_key, vm_name, subscription_name,
                        bucket_key, decision, note, ref_monthly_payg,
                        ref_monthly_commitment, ref_monthly_savings,
                        economics_status, source, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'import',
                              ?, ?)
                    """,
                    [
                        resolved_board_id,
                        vm_key,
                        move["vmName"],
                        move["subscriptionName"],
                        move["bucketKey"],
                        move["decision"],
                        move["note"],
                        move.get("refMonthlyPayg"),
                        move.get("refMonthlyCommitment"),
                        move.get("refMonthlySavings"),
                        move.get("economicsStatus") or "",
                        actor,
                        now,
                    ],
                )
                imported += 1
            log_imported = 0
            # Re-imports replace previously imported history wholesale, or
            # every run would append the same entries again under fresh ids.
            db.execute(
                "DELETE FROM rightsizing_plan_log "
                "WHERE board_id = ? AND actor LIKE 'import%'",
                [resolved_board_id],
            )
            for entry in incoming_log:
                ts_ms = entry.get("ts")
                moment = (
                    datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    if isinstance(ts_ms, (int, float))
                    else now
                )
                vm_key, vm_name, _ = resolve(str(entry.get("vmId") or ""))
                db.execute(
                    """
                    INSERT INTO rightsizing_plan_log (
                        id, board_id, ts, actor, vm_key, vm_name,
                        from_label, to_label, decision, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        str(uuid4()),
                        resolved_board_id,
                        moment,
                        actor,
                        vm_key,
                        str(entry.get("vmName") or vm_name),
                        str(entry.get("from") or ""),
                        str(entry.get("to") or ""),
                        str(entry.get("decision") or ""),
                        str(entry.get("note") or ""),
                    ],
                )
                log_imported += 1
            db.commit()
        return {
            "dryRun": False,
            "boardId": resolved_board_id,
            "bucketsImported": buckets_imported,
            "bucketsSkipped": buckets_skipped,
            "assignmentsImported": imported,
            "matched": matched,
            "unmatched": unmatched,
            "logImported": log_imported,
            # When matching fails outright, the two name samples make the
            # cause visible in the UI instead of requiring server access.
            "unmatchedSamples": unmatched_names[:5],
            "inventorySample": sorted(inventory_names)[:5],
            "inventoryVmCount": len(inventory_names),
        }

    def compute_rightsizing_recommendations(
        self,
        run_id: str,
        *,
        minimum_window_days: int = 14,
        minimum_coverage_percent: float = 70,
        idle_cpu_p95: float = 5,
        idle_cpu_maximum: float = 20,
        idle_network_p95_bytes: float = 52_428_800,
        review_cpu_p95: float = 30,
        memory_review_percent: float = 80,
        cpu_disagreement_percent: float = 20,
    ) -> int:
        computed_at = utc_now()
        with self.connect(read_only=True) as db:
            resources = db.execute(
                """
                WITH advisor AS (
                    SELECT * EXCLUDE (advisor_rank)
                    FROM (
                        SELECT
                            resource_id,
                            recommended_sku,
                            savings_amount,
                            annual_savings_amount,
                            savings_currency,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY observed_at DESC, recommendation_id
                            ) AS advisor_rank
                        FROM advisor_recommendations_current
                        WHERE lower(problem || ' ' || solution)
                            LIKE '%underutilized%virtual machine%'
                    )
                    WHERE advisor_rank = 1
                ),
                cost_by_type AS (
                    SELECT
                        resource_id,
                        cost_type,
                        sum(amount) AS amount,
                        max(currency) AS currency,
                        max(period_start) AS period_start,
                        max(period_end) AS period_end,
                        arg_max(snapshot_id, observed_at) AS cost_snapshot_id
                    FROM costs_current
                    WHERE resource_id <> ''
                    GROUP BY resource_id, cost_type
                ),
                resource_cost AS (
                    SELECT * EXCLUDE (cost_rank)
                    FROM (
                        SELECT *,
                            row_number() OVER (
                                PARTITION BY resource_id
                                ORDER BY CASE cost_type
                                    WHEN 'AmortizedCost' THEN 1 ELSE 2 END
                            ) AS cost_rank
                        FROM cost_by_type
                    )
                    WHERE cost_rank = 1
                ),
                logicmonitor AS (
                    SELECT resource_id, TRUE AS matched
                    FROM resource_source_matches_current
                    WHERE source = 'logicmonitor' AND status = 'matched'
                    GROUP BY resource_id
                )
                SELECT
                    resource.resource_id,
                    resource.name,
                    resource.subscription_id,
                    resource.subscription_name,
                    resource.resource_group,
                    resource.region,
                    resource.sku,
                    COALESCE(advisor.recommended_sku, ''),
                    advisor.savings_amount,
                    advisor.annual_savings_amount,
                    COALESCE(
                        NULLIF(advisor.savings_currency, ''),
                        cost.currency,
                        ''
                    ),
                    cost.amount,
                    COALESCE(cost.cost_type, ''),
                    cost.period_start,
                    cost.period_end,
                    COALESCE(cost.cost_snapshot_id, ''),
                    COALESCE(logicmonitor.matched, FALSE),
                    valuation.monthly_gross,
                    COALESCE(NULLIF(valuation.currency, ''), ''),
                    COALESCE(valuation.value_source, '')
                FROM resources_current AS resource
                LEFT JOIN advisor
                  ON lower(advisor.resource_id) = lower(resource.resource_id)
                LEFT JOIN resource_cost AS cost
                  ON cost.resource_id = lower(resource.resource_id)
                LEFT JOIN logicmonitor
                  ON logicmonitor.resource_id = lower(resource.resource_id)
                LEFT JOIN opportunity_valuation_current AS valuation
                  ON valuation.resource_id = lower(resource.resource_id)
                 AND valuation.opportunity_type = 'compute_shutdown'
                WHERE resource.resource_type =
                    'microsoft.compute/virtualmachines'
                """
            ).fetchall()
            metric_rows = db.execute(
                """
                SELECT
                    resource_id, source, metric, window_start, window_end,
                    sample_count, coverage_percent, average, p95, maximum,
                    unit, aggregation_method, lineage_json
                FROM telemetry_metric_summaries_current
                WHERE source IN ('azure_monitor', 'logicmonitor')
                """
            ).fetchall()

        telemetry: dict[str, dict[str, dict[str, Any]]] = {}
        for metric in metric_rows:
            telemetry.setdefault(metric[0], {}).setdefault(metric[1], {})[
                str(metric[2]).lower()
            ] = {
                "windowStart": metric[3],
                "windowEnd": metric[4],
                "sampleCount": metric[5],
                "coverage": metric[6],
                "average": metric[7],
                "p95": metric[8],
                "maximum": metric[9],
                "unit": metric[10],
                "aggregationMethod": metric[11] or "",
                "lineage": json.loads(metric[12] or "{}"),
            }

        rows = []
        for row in resources:
            resource_id = str(row[0]).lower()
            source_metrics = telemetry.get(resource_id, {})
            evidence_sources: dict[str, Any] = {}
            cpu_values: list[float] = []
            cpu_maxima: list[float] = []
            cpu_coverages: list[float] = []
            window_starts = []
            window_ends = []
            memory_values: list[float] = []
            network_in_values: list[float] = []
            network_out_values: list[float] = []
            active_sources = []
            for source in ("azure_monitor", "logicmonitor"):
                metrics = source_metrics.get(source, {})
                cpu = metrics.get("percentage cpu")
                if not cpu or cpu.get("p95") is None:
                    continue
                active_sources.append(source)
                cpu_values.append(float(cpu["p95"]))
                if cpu.get("maximum") is not None:
                    cpu_maxima.append(float(cpu["maximum"]))
                if cpu.get("coverage") is not None:
                    cpu_coverages.append(float(cpu["coverage"]))
                if cpu.get("windowStart"):
                    window_starts.append(cpu["windowStart"])
                if cpu.get("windowEnd"):
                    window_ends.append(cpu["windowEnd"])
                memory = metrics.get("memory used percentage")
                network_in = metrics.get("network in total")
                network_out = metrics.get("network out total")
                if memory and memory.get("p95") is not None:
                    memory_values.append(float(memory["p95"]))
                if network_in and network_in.get("p95") is not None:
                    network_in_values.append(float(network_in["p95"]))
                if network_out and network_out.get("p95") is not None:
                    network_out_values.append(float(network_out["p95"]))
                evidence_sources[source] = {
                    metric_name: {
                        "p95": value.get("p95"),
                        "maximum": value.get("maximum"),
                        "coveragePercent": value.get("coverage"),
                        "windowStart": value["windowStart"].isoformat()
                        if value.get("windowStart") else None,
                        "windowEnd": value["windowEnd"].isoformat()
                        if value.get("windowEnd") else None,
                        "aggregationMethod": value.get("aggregationMethod", ""),
                        "lineage": value.get("lineage", {}),
                    }
                    for metric_name, value in metrics.items()
                }
            complete_memory = (
                max(memory_values) if len(memory_values) == len(active_sources)
                and active_sources else None
            )
            complete_network_in = (
                max(network_in_values)
                if len(network_in_values) == len(active_sources)
                and active_sources else None
            )
            complete_network_out = (
                max(network_out_values)
                if len(network_out_values) == len(active_sources)
                and active_sources else None
            )
            cpu_delta = (
                max(cpu_values) - min(cpu_values)
                if len(cpu_values) > 1 else 0
            )
            assessment = assess_resource(
                attempt_status="covered" if active_sources else "not_attempted",
                window_start=max(window_starts) if window_starts else None,
                window_end=min(window_ends) if window_ends else None,
                cpu_p95=max(cpu_values) if cpu_values else None,
                cpu_maximum=max(cpu_maxima) if cpu_maxima else None,
                cpu_coverage=min(cpu_coverages) if cpu_coverages else None,
                memory_p95=complete_memory,
                network_in_p95=complete_network_in,
                network_out_p95=complete_network_out,
                source_disagreement=cpu_delta > cpu_disagreement_percent,
                advisor_target_sku=row[7],
                advisor_monthly_savings=row[8],
                minimum_window_days=minimum_window_days,
                minimum_coverage_percent=minimum_coverage_percent,
                idle_cpu_p95=idle_cpu_p95,
                idle_cpu_maximum=idle_cpu_maximum,
                idle_network_p95_bytes=idle_network_p95_bytes,
                review_cpu_p95=review_cpu_p95,
                memory_review_percent=memory_review_percent,
            )
            saving = None
            value_source = ""
            if assessment["status"] == "candidate":
                if assessment["kind"] == "resize":
                    saving = (
                        float(row[17])
                        if row[17] is not None
                        else float(row[8])
                        if row[8] is not None
                        else round(float(row[9]) / 12, 2)
                        if row[9] is not None
                        else None
                    )
                    value_source = row[19] or "azure_advisor"
                elif (
                    assessment["kind"] == "shutdown"
                    and row[11] is not None
                    and row[13]
                    and row[14]
                ):
                    saving = monthly_run_rate(row[11], row[13], row[14])
                    value_source = (
                        "amortized_cost_run_rate"
                        if row[12] == "AmortizedCost"
                        else "actual_cost_run_rate"
                    )
            evidence = {
                "attemptStatus": "covered" if active_sources else "not_attempted",
                "sourcesUsed": active_sources,
                "sourceEvidence": evidence_sources,
                "cpuP95Delta": round(cpu_delta, 2),
                "logicMonitorMatched": bool(row[16]),
                "logicMonitorMetricsUsed": "logicmonitor" in active_sources,
                "advisorTargetSku": row[7],
                "costSnapshotId": row[15],
                "costType": row[12],
                "costPeriodStart": row[13].isoformat() if row[13] else None,
                "costPeriodEnd": row[14].isoformat() if row[14] else None,
                "governedValuationSource": row[19],
                "thresholds": {
                    "minimumWindowDays": minimum_window_days,
                    "minimumCoveragePercent": minimum_coverage_percent,
                    "idleCpuP95": idle_cpu_p95,
                    "idleCpuMaximum": idle_cpu_maximum,
                    "idleNetworkP95Bytes": idle_network_p95_bytes,
                    "reviewCpuP95": review_cpu_p95,
                    "memoryReviewPercent": memory_review_percent,
                    "cpuDisagreementPercent": cpu_disagreement_percent,
                },
            }
            rows.append(
                [
                    run_id,
                    computed_at,
                    resource_id,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    assessment["kind"],
                    assessment["status"],
                    row[6],
                    assessment["targetSku"],
                    assessment["evidenceWindowDays"],
                    assessment["coverageFlag"],
                    "+".join(active_sources),
                    assessment["cpuP95"],
                    assessment["cpuMaximum"],
                    complete_memory,
                    assessment["networkInP95"],
                    assessment["networkOutP95"],
                    assessment["metricCoveragePercent"],
                    saving,
                    row[18] or row[10],
                    value_source,
                    assessment["reason"],
                    json_value(evidence),
                    RIGHTSIZING_METHOD_VERSION,
                ]
            )

        with self.connect() as db:
            db.execute(
                "DELETE FROM rightsizing_recommendation_snapshots WHERE run_id = ?",
                [run_id],
            )
            if rows:
                db.executemany(
                    """
                    INSERT INTO rightsizing_recommendation_snapshots (
                        run_id, computed_at, resource_id, resource_name,
                        subscription_id, subscription_name, resource_group, region,
                        kind, status, current_sku, target_sku, evidence_window_days,
                        coverage_flag, telemetry_source, cpu_p95, cpu_maximum,
                        memory_p95, network_in_p95, network_out_p95,
                        metric_coverage_percent, estimated_monthly_saving, currency,
                        value_source, reason, evidence_json, method_version
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    rows,
                )
        return len(rows)

    def ensure_rightsizing_recommendations(
        self,
        **thresholds: Any,
    ) -> int:
        with self.connect(read_only=True) as db:
            latest_run = db.execute(
                """
                SELECT arg_max(id, completed_at)
                FROM telemetry_runs
                WHERE source IN ('azure_monitor', 'logicmonitor')
                  AND status = 'succeeded'
                """
            ).fetchone()[0]
            existing = db.execute(
                """
                SELECT count(*)
                FROM rightsizing_recommendation_snapshots
                WHERE run_id = ? AND method_version = ?
                """,
                [latest_run, RIGHTSIZING_METHOD_VERSION],
            ).fetchone()[0] if latest_run else 0
        if not latest_run or existing:
            return 0
        return self.compute_rightsizing_recommendations(
            latest_run,
            **thresholds,
        )

    def rightsizing_dossier(self, resource_id: str) -> dict[str, Any]:
        """Everything the estate knows about one VM as a resize candidate.

        Built for the deep Review-with-Flux workflow: telemetry (all
        sources, including guest memory), the governed assessment with its
        evidence, per-resource cost history, FOCUS pricing-category and
        commitment-coverage exposure, retail price comparison between the
        current and target SKU in the VM's own region, and the human plan
        assignment. One bounded call instead of the model piecing together
        five tools.
        """
        normalized = resource_id.lower()
        dossier = self.resource_telemetry(resource_id)
        with self.connect(read_only=True) as db:
            resource = db.execute(
                """
                SELECT name, subscription_id, subscription_name,
                       resource_group, region, sku, tags_json
                FROM resources_current
                WHERE lower(resource_id) = ?
                """,
                [normalized],
            ).fetchone()
            focus = db.execute(
                """
                SELECT pricing_category,
                       CASE WHEN commitment_discount_id <> ''
                            THEN commitment_discount_type
                            ELSE 'None' END AS commitment,
                       round(sum(billed_cost), 2),
                       round(sum(effective_cost), 2)
                FROM focus_cost_current
                WHERE lower(resource_id) = ?
                  AND charge_period_start >= now() - INTERVAL 90 DAY
                GROUP BY 1, 2
                ORDER BY 4 DESC
                """,
                [normalized],
            ).fetchall()
            assessment = dossier.get("rightsizingAssessment") or {}
            current_sku = str(
                assessment.get("currentSku")
                or (resource[5] if resource else "")
                or ""
            )
            target_sku = str(assessment.get("targetSku") or "")
            region = str(resource[4] if resource else "").lower()
            prices = []
            if region and (current_sku or target_sku):
                skus = [sku for sku in {current_sku, target_sku} if sku]
                placeholders = ", ".join("?" for _ in skus)
                prices = db.execute(
                    f"""
                    SELECT arm_sku_name, price_profile, currency,
                           hourly_price, monthly_price
                    FROM retail_prices_current
                    WHERE lower(arm_region_name) = ?
                      AND arm_sku_name IN ({placeholders})
                    ORDER BY arm_sku_name, price_profile
                    """,
                    [region, *skus],
                ).fetchall()
        plan = None
        try:
            board = self.rightsizing_plan_board()
            for vm in board.get("vms", []):
                # vmKey is the lowercased resource ID.
                if str(vm.get("vmKey") or "") == normalized:
                    assignment = board.get("assignments", {}).get(
                        vm.get("vmKey"), {}
                    )
                    plan = {
                        "bucketKey": assignment.get("bucketKey")
                        or ("__nodata__" if vm.get("noData") else "__unassigned__"),
                        "decision": assignment.get("decision") or "Pending",
                        "note": assignment.get("note") or "",
                    }
                    break
        except (duckdb.Error, FileLockTimeout, OSError, ValueError, TypeError, KeyError, AttributeError) as _error:
            _logger.warning("rightsizing plan probe failed", exc_info=_error)
            plan = None
        dossier.update(
            {
                "resource": {
                    "name": resource[0],
                    "subscriptionId": resource[1],
                    "subscriptionName": resource[2],
                    "resourceGroup": resource[3],
                    "region": resource[4],
                    "sku": resource[5],
                    "tags": json.loads(resource[6] or "{}"),
                }
                if resource
                else None,
                "focusExposure90d": [
                    {
                        "pricingCategory": row[0],
                        "commitmentDiscountType": row[1],
                        "billedCost": row[2],
                        "effectiveCost": row[3],
                    }
                    for row in focus
                ],
                "retailPriceComparison": [
                    {
                        "sku": row[0],
                        "priceProfile": row[1],
                        "currency": row[2],
                        "hourlyPrice": row[3],
                        "monthlyPrice": row[4],
                    }
                    for row in prices
                ],
                "planAssignment": plan,
            }
        )
        return dossier

    def rightsizing_recommendations(
        self,
        *,
        status: str = "",
        subscription_id: str = "",
        resource_id: str = "",
        limit: int = 250,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if subscription_id:
            conditions.append("subscription_id = ?")
            params.append(subscription_id)
        if resource_id:
            conditions.append("resource_id = ?")
            params.append(resource_id.lower())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect(read_only=True) as db:
            total = db.execute(
                f"SELECT count(*) FROM rightsizing_recommendations_current {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT
                    run_id, computed_at, resource_id, resource_name,
                    subscription_id, subscription_name, resource_group, region,
                    kind, status, current_sku, target_sku,
                    evidence_window_days, coverage_flag, telemetry_source,
                    cpu_p95, cpu_maximum, memory_p95,
                    network_in_p95, network_out_p95,
                    metric_coverage_percent, estimated_monthly_saving,
                    currency, value_source, reason, evidence_json, method_version
                FROM rightsizing_recommendations_current
                {where}
                ORDER BY
                    CASE status WHEN 'candidate' THEN 1
                        WHEN 'needs_review' THEN 2
                        WHEN 'target_rate_unavailable' THEN 3
                        WHEN 'warming_up' THEN 4
                        WHEN 'partial_telemetry' THEN 5
                        WHEN 'insufficient_telemetry' THEN 6 ELSE 7 END,
                    estimated_monthly_saving DESC NULLS LAST,
                    resource_name
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            summary = db.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (WHERE status = 'candidate'),
                    count(*) FILTER (WHERE status = 'warming_up'),
                    count(*) FILTER (WHERE status = 'needs_review'),
                    count(*) FILTER (
                        WHERE status IN (
                            'insufficient_telemetry', 'partial_telemetry'
                        )
                    ),
                    count(*) FILTER (WHERE coverage_flag = 'covered'),
                    COALESCE(sum(estimated_monthly_saving) FILTER (
                        WHERE status = 'candidate'
                    ), 0)
                FROM rightsizing_recommendations_current
                """
            ).fetchone()
        return {
            "items": [
                {
                    "runId": row[0],
                    "computedAt": row[1].isoformat(),
                    "resourceId": row[2],
                    "resourceName": row[3],
                    "subscriptionId": row[4],
                    "subscriptionName": row[5],
                    "resourceGroup": row[6],
                    "region": row[7],
                    "kind": row[8],
                    "status": row[9],
                    "currentSku": row[10],
                    "targetSku": row[11],
                    "evidenceWindowDays": row[12],
                    "coverageFlag": row[13],
                    "telemetrySource": row[14],
                    "cpuP95": row[15],
                    "cpuMaximum": row[16],
                    "memoryP95": row[17],
                    "networkInP95": row[18],
                    "networkOutP95": row[19],
                    "metricCoveragePercent": row[20],
                    "estimatedMonthlySaving": row[21],
                    "currency": row[22],
                    "valueSource": row[23],
                    "reason": row[24],
                    "evidence": json.loads(row[25]) if row[25] else {},
                    "methodVersion": row[26],
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": {
                "virtualMachines": summary[0],
                "candidates": summary[1],
                "warmingUp": summary[2],
                "needsReview": summary[3],
                "insufficient": summary[4],
                "covered": summary[5],
                "estimatedMonthlySaving": summary[6],
            },
        }

