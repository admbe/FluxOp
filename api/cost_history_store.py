"""Cost-history domain extracted from the god object (see #13).

FluxDatabase delegates to this mixin so the public surface
``from api.database import FluxDatabase`` is unchanged. Covers the
cost-history run/scope/backfill lifecycle and the cost_history_status
observability view. Coverage helpers (cost_history_coverage,
_cost_coverage_evidence) stay in database.py for now; a follow-up
commit can move them alongside this seam.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import uuid4


def _utc_now():
    from .database import utc_now
    return utc_now()

def utc_now():  # type: ignore[no-redef]
    return _utc_now()

def _parse_iso(value):
    from .database import parse_iso_timestamp
    return parse_iso_timestamp(value)


class CostHistoryMixin:
    def start_cost_history_run(
        self,
        run_id: str,
        expected_scopes: int,
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_history_runs VALUES (
                    ?, ?, NULL, 'running', ?, 0, 0, 0, ''
                )
                """,
                [run_id, utc_now(), expected_scopes],
            )

    def begin_cost_history_scope(
        self,
        run_id: str,
        subscription_id: str,
        cost_type: str,
        query_start: date,
        query_end: date,
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_history_scope_runs VALUES (
                    ?, ?, ?, ?, NULL, 'running', ?, ?, 0, FALSE, NULL, ''
                )
                """,
                [
                    run_id,
                    subscription_id.lower(),
                    cost_type,
                    utc_now(),
                    query_start,
                    query_end,
                ],
            )

    def finish_cost_history_scope(
        self,
        run_id: str,
        subscription_id: str,
        cost_type: str,
        *,
        status: str,
        row_count: int = 0,
        retained_last_good: bool = False,
        status_code: int | None = None,
        message: str = "",
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE cost_history_scope_runs
                SET completed_at = ?, status = ?, row_count = ?,
                    retained_last_good = ?, status_code = ?, message = ?
                WHERE run_id = ? AND subscription_id = ? AND cost_type = ?
                """,
                [
                    utc_now(),
                    status,
                    row_count,
                    retained_last_good,
                    status_code,
                    message[:1000],
                    run_id,
                    subscription_id.lower(),
                    cost_type,
                ],
            )

    def record_cost_history_request_attempt(
        self,
        run_id: str,
        subscription_id: str,
        cost_type: str,
        event: dict[str, Any],
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_history_request_attempts (
                    attempt_id, run_id, subscription_id, cost_type,
                    observed_at, attempt_number, status, status_code,
                    retry_after_seconds, qpu_consumed, qpu_remaining, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid4()),
                    run_id,
                    subscription_id.lower(),
                    cost_type,
                    utc_now(),
                    int(event.get("attemptNumber") or 1),
                    str(event.get("status") or "unknown"),
                    event.get("statusCode"),
                    event.get("retryAfterSeconds"),
                    event.get("qpuConsumed"),
                    event.get("qpuRemaining"),
                    str(event.get("message") or "")[:1000],
                ],
            )

    def finish_cost_history_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_scopes: int,
        failed_scopes: int,
        row_count: int,
        message: str,
    ) -> None:
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE cost_history_runs
                SET completed_at = ?, status = ?, completed_scopes = ?,
                    failed_scopes = ?, row_count = ?, message = ?
                WHERE run_id = ?
                """,
                [
                    utc_now(),
                    status,
                    completed_scopes,
                    failed_scopes,
                    row_count,
                    message[:2000],
                    run_id,
                ],
            )

    def cost_history_scope_order(
        self,
        scopes: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Put never-completed and previously failed scopes first."""
        if not scopes:
            return []
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                WITH ranked AS (
                    SELECT subscription_id, cost_type, status, completed_at,
                        row_number() OVER (
                            PARTITION BY subscription_id, cost_type
                            ORDER BY started_at DESC, run_id DESC
                        ) AS rank
                    FROM cost_history_scope_runs
                )
                SELECT subscription_id, cost_type, status
                FROM ranked WHERE rank = 1
                """
            ).fetchall()
        with self.connect(read_only=True) as db:
            existing_rows = db.execute(
                """
                SELECT DISTINCT subscription_id, cost_type
                FROM daily_cost_history
                """
            ).fetchall()
        last_status = {(row[0], row[1]): row[2] for row in rows}
        existing = {(row[0], row[1]) for row in existing_rows}
        priority = {
            "failed": 0,
            "running": 1,
            "succeeded": 3,
        }
        return sorted(
            scopes,
            key=lambda scope: (
                priority.get(
                    last_status.get(scope, ""),
                    2 if scope in existing else 0,
                ),
                scope[0],
                scope[1],
            ),
        )

    def next_cost_details_backfill_period(
        self,
        subscription_id: str,
        cost_type: str,
        *,
        initial_days: int,
        current_refresh_days: int,
        as_of: date | None = None,
    ) -> tuple[date, date] | None:
        """Return the next bounded calendar-month report for a failed scope."""
        today = as_of or utc_now().date()
        earliest = today - timedelta(days=max(initial_days, 1) - 1)
        month = today.replace(day=1)
        earliest_month = earliest.replace(day=1)
        periods: list[tuple[date, date]] = []
        while month >= earliest_month:
            if month.year == today.year and month.month == today.month:
                period_end = today
            else:
                next_month = (
                    month.replace(year=month.year + 1, month=1)
                    if month.month == 12
                    else month.replace(month=month.month + 1)
                )
                period_end = next_month - timedelta(days=1)
            periods.append((month, period_end))
            month = (
                month.replace(year=month.year - 1, month=12)
                if month.month == 1
                else month.replace(month=month.month - 1)
            )

        normalized_subscription = subscription_id.lower()
        with self.operational_connect(read_only=True) as db:
            rows = db.execute(
                """
                SELECT period_start, period_end, status, last_attempt_at,
                       next_retry_at
                FROM cost_details_backfill_scopes
                WHERE subscription_id = ? AND cost_type = ?
                """,
                [normalized_subscription, cost_type],
            ).fetchall()
        checkpoints = {row[0]: row for row in rows}
        now = utc_now()
        for period_start, period_end in periods:
            checkpoint = checkpoints.get(period_start)
            if checkpoint is None:
                return period_start, period_end
            status = checkpoint[2]
            last_attempt_at = checkpoint[3]
            next_retry_at = checkpoint[4]
            if status == "unsupported":
                continue
            if status == "succeeded":
                is_current = period_start == today.replace(day=1)
                refresh_due = (
                    is_current
                    and last_attempt_at is not None
                    and last_attempt_at
                    <= now - timedelta(days=max(current_refresh_days, 1))
                )
                if refresh_due:
                    return period_start, period_end
                continue
            if status == "running" and last_attempt_at:
                if last_attempt_at > now - timedelta(hours=6):
                    continue
            if next_retry_at is None or next_retry_at <= now:
                return period_start, period_end
        return None

    def begin_cost_details_backfill(
        self,
        subscription_id: str,
        cost_type: str,
        period_start: date,
        period_end: date,
    ) -> None:
        now = utc_now()
        with self.operational_connect() as db:
            db.execute(
                """
                INSERT INTO cost_details_backfill_scopes VALUES (
                    ?, ?, ?, ?, 'running', 1, 0, ?, ?, NULL, NULL,
                    NULL, '', 'azure_cost_details_report'
                )
                ON CONFLICT (subscription_id, cost_type, period_start)
                DO UPDATE SET
                    period_end = excluded.period_end,
                    status = 'running',
                    attempt_count =
                        cost_details_backfill_scopes.attempt_count + 1,
                    last_attempt_at = excluded.last_attempt_at,
                    completed_at = NULL,
                    next_retry_at = NULL,
                    status_code = NULL,
                    message = ''
                """,
                [
                    subscription_id.lower(),
                    cost_type,
                    period_start,
                    period_end,
                    now,
                    now,
                ],
            )

    def finish_cost_details_backfill(
        self,
        subscription_id: str,
        cost_type: str,
        period_start: date,
        *,
        status: str,
        row_count: int = 0,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        message: str = "",
    ) -> None:
        next_retry_at = (
            utc_now() + timedelta(seconds=max(retry_after_seconds, 0))
            if retry_after_seconds is not None
            else None
        )
        with self.operational_connect() as db:
            db.execute(
                """
                UPDATE cost_details_backfill_scopes
                SET status = ?, row_count = ?, completed_at = ?,
                    next_retry_at = ?, status_code = ?, message = ?
                WHERE subscription_id = ? AND cost_type = ?
                  AND period_start = ?
                """,
                [
                    status,
                    row_count,
                    utc_now(),
                    next_retry_at,
                    status_code,
                    message[:1000],
                    subscription_id.lower(),
                    cost_type,
                    period_start,
                ],
            )

    def cost_sync_scope_order(
        self,
        scopes: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        """Prioritize missing and failed current-cost scopes.

        Commitment collection is intentionally first within each priority tier.
        It used to run after every actual and amortized query, which meant a
        tenant throttle could repeatedly prevent a never-collected commitment
        scope from receiving its first attempt.
        """
        if not scopes:
            return []
        with self.connect(read_only=True) as db:
            current_rows = db.execute(
                """
                SELECT source, scope_id, max(observed_at)
                FROM source_sync_state
                WHERE source IN (
                    'ActualCost', 'AmortizedCost', 'CommitmentCoverage'
                )
                GROUP BY source, scope_id
                """
            ).fetchall()
        with self.operational_connect(read_only=True) as db:
            attempt_rows = db.execute(
                """
                WITH ranked AS (
                    SELECT source, scope_id, status, started_at,
                        row_number() OVER (
                            PARTITION BY source, scope_id
                            ORDER BY started_at DESC, sync_id DESC
                        ) AS rank
                    FROM sync_source_runs
                    WHERE source IN (
                        'ActualCost', 'AmortizedCost', 'CommitmentCoverage'
                    )
                )
                SELECT source, scope_id, status
                FROM ranked WHERE rank = 1
                """
            ).fetchall()
        last_status = {
            (str(row[0]), str(row[1])): str(row[2])
            for row in attempt_rows
        }
        current = {
            (str(row[0]), str(row[1])): row[2]
            for row in current_rows
        }
        source_priority = {
            "CommitmentCoverage": 0,
            "ActualCost": 1,
            "AmortizedCost": 2,
        }

        def priority(scope: tuple[str, str, str]) -> tuple[Any, ...]:
            subscription_id, _label, source = scope
            key = (source, subscription_id)
            status = last_status.get(key, "")
            if status == "failed" or key not in current:
                state_priority = 0
            elif status == "running":
                state_priority = 1
            else:
                state_priority = 2
            observed_at = current.get(key)
            return (
                state_priority,
                source_priority.get(source, 9),
                observed_at.isoformat() if observed_at else "",
                subscription_id,
            )

        return sorted(scopes, key=priority)

    def cost_history_status(self) -> dict[str, Any]:
        integration = self.integration()
        names = {
            item["subscriptionId"].lower(): (
                item.get("label") or item["subscriptionId"]
            )
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        }
        with self.operational_connect(read_only=True) as db:
            run = db.execute(
                """
                SELECT run_id, started_at, completed_at, status,
                       expected_scopes, completed_scopes, failed_scopes,
                       row_count, message
                FROM cost_history_runs
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            scopes = db.execute(
                """
                WITH ranked AS (
                    SELECT *,
                        row_number() OVER (
                            PARTITION BY subscription_id, cost_type
                            ORDER BY started_at DESC, run_id DESC
                        ) AS rank
                    FROM cost_history_scope_runs
                )
                SELECT run_id, subscription_id, cost_type, started_at,
                       completed_at, status, query_start, query_end,
                       row_count, retained_last_good, status_code, message
                FROM ranked
                WHERE rank = 1
                ORDER BY
                    CASE status WHEN 'failed' THEN 1
                        WHEN 'running' THEN 2 ELSE 3 END,
                    subscription_id, cost_type
                """
            ).fetchall()
            request_attempt_rows = db.execute(
                """
                SELECT run_id, subscription_id, cost_type, observed_at,
                       status, retry_after_seconds, qpu_consumed, qpu_remaining
                FROM cost_history_request_attempts
                ORDER BY observed_at ASC
                """
            ).fetchall()
            quota_rows = db.execute(
                """
                SELECT name, next_allowed_at, cooldown_until, updated_at
                FROM cost_management_quota_state
                ORDER BY name
                """
            ).fetchall()
            backfill_rows = db.execute(
                """
                SELECT subscription_id, cost_type, period_start, period_end,
                       status, attempt_count, row_count, first_attempt_at,
                       last_attempt_at, completed_at, next_retry_at,
                       status_code, message, source
                FROM cost_details_backfill_scopes
                ORDER BY period_start DESC, subscription_id, cost_type
                """
            ).fetchall()
        attempt_status: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in request_attempt_rows:
            key = (row[0], row[1], row[2])
            observed_at = row[3]
            retry_after = row[5]
            status = attempt_status.setdefault(
                key,
                {
                    "attemptCount": 0,
                    "retryCount": 0,
                    "lastAttemptAt": None,
                    "nextRetryAt": None,
                    "retryAfterSeconds": None,
                    "qpuConsumed": None,
                    "qpuRemaining": None,
                },
            )
            status["attemptCount"] += 1
            if row[4] == "retrying":
                status["retryCount"] += 1
            status["lastAttemptAt"] = (
                observed_at.isoformat() if observed_at else None
            )
            if row[6] is not None:
                status["qpuConsumed"] = row[6]
            if row[7] is not None:
                status["qpuRemaining"] = row[7]
            if retry_after is not None and observed_at is not None:
                status["retryAfterSeconds"] = retry_after
                status["nextRetryAt"] = (
                    observed_at + timedelta(seconds=float(retry_after))
                ).isoformat()
        return {
            "latestRun": {
                "runId": run[0],
                "startedAt": run[1].isoformat(),
                "completedAt": run[2].isoformat() if run[2] else None,
                "status": run[3],
                "expectedScopes": run[4],
                "completedScopes": run[5],
                "failedScopes": run[6],
                "rowCount": run[7],
                "message": run[8],
            }
            if run
            else None,
            "scopes": [
                {
                    "runId": row[0],
                    "subscriptionId": row[1],
                    "subscriptionName": names.get(row[1], row[1]),
                    "costType": row[2],
                    "startedAt": row[3].isoformat(),
                    "completedAt": (
                        row[4].isoformat() if row[4] else None
                    ),
                    "status": row[5],
                    "queryStart": row[6].isoformat(),
                    "queryEnd": row[7].isoformat(),
                    "rowCount": row[8],
                    "retainedLastGood": row[9],
                    "statusCode": row[10],
                    "message": row[11],
                    **attempt_status.get(
                        (row[0], row[1], row[2]),
                        {
                            "attemptCount": 0,
                            "retryCount": 0,
                            "lastAttemptAt": None,
                            "nextRetryAt": None,
                            "retryAfterSeconds": None,
                            "qpuConsumed": None,
                            "qpuRemaining": None,
                        },
                    ),
                }
                for row in scopes
            ],
            "backfill": {
                "completedPeriods": sum(
                    1 for row in backfill_rows if row[4] == "succeeded"
                ),
                "failedPeriods": sum(
                    1 for row in backfill_rows if row[4] == "failed"
                ),
                "runningPeriods": sum(
                    1 for row in backfill_rows if row[4] == "running"
                ),
                "periods": [
                    {
                        "subscriptionId": row[0],
                        "subscriptionName": names.get(row[0], row[0]),
                        "costType": row[1],
                        "periodStart": row[2].isoformat(),
                        "periodEnd": row[3].isoformat(),
                        "status": row[4],
                        "attemptCount": row[5],
                        "rowCount": row[6],
                        "firstAttemptAt": (
                            row[7].isoformat() if row[7] else None
                        ),
                        "lastAttemptAt": (
                            row[8].isoformat() if row[8] else None
                        ),
                        "completedAt": (
                            row[9].isoformat() if row[9] else None
                        ),
                        "nextRetryAt": (
                            row[10].isoformat() if row[10] else None
                        ),
                        "statusCode": row[11],
                        "message": row[12],
                        "source": row[13],
                    }
                    for row in backfill_rows
                ],
            },
            "quota": [
                {
                    "name": row[0],
                    "nextAllowedAt": row[1].isoformat() if row[1] else None,
                    "cooldownUntil": row[2].isoformat() if row[2] else None,
                    "updatedAt": row[3].isoformat() if row[3] else None,
                }
                for row in quota_rows
            ],
        }

