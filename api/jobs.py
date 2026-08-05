from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from functools import wraps
from pathlib import Path
from uuid import uuid4

from azure.identity import AzurePowerShellCredential, ManagedIdentityCredential
from filelock import Timeout

from .config import settings
from .cost import (
    CostManagementError,
    CostManagementProvider,
    SharedRequestGate as CostSharedRequestGate,
)
from .cost_details import CostDetailsReportProvider
from .database import FluxDatabase
from .deployment import DeploymentQuiesced, deployment_lease
from .finops_toolkit import synchronize_open_data
from .focus import (
    azure_blob_manifests,
    focus_error_message,
    import_manifests,
    local_manifests,
)
from .analytics_writer import apply_pending, stage_payload
from .operational_store import SingletonLeaseUnavailable
from .pricing import AzureRetailPriceProvider, retail_price_stage_parts
from .synchronization import process_sync_queue_once, run_sync_worker
from .telemetry import (
    AmaLogAnalyticsProvider,
    AzureMonitorProvider,
    LogicMonitorProvider,
)
from .telemetry_import import import_bootstrap

database = FluxDatabase(
    settings.database_path,
    default_azure_provider=settings.azure_provider,
    operational_database_url=(
        settings.operational_database_url
        if settings.operational_database_enabled
        else ""
    ),
    operational_duckdb_path=settings.operational_duckdb_path,
    focus_cost_enabled=settings.focus_cost_enabled,
    focus_cost_required=settings.focus_cost_required,
)


def publish_analytics_snapshot(force: bool = False) -> int:
    """Publish the current mutable database as an immutable snapshot."""
    from .analytics_snapshot import SnapshotPublisher, storage_from_settings

    publisher = SnapshotPublisher(
        database,
        storage_from_settings(settings),
        retention=settings.analytics_snapshot_retention,
        min_interval_seconds=settings.analytics_snapshot_min_interval_seconds,
        daily_retention_days=settings.analytics_snapshot_daily_retention_days,
    )
    publication = publisher.publish(force=force)
    if publication is None:
        print(
            "Snapshot candidate was rejected; the previous version remains current."
        )
        return 1
    if publication.get("status") == "approved":
        print(
            f"Published analytical snapshot version {publication['version']} "
            f"({publication['fileSizeBytes']:,} bytes, "
            f"{publication.get('durationMs', 0):,} ms)."
        )
    return 0


def _with_publication(exit_code: int) -> int:
    """Publish a snapshot after a successful data-writing job.

    Publication failure never fails the job that produced the data; the
    previous approved snapshot simply remains current.
    """
    if exit_code == 0 and settings.analytics_snapshot_publish:
        try:
            publish_analytics_snapshot()
        except Exception as error:
            print(f"Snapshot publication failed: {error}")
    return exit_code


def deployment_guarded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with deployment_lease(settings.database_path):
                return function(*args, **kwargs)
        except DeploymentQuiesced:
            print("Flux deployment quiesce is active; scheduled work skipped.")
            return 0

    return wrapped


@deployment_guarded
def scheduled_sync(
    sources: list[str] | tuple[str, ...] | None = None,
) -> int:
    database.init()
    integration = database.integration()
    if not integration["enabled"]:
        print("Azure integration is disabled; scheduled sync skipped.")
        return 0
    requested = list(
        sources or ("inventory", "advisor", "intelligence", "policy", "cost")
    )
    sync_id = database.start_sync(
        integration["authMode"],
        trigger="scheduled",
        sources=requested,
    )
    print(
        f"Queued scheduled Azure synchronization {sync_id} "
        f"for {', '.join(requested)}."
    )
    return 0


@deployment_guarded
def pipeline_alerts() -> int:
    """Push new pipeline warnings to the configured webhook.

    Detection alone failed four times in one week: warnings rendered on the
    Administration page while nobody was looking at it. This job makes them
    arrive instead.
    """
    from .alerts import WarningState, post_webhook, select_notifications
    from .database import utc_now as _utc_now

    database.init_operational()
    if not settings.alert_webhook_url:
        print(
            "FLUX_ALERT_WEBHOOK_URL is not configured; pipeline warnings "
            "stay on the Administration page only."
        )
        # SLO state still advances so /api/operations/slo shows honest
        # since/last-notified tracking; messages are logged, not posted.
        slo_alerts()
        return 0
    warnings = list(database.pipeline_status()["warnings"])
    # Freshness reads the analytical store and can wait behind a writer;
    # a degraded source is worth alerting on, but a busy database is not
    # worth failing the whole alert run over.
    try:
        for item in database.source_freshness():
            if item.get("health") in ("degraded", "stale"):
                age = item.get("ageHours")
                warnings.append(
                    f"Data source {item.get('label') or item.get('source')} "
                    f"is {item['health']}"
                    + (f" (age {age:.0f}h)." if age is not None else ".")
                )
    except Exception as error:
        print(f"Freshness check skipped for this alert run: {error}")

    now = _utc_now()
    with database.operational_connect() as db:
        known = {
            str(row[0]): WarningState(str(row[0]), row[1], row[2])
            for row in db.execute(
                "SELECT warning_key, first_seen, last_notified "
                "FROM pipeline_alert_state"
            ).fetchall()
        }
        to_send, active = select_notifications(
            warnings, known, now, settings.alert_renotify_hours
        )
        if to_send:
            post_webhook(
                settings.alert_webhook_url,
                [message for _, message in to_send],
            )
            for key, _ in to_send:
                db.execute(
                    """
                    INSERT INTO pipeline_alert_state VALUES (?, ?, ?)
                    ON CONFLICT (warning_key) DO UPDATE
                    SET last_notified = excluded.last_notified
                    """,
                    [key, now, now],
                )
        # Rows for resolved warnings are dropped so a condition that
        # returns later re-alerts immediately as new.
        if active:
            placeholders = ", ".join("?" for _ in active)
            db.execute(
                f"DELETE FROM pipeline_alert_state "
                f"WHERE warning_key NOT IN ({placeholders})",
                active,
            )
        else:
            db.execute("DELETE FROM pipeline_alert_state")
    print(
        f"Pipeline alerts: {len(warnings)} active warning(s), "
        f"{len(to_send)} notified."
    )
    slo_alerts()
    return 0


