from __future__ import annotations

from pathlib import Path
import time
from threading import Event
from typing import Any

from azure.identity import AzurePowerShellCredential, ManagedIdentityCredential
from filelock import Timeout

from .azure import LocalPowerShellArgProvider, ManagedIdentityArgProvider
from .backup import backup_database
from .config import Settings
from .cost import CostManagementProvider, SharedRequestGate
from .database import DatabaseBusyError, FluxDatabase
from .deployment import DeploymentQuiesced, deployment_lease
from .operational_store import SingletonLeaseUnavailable


SOURCE_ORDER = ("inventory", "advisor", "intelligence", "policy", "cost")
SOURCE_NAMES = {
    "inventory": "AzureResourceGraph",
    "advisor": "AzureAdvisor",
    "intelligence": "FluxIntelligence",
    "policy": "AzurePolicy",
}
CONFIGURED_SCOPE = "configured-subscriptions"


def _clients(config: Settings, integration: dict[str, Any]):
    if integration["authMode"] == "managed_identity":
        credential = ManagedIdentityCredential(
            client_id=config.managed_identity_client_id or None
        )
        provider = ManagedIdentityArgProvider(
            management_endpoint=config.azure_management_endpoint,
            timeout_seconds=config.azure_timeout_seconds,
            intelligence_snapshot_age_days=config.intelligence_snapshot_age_days,
            intelligence_required_tags=config.intelligence_required_tags,
            intelligence_tag_excluded_types=config.intelligence_tag_excluded_types,
            finops_toolkit_ahb_enabled=config.finops_toolkit_ahb_enabled,
            credential=credential,
        )
    else:
        provider = LocalPowerShellArgProvider(
            executable=config.azure_powershell,
            timeout_seconds=config.azure_timeout_seconds,
            intelligence_snapshot_age_days=config.intelligence_snapshot_age_days,
            intelligence_required_tags=config.intelligence_required_tags,
            intelligence_tag_excluded_types=config.intelligence_tag_excluded_types,
            finops_toolkit_ahb_enabled=config.finops_toolkit_ahb_enabled,
        )
        credential = AzurePowerShellCredential(
            tenant_id=integration.get("tenantId") or "",
        )
    return provider, credential


