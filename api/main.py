from __future__ import annotations

from contextlib import asynccontextmanager
import csv
from datetime import date, datetime, timezone
from io import StringIO
import json
import logging
from threading import Event, Thread
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .auth import AuthService, require_admin, require_reader, session_from_request
from .config import settings
from .database import DatabaseBusyError, FluxDatabase
from .evidence import anomaly_evidence, evidence_markdown, opportunity_evidence
from .intelligence_assistant import (
    IntelligenceAssistant,
    IntelligenceBudgetExceeded,
    IntelligenceProviderError,
    IntelligenceUnavailable,
)
from .models import (
    AiIntelligenceConfigUpdate,
    AllocationConfigUpdate,
    AzureIntegrationUpdate,
    BudgetGroupsUpdate,
    BudgetTargetsUpdate,
    OpportunityLifecycleUpdate,
    CostAnomalyReviewUpdate,
    ClientErrorReport,
    FiscalOutlookConfigUpdate,
    GovernedReportRequest,
    ExpertExplorerRequest,
    SemanticQueryRequest,
    IntelligenceChatRequest,
    IntelligenceClientPerformance,
    IntelligenceFeedback,
    JobRunRequest,
    RightsizingAssignmentsUpdate,
    RightsizingBoardCreate,
    RightsizingBoardUpdate,
    RightsizingBucketUpdate,
    RightsizingPlanImport,
)
from .operational_store import SingletonLeaseUnavailable
from .report_catalog import report_catalog, validate_report_request
from .recovery import recover_database_from_latest_backup
from .synchronization import run_sync_worker


# Nothing in this app calls logging.basicConfig(); most modules use print()
# instead, which PYTHONUNBUFFERED makes land in the docker log immediately.
# The one exception, _client_error_logger below, relies on stdlib logging and
# was silently going nowhere without a configured handler -- reported
# client-render-crash detail (area, message, component stack) never reached
# the docker log despite the endpoint returning 204. This makes it land.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


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
    connect_timeout_seconds=settings.duckdb_connect_timeout_seconds,
)
auth = AuthService(settings)
intelligence_assistant = IntelligenceAssistant(database, settings)
snapshot_manager = None
if settings.analytics_snapshot_mode == "snapshot":
    from .analytics_snapshot import AnalyticsSnapshotManager, storage_from_settings

    snapshot_manager = AnalyticsSnapshotManager(
        database,
        storage_from_settings(settings),
        settings.analytics_snapshot_cache_directory,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.recover_database_from_latest_backup:
        recovery = recover_database_from_latest_backup(
            settings.database_path,
            account_url=settings.backup_storage_account_url,
            container_name=settings.backup_container,
            managed_identity_client_id=settings.managed_identity_client_id,
        )
        if recovery.restored:
            print(
                f"Restored {recovery.backup_name} with "
                f"{recovery.resource_count:,} current resources. "
                f"Preserved damaged database at {recovery.preserved_path}."
            )
        else:
            print(
                "Database recovery flag is enabled, but the current database "
                f"is healthy with {recovery.resource_count:,} current resources."
            )
    if not settings.operational_database_enabled:
        database.init()
    # Materialized "current" tables must exist, but only this process may
    # build them. In snapshot mode the published snapshot already contains
    # them, built by the singleton writer, so the web must not open the
    # mutable database for writing at all: doing so made every instance
    # contend for the writer lease on boot and, when that file was damaged,
    # crashed startup outright (DuckDB raises on connect, before any query).
    # Startup must never depend on the mutable analytical store.
    if settings.analytics_snapshot_mode != "snapshot":
        try:
            database.refresh_current_views()
        except Exception as error:
            print(
                "Could not refresh materialized current tables at startup: "
                f"{type(error).__name__}: {error}. Serving with existing "
                "tables; the owning collector will rebuild them."
            )
    # In production the web process is a read-only consumer of the migrated
    # PostgreSQL control plane and DuckDB analytical store. Scheduled
    # collectors own schema initialization and migrations; boot must not be
    # blocked by either database.
    # Derived confidence, valuation, drift, and right-sizing datasets are
    # refreshed by their owning collectors. Rebuilding the full analytical
    # estate here blocks every web-process restart and can exhaust the
    # constrained App Service worker before FastAPI is ready to serve.
    if settings.dev_seed:
        database.seed_demo()
    stop_event = Event()
    refresher = None
    if snapshot_manager is not None:
        # Adopt in the background. A published snapshot is multi-gigabyte, so
        # blocking startup on the download can exceed the platform's startup
        # probe and leave the site restarting instead of serving. The refresher
        # attempts adoption immediately, then polls.
        def refresh_snapshots() -> None:
            try:
                adopted = snapshot_manager.refresh_once()
                if not adopted and snapshot_manager.active_version is None:
                    print(
                        "No approved analytical snapshot adopted yet; "
                        "analytical reads are unavailable until one loads."
                    )
            except Exception as error:
                print(f"Analytical snapshot adoption failed: {error}")
            while not stop_event.is_set():
                stop_event.wait(max(5, settings.analytics_snapshot_refresh_seconds))
                if stop_event.is_set():
                    return
                try:
                    snapshot_manager.refresh_once()
                except Exception as error:
                    print(f"Analytical snapshot refresh failed: {error}")

        refresher = Thread(
            target=refresh_snapshots,
            daemon=True,
            name="flux-snapshot-refresher",
        )
        refresher.start()
    worker = None
    if settings.sync_worker_mode == "embedded":
        worker = Thread(
            target=run_sync_worker,
            args=(database, settings, stop_event),
            daemon=True,
            name="flux-embedded-sync-worker",
        )
        worker.start()
    try:
        yield
    finally:
        stop_event.set()
        if worker:
            worker.join(timeout=10)


app = FastAPI(
    title="Flux Cloud Intelligence API",
    version="2.0.0",
    description="Focused Azure inventory and FinOps enrichment API.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "PUT", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.state.auth = auth


@app.exception_handler(DatabaseBusyError)
async def database_busy_handler(_: Request, error: DatabaseBusyError) -> Response:
    """Fail fast when the analytical store is behind a long-running writer.

    A bounded 503 with Retry-After replaces the historical behavior of
    hanging indefinitely on the cross-process DuckDB lease and surfacing an
    eventual 500 through the App Service front end.
    """
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": (
                "The analytical database is temporarily busy while a "
                "synchronization write completes. Retry shortly."
            ),
            "waitedSeconds": round(error.waited_seconds, 1),
        },
        headers={"Retry-After": "15"},
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    payload = {
        "status": "ok",
        "service": "flux-api",
        "version": app.version,
        "database": "duckdb",
        "operationalDatabase": database.operational_backend,
        "authMode": settings.auth_mode,
        "analyticsReadMode": settings.analytics_snapshot_mode,
    }
    if snapshot_manager is not None:
        payload["analyticsSnapshotVersion"] = snapshot_manager.active_version
    return payload