def slo_alerts() -> int:
    """Evaluate SLOs and notify state transitions through the webhook.

    Runs on the flux-alerts cadence. Warnings say a component misbehaved;
    these say the service commitment itself is (or is no longer) being
    missed -- enter/worsen notifies immediately, persistence re-notifies
    slowly, and recovery reports exactly once.
    """
    from .alerts import post_webhook
    from .database import utc_now as _utc_now
    from .slo import (
        DEFAULT_SLOS,
        SloState,
        apply_threshold_overrides,
        evaluate_slo,
        select_slo_transitions,
    )

    definitions = apply_threshold_overrides(
        DEFAULT_SLOS, settings.slo_threshold_overrides
    )
    measurements = database.slo_measurements(
        initial_days=settings.cost_history_initial_days
    )
    evaluations = [
        evaluate_slo(definition, measurements.get(definition.key))
        for definition in definitions
    ]
    now = _utc_now()
    with database.operational_connect() as db:
        known = {
            str(row[0]): SloState(str(row[0]), str(row[1]), row[2], row[3])
            for row in db.execute(
                "SELECT slo_key, state, since, last_notified FROM slo_state"
            ).fetchall()
        }
        messages, upserts, clears = select_slo_transitions(
            evaluations, known, now, settings.alert_renotify_hours
        )
        if messages and settings.alert_webhook_url:
            post_webhook(settings.alert_webhook_url, messages)
        elif messages:
            for message in messages:
                print(f"SLO (webhook not configured): {message}")
        db.execute("DELETE FROM slo_state")
        for key, state, since, last_notified in upserts:
            db.execute(
                "INSERT INTO slo_state VALUES (?, ?, ?, ?)",
                [key, state, since, last_notified],
            )
        # Recovery removes the row; the DELETE above already handled it, the
        # clears list exists so the log states what recovered.
        for key in clears:
            print(f"SLO recovered: {key}")
    states = {evaluation["key"]: evaluation["state"] for evaluation in evaluations}
    print(f"SLO evaluation: {states}, {len(messages)} notified.")
    return 0


@deployment_guarded
def analytics_history_prune() -> int:
    """Daily retention sweep over the append-only analytical histories."""
    database.init()
    try:
        with database.singleton_lease("analytics-history-prune"):
            deleted = database.prune_analytics_history(
                history_days=settings.analytics_history_retention_days,
                telemetry_sample_days=(
                    settings.telemetry_sample_retention_days
                ),
            )
            removed = {k: v for k, v in deleted.items() if v}
            if removed:
                detail = ", ".join(
                    f"{table} {count:,}"
                    for table, count in sorted(removed.items())
                )
                print(f"Analytics history retention removed: {detail}.")
            else:
                print("Analytics history retention: nothing to remove.")
            return 0
    except (SingletonLeaseUnavailable, Timeout):
        print("An analytics history prune is already running.")
        return 0
    except Exception as error:
        print(f"Analytics history prune failed: {error}")
        return 1


@deployment_guarded
def sync_worker_once() -> int:
    database.init()
    processed = process_sync_queue_once(database, settings)
    print("Processed one Azure synchronization." if processed else "No sync work queued.")
    return 0


def _credential(integration: dict):
    if integration["authMode"] == "managed_identity":
        return ManagedIdentityCredential(
            client_id=settings.managed_identity_client_id or None
        )
    return AzurePowerShellCredential(tenant_id=integration.get("tenantId") or "")


