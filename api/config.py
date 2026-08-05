from dataclasses import dataclass, field
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _json_env(name: str) -> dict:
    """Parse a JSON-object environment variable; malformed input is treated
    as absent rather than crashing settings import."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip().lower()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("FLUX_HOST", "127.0.0.1")
    port: int = int(os.getenv("FLUX_PORT", os.getenv("PORT", "8765")))
    database_path: Path = Path(
        os.getenv("FLUX_DUCKDB_PATH", str(ROOT / "data" / "flux.duckdb"))
    )
    operational_database_url: str = os.getenv(
        "FLUX_OPERATIONAL_DATABASE_URL",
        "",
    ).strip()
    operational_database_enabled: bool = env_bool(
        "FLUX_OPERATIONAL_DATABASE_ENABLED",
        False,
    )
    operational_duckdb_path: Path = Path(
        os.getenv(
            "FLUX_OPERATIONAL_DUCKDB_PATH",
            os.getenv(
                "FLUX_DUCKDB_PATH",
                str(ROOT / "data" / "flux.duckdb"),
            ),
        )
    )
    # Web-process bound on waiting for the cross-process DuckDB lease. The
    # request fails fast with 503 + Retry-After instead of hanging behind a
    # long-running writer. Worker and CLI jobs keep the historical unbounded
    # wait; -1 disables the bound.
    duckdb_connect_timeout_seconds: float = float(
        os.getenv("FLUX_DUCKDB_CONNECT_TIMEOUT_SECONDS", "15")
    )
    # Analytical snapshot publication and consumption (interim-architecture
    # plan sections 12-13). Mode "direct" keeps lease-guarded reads of the
    # mutable database; "snapshot" serves analytical reads from published
    # immutable copies so the web process never opens the writer database.
    analytics_snapshot_mode: str = os.getenv(
        "FLUX_ANALYTICS_SNAPSHOT_MODE", "direct"
    ).strip().lower()
    analytics_snapshot_publish: bool = env_bool(
        "FLUX_ANALYTICS_SNAPSHOT_PUBLISH",
        False,
    )
    analytics_snapshot_storage_account_url: str = os.getenv(
        "FLUX_SNAPSHOT_STORAGE_ACCOUNT_URL", ""
    ).strip()
    analytics_snapshot_container: str = os.getenv(
        "FLUX_SNAPSHOT_CONTAINER", "flux-analytics-snapshots"
    )
    analytics_snapshot_local_directory: Path = Path(
        os.getenv(
            "FLUX_SNAPSHOT_LOCAL_DIRECTORY",
            str(ROOT / "data" / "snapshots"),
        )
    )
    # API-instance cache for downloaded snapshots. Instance-local storage
    # (for example /tmp on App Service) is preferred over the shared /home
    # mount; copies are disposable and re-downloadable.
    analytics_snapshot_cache_directory: Path = Path(
        os.getenv(
            "FLUX_SNAPSHOT_CACHE_DIRECTORY",
            str(ROOT / "data" / "snapshot-cache"),
        )
    )
    analytics_snapshot_refresh_seconds: int = int(
        os.getenv("FLUX_ANALYTICS_SNAPSHOT_REFRESH_SECONDS", "60")
    )
    # Durable staged analytical payloads (plan Phase 5) awaiting the
    # singleton analytics writer.
    analytics_staging_directory: Path = Path(
        os.getenv(
            "FLUX_ANALYTICS_STAGING_DIRECTORY",
            str(ROOT / "data" / "staging"),
        )
    )
    analytics_snapshot_retention: int = int(
        os.getenv("FLUX_ANALYTICS_SNAPSHOT_RETENTION", "5")
    )
    # Coalesce publication bursts: skip publishing when the newest approved
    # snapshot is younger than this. 0 disables coalescing.
    analytics_snapshot_min_interval_seconds: float = float(
        os.getenv("FLUX_ANALYTICS_SNAPSHOT_MIN_INTERVAL_SECONDS", "600")
    )
    # Approved snapshots double as the analytical backup tier: keep the
    # newest version per UTC day this many days in addition to the newest
    # FLUX_ANALYTICS_SNAPSHOT_RETENTION versions.
    analytics_snapshot_daily_retention_days: int = int(
        os.getenv("FLUX_ANALYTICS_SNAPSHOT_DAILY_RETENTION_DAYS", "14")
    )
    frontend_dist: Path = Path(
        os.getenv("FLUX_FRONTEND_DIST", str(ROOT / "frontend" / "dist"))
    )
    azure_provider: str = os.getenv("FLUX_AZURE_PROVIDER", "local_powershell")
    azure_powershell: str = os.getenv("FLUX_AZURE_POWERSHELL", "pwsh")
    azure_timeout_seconds: int = int(os.getenv("FLUX_AZURE_TIMEOUT_SECONDS", "180"))
    azure_management_endpoint: str = os.getenv(
        "FLUX_AZURE_MANAGEMENT_ENDPOINT", "https://management.azure.com"
    ).rstrip("/")
    cost_management_enabled: bool = env_bool(
        "FLUX_COST_MANAGEMENT_ENABLED",
        True,
    )
    cost_management_api_version: str = os.getenv(
        "FLUX_COST_MANAGEMENT_API_VERSION",
        "2025-03-01",
    )
    cost_management_timeout_seconds: int = int(
        os.getenv("FLUX_COST_MANAGEMENT_TIMEOUT_SECONDS", "120")
    )
    cost_management_max_retries: int = int(
        os.getenv("FLUX_COST_MANAGEMENT_MAX_RETRIES", "5")
    )
    cost_management_request_delay_seconds: float = float(
        os.getenv("FLUX_COST_MANAGEMENT_REQUEST_DELAY_SECONDS", "20")
    )
    cost_management_client_type: str = os.getenv(
        "FLUX_COST_MANAGEMENT_CLIENT_TYPE",
        "FluxFinOps",
    )
    cost_management_throttle_cooldown_seconds: float = float(
        os.getenv("FLUX_COST_MANAGEMENT_THROTTLE_COOLDOWN_SECONDS", "30")
    )
    # Flux's default operating ceiling is 50% of Microsoft's currently
    # published Cost Management QPU quotas (12/10s, 60/min, 600/hour).
    cost_management_qpu_budget_10_seconds: float = float(
        os.getenv("FLUX_COST_MANAGEMENT_QPU_BUDGET_10_SECONDS", "6")
    )
    cost_management_qpu_budget_60_seconds: float = float(
        os.getenv("FLUX_COST_MANAGEMENT_QPU_BUDGET_60_SECONDS", "30")
    )
    cost_management_qpu_budget_3600_seconds: float = float(
        os.getenv("FLUX_COST_MANAGEMENT_QPU_BUDGET_3600_SECONDS", "300")
    )
    cost_history_initial_days: int = int(
        os.getenv("FLUX_COST_HISTORY_INITIAL_DAYS", "90")
    )
    cost_history_refresh_days: int = int(
        os.getenv("FLUX_COST_HISTORY_REFRESH_DAYS", "14")
    )
    cost_history_chunk_days: int = int(
        os.getenv("FLUX_COST_HISTORY_CHUNK_DAYS", "14")
    )
    cost_details_backfill_enabled: bool = env_bool(
        "FLUX_COST_DETAILS_BACKFILL_ENABLED",
        True,
    )
    cost_details_max_reports_per_run: int = int(
        os.getenv("FLUX_COST_DETAILS_MAX_REPORTS_PER_RUN", "4")
    )
    cost_details_poll_interval_seconds: float = float(
        os.getenv("FLUX_COST_DETAILS_POLL_INTERVAL_SECONDS", "20")
    )
    cost_details_max_poll_attempts: int = int(
        os.getenv("FLUX_COST_DETAILS_MAX_POLL_ATTEMPTS", "30")
    )
    cost_details_current_refresh_days: int = int(
        os.getenv("FLUX_COST_DETAILS_CURRENT_REFRESH_DAYS", "7")
    )
    cost_coverage_requeue_months: int = int(
        os.getenv("FLUX_COST_COVERAGE_REQUEUE_MONTHS", "3")
    )
    focus_cost_enabled: bool = env_bool("FLUX_FOCUS_COST_ENABLED", True)
    focus_cost_required: bool = env_bool("FLUX_FOCUS_COST_REQUIRED", False)
    focus_storage_account_url: str = os.getenv(
        "FLUX_FOCUS_STORAGE_ACCOUNT_URL",
        "",
    ).rstrip("/")
    focus_storage_container: str = os.getenv(
        "FLUX_FOCUS_STORAGE_CONTAINER", "cost-management"
    )
    focus_storage_prefix: str = os.getenv(
        "FLUX_FOCUS_STORAGE_PREFIX", "focus/"
    ).lstrip("/")
    focus_local_path: Path | None = (
        Path(os.environ["FLUX_FOCUS_LOCAL_PATH"])
        if os.getenv("FLUX_FOCUS_LOCAL_PATH")
        else None
    )
    # Price sheet exports land in the same storage account and container as
    # the FOCUS exports, under their own folder.
    price_sheet_storage_prefix: str = os.getenv(
        "FLUX_PRICESHEET_STORAGE_PREFIX", "pricesheet/"
    ).lstrip("/")
    focus_max_manifests_per_run: int = int(
        os.getenv("FLUX_FOCUS_MAX_MANIFESTS_PER_RUN", "16")
    )
    cost_anomaly_latency_days: int = int(
        os.getenv("FLUX_COST_ANOMALY_LATENCY_DAYS", "2")
    )
    cost_anomaly_minimum_history_days: int = int(
        os.getenv("FLUX_COST_ANOMALY_MINIMUM_HISTORY_DAYS", "28")
    )
    cost_anomaly_minimum_baseline_points: int = int(
        os.getenv("FLUX_COST_ANOMALY_MINIMUM_BASELINE_POINTS", "4")
    )
    cost_anomaly_baseline_weeks: int = int(
        os.getenv("FLUX_COST_ANOMALY_BASELINE_WEEKS", "8")
    )
    cost_anomaly_threshold_k: float = float(
        os.getenv("FLUX_COST_ANOMALY_THRESHOLD_K", "3.5")
    )
    cost_anomaly_minimum_increase: float = float(
        os.getenv("FLUX_COST_ANOMALY_MINIMUM_INCREASE", "10")
    )
    # ServiceNow target discovered 2026-08-02: standard-user access to the
    # planned_task form with assignment group AzureCloud_CF. Used to build
    # pre-filled form links; no credentials are stored or used.
    servicenow_instance_url: str = os.getenv(
        "FLUX_SERVICENOW_INSTANCE_URL",
        "",
    ).rstrip("/")
    servicenow_task_table: str = os.getenv(
        "FLUX_SERVICENOW_TASK_TABLE", "planned_task"
    )
    servicenow_assignment_group: str = os.getenv(
        "FLUX_SERVICENOW_ASSIGNMENT_GROUP", "AzureCloud_CF"
    )
    # Field defaults chosen by the task owner 2026-08-03. The CI is the
    # closest existing record until a proper Azure CI exists.
    servicenow_priority: str = os.getenv("FLUX_SERVICENOW_PRIORITY", "3")
    servicenow_configuration_item: str = os.getenv(
        "FLUX_SERVICENOW_CONFIGURATION_ITEM", "Azure AD (Entra)"
    )
    servicenow_due_days: int = int(
        os.getenv("FLUX_SERVICENOW_DUE_DAYS", "30")
    )
    # Flux Signal kinds allowed to generate planned ServiceNow remediation
    # tasks. Deliberately small; extend only after the workflow proves out.
    remediation_signal_allowlist: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "FLUX_REMEDIATION_SIGNAL_ALLOWLIST", "unattached_disk"
        ).split(",")
        if item.strip()
    )
    # Outbound pipeline-warning notifications. Empty URL disables the
    # flux-alerts job; the URL should be a Teams/Slack incoming-webhook
    # endpoint and belongs in an app setting or Key Vault reference, never
    # in the repository.
    alert_webhook_url: str = os.getenv("FLUX_ALERT_WEBHOOK_URL", "").strip()
    alert_renotify_hours: float = float(
        os.getenv("FLUX_ALERT_RENOTIFY_HOURS", "24")
    )
    # Optional per-environment SLO threshold overrides as one JSON object,
    # e.g. {"cost_coverage_percent": {"warn": 99, "breach": 95}}. Objective
    # definitions live in api/slo.py; the alert catalog runbook documents
    # each objective and its first response.
    slo_threshold_overrides: dict = field(
        default_factory=lambda: _json_env("FLUX_SLO_THRESHOLD_OVERRIDES")
    )
    # Retention for the append-only analytical history tables; the daily
    # sweep never changes what *_current resolves to (see
    # FluxDatabase.prune_analytics_history).
    analytics_history_retention_days: int = int(
        os.getenv("FLUX_ANALYTICS_HISTORY_RETENTION_DAYS", "60")
    )
    telemetry_sample_retention_days: int = int(
        os.getenv("FLUX_TELEMETRY_SAMPLE_RETENTION_DAYS", "16")
    )
    sync_worker_mode: str = os.getenv(
        "FLUX_SYNC_WORKER_MODE",
        "external" if os.getenv("WEBSITE_SITE_NAME") else "embedded",
    ).strip().lower()
    sync_worker_poll_seconds: int = int(
        os.getenv("FLUX_SYNC_WORKER_POLL_SECONDS", "5")
    )
    drift_min_baseline_points: int = int(
        os.getenv("FLUX_DRIFT_MIN_BASELINE_POINTS", "5")
    )
    drift_mad_threshold: float = float(
        os.getenv("FLUX_DRIFT_MAD_THRESHOLD", "3")
    )
    rightsizing_min_window_days: int = int(
        os.getenv("FLUX_RIGHTSIZING_MIN_WINDOW_DAYS", "14")
    )
    rightsizing_min_coverage_percent: float = float(
        os.getenv("FLUX_RIGHTSIZING_MIN_COVERAGE_PERCENT", "70")
    )
    rightsizing_idle_cpu_p95: float = float(
        os.getenv("FLUX_RIGHTSIZING_IDLE_CPU_P95", "5")
    )
    rightsizing_idle_cpu_maximum: float = float(
        os.getenv("FLUX_RIGHTSIZING_IDLE_CPU_MAXIMUM", "20")
    )
    rightsizing_idle_network_p95_bytes: float = float(
        os.getenv("FLUX_RIGHTSIZING_IDLE_NETWORK_P95_BYTES", "52428800")
    )
    rightsizing_review_cpu_p95: float = float(
        os.getenv("FLUX_RIGHTSIZING_REVIEW_CPU_P95", "30")
    )
    rightsizing_memory_review_percent: float = float(
        os.getenv("FLUX_RIGHTSIZING_MEMORY_REVIEW_PERCENT", "80")
    )
    rightsizing_cpu_disagreement_percent: float = float(
        os.getenv("FLUX_RIGHTSIZING_CPU_DISAGREEMENT_PERCENT", "20")
    )
    premium_disk_review_window_days: int = int(
        os.getenv("FLUX_PREMIUM_DISK_REVIEW_WINDOW_DAYS", "30")
    )
    premium_disk_review_coverage_percent: float = float(
        os.getenv("FLUX_PREMIUM_DISK_REVIEW_COVERAGE_PERCENT", "70")
    )
    premium_disk_review_iops_p95: float = float(
        os.getenv("FLUX_PREMIUM_DISK_REVIEW_IOPS_P95", "20")
    )
    premium_disk_review_throughput_p95_bytes: float = float(
        os.getenv("FLUX_PREMIUM_DISK_REVIEW_THROUGHPUT_P95_BYTES", "1048576")
    )
    telemetry_bootstrap_root: Path = Path(
        os.getenv(
            "FLUX_TELEMETRY_BOOTSTRAP_ROOT",
            "/home/data/telemetry-bootstrap"
            if os.getenv("WEBSITE_SITE_NAME")
            else str(ROOT / "data" / "telemetry-bootstrap"),
        )
    )
    intelligence_snapshot_age_days: int = int(
        os.getenv("FLUX_INTELLIGENCE_SNAPSHOT_AGE_DAYS", "30")
    )
    intelligence_required_tags: tuple[str, ...] = env_list(
        "FLUX_INTELLIGENCE_REQUIRED_TAGS"
    )
    intelligence_tag_excluded_types: tuple[str, ...] = env_list(
        "FLUX_INTELLIGENCE_TAG_EXCLUDED_TYPES"
    )
    finops_toolkit_ahb_enabled: bool = env_bool(
        "FLUX_FINOPS_TOOLKIT_AHB_ENABLED",
        True,
    )
    finops_toolkit_cache_root: Path = Path(
        os.getenv(
            "FLUX_FINOPS_TOOLKIT_CACHE_ROOT",
            "/home/data/finops-toolkit"
            if os.getenv("WEBSITE_SITE_NAME")
            else str(ROOT / "data" / "finops-toolkit"),
        )
    )
    retail_prices_endpoint: str = os.getenv(
        "FLUX_RETAIL_PRICES_ENDPOINT",
        "https://prices.azure.com/api/retail/prices",
    ).rstrip("/")
    retail_prices_api_version: str = os.getenv(
        "FLUX_RETAIL_PRICES_API_VERSION",
        "2023-01-01-preview",
    )
    retail_prices_timeout_seconds: int = int(
        os.getenv("FLUX_RETAIL_PRICES_TIMEOUT_SECONDS", "30")
    )
    retail_prices_request_delay_ms: int = int(
        os.getenv("FLUX_RETAIL_PRICES_REQUEST_DELAY_MS", "100")
    )
    retail_prices_refresh_hours: int = int(
        os.getenv("FLUX_RETAIL_PRICES_REFRESH_HOURS", "24")
    )
    retail_prices_hours_per_month: float = float(
        os.getenv("FLUX_RETAIL_PRICES_HOURS_PER_MONTH", "730")
    )
    backup_storage_account_url: str = os.getenv(
        "FLUX_BACKUP_STORAGE_ACCOUNT_URL", ""
    ).rstrip("/")
    backup_container: str = os.getenv("FLUX_BACKUP_CONTAINER", "flux-backups")
    backup_retention_days: int = int(os.getenv("FLUX_BACKUP_RETENTION_DAYS", "30"))
    recover_database_from_latest_backup: bool = env_bool(
        "FLUX_RECOVER_DATABASE_FROM_LATEST_BACKUP",
        False,
    )
    azure_monitor_days: int = int(os.getenv("FLUX_AZURE_MONITOR_DAYS", "14"))
    # AMA/DCR guest telemetry: the Log Analytics workspace customer ID
    # (GUID) that the tenant's AMA baseline DCRs write Perf data
    # to. Empty disables guest-memory
    # collection; the platform-metric path is unaffected either way. The
    # app's identity needs Log Analytics Reader on the workspace.
    ama_log_analytics_workspace_id: str = os.getenv(
        "FLUX_AMA_LOG_ANALYTICS_WORKSPACE_ID", ""
    ).strip()
    ama_telemetry_days: int = int(os.getenv("FLUX_AMA_TELEMETRY_DAYS", "14"))
    azure_monitor_batch_size: int = int(
        os.getenv("FLUX_AZURE_MONITOR_BATCH_SIZE", "200")
    )
    logicmonitor_account: str = os.getenv("FLUX_LOGICMONITOR_ACCOUNT", "").strip()
    logicmonitor_group_ids: tuple[str, ...] = env_list(
        "FLUX_LOGICMONITOR_GROUP_IDS", "4,5"
    )
    logicmonitor_bearer_token: str = os.getenv("LM_BEARER_TOKEN", "").strip()
    logicmonitor_request_delay_ms: int = int(
        os.getenv("FLUX_LOGICMONITOR_REQUEST_DELAY_MS", "250")
    )
    logicmonitor_metric_batch_size: int = int(
        os.getenv("FLUX_LOGICMONITOR_METRIC_BATCH_SIZE", "12")
    )
    logicmonitor_initial_window_hours: int = int(
        os.getenv("FLUX_LOGICMONITOR_INITIAL_WINDOW_HOURS", "8")
    )
    logicmonitor_maximum_window_hours: int = int(
        os.getenv("FLUX_LOGICMONITOR_MAXIMUM_WINDOW_HOURS", "12")
    )
    logicmonitor_metric_history_days: int = int(
        os.getenv("FLUX_LOGICMONITOR_METRIC_HISTORY_DAYS", "14")
    )
    # Nothing reads raw samples past the 14-day summary window
    # (logicmonitor_metric_history_days); retaining 30 days doubled the
    # samples table and its memory-resident PK index for no reader, which
    # is what pushed the metrics import past the DuckDB memory cap.
    logicmonitor_metric_retention_days: int = int(
        os.getenv("FLUX_LOGICMONITOR_METRIC_RETENTION_DAYS", "16")
    )
    logicmonitor_maximum_instances: int = int(
        os.getenv("FLUX_LOGICMONITOR_MAXIMUM_INSTANCES", "8")
    )
    managed_identity_client_id: str = os.getenv(
        "FLUX_MANAGED_IDENTITY_CLIENT_ID", os.getenv("AZURE_CLIENT_ID", "")
    )
    auth_mode: str = os.getenv("FLUX_AUTH_MODE", "mock").strip().lower()
    mock_user_name: str = os.getenv("FLUX_MOCK_USER_NAME", "Local Administrator")
    mock_user_email: str = os.getenv("FLUX_MOCK_USER_EMAIL", "local@flux.invalid")
    entra_tenant_id: str = os.getenv("FLUX_ENTRA_TENANT_ID", "").strip().lower()
    entra_admin_assignments: tuple[str, ...] = env_list(
        "FLUX_ENTRA_ADMIN_ASSIGNMENTS", "Flux.Admin"
    )
    entra_reader_assignments: tuple[str, ...] = env_list(
        "FLUX_ENTRA_READER_ASSIGNMENTS", "Flux.Reader"
    )
    intelligence_ai_enabled: bool = env_bool(
        "FLUX_INTELLIGENCE_AI_ENABLED", False
    )
    intelligence_ai_provider: str = os.getenv(
        "FLUX_AI_PROVIDER", "deepseek"
    ).strip().lower()
    deepseek_base_url: str = os.getenv(
        "FLUX_DEEPSEEK_BASE_URL", "https://api.deepseek.com"
    ).rstrip("/")
    deepseek_api_key: str = os.getenv("FLUX_DEEPSEEK_API_KEY", "").strip()
    deepseek_chat_model: str = os.getenv(
        "FLUX_DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash"
    ).strip()
    deepseek_benchmark_model: str = os.getenv(
        "FLUX_DEEPSEEK_BENCHMARK_MODEL", "deepseek-v4-pro"
    ).strip()
    openrouter_api_key: str = os.getenv(
        "FLUX_OPENROUTER_API_KEY", ""
    ).strip()
    openrouter_base_url: str = os.getenv(
        "FLUX_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    ).rstrip("/")
    openrouter_chat_model: str = os.getenv(
        "FLUX_OPENROUTER_CHAT_MODEL", "google/gemini-2.5-flash-lite"
    ).strip()
    openrouter_benchmark_model: str = os.getenv(
        "FLUX_OPENROUTER_BENCHMARK_MODEL", "openai/gpt-4.1-mini"
    ).strip()
    foundry_endpoint: str = os.getenv("FLUX_FOUNDRY_ENDPOINT", "").rstrip("/")
    foundry_api_key: str = os.getenv("FLUX_FOUNDRY_API_KEY", "").strip()
    foundry_api_version: str = os.getenv(
        "FLUX_FOUNDRY_API_VERSION", "2024-05-01-preview"
    ).strip()
    foundry_chat_model: str = os.getenv("FLUX_FOUNDRY_CHAT_MODEL", "").strip()
    foundry_benchmark_model: str = os.getenv(
        "FLUX_FOUNDRY_BENCHMARK_MODEL", ""
    ).strip()
    # Claude deployments on Foundry are served through a dedicated
    # Anthropic-Messages-API-compatible route, not the OpenAI-shaped Chat
    # Completions endpoint the rest of foundry_* config targets. Left blank,
    # FoundryProvider derives this from foundry_endpoint (swap a trailing
    # "/models" for "/anthropic"); set explicitly only if that guess is wrong
    # for your resource.
    foundry_anthropic_endpoint: str = os.getenv(
        "FLUX_FOUNDRY_ANTHROPIC_ENDPOINT", ""
    ).rstrip("/")
    foundry_anthropic_api_version: str = os.getenv(
        "FLUX_FOUNDRY_ANTHROPIC_API_VERSION", "2023-06-01"
    ).strip()
    intelligence_ai_budget_usd: float = float(
        os.getenv("FLUX_AI_BUDGET_USD", "10")
    )
    intelligence_ai_stop_at_usd: float = float(
        os.getenv("FLUX_AI_STOP_AT_USD", "8")
    )
    intelligence_ai_retention_days: int = int(
        os.getenv("FLUX_AI_USAGE_RETENTION_DAYS", "30")
    )
    intelligence_ai_transcript_retention_days: int = int(
        os.getenv("FLUX_AI_TRANSCRIPT_RETENTION_DAYS", "30")
    )
    intelligence_ai_timeout_seconds: int = int(
        os.getenv("FLUX_AI_TIMEOUT_SECONDS", "90")
    )
    intelligence_ai_slow_request_ms: int = int(
        os.getenv("FLUX_AI_SLOW_REQUEST_MS", "20000")
    )
    intelligence_ai_max_tool_calls: int = int(
        os.getenv("FLUX_AI_MAX_TOOL_CALLS", "12")
    )
    intelligence_ai_max_input_chars: int = int(
        os.getenv("FLUX_AI_MAX_INPUT_CHARS", "24000")
    )
    intelligence_ai_max_output_tokens: int = int(
        os.getenv("FLUX_AI_MAX_OUTPUT_TOKENS", "4096")
    )
    intelligence_ai_tool_cache_seconds: int = int(
        os.getenv("FLUX_AI_TOOL_CACHE_SECONDS", "30")
    )
    intelligence_ai_telemetry_salt: str = os.getenv(
        "FLUX_AI_TELEMETRY_SALT", ""
    ).strip()
    # Wiki.js base URL is not a secret; the API token is, and must be a Key
    # Vault reference set on the App Service directly (see azure-pipelines.yml's
    # "Apply non-secret app settings" step) -- never in this file's default or
    # in pipeline YAML. search_documentation silently skips the live wiki
    # source when the token is blank, exactly like other optional providers.
    wiki_base_url: str = os.getenv(
        "FLUX_WIKI_BASE_URL", ""
    ).strip().rstrip("/")
    wiki_api_token: str = os.getenv("FLUX_WIKI_API_TOKEN", "").strip()
    wiki_request_timeout_seconds: int = int(
        os.getenv("FLUX_WIKI_TIMEOUT_SECONDS", "10")
    )
    auth_login_path: str = os.getenv("FLUX_AUTH_LOGIN_PATH", "/.auth/login/aad")
    auth_logout_path: str = os.getenv("FLUX_AUTH_LOGOUT_PATH", "/.auth/logout")
    cors_origins: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "FLUX_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if item.strip()
    )
    dev_seed: bool = env_bool("FLUX_DEV_SEED", False)


settings = Settings()