@app.get("/api/session")
def session(request: Request) -> dict[str, Any]:
    payload = session_from_request(request)
    # Data-currency provenance for the shell chip: which snapshot this
    # instance serves and when it was generated. Operational-store read only;
    # failures degrade to an absent block rather than failing the session.
    currency: dict[str, Any] = {"mode": settings.analytics_snapshot_mode}
    try:
        if snapshot_manager is not None:
            currency["snapshotVersion"] = snapshot_manager.active_version
            publication = database.latest_analytics_publication()
            if publication:
                currency["latestVersion"] = publication["version"]
                currency["generatedAt"] = publication["generatedAt"]
    except Exception:
        pass
    payload["dataCurrency"] = currency
    return payload


@app.get("/api/overview")
def overview(_: dict[str, Any] = Depends(require_reader)) -> dict[str, Any]:
    return database.overview()


@app.get("/api/inventory")
def inventory(
    search: str = "",
    resource_type: str = Query(default="", alias="resourceType"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    region: str = "",
    virtual_tag_key: str = Query(default="", alias="virtualTagKey"),
    virtual_tag_value: str = Query(default="", alias="virtualTagValue"),
    opportunity_only: bool = Query(default=False, alias="opportunityOnly"),
    limit: int = Query(default=250, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.inventory(
        search=search.strip(),
        resource_type=resource_type,
        subscription_id=subscription_id,
        region=region,
        virtual_tag_key=virtual_tag_key,
        virtual_tag_value=virtual_tag_value,
        opportunity_only=opportunity_only,
        limit=limit,
        offset=offset,
    )


@app.get("/api/changes")
def changes(
    search: str = "",
    change_type: str = Query(default="", alias="changeType"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    resource_group: str = Query(default="", alias="resourceGroup"),
    window_days: int = Query(default=7, ge=0, le=365, alias="windowDays"),
    limit: int = Query(default=250, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.changes(
        search=search.strip(),
        change_type=change_type,
        subscription_id=subscription_id,
        resource_group=resource_group,
        window_days=window_days,
        limit=limit,
        offset=offset,
    )


@app.get("/api/anomalies")
@app.get("/api/changes/anomalies")
def inventory_change_anomalies(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.change_anomalies()


@app.get("/api/cost/anomalies")
def cost_anomalies(
    search: str = "",
    cost_type: str = Query(
        default="AmortizedCost",
        alias="costType",
        pattern="^(ActualCost|AmortizedCost)?$",
    ),
    scope_type: str = Query(
        default="",
        alias="scopeType",
        pattern="^(subscription|service|resource)?$",
    ),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    service_name: str = Query(default="", alias="serviceName"),
    severity: str = Query(default="", pattern="^(high|medium)?$"),
    anomaly_status: str = Query(
        default="anomalous",
        alias="status",
        pattern="^(anomalous|warming_up)?$",
    ),
    limit: int = Query(default=250, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.cost_anomalies(
        search=search.strip(),
        cost_type=cost_type,
        scope_type=scope_type,
        subscription_id=subscription_id,
        service_name=service_name,
        severity=severity,
        status=anomaly_status,
        latency_days=settings.cost_anomaly_latency_days,
        limit=limit,
        offset=offset,
    )


@app.put("/api/cost/anomalies/review")
def review_cost_anomaly(
    payload: CostAnomalyReviewUpdate,
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    user = session.get("user") or {}
    updated_by = user.get("email") or user.get("displayName") or user.get("id") or "admin"
    try:
        return database.review_cost_anomaly(
            run_id=payload.run_id,
            cost_type=payload.cost_type,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            review_status=payload.review_status,
            note=payload.note,
            updated_by=updated_by,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/cost/anomalies/contributors")
def cost_anomaly_contributors(
    run_id: str = Query(alias="runId"),
    cost_type: str = Query(alias="costType"),
    scope_type: str = Query(alias="scopeType"),
    scope_id: str = Query(alias="scopeId"),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return {
        "items": database.cost_anomaly_contributors(
            run_id=run_id,
            cost_type=cost_type,
            scope_type=scope_type,
            scope_id=scope_id,
        )
    }


@app.get("/api/semantic")
def semantic_catalog(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.semantic_catalog()


@app.get("/api/remediation/servicenow-package")
def remediation_servicenow_package(
    format: str = "json",
    minMonthlyCost: float = 0.0,
    maxTasks: int = 0,
    _: dict[str, Any] = Depends(require_admin),
) -> Any:
    package = database.remediation_package(
        allowlist=settings.remediation_signal_allowlist,
        minimum_monthly_cost=minMonthlyCost,
    )
    from .remediation import (
        package_console_script,
        package_csv,
        servicenow_form_url,
    )

    package["tasks"].sort(
        key=lambda task: float(
            task["financials"].get("estimatedMonthlySavings") or 0.0
        ),
        reverse=True,
    )
    if maxTasks > 0:
        package["tasks"] = package["tasks"][:maxTasks]
    for task in package["tasks"]:
        task["servicenowFormUrl"] = servicenow_form_url(
            task,
            settings.servicenow_instance_url,
            settings.servicenow_task_table,
        )
        task["servicenowAssignmentGroup"] = settings.servicenow_assignment_group
    if format == "csv":
        return Response(
            content=package_csv(
                package["tasks"],
                assignment_group=settings.servicenow_assignment_group,
                priority=settings.servicenow_priority,
            ),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    "attachment; filename=flux-servicenow-tasks.csv"
                )
            },
        )
    if format == "script":
        return Response(
            content=package_console_script(
                package["tasks"],
                assignment_group=settings.servicenow_assignment_group,
                table=settings.servicenow_task_table,
                priority=settings.servicenow_priority,
                configuration_item=settings.servicenow_configuration_item,
                due_days=settings.servicenow_due_days,
            ),
            media_type="text/javascript",
            headers={
                "Content-Disposition": (
                    "attachment; filename=flux-servicenow-batch.js"
                )
            },
        )
    return package


@app.get("/api/remediation/status")
def remediation_status(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {"tasks": database.remediation_status()}


@app.post("/api/remediation/reconcile")
def remediation_reconcile(
    payload: dict[str, Any],
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    updates = payload.get("updates")
    if not isinstance(updates, list) or not updates:
        raise HTTPException(
            status_code=422, detail="updates must be a non-empty list."
        )
    return database.remediation_reconcile(updates)


@app.get("/api/virtual-tags/rules")
def virtual_tag_rules(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {"rules": database.virtual_tag_rules()}


@app.get("/api/virtual-tags/dimensions")
def virtual_tag_dimensions(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return {"dimensions": database.virtual_tag_dimensions()}


@app.post("/api/virtual-tags/dimensions")
def save_virtual_tag_dimension(
    payload: dict[str, Any],
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.save_virtual_tag_dimension(payload, _principal_actor(session))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.delete("/api/virtual-tags/dimensions/{dimension_key}")
def delete_virtual_tag_dimension(
    dimension_key: str,
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        database.delete_virtual_tag_dimension(dimension_key, _principal_actor(session))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"key": dimension_key, "status": "inactive"}


@app.post("/api/virtual-tags/rules")
def save_virtual_tag_rule(
    payload: dict[str, Any],
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.save_virtual_tag_rule(
            payload, _principal_actor(session)
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/virtual-tags/rules/{rule_id}/status")
def set_virtual_tag_rule_status(
    rule_id: str,
    payload: dict[str, Any],
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        database.set_virtual_tag_rule_status(
            rule_id, str(payload.get("status") or ""), _principal_actor(session)
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ruleId": rule_id, "status": payload.get("status")}


@app.delete("/api/virtual-tags/rules/{rule_id}")
def delete_virtual_tag_rule(
    rule_id: str,
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        database.delete_virtual_tag_rule(rule_id, _principal_actor(session))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ruleId": rule_id, "status": "inactive"}


@app.post("/api/virtual-tags/preview")
def preview_virtual_tag_rule(
    payload: dict[str, Any],
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.virtual_tags_preview(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/virtual-tags/overrides/import")
def import_virtual_tag_overrides(
    payload: dict[str, Any],
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    overrides = payload.get("overrides")
    if not isinstance(overrides, list) or not overrides:
        raise HTTPException(
            status_code=422, detail="overrides must be a non-empty list."
        )
    if len(overrides) > 20000:
        raise HTTPException(
            status_code=422, detail="At most 20000 overrides per import."
        )
    return database.import_virtual_tag_overrides(
        overrides, _principal_actor(session)
    )


@app.post("/api/virtual-tags/overrides/rollback")
def rollback_virtual_tag_overrides(
    payload: dict[str, Any],
    session: dict[str, Any] = Depends(require_admin),
) -> dict[str, int]:
    previous = payload.get("previous")
    if not isinstance(previous, list) or not previous:
        raise HTTPException(
            status_code=422, detail="previous must be a non-empty list."
        )
    if len(previous) > 20000:
        raise HTTPException(
            status_code=422, detail="At most 20000 rollback items per request."
        )
    return database.rollback_virtual_tag_overrides(
        previous, _principal_actor(session)
    )


@app.get("/api/virtual-tags/effective")
def effective_virtual_tags(
    resourceId: str,
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return {
        "resourceId": resourceId,
        "tags": database.effective_virtual_tags(resourceId),
    }


@app.get("/api/reports/virtual-tags")
def virtual_tag_report(
    dimension: str = "",
    value: str = "",
    costType: str = "AmortizedCost",
    startDate: date | None = None,
    endDate: date | None = None,
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.virtual_tag_report(
        dimension=dimension, value=value, cost_type=costType,
        start_date=startDate, end_date=endDate,
    )


@app.get("/api/reports/virtual-tags/export")
def virtual_tag_report_export(
    dimension: str = "",
    value: str = "",
    costType: str = "AmortizedCost",
    startDate: date | None = None,
    endDate: date | None = None,
    _: dict[str, Any] = Depends(require_reader),
) -> StreamingResponse:
    report = database.virtual_tag_report(
        dimension=dimension, value=value, cost_type=costType,
        start_date=startDate, end_date=endDate,
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "dimension", "value", "resource_id", "resource_name",
        "subscription", "resource_group", "resource_type", "source",
        "cost", "currency", "cost_type",
    ])
    for item in report["resources"]:
        writer.writerow([
            report["dimension"], item["value"], item["resourceId"],
            item["name"], item["subscriptionName"], item["resourceGroup"],
            item["resourceType"], item["source"], item["cost"],
            report["currency"], report["costType"],
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=flux-virtual-tags.csv"},
    )


@app.post("/api/semantic/expert")
def semantic_expert(
    payload: ExpertExplorerRequest,
    session: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    """Natural language to validated SQL over the governed semantic views.

    The model only proposes; expert_explorer validates every statement
    (read-only, allowlisted views, no file functions) and executes with a
    row cap and watchdog. One self-correction round on validation failure.
    """
    from datetime import date as _date, datetime as _datetime
    from decimal import Decimal

    from .expert_explorer import (
        ExpertQueryError,
        run_expert_query,
        validate_expert_sql,
    )
    from .intelligence_assistant import (
        IntelligenceBudgetExceeded,
        IntelligenceProviderError,
        IntelligenceUnavailable,
    )

    _, allowed_views = intelligence_assistant._expert_catalog_text()
    history = [
        {"question": turn.question, "sql": turn.sql}
        for turn in payload.history
    ]
    validation_error = ""
    generated: dict[str, Any] = {}
    sql = ""
    for _attempt in range(2):
        try:
            generated = intelligence_assistant.expert_sql(
                payload.question, history, session, validation_error
            )
        except IntelligenceBudgetExceeded as error:
            raise HTTPException(status_code=402, detail=str(error)) from error
        except IntelligenceUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (IntelligenceProviderError, ValueError) as error:
            raise HTTPException(
                status_code=502,
                detail=f"SQL generation failed: {error}",
            ) from error
        try:
            sql = validate_expert_sql(
                str(generated.get("sql") or ""), allowed_views
            )
            break
        except ExpertQueryError as error:
            validation_error = str(error)
            sql = ""
    if not sql:
        raise HTTPException(
            status_code=422,
            detail=(
                "The generated query could not be validated: "
                f"{validation_error}"
            ),
        )
    try:
        result = run_expert_query(database, sql)
    except ExpertQueryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    def cell(value: Any) -> Any:
        if isinstance(value, (_datetime, _date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

    return {
        "question": payload.question,
        "sql": sql,
        "columns": result["columns"],
        "rows": [[cell(value) for value in row] for row in result["rows"]],
        "truncated": result["truncated"],
        "rowLimit": result["rowLimit"],
        "durationMs": result["durationMs"],
        "chartType": str(generated.get("chartType") or "table"),
        "xKey": str(generated.get("xKey") or ""),
        "yKeys": [str(item) for item in generated.get("yKeys") or []],
        "seriesKey": generated.get("seriesKey") or None,
        "explanation": str(generated.get("explanation") or ""),
        "assumptions": [
            str(item) for item in generated.get("assumptions") or []
        ],
    }


@app.post("/api/semantic/query")
def semantic_query(
    payload: SemanticQueryRequest,
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    from .semantic_layer import SemanticQuery, SemanticQueryError

    try:
        return database.run_semantic_query(
            SemanticQuery(
                model=payload.model,
                measures=tuple(payload.measures),
                dimensions=tuple(payload.dimensions),
                filters={
                    key: tuple(values)
                    for key, values in payload.filters.items()
                },
                grain=payload.grain,
                start=payload.start,
                end=payload.end,
                limit=payload.limit,
            )
        )
    except SemanticQueryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/reports/cost")
def cost_report(
    cost_type: str = Query(
        default="AmortizedCost",
        alias="costType",
        pattern="^(ActualCost|AmortizedCost)$",
    ),
    currency: str = "",
    start_date: date | None = Query(default=None, alias="startDate"),
    end_date: date | None = Query(default=None, alias="endDate"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    service_name: str = Query(default="", alias="serviceName"),
    resource_id: str = Query(default="", alias="resourceId"),
    forecast_days: int = Query(default=30, ge=7, le=120, alias="forecastDays"),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="startDate must be on or before endDate.",
        )
    return database.cost_report(
        cost_type=cost_type,
        currency=currency,
        start_date=start_date,
        end_date=end_date,
        subscription_id=subscription_id,
        service_name=service_name,
        resource_id=resource_id,
        forecast_latency_days=settings.cost_anomaly_latency_days,
        forecast_horizon_days=forecast_days,
    )


@app.get("/api/reports/fiscal-outlook")
def fiscal_outlook_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.fiscal_year_outlook()


@app.put("/api/reports/fiscal-outlook/config")
def save_fiscal_outlook_config(
    payload: FiscalOutlookConfigUpdate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    database.save_fiscal_outlook_config(
        fy_start_month=payload.fy_start_month,
        cost_type=payload.cost_type,
        growth_percent_monthly=payload.growth_percent_monthly,
        include_planned_savings=payload.include_planned_savings,
        savings_ramp_months=payload.savings_ramp_months,
        notes=payload.notes,
        updated_by=_principal_actor(principal),
    )
    return database.fiscal_year_outlook()


@app.get("/api/reports/commitments")
def commitments_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.commitment_inventory()


@app.get("/api/reports/executive-summary/export")
def executive_summary_export(
    _: dict[str, Any] = Depends(require_reader),
) -> Response:
    from .executive_export import build_executive_workbook

    content = build_executive_workbook(database)
    stamp = date.today().isoformat()
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="flux-executive-{stamp}.xlsx"'
            )
        },
    )


@app.get("/api/integrations/budget-groups")
def budget_groups(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return {"groups": database.budget_groups()}


@app.put("/api/integrations/budget-groups")
def save_budget_groups(
    payload: BudgetGroupsUpdate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {
        "groups": database.save_budget_groups(
            [group.model_dump(by_alias=True) for group in payload.groups],
            updated_by=_principal_actor(principal),
        )
    }


@app.get("/api/reports/focus-cost")
def focus_cost_report(
    currency: str = "",
    start_date: date | None = Query(default=None, alias="startDate"),
    end_date: date | None = Query(default=None, alias="endDate"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    service_name: str = Query(default="", alias="serviceName"),
    resource_id: str = Query(default="", alias="resourceId"),
    charge_category: str = Query(default="", alias="chargeCategory"),
    pricing_category: str = Query(default="", alias="pricingCategory"),
    commitment_discount_type: str = Query(
        default="", alias="commitmentDiscountType"
    ),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="startDate must be on or before endDate.",
        )
    return database.focus_cost_report(
        currency=currency,
        start_date=start_date,
        end_date=end_date,
        subscription_id=subscription_id,
        service_name=service_name,
        resource_id=resource_id,
        charge_category=charge_category,
        pricing_category=pricing_category,
        commitment_discount_type=commitment_discount_type,
    )


@app.get("/api/reports/budgets")
def budget_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.budget_report()


@app.get("/api/integrations/budgets")
def get_budget_targets(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {"targets": database.budget_targets()}


@app.put("/api/integrations/budgets")
def put_budget_targets(
    payload: BudgetTargetsUpdate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {
        "targets": database.save_budget_targets(
            [
                {
                    "scopeType": target.scope_type,
                    "scopeId": target.scope_id,
                    "monthlyAmount": target.monthly_amount,
                    "currency": target.currency,
                }
                for target in payload.targets
            ],
            updated_by=_principal_actor(principal),
        )
    }


def _masked_key(raw_key: str) -> str:
    if not raw_key:
        return ""
    if len(raw_key) <= 4:
        return "*" * len(raw_key)
    return f"{raw_key[:3]}...{raw_key[-4:]}"


def _ai_intelligence_config_response() -> dict[str, Any]:
    override = database.ai_intelligence_config()
    provider = override["provider"] or settings.intelligence_ai_provider
    if provider == "openrouter":
        default_fast = settings.openrouter_chat_model
        default_deep = settings.openrouter_benchmark_model
    elif provider == "foundry":
        default_fast = settings.foundry_chat_model
        default_deep = settings.foundry_benchmark_model
    else:
        default_fast = settings.deepseek_chat_model
        default_deep = settings.deepseek_benchmark_model
    return {
        "provider": provider,
        "fastModel": override["fastModel"] or default_fast,
        "deepModel": override["deepModel"] or default_deep,
        "overrideActive": bool(override["provider"]),
        "updatedBy": override["updatedBy"],
        "updatedAt": override["updatedAt"],
        "keys": {
            "deepseek": {
                "configured": bool(settings.deepseek_api_key),
                "masked": _masked_key(settings.deepseek_api_key),
            },
            "openrouter": {
                "configured": bool(settings.openrouter_api_key),
                "masked": _masked_key(settings.openrouter_api_key),
            },
            "foundry": {
                "configured": bool(settings.foundry_api_key),
                "masked": _masked_key(settings.foundry_api_key),
            },
        },
    }


@app.get("/api/admin/ai-config")
def get_ai_intelligence_config(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return _ai_intelligence_config_response()


@app.put("/api/admin/ai-config")
def put_ai_intelligence_config(
    payload: AiIntelligenceConfigUpdate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    database.save_ai_intelligence_config(
        payload.provider,
        payload.fast_model,
        payload.deep_model,
        updated_by=_principal_actor(principal),
    )
    return _ai_intelligence_config_response()


@app.get("/api/reports/unit-economics")
def unit_economics_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.unit_economics_report()


@app.get("/api/reports/executive-summary")
def executive_summary(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.executive_summary()


@app.put("/api/opportunities/lifecycle")
def put_opportunity_lifecycle(
    payload: OpportunityLifecycleUpdate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return database.set_opportunity_lifecycle(
        payload.opportunity_id,
        payload.status,
        note=payload.note,
        updated_by=_principal_actor(principal),
        resource_id=payload.resource_id,
        estimated_monthly_savings=payload.estimated_monthly_savings,
    )


@app.get("/api/reports/savings")
def savings_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.savings_report()


@app.get("/api/reports/focus-analytics")
def focus_analytics_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.focus_analytics_report()


@app.get("/api/reports/allocation")
def allocation_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.allocation_report()


@app.get("/api/integrations/allocation")
def get_allocation_config(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return database.allocation_config()


@app.put("/api/integrations/allocation")
def put_allocation_config(
    payload: AllocationConfigUpdate,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return database.save_allocation_config(
        payload.cost_center_tags,
        payload.shared_values,
        unit_tag=payload.unit_tag,
        unit_label=payload.unit_label,
    )


@app.get("/api/reports/tag-hygiene")
def tag_hygiene_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.tag_hygiene_report(
        required_tags=settings.intelligence_required_tags,
        excluded_types=settings.intelligence_tag_excluded_types,
    )


@app.get("/api/reports/workload")
def workload_report(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.workload_report()


@app.get("/api/reports/governance")
def governance_report(
    subscription_id: str = Query(default="", alias="subscriptionId"),
    assignment_id: str = Query(default="", alias="assignmentId"),
    compliance_state: str = Query(default="", alias="complianceState"),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.policy_report(
        subscription_id=subscription_id,
        assignment_id=assignment_id,
        compliance_state=compliance_state,
    )


@app.get("/api/reports/catalog")
def governed_report_catalog(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return report_catalog()


@app.get("/api/intelligence/status")
def intelligence_status(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return intelligence_assistant.status()


@app.post("/api/intelligence/chat")
def intelligence_chat(
    payload: IntelligenceChatRequest,
    session: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    try:
        return intelligence_assistant.chat(
            messages=[
                {"role": item.role, "content": item.content}
                for item in payload.messages
            ],
            context=payload.context.model_dump(by_alias=True),
            model_profile=payload.model_profile,
            session=session,
        )
    except IntelligenceBudgetExceeded as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except IntelligenceUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except IntelligenceProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/intelligence/feedback", status_code=status.HTTP_204_NO_CONTENT)
def intelligence_feedback(
    payload: IntelligenceFeedback,
    _: dict[str, Any] = Depends(require_reader),
) -> Response:
    if not database.record_intelligence_feedback(
        payload.request_id,
        payload.rating,
        payload.reason,
    ):
        raise HTTPException(status_code=404, detail="Intelligence request not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_client_error_logger = logging.getLogger("flux.client_error")


def _principal_actor(session: dict[str, Any]) -> str:
    """Display name of the signed-in user, for audit attribution.

    require_reader/require_admin return the whole session; the identity
    fields live under its "user" key, not at the top level.
    """
    user = session.get("user") or {}
    return str(user.get("displayName") or user.get("email") or user.get("id") or "")


@app.post("/api/client-error", status_code=status.HTTP_204_NO_CONTENT)
def report_client_error(
    payload: ClientErrorReport,
    session: dict[str, Any] = Depends(require_reader),
) -> Response:
    # Render crashes previously died in the user's browser console; land them
    # in the application log so they can be diagnosed without a screen share.
    _client_error_logger.error(
        "client render error area=%r user=%r url=%r message=%r\n"
        "component stack:\n%s\nerror stack:\n%s",
        payload.area,
        _principal_actor(session),
        payload.url,
        payload.message,
        payload.component_stack.strip() or "(none)",
        payload.stack.strip() or "(none)",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/intelligence/performance", status_code=status.HTTP_204_NO_CONTENT)
def intelligence_performance(
    payload: IntelligenceClientPerformance,
    session: dict[str, Any] = Depends(require_reader),
) -> Response:
    if not intelligence_assistant.record_client_performance(
        request_id=payload.request_id,
        client_round_trip_ms=payload.client_round_trip_ms,
        client_render_ms=payload.client_render_ms,
        client_end_to_end_ms=payload.client_end_to_end_ms,
        session=session,
    ):
        raise HTTPException(status_code=404, detail="Intelligence request not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/intelligence/review")
def intelligence_review(
    limit: int = Query(default=25, ge=1, le=100),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return database.intelligence_transcript_review(
        limit,
        settings.intelligence_ai_transcript_retention_days,
    )


@app.post("/api/reports/catalog/validate")
def validate_governed_report(
    payload: GovernedReportRequest,
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    try:
        return validate_report_request(payload.model_dump(by_alias=True))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/evidence/opportunity")
def get_opportunity_evidence(
    opportunity_id: str = Query(alias="opportunityId"),
    output_format: str = Query(default="markdown", alias="format", pattern="^(markdown|json)$"),
    _: dict[str, Any] = Depends(require_reader),
) -> Any:
    result = database.opportunities(
        include_governance=True,
        sort="valuation",
        direction="desc",
        limit=50_000,
        offset=0,
    )
    item = next(
        (value for value in result["items"] if value["id"] == opportunity_id),
        None,
    )
    if not item:
        raise HTTPException(status_code=404, detail="Opportunity not found.")
    telemetry = (
        database.resource_telemetry(item["resourceId"])
        if item.get("resourceId")
        else None
    )
    rightsizing_result = (
        database.rightsizing_recommendations(
            resource_id=item["resourceId"],
            limit=1,
            offset=0,
        )
        if item.get("resourceId")
        else {"items": []}
    )
    rightsizing = (
        rightsizing_result["items"][0]
        if rightsizing_result["items"]
        else None
    )
    pack = opportunity_evidence(item, telemetry, rightsizing)
    if output_format == "json":
        return pack
    return Response(
        evidence_markdown(pack),
        media_type="text/markdown",
        headers={
            "Content-Disposition":
                "attachment; filename=flux-opportunity-change-request.md"
        },
    )


@app.get("/api/evidence/cost-anomaly")
def get_cost_anomaly_evidence(
    run_id: str = Query(alias="runId"),
    cost_type: str = Query(alias="costType"),
    scope_type: str = Query(alias="scopeType"),
    scope_id: str = Query(alias="scopeId"),
    output_format: str = Query(default="markdown", alias="format", pattern="^(markdown|json)$"),
    _: dict[str, Any] = Depends(require_reader),
) -> Any:
    result = database.cost_anomalies(
        cost_type=cost_type,
        scope_type=scope_type,
        status="",
        latency_days=settings.cost_anomaly_latency_days,
        limit=50_000,
        offset=0,
    )
    item = next(
        (
            value
            for value in result["items"]
            if value["runId"] == run_id and value["scopeId"] == scope_id
        ),
        None,
    )
    if not item:
        raise HTTPException(status_code=404, detail="Cost anomaly not found.")
    pack = anomaly_evidence(item)
    pack["contributors"] = database.cost_anomaly_contributors(
        run_id=run_id,
        cost_type=cost_type,
        scope_type=scope_type,
        scope_id=scope_id,
    )
    if output_format == "json":
        return pack
    return Response(
        evidence_markdown(pack),
        media_type="text/markdown",
        headers={
            "Content-Disposition":
                "attachment; filename=flux-cost-anomaly-change-request.md"
        },
    )


@app.get("/api/telemetry/status")
def telemetry_status(_: dict[str, Any] = Depends(require_reader)) -> dict[str, Any]:
    return database.telemetry_status()


@app.get("/api/integrations/finops-toolkit")
def finops_toolkit_status(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.finops_toolkit_status()


@app.get("/api/integrations/cost-history")
def cost_history_status(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.cost_history_status()


@app.get("/api/integrations/cost-coverage")
def cost_history_coverage(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    """Day-level completeness ledger: expected vs ingested days per scope."""
    return database.cost_history_coverage(
        initial_days=settings.cost_history_initial_days,
    )


@app.get("/api/integrations/telemetry-coverage")
def telemetry_coverage(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    """Estate telemetry coverage with uncovered VMs ranked by spend."""
    return database.telemetry_coverage_report()


@app.get("/api/integrations/cost-reconciliation")
def cost_reconciliation(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.cost_reconciliation()


@app.get("/api/operations/pipeline")
def pipeline_status(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """End-to-end data-pipeline status in one call.

    Answers "is the pipeline moving?" without log access: sync queue and
    claim ages, publication currency, staged-apply backlog, shared throttle
    state, and this instance's adopted snapshot version.
    """
    status = database.pipeline_status()
    status["analyticsRead"] = {
        "mode": settings.analytics_snapshot_mode,
        "activeSnapshotVersion": (
            snapshot_manager.active_version if snapshot_manager else None
        ),
    }
    return status


@app.get("/api/operations/health")
def operational_health(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return database.operational_health()


@app.get("/api/operations/slo")
def operations_slo(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Current SLO evaluations with tracked transition state.

    The alert catalog runbook documents each objective's meaning and first
    response; the flux-alerts job notifies transitions to the webhook.
    """
    return database.slo_report(
        initial_days=settings.cost_history_initial_days,
        threshold_overrides=settings.slo_threshold_overrides,
    )


@app.get("/api/admin/database-health")
def admin_database_health(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return database.database_health()


@app.get("/api/admin/audit")
def admin_configuration_audit(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {"entries": database.configuration_audit()}


@app.get("/api/admin/retention")
def admin_retention(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Read-only view of retention windows that are otherwise env-var only."""
    return {
        "policies": [
            {
                "name": "Ask Flux transcripts",
                "days": settings.intelligence_ai_transcript_retention_days,
                "setting": "FLUX_AI_TRANSCRIPT_RETENTION_DAYS",
                "note": "Prompts and validated replies for quality review. 0 disables storage.",
            },
            {
                "name": "Ask Flux usage metadata",
                "days": settings.intelligence_ai_retention_days,
                "setting": "FLUX_AI_USAGE_RETENTION_DAYS",
                "note": "Pseudonymous token counts, latency and cost. No prompt content.",
            },
            {
                "name": "LogicMonitor metric history",
                "days": settings.logicmonitor_metric_retention_days,
                "setting": "FLUX_LOGICMONITOR_METRIC_RETENTION_DAYS",
                "note": "Checkpointed performance samples backing right-sizing evidence.",
            },
            {
                "name": "Analytical snapshots",
                "days": None,
                "setting": "tiered retention",
                "note": "Newest 5 kept, then one per day for 14 days.",
            },
        ],
    }


# Sources the queue-backed worker can run on demand. Everything else is a
# scheduled WebJob that this process cannot trigger without granting it Kudu
# access to itself, which would be a real privilege expansion for a
# convenience feature -- those stay read-only here.
_TRIGGERABLE_SOURCES: dict[str, str] = {
    "AzureResourceGraph": "inventory",
    "AzureAdvisor": "advisor",
    "FluxIntelligence": "intelligence",
    "AzurePolicy": "policy",
}


@app.get("/api/admin/jobs")
def admin_jobs(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    jobs = []
    for item in database.source_freshness():
        jobs.append(
            {
                **item,
                "triggerSource": _TRIGGERABLE_SOURCES.get(item["source"], ""),
            }
        )
    jobs.sort(key=lambda item: str(item.get("label") or ""))
    return {"jobs": jobs, "activeSync": database.active_sync()}


@app.post("/api/admin/jobs/run", status_code=status.HTTP_202_ACCEPTED)
def admin_run_job(
    payload: JobRunRequest,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if payload.source not in _TRIGGERABLE_SOURCES.values():
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{payload.source}' is not an on-demand source. Scheduled "
                "WebJob collectors run on their own cadence."
            ),
        )
    integration = database.integration()
    if not integration["enabled"]:
        raise HTTPException(status_code=409, detail="The Azure integration is disabled.")
    if database.active_sync():
        raise HTTPException(
            status_code=409, detail="A synchronization is already running."
        )
    sync_id = database.start_sync(integration["authMode"], sources=[payload.source])
    return {"accepted": True, "syncId": sync_id, "source": payload.source}


@app.get("/api/rightsizing/boards")
def rightsizing_boards(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return {"boards": database.rightsizing_boards()}


@app.post("/api/rightsizing/boards")
def create_rightsizing_board(
    payload: RightsizingBoardCreate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.create_rightsizing_board(
            payload.name,
            payload.description,
            actor=_principal_actor(principal),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put("/api/rightsizing/boards/{board_id}")
def rename_rightsizing_board(
    board_id: str,
    payload: RightsizingBoardUpdate,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.rename_rightsizing_board(
            board_id, payload.name, payload.description
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/rightsizing/boards/{board_id}/primary")
def set_primary_rightsizing_board(
    board_id: str,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.set_primary_rightsizing_board(board_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/api/rightsizing/boards/{board_id}")
def delete_rightsizing_board(
    board_id: str,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.delete_rightsizing_board(board_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/rightsizing/boards/{board_id}/duplicate")
def duplicate_rightsizing_board(
    board_id: str,
    payload: RightsizingBoardCreate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.duplicate_rightsizing_board(
            board_id, payload.name, actor=_principal_actor(principal)
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/rightsizing/proposal/refresh")
def refresh_rightsizing_proposal(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    from .rightsizing_proposal import refresh_flux_proposal

    try:
        with database.singleton_lease("rightsizing-proposal"):
            return refresh_flux_proposal(database, force=True)
    except SingletonLeaseUnavailable as error:
        raise HTTPException(
            status_code=409,
            detail="The Flux proposal is already being refreshed.",
        ) from error


@app.get("/api/rightsizing/proposal/status")
def rightsizing_proposal_status(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    from .rightsizing_proposal import proposal_status

    return proposal_status(database)


@app.get("/api/rightsizing/plan")
def rightsizing_plan(
    board_id: str = Query(default="", alias="boardId", max_length=64),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.rightsizing_plan_board(board_id)


@app.get("/api/rightsizing/plan/log")
def rightsizing_plan_log(
    board_id: str = Query(default="", alias="boardId", max_length=64),
    limit: int = Query(default=250, ge=1, le=2000),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return {"entries": database.rightsizing_plan_log(board_id, limit)}


@app.put("/api/rightsizing/plan/bucket")
def put_rightsizing_bucket(
    payload: RightsizingBucketUpdate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.save_rightsizing_bucket(
            payload.model_dump(by_alias=True),
            updated_by=_principal_actor(principal),
        )
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/api/rightsizing/plan/bucket")
def delete_rightsizing_bucket(
    key: str = Query(min_length=1, max_length=200),
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.delete_rightsizing_bucket(
            key,
            actor=_principal_actor(principal),
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.put("/api/rightsizing/plan/assignments")
def put_rightsizing_assignments(
    payload: RightsizingAssignmentsUpdate,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return database.assign_rightsizing_vms(
            [move.model_dump(by_alias=True) for move in payload.moves],
            board_id=payload.board_id,
            actor=_principal_actor(principal),
        )
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/rightsizing/plan/import")
def import_rightsizing_plan(
    payload: RightsizingPlanImport,
    principal: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    dump = payload.model_dump(by_alias=True)
    try:
        return database.import_rightsizing_plan(
            dump,
            board_id=payload.board_id,
            new_board_name=payload.new_board_name,
            dry_run=payload.dry_run,
            actor="import:" + _principal_actor(principal),
        )
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/recommendations/quality")
def recommendation_quality(
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.recommendation_quality()


@app.get("/api/telemetry/resource")
def resource_telemetry(
    resource_id: str = Query(alias="resourceId"),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.resource_telemetry(resource_id)


@app.get("/api/recommendations/rightsizing")
def rightsizing_recommendations(
    recommendation_status: str = Query(default="", alias="status"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    limit: int = Query(default=250, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.rightsizing_recommendations(
        status=recommendation_status,
        subscription_id=subscription_id,
        limit=limit,
        offset=offset,
    )


@app.get("/api/recommendations/rightsizing/export")
def export_rightsizing_recommendations(
    recommendation_status: str = Query(default="", alias="status"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    export_format: str = Query(default="csv", alias="format", pattern="^(csv|xlsx)$"),
    _: dict[str, Any] = Depends(require_reader),
) -> StreamingResponse:
    result = database.rightsizing_recommendations(
        status=recommendation_status,
        subscription_id=subscription_id,
        limit=2000,
        offset=0,
    )
    fields = [
        "resourceName", "resourceId", "subscriptionName", "resourceGroup",
        "region", "status", "currentSku", "targetSku", "evidenceWindowDays",
        "coverageFlag", "telemetrySource", "cpuP95", "cpuMaximum",
        "networkInP95", "networkOutP95", "metricCoveragePercent",
        "estimatedMonthlySaving", "currency", "valueSource", "reason",
        "computedAt", "methodVersion",
    ]
    return _tabular_export(
        fields,
        result["items"],
        "flux-rightsizing-recommendations",
        export_format,
        filters={"status": recommendation_status, "subscriptionId": subscription_id},
    )


@app.get("/api/signals/aged-snapshots")
def aged_snapshots(
    age_days: int = Query(default=settings.intelligence_snapshot_age_days, ge=1, le=3650, alias="ageDays"),
    limit: int = Query(default=2000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.aged_snapshots(age_days=age_days, limit=limit, offset=offset)


@app.get("/api/opportunities")
def opportunities(
    search: str = "",
    resource_id: str = Query(default="", alias="resourceId"),
    resource_type: str = Query(default="", alias="resourceType"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    region: str = "",
    source: str = "",
    category: str = "",
    confidence: str = "",
    actionability: str = Query(
        default="",
        pattern="^(|actionable_now|portfolio_review|evidence_needed|governance_review)$",
    ),
    include_governance: bool = Query(default=False, alias="includeGovernance"),
    sort: str = Query(default="impact", pattern="^(impact|savings|valuation|cost|confidence|updated|resource)$"),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=250, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_reader),
) -> dict[str, Any]:
    return database.opportunities(
        search=search.strip(),
        resource_id=resource_id.strip(),
        resource_type=resource_type,
        subscription_id=subscription_id,
        region=region,
        source=source,
        category=category,
        confidence=confidence,
        actionability=actionability,
        include_governance=include_governance,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
    )


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _tabular_export(
    fields: list[str],
    rows: list[dict[str, Any]],
    filename_base: str,
    export_format: str,
    filters: dict[str, Any] | None = None,
) -> StreamingResponse:
    """Stream a governed tabular export as CSV or XLSX.

    XLSX exports carry a Metadata sheet (generation time, snapshot version,
    applied filters) so downstream spreadsheets keep their provenance.
    """
    if export_format == "xlsx":
        from io import BytesIO

        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "Data"
        sheet.append(fields)
        for row in rows:
            sheet.append([_csv_safe(row.get(key, "")) for key in fields])
        meta = book.create_sheet("Metadata")
        meta.append(["generatedAt", datetime.now(timezone.utc).isoformat()])
        meta.append(["source", "FluxFinOps governed export"])
        meta.append(["analyticsReadMode", settings.analytics_snapshot_mode])
        if snapshot_manager is not None:
            meta.append(
                ["snapshotVersion", snapshot_manager.active_version or ""]
            )
        for key, value in (filters or {}).items():
            if value not in ("", None, False):
                meta.append([f"filter:{key}", str(value)])
        buffer = BytesIO()
        book.save(buffer)
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition":
                    f"attachment; filename={filename_base}.xlsx"
            },
        )
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_safe(row.get(key, "")) for key in fields})
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f"attachment; filename={filename_base}.csv"
        },
    )


@app.get("/api/inventory/export")
def export_inventory(
    search: str = "",
    resource_type: str = Query(default="", alias="resourceType"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    region: str = "",
    virtual_tag_key: str = Query(default="", alias="virtualTagKey"),
    virtual_tag_value: str = Query(default="", alias="virtualTagValue"),
    opportunity_only: bool = Query(default=False, alias="opportunityOnly"),
    export_format: str = Query(default="csv", alias="format", pattern="^(csv|xlsx)$"),
    _: dict[str, Any] = Depends(require_reader),
) -> StreamingResponse:
    result = database.inventory(
        search=search.strip(),
        resource_type=resource_type,
        subscription_id=subscription_id,
        region=region,
        virtual_tag_key=virtual_tag_key,
        virtual_tag_value=virtual_tag_value,
        opportunity_only=opportunity_only,
        limit=50_000,
        offset=0,
    )
    fields = [
        "name", "resourceId", "resourceType", "subscriptionName",
        "subscriptionId", "resourceGroup", "region", "kind", "sku",
        "provisioningState", "managedBy", "estimatedMonthlyCost",
        "amortizedMonthlyCost", "costCurrency", "costSource",
        "utilizationPercent", "utilizationSource", "opportunityKind",
        "opportunityReason", "estimatedMonthlySavings", "observedAt", "tags",
        "effectiveVirtualTags",
    ]
    rows = []
    for item in result["items"]:
        row = dict(item)
        row["tags"] = json.dumps(
            item.get("tags") or {},
            separators=(",", ":"),
            sort_keys=True,
        )
        row["effectiveVirtualTags"] = json.dumps(
            item.get("effectiveVirtualTags") or {}, separators=(",", ":"),
            sort_keys=True,
        )
        rows.append(row)
    return _tabular_export(
        fields,
        rows,
        "flux-azure-inventory",
        export_format,
        filters={
            "search": search, "resourceType": resource_type,
            "subscriptionId": subscription_id, "region": region,
            "virtualTagKey": virtual_tag_key,
            "virtualTagValue": virtual_tag_value,
            "opportunityOnly": opportunity_only,
        },
    )


@app.get("/api/reports/cost/export")
def export_cost_report(
    cost_type: str = Query(
        default="AmortizedCost",
        alias="costType",
        pattern="^(ActualCost|AmortizedCost)$",
    ),
    currency: str = "",
    start_date: date | None = Query(default=None, alias="startDate"),
    end_date: date | None = Query(default=None, alias="endDate"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    service_name: str = Query(default="", alias="serviceName"),
    resource_id: str = Query(default="", alias="resourceId"),
    export_format: str = Query(default="csv", alias="format", pattern="^(csv|xlsx)$"),
    _: dict[str, Any] = Depends(require_reader),
) -> StreamingResponse:
    report = database.cost_report(
        cost_type=cost_type,
        currency=currency,
        start_date=start_date,
        end_date=end_date,
        subscription_id=subscription_id,
        service_name=service_name,
        resource_id=resource_id,
        forecast_latency_days=settings.cost_anomaly_latency_days,
    )
    fields = [
        "costType", "currency", "periodStart", "periodEnd",
        "subscriptionId", "resourceId", "resourceName", "resourceType",
        "resourceGroup", "region", "cost",
    ]
    rows = [
        {
            "costType": cost_type,
            "currency": report["summary"]["currency"],
            "periodStart": report["period"]["start"],
            "periodEnd": report["period"]["end"],
            **item,
        }
        for item in report["resources"]
    ]
    return _tabular_export(
        fields,
        rows,
        "flux-cost-summary",
        export_format,
        filters={
            "costType": cost_type, "currency": currency,
            "startDate": start_date, "endDate": end_date,
            "subscriptionId": subscription_id,
            "serviceName": service_name, "resourceId": resource_id,
        },
    )


@app.get("/api/reports/workload/retirement/export")
def export_retirement_report(
    _: dict[str, Any] = Depends(require_reader),
) -> StreamingResponse:
    report = database.workload_report()
    output = StringIO()
    fields = [
        "resourceName", "resourceId", "resourceType", "subscriptionName",
        "resourceGroup", "region", "title", "kind", "ageDays",
        "ownershipReady", "ownershipTags", "costExposure",
        "monthlyRiskAdjustedSavings", "valuationCurrency", "confidence",
        "observedAt", "isServiceRetirement",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in report["retirementCandidates"]:
        row = dict(item)
        row["ownershipTags"] = ",".join(item.get("ownershipTags") or [])
        writer.writerow({key: _csv_safe(row.get(key, "")) for key in fields})
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=flux-resource-retirement.csv"
        },
    )


@app.get("/api/cost/anomalies/export")
def export_cost_anomalies(
    search: str = "",
    cost_type: str = Query(default="AmortizedCost", alias="costType"),
    scope_type: str = Query(default="", alias="scopeType"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    service_name: str = Query(default="", alias="serviceName"),
    severity: str = "",
    anomaly_status: str = Query(default="anomalous", alias="status"),
    _: dict[str, Any] = Depends(require_reader),
) -> StreamingResponse:
    result = database.cost_anomalies(
        search=search.strip(),
        cost_type=cost_type,
        scope_type=scope_type,
        subscription_id=subscription_id,
        service_name=service_name,
        severity=severity,
        status=anomaly_status,
        latency_days=settings.cost_anomaly_latency_days,
        limit=50_000,
        offset=0,
    )
    output = StringIO()
    fields = [
        "evaluationDate", "costType", "scopeType", "scopeId",
        "subscriptionId", "resourceName", "resourceId", "resourceType",
        "resourceGroup", "serviceName", "severity", "currentAmount",
        "baselineMedian", "absoluteChange", "percentChange", "currency",
        "baselinePoints", "kScore", "reason", "reviewStatus", "reviewNote",
        "reviewedBy", "reviewedAt", "methodVersion",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in result["items"]:
        writer.writerow({key: _csv_safe(item.get(key, "")) for key in fields})
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=flux-cost-anomalies.csv"
        },
    )


@app.get("/api/opportunities/export")
def export_opportunities(
    search: str = "",
    resource_type: str = Query(default="", alias="resourceType"),
    subscription_id: str = Query(default="", alias="subscriptionId"),
    region: str = "",
    source: str = "",
    category: str = "",
    confidence: str = "",
    actionability: str = Query(
        default="",
        pattern="^(|actionable_now|portfolio_review|evidence_needed|governance_review)$",
    ),
    include_governance: bool = Query(default=False, alias="includeGovernance"),
    sort: str = Query(default="impact", pattern="^(impact|savings|valuation|cost|confidence|updated|resource)$"),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
    export_format: str = Query(default="csv", alias="format", pattern="^(csv|xlsx)$"),
    _: dict[str, Any] = Depends(require_reader),
) -> StreamingResponse:
    result = database.opportunities(
        search=search.strip(), resource_type=resource_type,
        subscription_id=subscription_id, region=region, source=source,
        category=category, confidence=confidence,
        actionability=actionability,
        include_governance=include_governance, sort=sort, direction=direction,
        limit=50_000, offset=0,
    )
    fields = [
        "source", "category", "impact", "confidence", "title", "reason",
        "resourceName", "resourceId", "resourceType", "subscriptionName",
        "subscriptionId", "resourceGroup", "region", "currentSku", "recommendedSku",
        "estimatedMonthlySavings", "annualSavingsAmount", "actualMonthlyCost",
        "savingsCurrency", "isCorroborated", "confidenceScore", "ageDays",
        "consecutiveCount", "reappearedAfterRemediation",
        "confidenceMethodVersion", "observedAt",
        "valuationStatus", "monthlyGrossSavings",
        "monthlyRiskAdjustedSavings", "valuationCurrency", "valuationSource",
        "valuationBasis", "valuationCostSnapshotId", "valuationCostType",
        "valuationPeriodStart", "valuationPeriodEnd",
        "valuationMethodVersion", "valuationComputedAt",
        "currentMonthlyCostRunRate", "targetMonthlyRetailCost",
        "currentCostBasis", "targetPriceBasis", "targetPriceSnapshotId",
        "targetPriceStatus", "targetHourlyPrice", "targetHoursPerMonth",
        "targetMeterId", "targetMeterName", "targetProductName",
        "targetPriceEffectiveStart", "priceOperatingSystem",
        "priceLicenseModel",
        "actionability", "actionabilityReason",
    ]
    return _tabular_export(
        fields,
        list(result["items"]),
        "flux-opportunities",
        export_format,
        filters={
            "search": search, "resourceType": resource_type,
            "subscriptionId": subscription_id, "region": region,
            "source": source, "category": category,
            "confidence": confidence, "actionability": actionability,
            "includeGovernance": include_governance,
        },
    )


@app.get("/api/integrations/azure")
def get_azure_integration(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return database.integration()


@app.put("/api/integrations/azure")
def put_azure_integration(
    payload: AzureIntegrationUpdate,
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    value = payload.model_dump(by_alias=False)
    normalized = {
        "name": value["name"],
        "tenantId": value["tenant_id"],
        "enabled": value["enabled"],
        "authMode": value["auth_mode"],
        "subscriptions": [
            {
                "subscriptionId": item["subscription_id"],
                "label": item["label"],
            }
            for item in value["subscriptions"]
        ],
    }
    return database.save_integration(normalized)


@app.post("/api/integrations/azure/sync", status_code=status.HTTP_202_ACCEPTED)
def sync_azure(
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    integration = database.integration()
    if not integration["enabled"]:
        raise HTTPException(status_code=409, detail="The Azure integration is disabled.")
    if database.active_sync():
        raise HTTPException(status_code=409, detail="A synchronization is already running.")
    # Cost Management is deliberately collected by its own paced daily jobs.
    # Keeping it out of an ad-hoc metadata refresh prevents a user click from
    # competing for the tenant's Cost Management QPU quota.
    sync_id = database.start_sync(
        integration["authMode"],
        sources=["inventory", "advisor", "intelligence", "policy"],
    )
    return {"accepted": True, "syncId": sync_id}


@app.post("/api/dev/seed", status_code=status.HTTP_204_NO_CONTENT)
def seed_demo(
    _: dict[str, Any] = Depends(require_admin),
) -> Response:
    if not settings.dev_seed:
        raise HTTPException(status_code=404, detail="Demo seeding is disabled.")
    database.seed_demo()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def mount_frontend() -> None:
    dist = settings.frontend_dist
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        requested = (dist / path).resolve()
        if (
            path
            and requested.is_file()
            and str(requested).startswith(str(dist.resolve()))
        ):
            return FileResponse(requested)
        index = dist / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(
            status_code=404,
            detail="Frontend build not found. Run npm run build in frontend/.",
        )


mount_frontend()