def _month_offset(month: date, count: int) -> date:
    total = month.year * 12 + (month.month - 1) + count
    return date(total // 12, total % 12 + 1, 1)


def _cost_history_windows(
    start_date: date,
    end_date: date,
    chunk_days: int,
) -> list[tuple[date, date]]:
    """Split a cost query into bounded, newest-first inclusive windows."""
    windows: list[tuple[date, date]] = []
    cursor = end_date
    span = max(chunk_days, 1)
    while cursor >= start_date:
        window_start = max(
            start_date,
            cursor - timedelta(days=span - 1),
        )
        windows.append((window_start, cursor))
        cursor = window_start - timedelta(days=1)
    return windows


@deployment_guarded
def cost_history_sync() -> int:
    """Collect checkpointed daily cost separately from the primary sync."""
    database.init()
    integration = database.integration()
    if not integration["enabled"] or not settings.cost_management_enabled:
        print("Cost Management integration is disabled; daily history skipped.")
        return 0
    subscriptions = [
        item
        for item in integration.get("subscriptions", [])
        if item.get("subscriptionId")
    ]
    if not subscriptions:
        print("No Azure subscriptions are configured; daily history skipped.")
        return 0
    run_id = f"cost-history-{uuid4()}"
    run_started = False
    completed = 0
    row_count = 0
    warnings_by_scope: dict[tuple[str, str], str] = {}
    backfill_candidates: list[tuple[str, str, date, date]] = []
    backfill_attempted = 0
    backfill_completed = 0
    backfill_rows = 0
    try:
        with database.singleton_lease("cost-history"):
            provider = CostManagementProvider(
                credential=_credential(integration),
                management_endpoint=settings.azure_management_endpoint,
                api_version=settings.cost_management_api_version,
                timeout_seconds=settings.cost_management_timeout_seconds,
                max_retries=settings.cost_management_max_retries,
                request_delay_seconds=(
                    settings.cost_management_request_delay_seconds
                ),
                request_gate=CostSharedRequestGate(
                    database,
                    tenant_key=integration.get("tenantId") or "default",
                    qpu_windows=[
                        (10, settings.cost_management_qpu_budget_10_seconds),
                        (60, settings.cost_management_qpu_budget_60_seconds),
                        (3600, settings.cost_management_qpu_budget_3600_seconds),
                    ],
                ),
                client_type=settings.cost_management_client_type,
            )
            details_provider = CostDetailsReportProvider(
                credential=_credential(integration),
                management_endpoint=settings.azure_management_endpoint,
                api_version=settings.cost_management_api_version,
                timeout_seconds=settings.cost_management_timeout_seconds,
                max_retries=settings.cost_management_max_retries,
                poll_interval_seconds=(
                    settings.cost_details_poll_interval_seconds
                ),
                max_poll_attempts=settings.cost_details_max_poll_attempts,
                client_type=settings.cost_management_client_type,
                request_gate=CostSharedRequestGate(
                    database,
                    name="cost-management",
                    tenant_key=integration.get("tenantId") or "default",
                    qpu_windows=[
                        (10, settings.cost_management_qpu_budget_10_seconds),
                        (60, settings.cost_management_qpu_budget_60_seconds),
                        (3600, settings.cost_management_qpu_budget_3600_seconds),
                    ],
                ),
            )
            scopes = [
                (item["subscriptionId"].lower(), cost_type)
                for item in subscriptions
                for cost_type in ("ActualCost", "AmortizedCost")
            ]
            scopes = database.cost_history_scope_order(scopes)
            labels = {
                item["subscriptionId"].lower(): (
                    item.get("label") or item["subscriptionId"]
                )
                for item in subscriptions
            }
            database.start_cost_history_run(run_id, len(scopes))
            run_started = True
            access_token = provider.access_token()
            end_date = date.today()
            for index, (subscription_id, cost_type) in enumerate(
                scopes,
                start=1,
            ):
                label = labels.get(subscription_id, subscription_id)
                start_date = database.daily_cost_query_start(
                    subscription_id,
                    cost_type,
                    initial_days=settings.cost_history_initial_days,
                    refresh_days=settings.cost_history_refresh_days,
                    as_of=end_date,
                )
                database.begin_cost_history_scope(
                    run_id,
                    subscription_id,
                    cost_type,
                    start_date,
                    end_date,
                )
                print(
                    f"[{index}/{len(scopes)}] Collecting {label} "
                    f"{cost_type} from {start_date} to {end_date}.",
                    flush=True,
                )
                scope_rows = 0
                try:
                    windows = _cost_history_windows(
                        start_date,
                        end_date,
                        settings.cost_history_chunk_days,
                    )
                    for window_index, (
                        window_start,
                        window_end,
                    ) in enumerate(windows, start=1):
                        print(
                            f"[{index}/{len(scopes)}] Window "
                            f"{window_index}/{len(windows)}: "
                            f"{window_start} to {window_end}.",
                            flush=True,
                        )
                        records = provider.fetch_daily_scope(
                            subscription_id,
                            cost_type,
                            window_start,
                            window_end,
                            access_token=access_token,
                            attempt_callback=lambda event, scope_subscription=subscription_id, scope_cost_type=cost_type: database.record_cost_history_request_attempt(
                                run_id,
                                scope_subscription,
                                scope_cost_type,
                                event,
                            ),
                        )
                        stored = database.store_daily_cost_scope(
                            (
                                f"{run_id}:{window_start.isoformat()}:"
                                f"{window_end.isoformat()}"
                            ),
                            subscription_id,
                            cost_type,
                            records,
                            start_date=window_start,
                            end_date=window_end,
                        )
                        scope_rows += stored
                        row_count += stored
                        del records
                        if window_index < len(windows):
                            time.sleep(
                                settings.cost_management_request_delay_seconds
                            )
                    completed += 1
                    database.finish_cost_history_scope(
                        run_id,
                        subscription_id,
                        cost_type,
                        status="succeeded",
                        row_count=scope_rows,
                    )
                    print(
                        f"[{index}/{len(scopes)}] Stored "
                        f"{scope_rows:,} rows across "
                        f"{len(windows):,} bounded windows.",
                        flush=True,
                    )
                except CostManagementError as error:
                    warning = f"{label} {cost_type}: {error}"
                    warnings_by_scope[(subscription_id, cost_type)] = warning
                    if (
                        settings.cost_details_backfill_enabled
                        and error.status_code not in {401, 403}
                    ):
                        backfill_candidates.append(
                            (
                                subscription_id,
                                cost_type,
                                start_date,
                                end_date,
                            )
                        )
                    database.finish_cost_history_scope(
                        run_id,
                        subscription_id,
                        cost_type,
                        status="failed",
                        row_count=scope_rows,
                        retained_last_good=(
                            scope_rows > 0 or start_date
                            > end_date
                            - timedelta(
                                days=settings.cost_history_initial_days - 1
                            )
                        ),
                        status_code=error.status_code,
                        message=str(error),
                    )
                    print(
                        f"[{index}/{len(scopes)}] Warning: {warning}",
                        flush=True,
                    )
                    if error.status_code == 429:
                        time.sleep(
                            settings.cost_management_throttle_cooldown_seconds
                        )
                except Exception as error:
                    warning = (
                        f"{label} {cost_type} storage: {error}"
                    )
                    warnings_by_scope[
                        (subscription_id, cost_type)
                    ] = warning
                    database.finish_cost_history_scope(
                        run_id,
                        subscription_id,
                        cost_type,
                        status="failed",
                        row_count=scope_rows,
                        retained_last_good=scope_rows > 0,
                        message=str(error),
                    )
                    print(
                        f"[{index}/{len(scopes)}] Warning: {warning}",
                        flush=True,
                    )
                time.sleep(
                    settings.cost_management_request_delay_seconds
                )
            # Monthly totals for the fiscal-year outlook. One tiny query per
            # scope (no grouping), plus a one-time extra window on first run
            # to reach Cost Management's full thirteen-month lookback.
            monthly_rows = 0
            monthly_skipped = 0
            current_month = end_date.replace(day=1)
            main_start = _month_offset(current_month, -11)
            # Rotate the starting scope daily and skip scopes refreshed in
            # the last day: runs regularly die partway through, and a fixed
            # order would refill the same head scopes forever while the tail
            # never got its first backfill.
            monthly_scopes = list(scopes)
            if monthly_scopes:
                rotation = end_date.toordinal() % len(monthly_scopes)
                monthly_scopes = (
                    monthly_scopes[rotation:] + monthly_scopes[:rotation]
                )
            for monthly_index, (subscription_id, cost_type) in enumerate(
                monthly_scopes, start=1
            ):
                label = labels.get(subscription_id, subscription_id)
                if database.monthly_cost_scope_fresh(
                    subscription_id, cost_type, current_month
                ):
                    monthly_skipped += 1
                    continue
                # Print per scope: the platform watchdog kills the job after
                # 120 quiet seconds, and a fully successful pass through this
                # phase previously produced no output at all.
                print(
                    f"[monthly {monthly_index}/{len(monthly_scopes)}] {label} "
                    f"{cost_type}",
                    flush=True,
                )
                try:
                    windows = [(main_start, end_date)]
                    if database.monthly_cost_scope_bounds(
                        subscription_id, cost_type
                    ) is None:
                        extra_start = _month_offset(current_month, -13)
                        extra_end = _month_offset(
                            current_month, -11
                        ) - timedelta(days=1)
                        windows.insert(0, (extra_start, extra_end))
                    for window_start, window_end in windows:
                        records = provider.fetch_monthly_scope(
                            subscription_id,
                            cost_type,
                            window_start,
                            window_end,
                            access_token=access_token,
                            attempt_callback=lambda event: print(
                                "[monthly] request "
                                f"{event.get('status', 'attempt')} "
                                f"(HTTP {event.get('statusCode', '-')})",
                                flush=True,
                            ),
                        )
                        stored = database.store_monthly_cost_scope(
                            f"{run_id}:monthly",
                            subscription_id,
                            cost_type,
                            records,
                            start_month=window_start.replace(day=1),
                            end_month=window_end.replace(day=1),
                        )
                        monthly_rows += stored
                        print(
                            f"[monthly {monthly_index}/"
                            f"{len(monthly_scopes)}] stored {stored} "
                            "months.",
                            flush=True,
                        )
                except CostManagementError as error:
                    print(
                        f"Monthly totals warning for {label} {cost_type}: "
                        f"{error}",
                        flush=True,
                    )
                    if error.status_code == 429:
                        time.sleep(
                            settings.cost_management_throttle_cooldown_seconds
                        )
                except Exception as error:
                    print(
                        f"Monthly totals storage warning for {label} "
                        f"{cost_type}: {error}",
                        flush=True,
                    )
            print(
                f"Stored {monthly_rows:,} monthly cost totals; "
                f"{monthly_skipped} of {len(monthly_scopes)} scopes were "
                "already fresh.",
                flush=True,
            )
            if settings.cost_details_backfill_enabled:
                # Day-level holes behind succeeded checkpoints never re-enter
                # the failure-driven fallback on their own; the coverage
                # ledger feeds them back in, oldest month first, bounded so
                # backfill cannot starve the daily sync.
                coverage_gaps = database.requeue_cost_coverage_gaps(
                    initial_days=settings.cost_history_initial_days,
                    as_of=end_date,
                    limit=settings.cost_coverage_requeue_months,
                )
                for gap in coverage_gaps["requeued"]:
                    print(
                        "Coverage gap requeued: "
                        f"{labels.get(gap['subscriptionId'], gap['subscriptionId'])} "
                        f"{gap['costType']} {gap['periodStart']} "
                        f"({gap['missingDays']} missing days).",
                        flush=True,
                    )
                    backfill_candidates.append(
                        (
                            gap["subscriptionId"],
                            gap["costType"],
                            date.fromisoformat(gap["periodStart"]),
                            date.fromisoformat(gap["periodEnd"]),
                        )
                    )
            details_access_token = (
                details_provider.access_token()
                if backfill_candidates
                else None
            )
            for (
                subscription_id,
                cost_type,
                query_start,
                query_end,
            ) in backfill_candidates:
                if (
                    backfill_attempted
                    >= max(settings.cost_details_max_reports_per_run, 0)
                ):
                    break
                label = labels.get(subscription_id, subscription_id)
                period = database.next_cost_details_backfill_period(
                    subscription_id,
                    cost_type,
                    initial_days=settings.cost_history_initial_days,
                    current_refresh_days=(
                        settings.cost_details_current_refresh_days
                    ),
                    as_of=end_date,
                )
                if period is None:
                    continue
                period_start, period_end = period
                backfill_attempted += 1
                database.begin_cost_details_backfill(
                    subscription_id,
                    cost_type,
                    period_start,
                    period_end,
                )
                print(
                    f"Cost Details fallback: collecting {label} "
                    f"{cost_type} from {period_start} to {period_end}.",
                    flush=True,
                )
                report_events: list[dict] = []

                def record_report_attempt(
                    event: dict,
                    scope_subscription: str = subscription_id,
                    scope_cost_type: str = cost_type,
                ) -> None:
                    report_events.append(event)
                    database.record_cost_history_request_attempt(
                        run_id,
                        scope_subscription,
                        scope_cost_type,
                        event,
                    )

                try:
                    records = details_provider.fetch_scope(
                        subscription_id,
                        cost_type,
                        period_start,
                        period_end,
                        access_token=details_access_token,
                        attempt_callback=record_report_attempt,
                    )
                    stored = database.store_daily_cost_scope(
                        f"cost-details-{uuid4()}",
                        subscription_id,
                        cost_type,
                        records,
                        start_date=period_start,
                        end_date=period_end,
                    )
                    database.finish_cost_details_backfill(
                        subscription_id,
                        cost_type,
                        period_start,
                        status="succeeded",
                        row_count=stored,
                        message=(
                            "Cost Details report completed and replaced the "
                            "checkpointed monthly scope."
                        ),
                    )
                    database.finish_cost_history_scope(
                        run_id,
                        subscription_id,
                        cost_type,
                        status=(
                            "succeeded"
                            if (
                                period_start <= query_start
                                and period_end >= query_end
                            )
                            else "failed"
                        ),
                        row_count=stored,
                        retained_last_good=not (
                            period_start <= query_start
                            and period_end >= query_end
                        ),
                        message=(
                            "Query API was unavailable; a bounded Cost "
                            "Details checkpoint completed. Remaining history "
                            "will stay retry-eligible."
                        ),
                    )
                    row_count += stored
                    backfill_completed += 1
                    backfill_rows += stored
                    if (
                        period_start <= query_start
                        and period_end >= query_end
                    ):
                        warnings_by_scope.pop(
                            (subscription_id, cost_type),
                            None,
                        )
                        completed += 1
                    print(
                        f"Cost Details fallback stored {stored:,} rows for "
                        f"{label} {cost_type}.",
                        flush=True,
                    )
                except CostManagementError as error:
                    retry_after = max(
                        (
                            float(event.get("retryAfterSeconds") or 0)
                            for event in report_events
                        ),
                        default=0,
                    )
                    database.finish_cost_details_backfill(
                        subscription_id,
                        cost_type,
                        period_start,
                        status="failed",
                        status_code=error.status_code,
                        retry_after_seconds=(
                            retry_after
                            or settings.cost_management_throttle_cooldown_seconds
                        ),
                        message=str(error),
                    )
                    fallback_warning = (
                        f"{label} {cost_type} Cost Details fallback: {error}"
                    )
                    warnings_by_scope[
                        (subscription_id, cost_type)
                    ] = fallback_warning
                    print(f"Warning: {fallback_warning}", flush=True)
            warnings = list(warnings_by_scope.values())
            if not completed:
                detail = warnings[0] if warnings else "No scopes completed."
                raise RuntimeError(
                    f"Daily Cost Management collection failed: {detail}"
                )
            anomaly = database.compute_cost_anomalies(
                f"cost-anomaly-{uuid4()}",
                latency_days=settings.cost_anomaly_latency_days,
                minimum_history_days=(
                    settings.cost_anomaly_minimum_history_days
                ),
                minimum_baseline_points=(
                    settings.cost_anomaly_minimum_baseline_points
                ),
                baseline_weeks=settings.cost_anomaly_baseline_weeks,
                threshold_k=settings.cost_anomaly_threshold_k,
                minimum_increase=settings.cost_anomaly_minimum_increase,
                as_of=end_date,
            )
            message = (
                f"Stored {row_count:,} daily cost rows across "
                f"{completed:,} of {len(subscriptions) * 2:,} scopes. "
                f"{anomaly['message']}"
            )
            if backfill_completed:
                message += (
                    f" Cost Details completed {backfill_completed:,} "
                    f"monthly checkpoints with {backfill_rows:,} rows."
                )
            if warnings:
                message += (
                    f" {len(warnings):,} warnings; first: "
                    f"{warnings[0][:300]}"
                )
            database.finish_cost_history_run(
                run_id,
                status="partial" if warnings else "succeeded",
                completed_scopes=completed,
                failed_scopes=len(warnings),
                row_count=row_count,
                message=message,
            )
            print(message)
            return 0
    except (SingletonLeaseUnavailable, Timeout):
        print("Daily Cost Management history is already running.")
        return 0
    except Exception as error:
        if run_started:
            database.finish_cost_history_run(
                run_id,
                status="failed",
                completed_scopes=completed,
                failed_scopes=max(
                    len(subscriptions) * 2 - completed,
                    len(warnings_by_scope),
                ),
                row_count=row_count,
                message=str(error),
            )
        print(f"Daily Cost Management history failed: {error}")
        return 1


@deployment_guarded
def focus_cost_sync() -> int:
    database.init()
    if not settings.focus_cost_enabled:
        print("FOCUS Cost ingestion is disabled.")
        return 0
    integration = database.integration()
    run_id = f"focus-{uuid4()}"
    database.start_focus_import(run_id)
    try:
        with database.singleton_lease("focus-import"):
            if settings.focus_local_path:
                manifests = local_manifests(settings.focus_local_path)
                source = str(settings.focus_local_path)
            else:
                manifests = azure_blob_manifests(
                    settings.focus_storage_account_url,
                    settings.focus_storage_container,
                    settings.focus_storage_prefix,
                    _credential(integration),
                )
                source = (
                    f"{settings.focus_storage_account_url}/"
                    f"{settings.focus_storage_container}/"
                    f"{settings.focus_storage_prefix}"
                )
            result = import_manifests(
                database,
                run_id,
                manifests,
                maximum_manifests=settings.focus_max_manifests_per_run,
            )
            message = (
                f"FOCUS source {source}: imported {result['imported']:,} "
                f"manifests and {result['charges']:,} charges; "
                f"{result['skipped']:,} manifests already governed or deferred."
            )
            database.finish_focus_import(
                run_id,
                status="succeeded",
                manifest_count=result["imported"],
                charge_count=result["charges"],
                message=message,
            )
            print(message)
            return 0
    except (SingletonLeaseUnavailable, Timeout):
        database.finish_focus_import(
            run_id,
            status="skipped",
            manifest_count=0,
            charge_count=0,
            message="A FOCUS Cost import is already running.",
        )
        print("A FOCUS Cost import is already running.")
        return 0
    except Exception as error:
        message = focus_error_message(error)
        database.finish_focus_import(
            run_id,
            status="failed",
            manifest_count=0,
            charge_count=0,
            message=message,
        )
        print(f"FOCUS Cost import failed: {message}")
        return 1


@deployment_guarded
def azure_monitor_sync() -> int:
    database.init()
    try:
        with database.singleton_lease("telemetry-azure"):
            run_id = database.start_telemetry_run("azure_monitor")
            try:
                credential = _credential(database.integration())
                targets = database.telemetry_targets(settings.azure_monitor_batch_size)
                summaries, attempts = AzureMonitorProvider(
                    credential,
                    settings.azure_management_endpoint,
                    settings.azure_monitor_days,
                ).fetch(targets)
                database.store_telemetry_attempts(run_id, attempts)
                database.store_telemetry_summaries(run_id, summaries)
                # Guest memory from the AMA/DCR Log Analytics workspace,
                # stored before right-sizing reconciles so memory evidence
                # feeds the same evaluation. Its own telemetry run and its
                # own failure domain: a workspace problem must not fail
                # platform metrics.
                if settings.ama_log_analytics_workspace_id:
                    ama_run_id = database.start_telemetry_run(
                        "ama_log_analytics"
                    )
                    try:
                        ama_summaries, ama_attempts = AmaLogAnalyticsProvider(
                            credential,
                            settings.ama_log_analytics_workspace_id,
                            settings.ama_telemetry_days,
                        ).fetch(targets)
                        database.store_telemetry_attempts(
                            ama_run_id, ama_attempts
                        )
                        database.store_telemetry_summaries(
                            ama_run_id, ama_summaries
                        )
                        covered = sum(
                            1
                            for item in ama_attempts
                            if item["status"] == "covered"
                        )
                        database.finish_telemetry_run(
                            ama_run_id,
                            "succeeded",
                            len(targets),
                            (
                                f"Collected guest memory for {covered:,} of "
                                f"{len(targets):,} VMs from Log Analytics."
                            ),
                        )
                    except Exception as error:
                        database.finish_telemetry_run(
                            ama_run_id,
                            "failed",
                            len(targets),
                            str(error)[:500],
                        )
                        print(
                            "AMA guest telemetry failed (platform metrics "
                            f"unaffected): {error}"
                        )
                warnings = [
                    f"{item['resourceId']}: {item['message']}"
                    for item in attempts
                    if item["status"] == "error"
                ]
                if targets and not summaries:
                    detail = warnings[0] if warnings else "No metric data was returned."
                    raise RuntimeError(
                        f"Azure Monitor returned no metric summaries; first warning: {detail}"
                    )
                # Current telemetry views intentionally expose successful runs
                # only. Publish the metric run before reconciling its evidence;
                # any reconciliation failure marks the run failed below.
                database.finish_telemetry_run(
                    run_id, "succeeded", len(targets), "Reconciling telemetry."
                )
                recommendation_count = database.compute_rightsizing_recommendations(
                    run_id,
                    minimum_window_days=settings.rightsizing_min_window_days,
                    minimum_coverage_percent=(
                        settings.rightsizing_min_coverage_percent
                    ),
                    idle_cpu_p95=settings.rightsizing_idle_cpu_p95,
                    idle_cpu_maximum=settings.rightsizing_idle_cpu_maximum,
                    idle_network_p95_bytes=(
                        settings.rightsizing_idle_network_p95_bytes
                    ),
                    review_cpu_p95=settings.rightsizing_review_cpu_p95,
                    memory_review_percent=(
                        settings.rightsizing_memory_review_percent
                    ),
                    cpu_disagreement_percent=(
                        settings.rightsizing_cpu_disagreement_percent
                    ),
                )
                message = (
                    f"Collected {len(summaries):,} metric summaries for "
                    f"{len(targets):,} virtual machines and evaluated "
                    f"{recommendation_count:,} coverage-aware right-sizing records."
                )
                outcome_counts = {
                    status: sum(1 for item in attempts if item["status"] == status)
                    for status in ("covered", "no_data", "error")
                }
                message += (
                    f" Coverage: {outcome_counts['covered']:,} covered, "
                    f"{outcome_counts['no_data']:,} no data, "
                    f"{outcome_counts['error']:,} errors."
                )
                if warnings:
                    message += f" {len(warnings):,} resources returned warnings; first: {warnings[0]}"
                database.finish_telemetry_run(run_id, "succeeded", len(targets), message)
                print(message)
                return 0
            except Exception as error:
                database.finish_telemetry_run(run_id, "failed", 0, str(error))
                print(str(error))
                return 1
    except (SingletonLeaseUnavailable, Timeout):
        print("Azure Monitor collection is already running.")
        return 0


@deployment_guarded
def logicmonitor_discovery_sync() -> int:
    database.init()
    try:
        with database.singleton_lease("telemetry-logicmonitor"):
            run_id = database.start_telemetry_run("logicmonitor")
            try:
                provider = LogicMonitorProvider(
                    settings.logicmonitor_account,
                    settings.logicmonitor_bearer_token,
                    settings.logicmonitor_group_ids,
                    settings.logicmonitor_request_delay_ms,
                )
                devices = provider.discover()
                resources = database.telemetry_targets(100_000)
                matches = provider.match(devices, resources)
                database.store_source_matches(run_id, matches)
                counts = {
                    status: sum(1 for item in matches if item["status"] == status)
                    for status in ("matched", "ambiguous", "unmatched")
                }
                message = (
                    f"Discovered {len(devices):,} LogicMonitor devices: "
                    f"{counts['matched']:,} matched, {counts['ambiguous']:,} ambiguous, "
                    f"{counts['unmatched']:,} unmatched."
                )
                database.finish_telemetry_run(run_id, "succeeded", len(devices), message)
                print(message)
                return 0
            except Exception as error:
                database.finish_telemetry_run(run_id, "failed", 0, str(error))
                print(str(error))
                return 1
    except (SingletonLeaseUnavailable, Timeout):
        print("LogicMonitor discovery is already running.")
        return 0


@deployment_guarded
def logicmonitor_metrics_sync() -> int:
    database.init()
    try:
        with database.singleton_lease("telemetry-logicmonitor-metrics"):
            run_id = database.start_telemetry_run("logicmonitor")
            try:
                provider = LogicMonitorProvider(
                    settings.logicmonitor_account,
                    settings.logicmonitor_bearer_token,
                    settings.logicmonitor_group_ids,
                    settings.logicmonitor_request_delay_ms,
                )
                targets = database.logicmonitor_metric_targets(
                    settings.logicmonitor_metric_batch_size,
                    initial_hours=settings.logicmonitor_initial_window_hours,
                    maximum_window_hours=(
                        settings.logicmonitor_maximum_window_hours
                    ),
                )
                attempts = []
                completed_resources = []
                warnings = []
                sample_count = 0
                for target in targets:
                    try:
                        samples, target_warnings = provider.fetch_metrics(
                            target,
                            target["windowStart"],
                            target["windowEnd"],
                            maximum_instances=(
                                settings.logicmonitor_maximum_instances
                            ),
                        )
                        database.store_telemetry_samples(
                            run_id,
                            samples,
                            retention_days=(
                                settings.logicmonitor_metric_retention_days
                            ),
                        )
                        sample_count += len(samples)
                        completed_resources.append(target["resourceId"])
                        message = (
                            f"Collected {len(samples):,} samples through "
                            f"{target['windowEnd'].isoformat()}."
                        )
                        if target_warnings:
                            warning = (
                                f"{target['sourceName']}: "
                                + "; ".join(target_warnings)
                            )
                            warnings.append(warning)
                            message += (
                                f" {len(target_warnings)} datasource warnings; "
                                f"first: {target_warnings[0]}"
                            )
                        else:
                            database.update_telemetry_checkpoint(
                                target["sourceResourceId"],
                                target["windowEnd"],
                                status="succeeded",
                                message=message,
                            )
                        attempts.append(
                            {
                                "resourceId": target["resourceId"],
                                "source": "logicmonitor",
                                "status": "covered" if samples else "no_data",
                                "metricCount": len(
                                    {item["metric"] for item in samples}
                                ),
                                "message": message,
                            }
                        )
                    except Exception as error:
                        message = str(error)[:500]
                        warnings.append(
                            f"{target['sourceName']}: {message}"
                        )
                        attempts.append(
                            {
                                "resourceId": target["resourceId"],
                                "source": "logicmonitor",
                                "status": "error",
                                "metricCount": 0,
                                "message": message,
                            }
                        )
                database.store_telemetry_attempts(run_id, attempts)
                if targets and not completed_resources:
                    raise RuntimeError(
                        "LogicMonitor returned no completed metric targets; "
                        f"first error: {warnings[0] if warnings else 'unknown'}"
                    )
                # Once per run, not once per target (see
                # store_telemetry_samples): a failed prune must not fail an
                # import whose samples already committed.
                try:
                    database.prune_telemetry_samples(
                        settings.logicmonitor_metric_retention_days
                    )
                except Exception as error:
                    print(f"Telemetry sample retention deferred: {error}")
                summary_count = database.summarize_logicmonitor_samples(
                    run_id,
                    completed_resources,
                    history_days=(
                        settings.logicmonitor_metric_history_days
                    ),
                )
                database.finish_telemetry_run(
                    run_id,
                    "succeeded",
                    len(targets),
                    "Reconciling telemetry.",
                )
                recommendation_count = (
                    database.compute_rightsizing_recommendations(
                        run_id,
                        minimum_window_days=(
                            settings.rightsizing_min_window_days
                        ),
                        minimum_coverage_percent=(
                            settings.rightsizing_min_coverage_percent
                        ),
                        idle_cpu_p95=settings.rightsizing_idle_cpu_p95,
                        idle_cpu_maximum=(
                            settings.rightsizing_idle_cpu_maximum
                        ),
                        idle_network_p95_bytes=(
                            settings.rightsizing_idle_network_p95_bytes
                        ),
                        review_cpu_p95=(
                            settings.rightsizing_review_cpu_p95
                        ),
                        memory_review_percent=(
                            settings.rightsizing_memory_review_percent
                        ),
                        cpu_disagreement_percent=(
                            settings.rightsizing_cpu_disagreement_percent
                        ),
                    )
                )
                message = (
                    f"Collected {sample_count:,} incremental LogicMonitor "
                    f"samples for {len(completed_resources):,} of "
                    f"{len(targets):,} targets, published "
                    f"{summary_count:,} rolling summaries, and evaluated "
                    f"{recommendation_count:,} right-sizing records."
                )
                if warnings:
                    message += (
                        f" {len(warnings):,} warnings; first: "
                        f"{warnings[0][:300]}"
                    )
                database.finish_telemetry_run(
                    run_id,
                    "succeeded",
                    len(targets),
                    message,
                )
                print(message)
                return 0
            except Exception as error:
                database.finish_telemetry_run(
                    run_id,
                    "failed",
                    0,
                    str(error),
                )
                print(str(error))
                return 1
    except (SingletonLeaseUnavailable, Timeout):
        print("LogicMonitor metric collection is already running.")
        return 0


@deployment_guarded
def telemetry_bootstrap_import(root: Path | None = None) -> int:
    database.init()
    bootstrap_root = root or settings.telemetry_bootstrap_root
    try:
        with database.singleton_lease("telemetry-bootstrap"):
            result = import_bootstrap(
                database,
                bootstrap_root / "logicmonitor",
                bootstrap_root / "azure-monitor",
                rightsizing_thresholds={
                    "minimum_window_days": settings.rightsizing_min_window_days,
                    "minimum_coverage_percent": (
                        settings.rightsizing_min_coverage_percent
                    ),
                    "idle_cpu_p95": settings.rightsizing_idle_cpu_p95,
                    "idle_cpu_maximum": settings.rightsizing_idle_cpu_maximum,
                    "idle_network_p95_bytes": (
                        settings.rightsizing_idle_network_p95_bytes
                    ),
                    "review_cpu_p95": settings.rightsizing_review_cpu_p95,
                    "memory_review_percent": (
                        settings.rightsizing_memory_review_percent
                    ),
                    "cpu_disagreement_percent": (
                        settings.rightsizing_cpu_disagreement_percent
                    ),
                },
            )
            print(
                "Imported historical telemetry: "
                f"{result['logicMonitorResources']:,} LogicMonitor VMs, "
                f"{result['azureMonitorResources']:,} Azure Monitor VMs, and "
                f"{result['recommendationCount']:,} reconciled decisions."
            )
            return 0
    except (SingletonLeaseUnavailable, Timeout):
        print("A telemetry bootstrap import is already running.")
        return 0
    except Exception as error:
        print(f"Telemetry bootstrap import failed: {error}")
        return 1


@deployment_guarded
def finops_toolkit_open_data_sync() -> int:
    database.init()
    try:
        with database.singleton_lease("finops-toolkit"):
            counts = synchronize_open_data(
                database,
                settings.finops_toolkit_cache_root,
            )
            detail = ", ".join(
                f"{dataset} {count:,}"
                for dataset, count in sorted(counts.items())
            )
            print(f"Imported Microsoft FinOps Toolkit v14 open data: {detail}.")
            return 0
    except (SingletonLeaseUnavailable, Timeout):
        print("A Microsoft FinOps Toolkit open-data import is already running.")
        return 0
    except Exception as error:
        print(f"Microsoft FinOps Toolkit open-data import failed: {error}")
        return 1


@deployment_guarded
@deployment_guarded
def price_sheet_sync() -> int:
    """Import the newest negotiated price sheet export run.

    The price sheet is a full replacement each run, so only the newest
    manifest under the pricesheet prefix is imported; re-running is
    idempotent.
    """
    import tempfile as _tempfile
    from pathlib import Path as _Path

    database.init()
    if not settings.focus_cost_enabled:
        print("Export storage ingestion is disabled; price sheet skipped.")
        return 0
    integration = database.integration()
    try:
        with database.singleton_lease("price-sheet"):
            if settings.focus_local_path:
                manifests = local_manifests(
                    settings.focus_local_path, expected_type="PriceSheet"
                )
            else:
                manifests = azure_blob_manifests(
                    settings.focus_storage_account_url,
                    settings.focus_storage_container,
                    settings.price_sheet_storage_prefix,
                    _credential(integration),
                    expected_type="PriceSheet",
                )
            if not manifests:
                print(
                    "No price sheet manifests found under "
                    f"'{settings.price_sheet_storage_prefix}'. Provision the "
                    "PriceSheet export "
                    "(scripts/provision_price_sheet_exports.ps1)."
                )
                return 0
            newest = manifests[-1]
            with _tempfile.TemporaryDirectory(prefix="flux-pricesheet-") as tmp:
                root = _Path(tmp)
                files: list[_Path] = []
                for index, blob in enumerate(newest.payload["blobs"]):
                    target = root / f"{index:04d}.csv"
                    newest.open_blob(str(blob["blobName"]), target)
                    files.append(target)
                rows = database.store_price_sheet(files)
            print(
                f"Price sheet: {rows:,} meter rows from {newest.path}."
            )
            return 0
    except (SingletonLeaseUnavailable, Timeout):
        print("A price sheet import is already running.")
        return 0
    except Exception as error:
        print(f"Price sheet import failed: {focus_error_message(error)}")
        return 1


@deployment_guarded
def commitments_sync() -> int:
    """Collect reservation inventory, utilization, and recommendations."""
    from uuid import uuid4 as _uuid4

    from .commitments import fetch_commitments

    database.init()
    integration = database.integration()
    if not integration["enabled"]:
        print("Azure integration is disabled; commitments sync skipped.")
        return 0
    try:
        with database.singleton_lease("commitments"):
            result = fetch_commitments(
                credential=_credential(integration),
                management_endpoint=settings.azure_management_endpoint,
                subscriptions=integration.get("subscriptions", []),
                timeout_seconds=settings.azure_timeout_seconds,
            )
            stored = database.store_commitments(
                f"commitments-{_uuid4()}",
                result["reservations"],
                result["recommendations"],
            )
            print(
                f"Commitments: {stored['reservations']:,} reservations, "
                f"{stored['recommendations']:,} recommendations."
            )
            if result["reservationError"]:
                print(result["reservationError"])
            for error in result["recommendationErrors"]:
                print(f"Recommendations {error}")
            # Only a run that produced nothing at all counts as failed;
            # a missing role on one feed must not hide the other.
            if (
                not result["reservations"]
                and not result["recommendations"]
                and (
                    result["reservationError"]
                    or result["recommendationErrors"]
                )
            ):
                return 1
            return 0
    except (SingletonLeaseUnavailable, Timeout):
        print("A commitments sync is already running.")
        return 0


def retail_prices_sync() -> int:
    database.init()
    try:
        with database.singleton_lease("retail-prices"):
            requests = database.retail_price_requests(
                refresh_hours=settings.retail_prices_refresh_hours,
            )
            prices = AzureRetailPriceProvider(
                endpoint=settings.retail_prices_endpoint,
                api_version=settings.retail_prices_api_version,
                timeout_seconds=settings.retail_prices_timeout_seconds,
                request_delay_ms=settings.retail_prices_request_delay_ms,
                hours_per_month=settings.retail_prices_hours_per_month,
            ).fetch(requests)
            errors = [item for item in prices if item["status"] == "error"]
            if prices:
                # Phase 5 staged-write path: the payload becomes a durable
                # apply job; the singleton analytics writer commits it and
                # only then does the job count as delivered. A crash between
                # staging and apply is recovered by the next apply pass.
                from .database import utc_now as _utc_now

                stage_key, stage_body = retail_price_stage_parts(
                    prices,
                    complete=not errors,
                    collected_at=_utc_now(),
                )
                stage_payload(
                    database,
                    settings.analytics_staging_directory,
                    "retail-prices",
                    stage_key,
                    stage_body,
                )
                apply_pending(database, settings.analytics_staging_directory)
            inventory_snapshot = database.latest_inventory_snapshot_id()
            valuation_count = (
                database.compute_opportunity_valuation(inventory_snapshot)
                if inventory_snapshot
                else 0
            )
            telemetry_run = database.latest_telemetry_run_id()
            rightsizing_count = (
                database.compute_rightsizing_recommendations(
                    telemetry_run,
                    minimum_window_days=settings.rightsizing_min_window_days,
                    minimum_coverage_percent=(
                        settings.rightsizing_min_coverage_percent
                    ),
                    idle_cpu_p95=settings.rightsizing_idle_cpu_p95,
                    idle_cpu_maximum=settings.rightsizing_idle_cpu_maximum,
                    idle_network_p95_bytes=(
                        settings.rightsizing_idle_network_p95_bytes
                    ),
                    review_cpu_p95=settings.rightsizing_review_cpu_p95,
                    memory_review_percent=(
                        settings.rightsizing_memory_review_percent
                    ),
                    cpu_disagreement_percent=(
                        settings.rightsizing_cpu_disagreement_percent
                    ),
                )
                if telemetry_run
                else 0
            )
            counts = {
                status: sum(
                    1 for item in prices if item["status"] == status
                )
                for status in (
                    "matched",
                    "ambiguous",
                    "not_found",
                    "unsupported_os",
                    "error",
                )
            }
            message = (
                f"Processed {len(requests):,} governed retail-price keys: "
                f"{counts['matched']:,} matched, "
                f"{counts['ambiguous']:,} ambiguous, "
                f"{counts['not_found']:,} not found, "
                f"{counts['unsupported_os']:,} unsupported OS, and "
                f"{counts['error']:,} errors. Revalued "
                f"{valuation_count:,} opportunities and "
                f"{rightsizing_count:,} right-sizing records."
            )
            if not requests:
                message = (
                    "All governed retail-price keys are fresh. Revalued "
                    f"{valuation_count:,} opportunities and "
                    f"{rightsizing_count:,} right-sizing records."
                )
            elif errors:
                message += f" First error: {errors[0]['message']}"
            print(message)
            return 1 if requests and len(errors) == len(requests) else 0
    except (SingletonLeaseUnavailable, Timeout):
        print("Azure retail-price collection is already running.")
        return 0
    except Exception as error:
        print(f"Azure retail-price collection failed: {error}")
        return 1


def rightsizing_proposal_sync() -> int:
    """Refresh the system proposal only when its 72-hour cadence is due."""
    from .rightsizing_proposal import refresh_flux_proposal

    try:
        with database.singleton_lease("rightsizing-proposal"):
            result = refresh_flux_proposal(database, force=False)
        if result["status"] == "current":
            print(
                "Flux right-sizing proposal is current; next refresh "
                f"{result['nextRefreshAt']}."
            )
        else:
            print(
                "Refreshed Flux right-sizing proposal: "
                f"{result['bucketCount']} buckets, "
                f"{result['placed']} committed placements, "
                f"{result['review']} kept on demand for technical review, "
                f"{result['savingsPlan']} Savings Plan candidates, "
                f"{result['waste']} waste exclusions, and "
                f"{result['provisional']} provisional placements; "
                f"{result['noData']} truly unmonitored. Modeled retail-"
                f"reconciled savings: ${result['modeledMonthlySavings']:,.2f}/mo."
            )
        return 0
    except (SingletonLeaseUnavailable, Timeout):
        print("Flux right-sizing proposal refresh is already running.")
        return 0
    except Exception as error:
        print(f"Flux right-sizing proposal refresh failed: {error}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "sync":
        raise SystemExit(scheduled_sync())
    if len(sys.argv) == 2 and sys.argv[1] == "sync-inventory":
        raise SystemExit(_with_publication(scheduled_sync(["inventory"])))
    if len(sys.argv) == 2 and sys.argv[1] == "sync-advisor":
        raise SystemExit(_with_publication(scheduled_sync(["advisor"])))
    if len(sys.argv) == 2 and sys.argv[1] == "sync-intelligence":
        raise SystemExit(_with_publication(scheduled_sync(["intelligence"])))
    if len(sys.argv) == 2 and sys.argv[1] == "sync-policy":
        raise SystemExit(_with_publication(scheduled_sync(["inventory", "policy"])))
    if len(sys.argv) == 2 and sys.argv[1] == "sync-cost":
        raise SystemExit(_with_publication(scheduled_sync(["cost"])))
    if len(sys.argv) == 2 and sys.argv[1] == "cost-history":
        raise SystemExit(_with_publication(cost_history_sync()))
    if len(sys.argv) == 2 and sys.argv[1] == "focus-cost":
        raise SystemExit(_with_publication(focus_cost_sync()))
    if len(sys.argv) == 2 and sys.argv[1] == "sync-worker":
        run_sync_worker(database, settings)
        raise SystemExit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "sync-worker-once":
        raise SystemExit(_with_publication(sync_worker_once()))
    if len(sys.argv) == 2 and sys.argv[1] == "telemetry-azure":
        raise SystemExit(_with_publication(azure_monitor_sync()))
    if len(sys.argv) == 2 and sys.argv[1] == "telemetry-logicmonitor":
        raise SystemExit(_with_publication(logicmonitor_discovery_sync()))
    if (
        len(sys.argv) == 2
        and sys.argv[1] == "telemetry-logicmonitor-metrics"
    ):
        raise SystemExit(_with_publication(logicmonitor_metrics_sync()))
    if len(sys.argv) == 2 and sys.argv[1] == "telemetry-bootstrap":
        raise SystemExit(_with_publication(telemetry_bootstrap_import()))
    if len(sys.argv) == 2 and sys.argv[1] == "finops-toolkit":
        raise SystemExit(_with_publication(finops_toolkit_open_data_sync()))
    if len(sys.argv) == 2 and sys.argv[1] == "retail-prices":
        raise SystemExit(_with_publication(retail_prices_sync()))
    if len(sys.argv) == 2 and sys.argv[1] == "rightsizing-proposal":
        raise SystemExit(rightsizing_proposal_sync())
    if len(sys.argv) == 2 and sys.argv[1] == "publish-analytics-snapshot":
        # Operator-driven publication bypasses burst coalescing.
        raise SystemExit(publish_analytics_snapshot(force=True))
    print(
        "Usage: python -m api.jobs "
        "sync|sync-inventory|sync-advisor|sync-intelligence|sync-policy|sync-cost|"
        "cost-history|focus-cost|"
        "sync-worker|sync-worker-once|telemetry-azure|telemetry-logicmonitor|"
        "telemetry-logicmonitor-metrics|telemetry-bootstrap|finops-toolkit|"
        "retail-prices|rightsizing-proposal|publish-analytics-snapshot"
    )
    raise SystemExit(2)
