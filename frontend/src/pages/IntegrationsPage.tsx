import {
  Activity, AlertTriangle, Check, Cloud, Database, HardDrive, History,
  Play, Plus, RefreshCw, Save, ShieldCheck, Sparkles, Trash2,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { absoluteTime, currency, relativeTime, titleCase } from "../format";
import type {
  AdminJob,
  AiIntelligenceConfig,
  AuditEntry,
  AzureIntegration,
  CostCoverage,
  CostDatasetStatus,
  CostHistoryStatus,
  DatabaseHealth,
  FinOpsToolkitStatus,
  OperationalHealth,
  RetentionPolicy,
  RightsizingProposalStatus,
  SloReport,
  TelemetryCoverage,
  TelemetryStatus,
} from "../types";
import { Card, ErrorPanel, Loading, PageHeader, Tabs } from "../components/Ui";
import { VirtualTagsAdmin } from "../components/VirtualTagsAdmin";

type AdminTab = "connectors" | "configuration" | "health" | "quality";

function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** exponent).toFixed(exponent ? 1 : 0)} ${units[exponent]}`;
}

/** Both storage engines at a glance. The analytical row is filesystem-only
 *  by design; opening the mutable DuckDB file from the web process is what
 *  caused the 2026-07-29 outage. */
function DatabaseHealthPanel() {
  const [health, setHealth] = useState<DatabaseHealth | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api.databaseHealth().then(setHealth).catch(() => setFailed(true));
  }, []);

  return (
    <Card className="operations-health-card">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Storage engines</span>
          <h2>Database health</h2>
          <p>
            The PostgreSQL control plane holds operational state; DuckDB holds the
            analytical estate. Analytical figures are read from filesystem metadata
            only, never by opening the mutable database.
          </p>
        </div>
        <Database size={18} />
      </div>
      {failed && <p className="muted">Database health is unavailable right now.</p>}
      {!failed && !health && <p className="muted">Loading…</p>}
      {health && (
        <div className="db-health-grid">
          <div className="db-health-item">
            <h3>Control plane</h3>
            <span className={`pill pill--${health.controlPlane.reachable ? "flux" : "danger"}`}>
              {health.controlPlane.reachable ? "Reachable" : "Unreachable"}
            </span>
            <dl>
              <dt>Engine</dt><dd>{titleCase(health.controlPlane.engine)}</dd>
              <dt>Round trip</dt>
              <dd>{health.controlPlane.latencyMs === null ? "—" : `${health.controlPlane.latencyMs} ms`}</dd>
              {health.controlPlane.error && (<><dt>Error</dt><dd>{health.controlPlane.error}</dd></>)}
            </dl>
          </div>
          <div className="db-health-item">
            <h3>Analytical store</h3>
            <span className={`pill pill--${health.analytical.exists ? "flux" : "danger"}`}>
              {health.analytical.exists ? "Present" : "Missing"}
            </span>
            <dl>
              <dt>Engine</dt><dd>{titleCase(health.analytical.engine)}</dd>
              <dt>Size</dt><dd>{formatBytes(health.analytical.sizeBytes)}</dd>
              <dt>Last written</dt>
              <dd>{health.analytical.modifiedAt ? relativeTime(health.analytical.modifiedAt) : "—"}</dd>
              <dt>Writer lease</dt>
              <dd>{health.analytical.writerLeaseHeld === null ? "Unknown" : health.analytical.writerLeaseHeld ? "Held" : "Free"}</dd>
              {health.analytical.error && (<><dt>Error</dt><dd>{health.analytical.error}</dd></>)}
            </dl>
          </div>
        </div>
      )}
    </Card>
  );
}

/** Collector schedules, last outcome, and on-demand runs for the sources the
 *  in-app queue can drive. Scheduled WebJob collectors are shown read-only
 *  rather than granting this process Kudu access to trigger itself. */
function JobControlPanel() {
  const [jobs, setJobs] = useState<AdminJob[] | null>(null);
  const [busySource, setBusySource] = useState("");
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");

  const load = useCallback(() => {
    api.adminJobs().then((value) => setJobs(value.jobs)).catch(() => setJobs([]));
  }, []);
  useEffect(load, [load]);

  function run(job: AdminJob) {
    setBusySource(job.triggerSource);
    setMessage("");
    setFailure("");
    api.runAdminJob(job.triggerSource)
      .then(() => {
        setMessage(`${job.label} queued. The worker picks it up within a minute.`);
        load();
      })
      .catch((reason) => setFailure(reason.message))
      .finally(() => setBusySource(""));
  }

  return (
    <Card className="operations-health-card">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Collection schedule</span>
          <h2>Jobs</h2>
          <p>
            Every collector, its cadence, and its last outcome. Azure metadata
            sources can be queued on demand; cost and telemetry collectors run on
            their own paced schedules to protect API quota.
          </p>
        </div>
        <RefreshCw size={18} />
      </div>
      {message && <div className="inline-alert"><Check size={16} />{message}</div>}
      {failure && <div className="inline-alert inline-alert--error">{failure}</div>}
      {!jobs && <p className="muted">Loading…</p>}
      {jobs && (
        <div className="job-table">
          <div className="job-row job-row--header">
            <span>Collector</span><span>Cadence</span><span>Last run</span><span />
          </div>
          {jobs.map((job) => (
            <div className="job-row" key={job.source}>
              <span>
                <strong>{job.label}</strong>
                {job.lastAttemptStatus === "failed" && job.lastAttemptMessage && (
                  <small title={job.lastAttemptMessage}>{job.lastAttemptMessage}</small>
                )}
              </span>
              <span>{job.schedule || "—"}</span>
              <span>
                {job.observedAt ? relativeTime(job.observedAt) : "Never"}
                <small>
                  {job.rowCount.toLocaleString()} rows
                  {job.health && job.health !== "healthy" ? ` · ${titleCase(job.health)}` : ""}
                </small>
              </span>
              {job.triggerSource ? (
                <button
                  className="button button--ghost"
                  onClick={() => run(job)}
                  disabled={Boolean(busySource)}
                >
                  <Play size={13} />{busySource === job.triggerSource ? "Queuing…" : "Run now"}
                </button>
              ) : (
                <span className="muted" title="Runs on its own schedule to protect API quota.">
                  Scheduled
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function RightsizingProposalControl() {
  const [status, setStatus] = useState<RightsizingProposalStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");

  const load = useCallback(() => {
    api.rightsizingProposalStatus().then(setStatus).catch(() => setStatus(null));
  }, []);
  useEffect(load, [load]);

  function refresh() {
    setBusy(true);
    setMessage("");
    setFailure("");
    api.refreshRightsizingProposal()
      .then((result) => {
        setMessage(
          `Flux proposal refreshed: ${result.bucketCount ?? 0} commitment buckets, `
          + `${result.waste ?? 0} waste exclusions, ${result.provisional ?? 0} provisional placements, `
          + `${result.review ?? 0} kept on demand for technical review, `
          + `${result.savingsPlan ?? 0} Savings Plan candidates, and `
          + `${currency(result.modeledMonthlySavings ?? 0)}/month retail-reconciled savings.`,
        );
        load();
      })
      .catch((reason) => setFailure(reason.message))
      .finally(() => setBusy(false));
  }

  return (
    <Card className="integration-form">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Optimization planning</span>
          <h2>Flux right-sizing proposal</h2>
          <p>
            Regenerated every three days from governed telemetry, waste findings,
            reservation coverage, Advisor recommendations, and retail pricing. The
            system board is read-only; planners work from an editable copy.
          </p>
        </div>
        <Sparkles size={18} />
      </div>
      {message && <div className="inline-alert"><Check size={16} />{message}</div>}
      {failure && <div className="inline-alert inline-alert--error">{failure}</div>}
      <dl className="integration-detail-list">
        <div><dt>Board</dt><dd>{status?.board?.name ?? "Not generated yet"}</dd></div>
        <div><dt>Last refreshed</dt><dd>{status?.lastRefreshedAt ? absoluteTime(status.lastRefreshedAt) : "Never"}</dd></div>
        <div><dt>Next scheduled refresh</dt><dd>{status?.nextRefreshAt ? absoluteTime(status.nextRefreshAt) : "Pending"}</dd></div>
      </dl>
      <div className="form-actions">
        <button className="button" onClick={refresh} disabled={busy}>
          <RefreshCw className={busy ? "spin" : ""} size={16} />
          {busy ? "Building proposal…" : "Refresh proposal now"}
        </button>
      </div>
    </Card>
  );
}

/** Retention windows are environment-driven and were previously invisible.
 *  Read-only: changing them is a deployment concern, not a runtime toggle. */
function RetentionPanel() {
  const [policies, setPolicies] = useState<RetentionPolicy[] | null>(null);

  useEffect(() => {
    api.retentionPolicies().then((value) => setPolicies(value.policies)).catch(() => setPolicies([]));
  }, []);

  return (
    <Card className="integration-form">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Data governance</span>
          <h2>Retention</h2>
          <p>How long each class of retained data is kept. Set at deployment time; shown here for audit and review.</p>
        </div>
        <ShieldCheck size={18} />
      </div>
      {!policies && <p className="muted">Loading…</p>}
      {policies && (
        <div className="retention-list">
          {policies.map((policy) => (
            <div key={policy.name}>
              <strong>{policy.name}</strong>
              <span className="retention-days">{policy.days === null ? "Tiered" : `${policy.days} days`}</span>
              <small>{policy.note} · <code>{policy.setting}</code></small>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/** Who last changed each governed configuration surface. */
function AuditPanel() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);

  useEffect(() => {
    api.configurationAudit().then((value) => setEntries(value.entries)).catch(() => setEntries([]));
  }, []);

  return (
    <Card className="integration-form">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Change history</span>
          <h2>Configuration audit</h2>
          <p>Most recent change to each governed configuration surface.</p>
        </div>
        <History size={18} />
      </div>
      {!entries && <p className="muted">Loading…</p>}
      {entries && !entries.length && (
        <p className="muted">No configuration changes recorded yet.</p>
      )}
      {entries && entries.length > 0 && (
        <div className="audit-list">
          {entries.map((entry, index) => (
            <div key={`${entry.surface}-${entry.scope}-${index}`}>
              <span>
                <strong>{entry.surface}</strong>
                {entry.scope ? ` · ${entry.scope}` : ""}
              </span>
              <span className="muted">
                {entry.updatedBy} · {entry.updatedAt ? absoluteTime(entry.updatedAt) : "—"}
              </span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

const emptyIntegration: AzureIntegration = {
  name: "Azure",
  tenantId: "",
  enabled: true,
  authMode: "local_powershell",
  subscriptions: [],
  lastSyncAt: null,
  lastSyncStatus: "never",
  lastSyncMessage: "Not synchronized yet.",
  updatedAt: null,
};

export function IntegrationsPage({ onChanged }: { onChanged: () => void }) {
  const [integration, setIntegration] = useState<AzureIntegration | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [telemetry, setTelemetry] = useState<TelemetryStatus | null>(null);
  const [toolkit, setToolkit] = useState<FinOpsToolkitStatus | null>(null);
  const [costHistory, setCostHistory] = useState<CostHistoryStatus | null>(null);
  const [costCoverage, setCostCoverage] = useState<CostCoverage | null>(null);
  const [telemetryCoverage, setTelemetryCoverage] = useState<TelemetryCoverage | null>(null);
  const [slo, setSlo] = useState<SloReport | null>(null);
  const [costDatasets, setCostDatasets] = useState<CostDatasetStatus[]>([]);
  const [operations, setOperations] = useState<OperationalHealth | null>(null);
  const [allocationTags, setAllocationTags] = useState("");
  const [allocationShared, setAllocationShared] = useState("");
  const [allocationBusy, setAllocationBusy] = useState(false);
  const [allocationSavedAt, setAllocationSavedAt] = useState(false);
  const [unitTag, setUnitTag] = useState("");
  const [unitLabel, setUnitLabel] = useState("");
  const [budgetRows, setBudgetRows] = useState<{ scopeType: string; scopeId: string; monthlyAmount: string; currency: string }[]>([]);
  const [budgetBusy, setBudgetBusy] = useState(false);
  const [budgetSaved, setBudgetSaved] = useState(false);
  const [groupRows, setGroupRows] = useState<{ id: string; name: string; annualAmount: string; subscriptionIds: string[] }[]>([]);
  const [groupBusy, setGroupBusy] = useState(false);
  const [groupSaved, setGroupSaved] = useState(false);
  const [aiConfig, setAiConfig] = useState<AiIntelligenceConfig | null>(null);
  const [aiProvider, setAiProvider] = useState<"deepseek" | "openrouter" | "foundry">("deepseek");
  const [aiFastModel, setAiFastModel] = useState("");
  const [aiDeepModel, setAiDeepModel] = useState("");
  const [aiConfigBusy, setAiConfigBusy] = useState(false);
  const [aiConfigSaved, setAiConfigSaved] = useState(false);
  const [tab, setTab] = useState<AdminTab>("connectors");

  function saveAiConfig() {
    setAiConfigBusy(true);
    setAiConfigSaved(false);
    api.saveAiIntelligenceConfig({
      provider: aiProvider,
      fastModel: aiFastModel.trim(),
      deepModel: aiDeepModel.trim(),
    })
      .then((config) => {
        setAiConfig(config);
        setAiProvider(config.provider);
        setAiFastModel(config.fastModel);
        setAiDeepModel(config.deepModel);
        setAiConfigSaved(true);
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setAiConfigBusy(false));
  }

  function saveAllocation() {
    setAllocationBusy(true);
    setAllocationSavedAt(false);
    api.saveAllocationConfig({
      costCenterTags: allocationTags.split(",").map((item) => item.trim()).filter(Boolean),
      sharedValues: allocationShared.split(",").map((item) => item.trim()).filter(Boolean),
      unitTag: unitTag.trim(),
      unitLabel: unitLabel.trim(),
    })
      .then((config) => {
        setAllocationTags(config.costCenterTags.join(", "));
        setAllocationShared(config.sharedValues.join(", "));
        setUnitTag(config.unitTag);
        setUnitLabel(config.unitLabel);
        setAllocationSavedAt(true);
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setAllocationBusy(false));
  }

  function saveGroups() {
    setGroupBusy(true);
    setGroupSaved(false);
    api.saveBudgetGroups(
      groupRows
        .filter((group) => group.name.trim() && Number(group.annualAmount) > 0)
        .map((group) => ({
          id: group.id || undefined,
          name: group.name.trim(),
          annualAmount: Number(group.annualAmount),
          currency: "USD",
          subscriptionIds: group.subscriptionIds,
        })),
    )
      .then((value) => {
        setGroupRows(value.groups.map((group) => ({
          id: group.id,
          name: group.name,
          annualAmount: String(group.annualAmount),
          subscriptionIds: group.subscriptionIds,
        })));
        setGroupSaved(true);
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setGroupBusy(false));
  }

  function saveBudgets() {
    setBudgetBusy(true);
    setBudgetSaved(false);
    api.saveBudgetTargets(
      budgetRows
        .filter((row) => Number(row.monthlyAmount) > 0)
        .map((row) => ({
          scopeType: row.scopeType,
          scopeId: row.scopeType === "estate" ? "" : row.scopeId.trim(),
          monthlyAmount: Number(row.monthlyAmount),
          currency: row.currency.trim() || "USD",
        })),
    )
      .then((value) => {
        setBudgetRows(value.targets.map((target) => ({
          scopeType: target.scopeType,
          scopeId: target.scopeId,
          monthlyAmount: String(target.monthlyAmount),
          currency: target.currency,
        })));
        setBudgetSaved(true);
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setBudgetBusy(false));
  }

  useEffect(() => {
    api.azureIntegration().then(setIntegration).catch((reason) => setError(reason.message));
    api.allocationConfig()
      .then((config) => {
        setAllocationTags(config.costCenterTags.join(", "));
        setAllocationShared(config.sharedValues.join(", "));
        setUnitTag(config.unitTag);
        setUnitLabel(config.unitLabel);
      })
      .catch(() => undefined);
    api.budgetTargets()
      .then((value) => setBudgetRows(value.targets.map((target) => ({
        scopeType: target.scopeType,
        scopeId: target.scopeId,
        monthlyAmount: String(target.monthlyAmount),
        currency: target.currency,
      }))))
      .catch(() => undefined);
    api.budgetGroups()
      .then((value) => setGroupRows(value.groups.map((group) => ({
        id: group.id,
        name: group.name,
        annualAmount: String(group.annualAmount),
        subscriptionIds: group.subscriptionIds,
      }))))
      .catch(() => undefined);
    api.telemetryStatus().then(setTelemetry).catch(() => undefined);
    api.finopsToolkitStatus().then(setToolkit).catch(() => undefined);
    api.costHistoryStatus().then(setCostHistory).catch(() => undefined);
    api.costCoverage().then(setCostCoverage).catch(() => undefined);
    api.telemetryCoverage().then(setTelemetryCoverage).catch(() => undefined);
    // Admin-only; readers simply do not see the SLO strip.
    api.sloReport().then(setSlo).catch(() => undefined);
    api.costReconciliation()
      .then((value) => setCostDatasets(value.datasets))
      .catch(() => undefined);
    api.operationalHealth().then(setOperations).catch(() => undefined);
    api.aiIntelligenceConfig()
      .then((config) => {
        setAiConfig(config);
        setAiProvider(config.provider);
        setAiFastModel(config.fastModel);
        setAiDeepModel(config.deepModel);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!["queued", "running"].includes(integration?.lastSyncStatus ?? "")) return;
    let cancelled = false;
    let timer: number | undefined;

    async function poll() {
      try {
        const current = await api.azureIntegration();
        if (cancelled) return;
        setIntegration(current);
        if (current.lastSyncStatus === "failed") {
          setNotice("");
          setError(current.lastSyncMessage);
          onChanged();
        } else if (current.lastSyncStatus === "succeeded") {
          setError("");
          setNotice(current.lastSyncMessage);
          onChanged();
        } else {
          timer = window.setTimeout(poll, 2000);
        }
      } catch (reason) {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : "Unable to check synchronization status.");
        timer = window.setTimeout(poll, 5000);
      }
    }

    timer = window.setTimeout(poll, 1500);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [integration?.lastSyncStatus, onChanged]);

  function updateSubscription(index: number, key: "label" | "subscriptionId", value: string) {
    if (!integration) return;
    setIntegration({
      ...integration,
      subscriptions: integration.subscriptions.map((item, itemIndex) =>
        itemIndex === index ? { ...item, [key]: value } : item,
      ),
    });
  }

  async function save() {
    if (!integration) return;
    setBusy(true);
    setError("");
    try {
      const saved = await api.saveAzureIntegration(integration);
      setIntegration(saved);
      setNotice("Integration settings saved.");
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save.");
    } finally {
      setBusy(false);
    }
  }

  async function sync() {
    setBusy(true);
    setError("");
    try {
      await api.syncAzure();
      setIntegration((value) => value ? { ...value, lastSyncStatus: "queued", lastSyncMessage: "Synchronization is queued for the durable worker." } : value);
      setNotice("Azure metadata synchronization queued. Cost collection runs independently on its daily schedule.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to synchronize.");
    } finally {
      setBusy(false);
    }
  }

  async function refreshOperationalStatus() {
    setBusy(true);
    setError("");
    try {
      const [freshOperations, freshCostHistory, freshCostReconciliation] = await Promise.all([
        api.operationalHealth(),
        api.costHistoryStatus(),
        api.costReconciliation(),
      ]);
      setOperations(freshOperations);
      setCostHistory(freshCostHistory);
      setCostDatasets(freshCostReconciliation.datasets);
      setNotice("Operational status refreshed.");
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to refresh operational status.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !integration) return <ErrorPanel message={error} />;
  if (!integration) return <Loading />;
  const azureFreshness = integration.sourceFreshness?.find(
    (source) => source.source === "AzureResourceGraph",
  );
  const logicMonitorFreshness = integration.sourceFreshness?.find(
    (source) => source.source === "logicmonitor",
  );
  const intelligenceFreshness = integration.sourceFreshness?.find(
    (source) => source.source === "FluxIntelligence",
  );
  const dailyActualHistory = costDatasets.find(
    (dataset) => dataset.source === "DailyActualCost",
  );
  const dailyAmortizedHistory = costDatasets.find(
    (dataset) => dataset.source === "DailyAmortizedCost",
  );
  const throttledScopes = costHistory?.scopes.filter((scope) => scope.retryAfterSeconds || scope.nextRetryAt) ?? [];
  const incompleteCostScopes = costDatasets.flatMap((dataset) =>
    dataset.scopes
      .filter((scope) => !scope.currentPeriod || scope.status === "failed")
      .map((scope) => ({
        dataset,
        scope,
        message: scope.message.replace(
          /^Cost Management returned HTTP \d+:\s*/i,
          "",
        ),
      })),
  );
  const costScopeRows = integration.subscriptions.map((subscription) => ({
    id: subscription.subscriptionId.toLowerCase(),
    name: subscription.label || subscription.subscriptionId,
  }));
  const costMatrixColumns = costDatasets.map((dataset) => ({
    dataset,
    scopes: new Map(dataset.scopes.map((scope) => [scope.subscriptionId.toLowerCase(), scope])),
  }));
  const systemAlerts = [
    operations?.worker.status === "stalled"
      ? {
          tone: "critical",
          title: "Worker stalled",
          detail: operations.worker.activeAgeMinutes
            ? `The sync worker has been active for ${operations.worker.activeAgeMinutes.toLocaleString()} minutes.`
            : "The sync worker is not making progress.",
          action: "Review worker",
        }
      : null,
    throttledScopes.length > 0
      ? {
          tone: "warning",
          title: "Cost scopes are throttled",
          detail: `${throttledScopes.length} cost scope${throttledScopes.length === 1 ? "" : "s"} have retry guidance recorded. Retried scopes are prioritized first.`,
          action: "Inspect retries",
        }
      : null,
    incompleteCostScopes.length > 0
      ? {
          tone: "warning",
          title: "Cost history is incomplete",
          detail: `${incompleteCostScopes.length} scope${incompleteCostScopes.length === 1 ? "" : "s"} are still partial or failed and will be retried before new windows.`,
          action: "Inspect cost history",
        }
      : null,
    costCoverage?.coveragePercent != null && costCoverage.coveragePercent < 98
      ? {
          tone: costCoverage.coveragePercent < 90 ? "critical" : "warning",
          title: "Cost data has ingestion gaps",
          detail: `${costCoverage.coveragePercent}% of expected subscription-days are ingested (${costCoverage.completeScopes} of ${costCoverage.scopeCount} scopes complete). Reports understate spend until the backfill closes the gaps.`,
          action: "Review coverage",
        }
      : null,
    operations && operations.summary.staleSources + operations.summary.degradedSources > 0
      ? {
          tone: "warning",
          title: "Source freshness is lagging",
          detail: `${operations.summary.staleSources + operations.summary.degradedSources} source${operations.summary.staleSources + operations.summary.degradedSources === 1 ? "" : "s"} are stale or degraded.`,
          action: "Review freshness",
        }
      : null,
  ].filter((item): item is {
    tone: "warning" | "critical";
    title: string;
    detail: string;
    action: string;
  } => item !== null);
  // Surface degraded collection on the tab itself, so a problem is visible
  // without first opening System health.
  const healthBadge = operations
    ? operations.summary.staleSources + operations.summary.degradedSources
    : 0;
  const syncButtonLabel = busy
    ? "Working…"
    : ["queued", "running"].includes(integration.lastSyncStatus)
      ? "Syncing Azure metadata"
      : "Sync Azure metadata";

  return (
    <>
      <PageHeader
        eyebrow="Administration"
        title="Administration"
        description="Connectors, platform configuration, system health, and data-quality reconciliation."
        action={
          <button className="button" onClick={sync} disabled={busy || ["queued", "running"].includes(integration.lastSyncStatus)}>
            <RefreshCw className={["queued", "running"].includes(integration.lastSyncStatus) ? "spin" : ""} size={16} />
            {syncButtonLabel}
          </button>
        }
        />
      {systemAlerts.length > 0 && (
        <div className="system-alert-stack">
          {systemAlerts.map((alert) => (
            <div className={`system-alert system-alert--${alert.tone}`} key={alert.title}>
              <div className="system-alert__content">
                <AlertTriangle size={14} />
                <div>
                  <strong>{alert.title}</strong>{" "}
                  <span>{alert.detail}</span>
                </div>
              </div>
              <button className="button button--ghost system-alert__action" onClick={refreshOperationalStatus} disabled={busy}>
                {busy ? "Refreshing…" : alert.action}
              </button>
            </div>
          ))}
        </div>
      )}
      {error && <div className="inline-alert inline-alert--error">{error}</div>}
      {notice && <div className="inline-alert"><Check size={16} />{notice}</div>}
      <Tabs
        label="Administration sections"
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "connectors", label: "Connectors" },
          { id: "configuration", label: "Configuration" },
          { id: "health", label: "System health", badge: healthBadge },
          { id: "quality", label: "Data quality" },
        ]}
      />
      {tab === "connectors" && (
        <div className="admin-panel" role="tabpanel" id="tabpanel-connectors" aria-labelledby="tab-connectors">
      <div className="integration-catalog">
        <Card className="integration-service-card">
          <div className="integration-logo"><Cloud size={24} /></div>
          <div>
            <h2>Microsoft Azure</h2>
            <p>ARG inventory, Advisor, Policy, cost, and Azure Monitor</p>
            <strong>{azureFreshness?.rowCount.toLocaleString() || 0} current resources</strong>
            <small>{azureFreshness?.schedule || "Daily inventory"} · {relativeTime(azureFreshness?.observedAt ?? null)}</small>
          </div>
          <span className={`connection-state connection-state--${azureFreshness?.health === "healthy" ? "succeeded" : "failed"}`}><i />{titleCase(azureFreshness?.health || "not connected")}</span>
        </Card>
        <Card className="integration-service-card">
          <div className="integration-logo integration-logo--telemetry"><Activity size={24} /></div>
          <div>
            <h2>LogicMonitor</h2>
            <p>Independent checkpointed performance telemetry</p>
            <strong>{telemetry?.logicMonitorMetricCovered.toLocaleString() || 0} resources with metrics</strong>
            <small>{logicMonitorFreshness?.schedule || "Every 6 hours"} · {relativeTime(logicMonitorFreshness?.observedAt ?? null)}</small>
          </div>
          <span className={`connection-state connection-state--${logicMonitorFreshness?.health === "healthy" ? "succeeded" : "failed"}`}><i />{titleCase(logicMonitorFreshness?.health || "not connected")}</span>
        </Card>
        <Card className="integration-service-card">
          <div className="integration-logo integration-logo--flux"><Sparkles size={24} /></div>
          <div>
            <h2>Flux Signals</h2>
            <p>Versioned read-only optimization and lifecycle rules</p>
            <strong>{intelligenceFreshness?.rowCount.toLocaleString() || 0} unique rule findings</strong>
            <small>{intelligenceFreshness?.schedule || "Daily"} · {relativeTime(intelligenceFreshness?.observedAt ?? null)}</small>
          </div>
          <span className={`connection-state connection-state--${intelligenceFreshness?.health === "healthy" ? "succeeded" : "failed"}`}><i />{titleCase(intelligenceFreshness?.health || "not connected")}</span>
        </Card>
      </div>
        <Card className="integration-summary">
          <div className="integration-logo"><Cloud size={28} /></div>
          <h2>Microsoft Azure</h2>
          <p>Azure Resource Graph inventory</p>
          <span className={`connection-state connection-state--${integration.lastSyncStatus}`}>
            <i />
            {titleCase(integration.lastSyncStatus)}
          </span>
          <dl>
            <div><dt>Last synchronized</dt><dd>{relativeTime(integration.lastSyncAt)}</dd></div>
            <div><dt>Subscriptions</dt><dd>{integration.subscriptions.length}</dd></div>
            <div><dt>Provider</dt><dd>{integration.authMode === "local_powershell" ? "Local Az context" : "Managed identity"}</dd></div>
          </dl>
          <p className="sync-message">{integration.lastSyncMessage}</p>
          {integration.latestSync && ["queued", "running"].includes(integration.latestSync.status) && (
            <div className="sync-stage">
              <RefreshCw className="spin" size={14} />
              <span><strong>{titleCase(integration.latestSync.stage)}</strong>{integration.latestSync.stageMessage}</span>
            </div>
          )}
          {!!integration.latestSync?.sourceRuns?.length && (
            <div className="source-progress">
              {integration.latestSync.sourceRuns.map((run) => (
                <div key={`${run.source}:${run.scopeId}`}>
                  <i className={`status-dot status-dot--${run.status}`} />
                  <span>{titleCase(run.source)}</span>
                  <small>{run.scopeId === "configured-subscriptions" ? run.rowCount.toLocaleString() : run.scopeId.slice(0, 8)}</small>
                </div>
              ))}
            </div>
          )}
          {!!integration.sourceFreshness?.length && (
            <div className="source-freshness">
              <h3>Source reliability</h3>
              {integration.sourceFreshness.map((source) => (
                <article className={source.health !== "healthy" ? "source-health source-health--warning" : "source-health"} key={source.source}>
                  <header>
                    <span>{source.label}</span>
                    {source.health !== "healthy" ? <AlertTriangle size={12} /> : <Check size={12} />}
                  </header>
                  <strong>{source.rowCount.toLocaleString()} rows · {relativeTime(source.observedAt)}</strong>
                  <small>{source.schedule}{source.scopeTotal ? ` · ${source.scopeSucceeded ?? 0}/${source.scopeTotal} scopes` : ""}</small>
                  {source.retainedLastGood && <small>Last attempt failed; serving retained data.</small>}
                </article>
              ))}
            </div>
          )}
        </Card>
        <Card className="integration-form">
          <div className="section-heading">
            <div><h2>Connection settings</h2><p>Use local PowerShell for development and managed identity in Azure.</p></div>
            <label className="switch"><input type="checkbox" checked={integration.enabled} onChange={(event) => setIntegration({ ...integration, enabled: event.target.checked })} /><span /></label>
          </div>
          <div className="form-grid">
            <label><span>Display name</span><input value={integration.name} onChange={(event) => setIntegration({ ...integration, name: event.target.value })} /></label>
            <label><span>Tenant ID</span><input value={integration.tenantId} onChange={(event) => setIntegration({ ...integration, tenantId: event.target.value })} placeholder="Optional validation" /></label>
            <label className="full"><span>Azure service authentication</span><select value={integration.authMode} onChange={(event) => setIntegration({ ...integration, authMode: event.target.value as AzureIntegration["authMode"] })}><option value="local_powershell">Local Azure PowerShell context</option><option value="managed_identity">App Service managed identity</option></select></label>
          </div>
          <div className="section-heading section-heading--subscriptions">
            <div><h2>Subscription scope</h2><p>Only these subscriptions will be queried.</p></div>
            <button className="button button--ghost" onClick={() => setIntegration({ ...integration, subscriptions: [...integration.subscriptions, { label: "", subscriptionId: "" }] })}><Plus size={16} />Add subscription</button>
          </div>
          <div className="subscription-list">
            {integration.subscriptions.length ? integration.subscriptions.map((item, index) => (
              <div className="subscription-row" key={index}>
                <input value={item.label} onChange={(event) => updateSubscription(index, "label", event.target.value)} placeholder="Friendly name" />
                <input value={item.subscriptionId} onChange={(event) => updateSubscription(index, "subscriptionId", event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" />
                <button className="icon-button" onClick={() => setIntegration({ ...integration, subscriptions: integration.subscriptions.filter((_, itemIndex) => itemIndex !== index) })} aria-label="Remove subscription"><Trash2 size={16} /></button>
              </div>
            )) : <p className="muted">No subscriptions configured.</p>}
          </div>
          <div className="form-actions">
            <button className="button" onClick={save} disabled={busy}><Save size={16} />Save settings</button>
          </div>
        </Card>
        </div>
      )}
      {tab === "configuration" && (
        <div className="admin-panel" role="tabpanel" id="tabpanel-configuration" aria-labelledby="tab-configuration">
        <VirtualTagsAdmin />
        <RightsizingProposalControl />
        <Card className="integration-form">
          <div className="section-heading">
            <div>
              <h2>Ask Flux AI configuration</h2>
              <p>Provider and model used for the Fast and Deep analysis profiles. Changes take effect within 30 seconds, no app restart needed.</p>
            </div>
          </div>
          <div className="form-grid">
            <label>
              <span>Provider</span>
              <select value={aiProvider} onChange={(event) => setAiProvider(event.target.value as "deepseek" | "openrouter" | "foundry")}>
                <option value="deepseek">DeepSeek</option>
                <option value="openrouter">OpenRouter</option>
                <option value="foundry">Azure AI Foundry</option>
              </select>
            </label>
            <label>
              <span>Fast model</span>
              <input
                value={aiFastModel}
                onChange={(event) => setAiFastModel(event.target.value)}
                placeholder={aiProvider === "foundry" ? "Foundry deployment name" : "e.g. deepseek-v4-flash"}
              />
            </label>
            <label className="full">
              <span>Deep analysis model</span>
              <input
                value={aiDeepModel}
                onChange={(event) => setAiDeepModel(event.target.value)}
                placeholder={aiProvider === "foundry" ? "Foundry deployment name" : "e.g. deepseek-v4-pro"}
              />
            </label>
          </div>
          <div className="section-heading">
            <div>
              <h2>API keys</h2>
              <p>
                Keys are never displayed or editable here. To rotate a key, set{" "}
                <code>FLUX_DEEPSEEK_API_KEY</code>, <code>FLUX_OPENROUTER_API_KEY</code>,
                {" "}or <code>FLUX_FOUNDRY_API_KEY</code>{" "}
                in the App Service configuration (or its Key Vault reference) and restart the app.
              </p>
            </div>
          </div>
          <div className="ai-config-key-status">
            <span>
              DeepSeek: {aiConfig?.keys.deepseek.configured
                ? <code>{aiConfig.keys.deepseek.masked}</code>
                : <span className="muted">not configured</span>}
            </span>
            <span>
              OpenRouter: {aiConfig?.keys.openrouter.configured
                ? <code>{aiConfig.keys.openrouter.masked}</code>
                : <span className="muted">not configured</span>}
            </span>
            <span>
              Azure AI Foundry: {aiConfig?.keys.foundry.configured
                ? <code>{aiConfig.keys.foundry.masked}</code>
                : <span className="muted">not configured</span>}
            </span>
          </div>
          <div className="form-actions">
            <button className="button" onClick={saveAiConfig} disabled={aiConfigBusy}>
              <Save size={16} />Save AI configuration
            </button>
            {aiConfigSaved && <span className="muted">Saved.</span>}
          </div>
        </Card>
        <Card className="integration-form">
          <div className="section-heading">
            <div>
              <h2>Cost allocation</h2>
              <p>Tag keys that identify a resource's cost center, tried in order. Values listed as shared are prorated across the other centers.</p>
            </div>
          </div>
          <div className="form-grid">
            <label className="full">
              <span>Cost-center tag keys (comma-separated)</span>
              <input
                value={allocationTags}
                onChange={(event) => setAllocationTags(event.target.value)}
                placeholder="cost-center, department, team"
              />
            </label>
            <label className="full">
              <span>Shared cost-center values (comma-separated, optional)</span>
              <input
                value={allocationShared}
                onChange={(event) => setAllocationShared(event.target.value)}
                placeholder="shared, platform"
              />
            </label>
            <label>
              <span>Unit-economics tag key (optional)</span>
              <input
                value={unitTag}
                onChange={(event) => setUnitTag(event.target.value)}
                placeholder="brand"
              />
            </label>
            <label>
              <span>Dimension label</span>
              <input
                value={unitLabel}
                onChange={(event) => setUnitLabel(event.target.value)}
                placeholder="Brand"
              />
            </label>
          </div>
          <div className="form-actions">
            <button className="button" onClick={saveAllocation} disabled={allocationBusy}>
              <Save size={16} />Save allocation
            </button>
            {allocationSavedAt && <span className="muted">Saved.</span>}
          </div>
        </Card>
        <Card className="integration-form">
          <div className="section-heading">
            <div>
              <h2>Budget targets</h2>
              <p>Monthly targets for the estate or individual subscriptions. Reports compare finalized actuals and a labeled run-rate projection against these.</p>
            </div>
            <button
              className="button button--ghost"
              onClick={() => setBudgetRows([...budgetRows, { scopeType: "subscription", scopeId: "", monthlyAmount: "", currency: "USD" }])}
            >
              <Plus size={16} />Add target
            </button>
          </div>
          <div className="subscription-list">
            {budgetRows.length ? budgetRows.map((row, index) => (
              <div className="subscription-row subscription-row--budget" key={index}>
                <select
                  value={row.scopeType}
                  onChange={(event) => setBudgetRows(budgetRows.map((item, itemIndex) => itemIndex === index ? { ...item, scopeType: event.target.value } : item))}
                >
                  <option value="estate">Entire estate</option>
                  <option value="subscription">Subscription</option>
                </select>
                <input
                  value={row.scopeId}
                  disabled={row.scopeType === "estate"}
                  onChange={(event) => setBudgetRows(budgetRows.map((item, itemIndex) => itemIndex === index ? { ...item, scopeId: event.target.value } : item))}
                  placeholder="Subscription ID"
                />
                <input
                  value={row.monthlyAmount}
                  onChange={(event) => setBudgetRows(budgetRows.map((item, itemIndex) => itemIndex === index ? { ...item, monthlyAmount: event.target.value } : item))}
                  placeholder="Monthly amount"
                  inputMode="decimal"
                />
                <button className="icon-button" onClick={() => setBudgetRows(budgetRows.filter((_, itemIndex) => itemIndex !== index))} aria-label="Remove budget target">
                  <Trash2 size={16} />
                </button>
              </div>
            )) : <p className="muted">No budget targets configured.</p>}
          </div>
          <div className="form-actions">
            <button className="button" onClick={saveBudgets} disabled={budgetBusy}>
              <Save size={16} />Save budgets
            </button>
            {budgetSaved && <span className="muted">Saved.</span>}
          </div>
        </Card>
        <Card className="integration-form">
          <div className="section-heading">
            <div>
              <h2>Budget groups</h2>
              <p>Named subscription groups, each with its own annual envelope — for example US and International. The Fiscal-year outlook tracks every group against its envelope.</p>
            </div>
            <button
              className="button button--ghost"
              onClick={() => setGroupRows([...groupRows, { id: "", name: "", annualAmount: "", subscriptionIds: [] }])}
            >
              <Plus size={16} />Add group
            </button>
          </div>
          <div className="subscription-list">
            {groupRows.length ? groupRows.map((group, index) => (
              <div className="budget-group" key={group.id || `new-${index}`}>
                <div className="budget-group__head">
                  <input
                    value={group.name}
                    onChange={(event) => setGroupRows(groupRows.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item))}
                    placeholder="Group name (e.g. US)"
                  />
                  <input
                    value={group.annualAmount}
                    onChange={(event) => setGroupRows(groupRows.map((item, itemIndex) => itemIndex === index ? { ...item, annualAmount: event.target.value } : item))}
                    placeholder="Annual budget (e.g. 785000)"
                    inputMode="decimal"
                  />
                  <button className="icon-button" onClick={() => setGroupRows(groupRows.filter((_, itemIndex) => itemIndex !== index))} aria-label={`Remove group ${group.name || index + 1}`}>
                    <Trash2 size={16} />
                  </button>
                </div>
                <div className="budget-group__members">
                  {integration.subscriptions.filter((item) => item.subscriptionId).map((item) => {
                    const subscriptionId = item.subscriptionId.toLowerCase();
                    const active = group.subscriptionIds.includes(subscriptionId);
                    return (
                      <button
                        key={subscriptionId}
                        className={`budget-chip ${active ? "budget-chip--active" : ""}`}
                        aria-pressed={active}
                        onClick={() => setGroupRows(groupRows.map((row, rowIndex) => rowIndex === index
                          ? {
                            ...row,
                            subscriptionIds: active
                              ? row.subscriptionIds.filter((value) => value !== subscriptionId)
                              : [...row.subscriptionIds, subscriptionId],
                          }
                          : row))}
                      >
                        {item.label || item.subscriptionId}
                      </button>
                    );
                  })}
                  {!group.subscriptionIds.length && <span className="muted">No subscriptions selected — this group tracks nothing yet.</span>}
                </div>
              </div>
            )) : <p className="muted">No budget groups configured.</p>}
          </div>
          <div className="form-actions">
            <button className="button" onClick={saveGroups} disabled={groupBusy}>
              <Save size={16} />Save groups
            </button>
            {groupSaved && <span className="muted">Saved.</span>}
          </div>
        </Card>
      <RetentionPanel />
      <AuditPanel />
        </div>
      )}
      {tab === "health" && (
        <div className="admin-panel" role="tabpanel" id="tabpanel-health" aria-labelledby="tab-health">
      {operations && (
        <Card className="operations-health-card">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Operational health</span>
              <h2>Collection and decision readiness</h2>
              <p>Current source freshness, retry state, worker progress, cost completeness, and recommendation regression checks.</p>
            </div>
            <span className={`connection-state connection-state--${operations.status === "healthy" ? "succeeded" : "failed"}`}>
              <i />{titleCase(operations.status)}
            </span>
          </div>
          <div className="operations-health-summary">
            <div><small>Healthy sources</small><strong>{operations.summary.healthySources}</strong></div>
            <div><small>Stale / degraded</small><strong>{operations.summary.staleSources + operations.summary.degradedSources}</strong></div>
            <div><small>Incomplete cost datasets</small><strong>{operations.summary.incompleteCostDatasets}</strong></div>
            <div><small>Worker</small><strong>{titleCase(operations.worker.status)}</strong></div>
            <div><small>Active requests</small><strong>{operations.summary.queuedRuns + operations.summary.runningRuns}</strong></div>
          </div>
          <div className="operations-source-grid">
            {operations.sources.map((source) => (
              <article key={source.source} className={`operations-source operations-source--${source.health}`}>
                <header>
                  <strong>{source.label}</strong>
                  <span>{titleCase(source.health)}</span>
                </header>
                <p>{source.rowCount.toLocaleString()} rows · {relativeTime(source.observedAt)}</p>
                <small>
                  {source.nextExpectedAt
                    ? `Next expected ${relativeTime(source.nextExpectedAt)}`
                    : source.schedule}
                  {source.scopeTotal ? ` · ${source.scopeSucceeded ?? 0}/${source.scopeTotal} scopes` : ""}
                </small>
                {source.lastAttemptMessage && source.health !== "healthy" && (
                  <small title={source.lastAttemptMessage}>{source.lastAttemptMessage}</small>
                )}
              </article>
            ))}
          </div>
        </Card>
      )}
      {(slo || costCoverage || telemetryCoverage) && (
        <Card className="coverage-card">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Coverage and objectives</span>
              <h2>Is the data actually all there?</h2>
              <p>Run status says the last attempt worked; this says whether the days and machines behind every report are present. Alert thresholds and first responses live in the alert catalog runbook.</p>
            </div>
            {slo && (
              <span className={`connection-state connection-state--${slo.worstState === "ok" ? "succeeded" : slo.worstState === "breach" ? "failed" : "pending"}`}>
                <i />SLO {titleCase(slo.worstState)}
              </span>
            )}
          </div>
          {slo && (
            <div className="slo-strip" role="list" aria-label="Service-level objectives">
              {slo.objectives.map((objective) => (
                <article key={objective.key} className={`slo-item slo-item--${objective.state}`} role="listitem" title={objective.description}>
                  <small>{objective.label}</small>
                  <strong>
                    {objective.value === null
                      ? "No data"
                      : `${objective.value.toLocaleString(undefined, { maximumFractionDigits: 1 })}${objective.unit === "%" ? "%" : ` ${objective.unit}`}`}
                  </strong>
                  <span>{titleCase(objective.state)}{objective.tracked?.since ? ` · since ${relativeTime(objective.tracked.since)}` : ""}</span>
                </article>
              ))}
            </div>
          )}
          <div className="coverage-grid">
            {costCoverage && (
              <div>
                <h3>Cost ingestion</h3>
                <p className="coverage-headline">
                  <strong>{costCoverage.coveragePercent ?? "—"}%</strong>
                  <span>of {costCoverage.expectedScopeDays.toLocaleString()} subscription-days ingested · {costCoverage.completeScopes}/{costCoverage.scopeCount} scopes complete</span>
                </p>
                {costCoverage.scopes.filter((scope) => scope.missingDays > 0).slice(0, 4).map((scope) => (
                  <p key={`${scope.subscriptionId}-${scope.costType}`} className="coverage-row">
                    <span>{scope.subscriptionName} · {scope.costType === "ActualCost" ? "Actual" : "Amortized"}</span>
                    <span>{scope.missingDays} day{scope.missingDays === 1 ? "" : "s"} missing{scope.missingRanges[0] ? ` (from ${scope.missingRanges[0].start})` : ""}</span>
                  </p>
                ))}
                {costCoverage.scopeCount === 0 ? (
                  <p className="coverage-row">No cost scopes are configured yet, so there is nothing to measure.</p>
                ) : costCoverage.scopes.every((scope) => scope.missingDays === 0) && (
                  <p className="coverage-row coverage-row--ok">Every configured scope is complete for the window.</p>
                )}
              </div>
            )}
            {telemetryCoverage && (
              <div>
                <h3>VM telemetry</h3>
                <p className="coverage-headline">
                  <strong>{telemetryCoverage.coveredPercent ?? "—"}%</strong>
                  <span>of {telemetryCoverage.totalVms.toLocaleString()} costed VMs have CPU evidence · ${telemetryCoverage.uncoveredMonthlyCost.toLocaleString()} /mo unexplained</span>
                </p>
                {telemetryCoverage.uncovered.slice(0, 4).map((vm) => (
                  <p key={vm.resourceId} className="coverage-row">
                    <span>{vm.name}</span>
                    <span>${vm.estimatedMonthlyCost.toLocaleString()} /mo · no telemetry</span>
                  </p>
                ))}
                {telemetryCoverage.uncovered.length === 0 && (
                  <p className="coverage-row coverage-row--ok">Every costed VM has at least one telemetry source.</p>
                )}
              </div>
            )}
          </div>
        </Card>
      )}
      <DatabaseHealthPanel />
      <JobControlPanel />
        </div>
      )}
      {tab === "quality" && (
        <div className="admin-panel" role="tabpanel" id="tabpanel-quality" aria-labelledby="tab-quality">
        <Card className="cost-reconciliation-card">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Cost source reconciliation</span>
              <h2>Independent coverage by dataset</h2>
              <p>Current snapshots, daily history, and commitment evidence have separate collection state. A retained scope serves its last successful result after a failed refresh.</p>
            </div>
          </div>
          <div className="cost-coverage-matrix" role="table" aria-label="Cost coverage by subscription and dataset">
            <div
              className="cost-coverage-matrix__row cost-coverage-matrix__row--header"
              style={{ gridTemplateColumns: `minmax(180px, 1.1fr) repeat(${costDatasets.length}, minmax(140px, 1fr))` }}
              role="row"
            >
              <span role="columnheader">Subscription scope</span>
              {costDatasets.map((dataset) => (
                <span role="columnheader" key={dataset.source}>
                  <strong>{dataset.label}</strong>
                  <small>{dataset.grain}</small>
                </span>
              ))}
            </div>
            {costScopeRows.map((scope) => (
              <div
                className="cost-coverage-matrix__row"
                style={{ gridTemplateColumns: `minmax(180px, 1.1fr) repeat(${costDatasets.length}, minmax(140px, 1fr))` }}
                role="row"
                key={scope.id}
              >
                <span role="rowheader"><strong>{scope.name}</strong><small>{scope.id}</small></span>
                {costMatrixColumns.map(({ dataset, scopes }) => {
                  const item = scopes.get(scope.id);
                  const state = !item
                    ? "not-collected"
                    : item.status === "failed"
                      ? "failed"
                      : item.currentPeriod
                        ? "complete"
                        : "partial";
                  const detail = !item
                    ? "Not collected"
                    : state === "complete"
                      ? `${item.rowCount.toLocaleString()} rows`
                      : item.statusCode
                        ? `HTTP ${item.statusCode}`
                        : item.status === "failed"
                          ? "Failed"
                          : `${item.rowCount.toLocaleString()} rows · partial`;
                  return (
                    <span className={`cost-coverage-cell cost-coverage-cell--${state}`} role="cell" key={dataset.source} title={item?.message || detail}>
                      <strong>{state === "complete" ? "Complete" : state === "partial" ? "Partial" : state === "failed" ? "Failed" : "Not collected"}</strong>
                      <small>{detail}</small>
                    </span>
                  );
                })}
              </div>
            ))}
          </div>
          {incompleteCostScopes.length > 0 && (
            <div className="cost-scope-failures">
              <div className="cost-scope-failures__heading">
                <span><AlertTriangle size={14} /></span>
                <div>
                  <strong>{incompleteCostScopes.length} scope refresh{incompleteCostScopes.length === 1 ? "" : "es"} need attention</strong>
                  <small>Last-good data remains available where shown. Failed scopes are retried first.</small>
                </div>
              </div>
              <div className="cost-scope-failures__grid">
                {incompleteCostScopes.map(({ dataset, scope, message }) => (
                  <article key={`${dataset.source}:${scope.subscriptionId}`}>
                    <header>
                      <span>
                        <strong>{scope.subscriptionName}</strong>
                        <small>{dataset.label}</small>
                      </span>
                      <b>{scope.statusCode ? `HTTP ${scope.statusCode}` : scope.status === "failed" ? "Failed" : "Pending"}</b>
                    </header>
                    <div className="cost-scope-failures__meta">
                      <span>{scope.lastAttemptAt ? `Attempted ${relativeTime(scope.lastAttemptAt)}` : "Never attempted"}</span>
                      {scope.nextRetryAt && <span>Retry eligible {relativeTime(scope.nextRetryAt)}</span>}
                      {scope.retainedLastGood && <span>Serving last-good data</span>}
                    </div>
                    {message && <p title={message}>{message}</p>}
                  </article>
                ))}
              </div>
            </div>
          )}
        </Card>
        {operations && (
          <Card className="cost-reconciliation-card">
            <div className="section-heading">
              <div>
                <span className="eyebrow">Recommendation regression</span>
                <h2>Advisor count reconciliation</h2>
                <p>{operations.recommendations.method}</p>
              </div>
              <span className={`connection-state connection-state--${operations.recommendations.status === "healthy" ? "succeeded" : "failed"}`}>
                <i />{titleCase(operations.recommendations.status)}
              </span>
            </div>
            <div className="operations-health-summary">
              <div><small>Active Advisor rows</small><strong>{operations.recommendations.storedActive.toLocaleString()}</strong></div>
              <div><small>Semantic actions</small><strong>{operations.recommendations.semanticActions.toLocaleString()}</strong></div>
              <div><small>Actionable now</small><strong>{operations.recommendations.portfolio.actionableNow.toLocaleString()}</strong></div>
              <div><small>Subscription scoped</small><strong>{operations.recommendations.subscriptionScoped.toLocaleString()}</strong></div>
              <div><small>Unresolved resources</small><strong>{operations.recommendations.unresolvedResources.toLocaleString()}</strong></div>
            </div>
            <div className="recommendation-checks">
              {operations.recommendations.checks.map((check) => (
                <article key={check.name}>
                  {check.status === "passed" ? <Check size={15} /> : <AlertTriangle size={15} />}
                  <span><strong>{check.name}</strong><small>{check.message}</small></span>
                </article>
              ))}
            </div>
          </Card>
        )}
        {telemetry && (
          <Card className="cost-reconciliation-card">
            <div className="section-heading">
              <div>
                <span className="eyebrow">Performance coverage</span>
                <h2>VM evidence by subscription</h2>
                <p>Every VM is counted as covered, attempted without data, failed, or not yet attempted. LogicMonitor coverage is shown independently and does not mask Azure Monitor gaps.</p>
              </div>
            </div>
            <div className="cost-reconciliation-table">
              <div className="cost-reconciliation-row cost-reconciliation-row--header">
                <span>Subscription</span><span>Azure Monitor</span><span>LogicMonitor</span><span>Decision state</span><span>Total VMs</span>
              </div>
              {telemetry.bySubscription.map((scope) => (
                <div className="cost-reconciliation-row" key={scope.subscriptionId}>
                  <span><strong>{scope.subscriptionName}</strong><small>{scope.subscriptionId}</small></span>
                  <span>
                    <strong>{scope.azureMonitorCovered}/{scope.virtualMachines} covered</strong>
                    <small>{scope.azureMonitorAttempted} attempted · {scope.azureMonitorNoData} no data · {scope.azureMonitorErrors} errors</small>
                  </span>
                  <span><strong>{scope.logicMonitorCovered} covered</strong><small>{scope.logicMonitorMatched} identities matched</small></span>
                  <span><strong>{scope.candidates} candidates</strong><small>{scope.warmingUp} warming · {scope.insufficient} insufficient</small></span>
                  <span><strong>{scope.virtualMachines.toLocaleString()}</strong><small>virtual machines</small></span>
                </div>
              ))}
            </div>
          </Card>
        )}
        </div>
      )}
    </>
  );
}