def execute_azure_sync(
    database: FluxDatabase,
    config: Settings,
    sync_id: str,
    integration: dict[str, Any],
    requested_sources: list[str] | None = None,
) -> None:
    requested = [
        source
        for source in SOURCE_ORDER
        if source in set(requested_sources or SOURCE_ORDER)
    ]
    warnings: list[str] = []
    succeeded_scopes = 0
    completed_rows: dict[str, int] = {}
    inventory_collected = False
    try:
        provider, credential = _clients(config, integration)

        if "inventory" in requested:
            source = SOURCE_NAMES["inventory"]
            if database.sync_source_completed(sync_id, source, CONFIGURED_SCOPE):
                succeeded_scopes += 1
            else:
                database.update_sync_stage(
                    sync_id, "inventory", "Collecting Azure inventory."
                )
                database.begin_sync_source(sync_id, source, CONFIGURED_SCOPE)
                try:
                    resources = provider.fetch(integration)
                    database.store_snapshot(
                        sync_id,
                        resources,
                        inventory_collected=True,
                    )
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "succeeded",
                        len(resources),
                        f"Collected {len(resources):,} resources.",
                    )
                    inventory_collected = True
                    succeeded_scopes += 1
                    completed_rows[source] = len(resources)
                except Exception as error:
                    message = f"Azure inventory unavailable: {error}"
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "failed",
                        0,
                        message,
                        retained_last_good=True,
                    )
                    warnings.append(message)

        if "advisor" in requested:
            source = SOURCE_NAMES["advisor"]
            if database.sync_source_completed(sync_id, source, CONFIGURED_SCOPE):
                succeeded_scopes += 1
            else:
                database.update_sync_stage(
                    sync_id, "advisor", "Collecting Azure Advisor recommendations."
                )
                database.begin_sync_source(sync_id, source, CONFIGURED_SCOPE)
                try:
                    advisor = provider.fetch_advisor(integration)
                    database.store_snapshot(
                        sync_id,
                        [],
                        inventory_collected=False,
                        advisor=advisor,
                        advisor_collected=True,
                    )
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "succeeded",
                        len(advisor),
                        f"Collected {len(advisor):,} recommendations.",
                    )
                    succeeded_scopes += 1
                    completed_rows[source] = len(advisor)
                except Exception as error:
                    message = f"Azure Advisor unavailable: {error}"
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "failed",
                        0,
                        message,
                        retained_last_good=True,
                    )
                    warnings.append(message)

        if "intelligence" in requested:
            source = SOURCE_NAMES["intelligence"]
            if database.sync_source_completed(sync_id, source, CONFIGURED_SCOPE):
                succeeded_scopes += 1
            else:
                database.update_sync_stage(
                    sync_id,
                    "intelligence",
                    "Evaluating Flux Intelligence rules.",
                )
                database.begin_sync_source(sync_id, source, CONFIGURED_SCOPE)
                try:
                    intelligence = provider.fetch_intelligence(integration)
                    database.store_snapshot(
                        sync_id,
                        [],
                        inventory_collected=False,
                        intelligence=intelligence,
                        intelligence_collected=True,
                    )
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "succeeded",
                        len(intelligence),
                        f"Evaluated {len(intelligence):,} findings.",
                    )
                    succeeded_scopes += 1
                    completed_rows[source] = len(intelligence)
                except Exception as error:
                    message = f"Flux Intelligence unavailable: {error}"
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "failed",
                        0,
                        message,
                        retained_last_good=True,
                    )
                    warnings.append(message)

        if "policy" in requested:
            source = SOURCE_NAMES["policy"]
            if database.sync_source_completed(sync_id, source, CONFIGURED_SCOPE):
                succeeded_scopes += 1
            else:
                database.update_sync_stage(
                    sync_id,
                    "policy",
                    "Collecting Azure Policy compliance posture.",
                )
                database.begin_sync_source(sync_id, source, CONFIGURED_SCOPE)
                try:
                    policy = provider.fetch_policy(integration)
                    policy_resources = provider.fetch_policy_resources(integration)
                    database.store_policy_posture(
                        sync_id,
                        policy,
                        policy_resources,
                    )
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "succeeded",
                        len(policy),
                        f"Collected {len(policy):,} policy assignments and "
                        f"{len(policy_resources):,} non-compliant or exempt "
                        "resource states.",
                    )
                    succeeded_scopes += 1
                    completed_rows[source] = len(policy)
                except Exception as error:
                    message = f"Azure Policy unavailable: {error}"
                    database.finish_sync_source(
                        sync_id,
                        source,
                        CONFIGURED_SCOPE,
                        "failed",
                        0,
                        message,
                        retained_last_good=True,
                    )
                    warnings.append(message)

        if "cost" in requested and config.cost_management_enabled:
            database.update_sync_stage(
                sync_id, "cost", "Collecting actual and amortized cost."
            )
            cost_provider = CostManagementProvider(
                credential=credential,
                management_endpoint=config.azure_management_endpoint,
                api_version=config.cost_management_api_version,
                timeout_seconds=config.cost_management_timeout_seconds,
                max_retries=config.cost_management_max_retries,
                request_delay_seconds=(
                    config.cost_management_request_delay_seconds
                ),
                request_gate=SharedRequestGate(
                    database,
                    tenant_key=integration.get("tenantId") or "default",
                    qpu_windows=[
                        (10, config.cost_management_qpu_budget_10_seconds),
                        (60, config.cost_management_qpu_budget_60_seconds),
                        (3600, config.cost_management_qpu_budget_3600_seconds),
                    ],
                ),
                client_type=getattr(
                    config,
                    "cost_management_client_type",
                    "FluxFinOps",
                ),
            )
            try:
                access_token = cost_provider.access_token()
            except Exception as error:
                access_token = ""
                warnings.append(f"Cost Management unavailable: {error}")
            scopes = [
                (
                    str(subscription["subscriptionId"]).lower(),
                    subscription.get("label") or subscription["subscriptionId"],
                    scope_type,
                )
                for scope_type in (
                    "ActualCost",
                    "AmortizedCost",
                    "CommitmentCoverage",
                )
                for subscription in integration.get("subscriptions", [])
                if subscription.get("subscriptionId")
            ]
            scopes = database.cost_sync_scope_order(scopes)
            for index, (subscription_id, label, scope_type) in enumerate(scopes):
                if database.sync_source_completed(
                    sync_id, scope_type, subscription_id
                ):
                    succeeded_scopes += 1
                    continue
                database.begin_sync_source(sync_id, scope_type, subscription_id)
                if not access_token:
                    message = f"{label} {scope_type}: token acquisition failed."
                    database.finish_sync_source(
                        sync_id,
                        scope_type,
                        subscription_id,
                        "failed",
                        0,
                        message,
                        retained_last_good=True,
                    )
                    continue
                try:
                    if scope_type == "CommitmentCoverage":
                        records = cost_provider.fetch_commitment_scope(
                            subscription_id,
                            access_token=access_token,
                            attempt_callback=lambda event, scope_subscription=subscription_id, scope_type_name=scope_type: database.record_sync_source_attempt(
                                sync_id,
                                scope_type_name,
                                scope_subscription,
                                event,
                            ),
                        )
                        database.store_snapshot(
                            sync_id,
                            [],
                            inventory_collected=False,
                            commitment_costs=records,
                            commitment_scopes=[subscription_id],
                        )
                    else:
                        records = cost_provider.fetch_scope(
                            subscription_id,
                            scope_type,
                            access_token=access_token,
                            attempt_callback=lambda event, scope_subscription=subscription_id, scope_type_name=scope_type: database.record_sync_source_attempt(
                                sync_id,
                                scope_type_name,
                                scope_subscription,
                                event,
                            ),
                        )
                        database.store_snapshot(
                            sync_id,
                            [],
                            inventory_collected=False,
                            costs=records,
                            cost_scopes=[(subscription_id, scope_type)],
                        )
                    database.finish_sync_source(
                        sync_id,
                        scope_type,
                        subscription_id,
                        "succeeded",
                        len(records),
                        f"Collected {len(records):,} rows for {label}.",
                    )
                    succeeded_scopes += 1
                    completed_rows[scope_type] = (
                        completed_rows.get(scope_type, 0) + len(records)
                    )
                except Exception as error:
                    message = f"{label} {scope_type}: {error}"
                    database.finish_sync_source(
                        sync_id,
                        scope_type,
                        subscription_id,
                        "failed",
                        0,
                        message,
                        retained_last_good=True,
                    )
                    warnings.append(message)
                    if getattr(error, "status_code", None) == 429:
                        time.sleep(
                            max(
                                0,
                                config.cost_management_throttle_cooldown_seconds,
                            )
                        )
                if index < len(scopes) - 1:
                    time.sleep(max(0, config.cost_management_request_delay_seconds))

        if succeeded_scopes:
            if {"advisor", "intelligence"} & set(requested):
                database.update_sync_stage(
                    sync_id,
                    "confidence",
                    "Scoring opportunity persistence and evidence.",
                )
                database.compute_opportunity_confidence(sync_id)
            if {"advisor", "intelligence", "cost"} & set(requested):
                database.update_sync_stage(
                    sync_id,
                    "valuation",
                    "Valuing opportunities from governed Azure cost evidence.",
                )
                database.compute_opportunity_valuation(sync_id)
            if inventory_collected:
                database.update_sync_stage(
                    sync_id,
                    "drift",
                    "Comparing inventory snapshots and evaluating change baselines.",
                )
                database.compute_inventory_drift(
                    sync_id,
                    minimum_points=config.drift_min_baseline_points,
                    threshold_k=config.drift_mad_threshold,
                )

        inventory_count = database.inventory(limit=1)["total"]
        details = ", ".join(
            f"{name} {count:,}" for name, count in completed_rows.items()
        )
        message = (
            f"Completed {succeeded_scopes:,} source scopes"
            + (f" ({details})" if details else "")
            + "."
        )
        if warnings:
            message += (
                f" Retained last-good data for {len(warnings):,} failed scopes; "
                f"first error: {warnings[0]}"
            )
        status = "succeeded" if succeeded_scopes else "failed"
        database.finish_sync(sync_id, status, message, inventory_count)
        if (
            succeeded_scopes
            and config.backup_storage_account_url
            and not config.analytics_snapshot_publish
        ):
            # Approved snapshot publications are validated, checksummed,
            # versioned copies with daily retention — they replace the legacy
            # per-sync full-database backup when publishing is enabled.
            backup_database(database, config)
    except Exception as error:
        database.finish_sync(sync_id, "failed", str(error), 0)


def process_sync_queue_once(database: FluxDatabase, config: Settings) -> bool:
    """Process one persisted sync request if the singleton lease is available."""
    try:
        with deployment_lease(config.database_path):
            with database.singleton_lease("sync-worker"):
                work = database.claim_next_sync()
                if not work:
                    return False
                execute_azure_sync(
                    database,
                    config,
                    work["id"],
                    database.integration(),
                    work.get("sources"),
                )
                return True
    except (
        DatabaseBusyError,
        DeploymentQuiesced,
        SingletonLeaseUnavailable,
        Timeout,
    ):
        # DatabaseBusyError only occurs for the embedded development worker,
        # which shares the web process's bounded connect timeout; the request
        # stays queued and the next poll retries it. A held singleton lease
        # means another worker is already processing.
        return False


def run_sync_worker(
    database: FluxDatabase,
    config: Settings,
    stop_event: Event | None = None,
) -> None:
    """Continuously consume persisted sync requests."""
    stop = stop_event or Event()
    while not stop.is_set():
        try:
            with deployment_lease(config.database_path):
                database.init()
            break
        except DeploymentQuiesced:
            stop.wait(max(1, config.sync_worker_poll_seconds))
    publisher = None
    if config.analytics_snapshot_publish:
        from .analytics_snapshot import SnapshotPublisher, storage_from_settings

        publisher = SnapshotPublisher(
            database,
            storage_from_settings(config),
            retention=config.analytics_snapshot_retention,
            min_interval_seconds=config.analytics_snapshot_min_interval_seconds,
            daily_retention_days=config.analytics_snapshot_daily_retention_days,
        )
    while not stop.is_set():
        processed = process_sync_queue_once(database, config)
        if processed and publisher:
            # Publish once per completed synchronization; failure keeps the
            # previous approved snapshot current and never fails the sync.
            try:
                publisher.publish()
            except Exception as error:
                print(f"Snapshot publication failed: {error}")
        if not processed:
            stop.wait(max(1, config.sync_worker_poll_seconds))
