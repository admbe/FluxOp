import {
  ArrowLeft, ArrowRight, Box, Download, RefreshCw, Search,
  SlidersHorizontal, X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  Area, AreaChart, CartesianGrid, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { api } from "../api";
import { currency, shortType, titleCase } from "../format";
import { useChartColors } from "../theme";
import type { Inventory, Opportunity, Resource, TelemetryStatus } from "../types";
import { Card, EmptyState, ErrorPanel, Loading, PageHeader } from "../components/Ui";
import { hashParams, navigateWithParams, SavedViews } from "../viewState";

function withFormat(params: URLSearchParams, format: string): URLSearchParams {
  const next = new URLSearchParams(params);
  next.set("format", format);
  return next;
}

function utilizationTone(value: number): string {
  if (value >= 80) return "danger";
  if (value >= 50) return "warning";
  return "ok";
}

function ResourceModal({ resource, onClose }: { resource: Resource; onClose: () => void }) {
  const [telemetry, setTelemetry] = useState<Awaited<ReturnType<typeof api.resourceTelemetry>> | null>(null);
  const [telemetryStatus, setTelemetryStatus] = useState<TelemetryStatus | null>(null);
  const [telemetryError, setTelemetryError] = useState("");
  const [virtualTags, setVirtualTags] = useState<Record<string, { value: string; source: string; ruleName?: string }>>({});
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const chart = useChartColors();

  useEffect(() => {
    api.effectiveVirtualTags(resource.resourceId)
      .then((result) => setVirtualTags(result.tags))
      .catch(() => setVirtualTags({}));
  }, [resource.resourceId]);

  useEffect(() => {
    api.opportunities(new URLSearchParams({ resourceId: resource.resourceId, includeGovernance: "true", limit: "25" }))
      .then((result) => setOpportunities(result.items))
      .catch(() => setOpportunities([]));
  }, [resource.resourceId]);

  useEffect(() => {
    if (resource.resourceType !== "microsoft.compute/virtualmachines") return;
    api.resourceTelemetry(resource.resourceId)
      .then(setTelemetry)
      .catch((reason) => setTelemetryError(reason.message));
    api.telemetryStatus().then(setTelemetryStatus).catch(() => undefined);
  }, [resource.resourceId, resource.resourceType]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const metricLabel = (value: string) => value
    .replace("Percentage CPU", "CPU")
    .replace(" Total", "")
    .replace(" Operations/Sec", " IOPS");
  const metricValue = (value: number | null, unit: string) => {
    if (value === null) return "—";
    if (unit.toLowerCase().includes("byte")) {
      const suffix = unit.toLowerCase().includes("second") ? " MiB/s" : " MiB";
      return `${(value / 1024 / 1024).toFixed(1)}${suffix}`;
    }
    return `${value.toFixed(1)}${unit === "Percent" ? "%" : ""}`;
  };
  const telemetrySources = [...new Set(telemetry?.metrics.map((metric) => metric.source) || [])];
  const costDaily = telemetry?.costDaily ?? [];
  const costCurrency = costDaily.find((item) => item.currency)?.currency || "USD";
  // Percentage metrics render as utilization bars against their guardrails;
  // everything else keeps the compact tile treatment.
  const percentMetrics = (telemetry?.metrics ?? []).filter(
    (metric) => metric.unit === "Percent" && metric.p95 !== null,
  );
  const otherMetrics = (telemetry?.metrics ?? []).filter(
    (metric) => !(metric.unit === "Percent" && metric.p95 !== null),
  );
  // Time-series sparklines only exist for sources that keep raw samples.
  const percentSeries = (telemetry?.sampleSeries ?? [])
    .filter((series) => series.unit === "Percent" && series.points.length > 3)
    .slice(0, 2);
  const tooltipStyle = {
    background: chart.surface,
    border: `1px solid ${chart.border}`,
    borderRadius: 8,
    color: chart.text,
    fontSize: 11,
  };

  return (
    <div className="resource-modal" role="dialog" aria-modal="true" aria-label={resource.name}>
      <button className="resource-modal__scrim" onClick={onClose} aria-label="Close resource detail" />
      <div className="resource-modal__panel">
        <header className="resource-modal__header">
          <div>
            <span className="eyebrow">{shortType(resource.resourceType)}</span>
            <h2>{resource.name}</h2>
            <p className="resource-id">{resource.resourceId}</p>
          </div>
          <button className="icon-button" onClick={onClose} title="Close"><X size={18} /></button>
        </header>
        <div className="resource-modal__grid">
          <div className="resource-modal__facts">
            <dl className="detail-grid">
              <div><dt>Subscription</dt><dd>{resource.subscriptionName || resource.subscriptionId}</dd></div>
              <div><dt>Resource group</dt><dd>{resource.resourceGroup || "—"}</dd></div>
              <div><dt>Region</dt><dd>{resource.region || "Global"}</dd></div>
              <div><dt>SKU</dt><dd>{resource.sku || "Not reported"}</dd></div>
              <div><dt>State</dt><dd>{resource.provisioningState || "Not reported"}</dd></div>
              <div><dt>Actual cost MTD</dt><dd>{currency(resource.estimatedMonthlyCost)}</dd></div>
              <div><dt>Amortized cost MTD</dt><dd>{currency(resource.amortizedMonthlyCost)}</dd></div>
              <div><dt>Cost source</dt><dd>{resource.costSource || "Not connected"}</dd></div>
              <div><dt>Observed</dt><dd>{new Date(resource.observedAt).toLocaleString()}</dd></div>
            </dl>
            <h3>Tags</h3>
            <div className="tag-list">
              {/* Effective view: native tags plus Flux virtual-tag values
                  (rules, imports, manual overrides), each labeled with its
                  provenance so nobody mistakes inferred metadata for what
                  Azure actually carries. */}
              {Object.keys(virtualTags).length ? Object.entries(virtualTags).map(([key, tag]) => (
                <span key={key} title={tag.source === "rule" ? `Rule: ${tag.ruleName || ""}` : tag.source}>
                  {key}: {tag.value}
                  {tag.source !== "native" && <em className={`tag-provenance tag-provenance--${tag.source}`}>{tag.source}</em>}
                </span>
              )) : Object.entries(resource.tags).length ? Object.entries(resource.tags).map(([key, value]) => (
                <span key={key}>{key}: {value}</span>
              )) : <span className="muted">No tags</span>}
            </div>
            {resource.opportunityKind && (
              <div className="finding-box">
                <strong>{titleCase(resource.opportunityKind)}</strong>
                <p>{resource.opportunityReason}</p>
              </div>
            )}
            <section className="related-opportunities">
              <h3>Related opportunities</h3>
              {opportunities.length ? opportunities.map((opportunity) => (
                <button className="related-opportunity" key={opportunity.id} onClick={() => navigateWithParams("opportunities", { resourceId: resource.resourceId, search: resource.resourceId })}>
                  <span><strong>{opportunity.title}</strong><small>{titleCase(opportunity.source)} · {titleCase(opportunity.actionability)}</small></span>
                  <ArrowRight size={15} />
                </button>
              )) : <p className="muted">No related opportunities found.</p>}
            </section>
            {!!telemetry?.matches.length && (
              <div className="monitor-match">
                <strong>LogicMonitor identity matched</strong>
                <span>
                  {telemetry.matches[0].sourceName} · {titleCase(telemetry.matches[0].method)}
                  {telemetrySources.includes("logicmonitor") ? " · metric history connected" : " · awaiting metric history"}
                </span>
                {telemetry.logicMonitorAttempt && (
                  <span>
                    {telemetry.logicMonitorAttempt.message} Last attempted{" "}
                    {new Date(telemetry.logicMonitorAttempt.observedAt).toLocaleString()}.
                  </span>
                )}
              </div>
            )}
          </div>
          <div className="resource-modal__insights">
            {costDaily.length > 1 && (
              <section className="modal-chart">
                <div className="modal-chart__title">
                  <h3>Daily cost</h3>
                  <p>Last 35 days · governed daily history ({costCurrency})</p>
                </div>
                <ResponsiveContainer width="100%" height={170}>
                  <AreaChart data={costDaily}>
                    <CartesianGrid stroke={chart.border} vertical={false} />
                    <XAxis
                      dataKey="date"
                      stroke={chart.muted}
                      tick={{ fontSize: 10 }}
                      tickFormatter={(value: string) => value.slice(5)}
                      minTickGap={28}
                    />
                    <YAxis stroke={chart.muted} tick={{ fontSize: 10 }} width={46} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Area type="monotone" dataKey="actual" name="Actual" stroke={chart.series[0]} fill={chart.series[0]} fillOpacity={0.15} />
                    <Area type="monotone" dataKey="amortized" name="Amortized" stroke={chart.series[1]} fill={chart.series[1]} fillOpacity={0.1} />
                  </AreaChart>
                </ResponsiveContainer>
              </section>
            )}
            {resource.resourceType === "microsoft.compute/virtualmachines" && (
              <section className="performance-panel">
                <div className="performance-heading">
                  <div><h3>Performance</h3><p>Governed source summaries</p></div>
                  <div className="tag-list">
                    {telemetrySources.map((source) => (
                      <span className="pill" key={source}>{titleCase(source)}</span>
                    ))}
                  </div>
                </div>
                {telemetryError ? <p className="muted">{telemetryError}</p> : !telemetry ? (
                  <p className="muted">Loading performance coverage…</p>
                ) : telemetry.metrics.length ? (
                  <>
                    {percentMetrics.length > 0 && (
                      <div className="util-bars">
                        {percentMetrics.map((metric) => (
                          <div className="util-bar" key={`${metric.source}-${metric.metric}`}>
                            <div className="util-bar__meta">
                              <span>{metricLabel(metric.metric)} · {titleCase(metric.source)}</span>
                              <strong>{(metric.p95 ?? 0).toFixed(1)}%</strong>
                            </div>
                            <div className="util-bar__track">
                              <div
                                className={`util-bar__fill util-bar__fill--${utilizationTone(metric.p95 ?? 0)}`}
                                style={{ width: `${Math.min(100, Math.max(2, metric.p95 ?? 0))}%` }}
                              />
                              {metric.average !== null && (
                                <span className="util-bar__avg" style={{ left: `${Math.min(100, metric.average)}%` }} title={`Average ${metric.average.toFixed(1)}%`} />
                              )}
                            </div>
                            <small>P95 of hourly samples · avg {metric.average === null ? "—" : `${metric.average.toFixed(1)}%`} · {metric.coveragePercent}% coverage</small>
                          </div>
                        ))}
                      </div>
                    )}
                    {percentSeries.map((series) => (
                      <div className="modal-chart modal-chart--spark" key={`${series.source}-${series.metric}`}>
                        <div className="modal-chart__title">
                          <h3>{metricLabel(series.metric)} trend</h3>
                          <p>{titleCase(series.source)} hourly samples · last 7 days</p>
                        </div>
                        <ResponsiveContainer width="100%" height={110}>
                          <LineChart data={series.points}>
                            <XAxis
                              dataKey="t"
                              stroke={chart.muted}
                              tick={{ fontSize: 9 }}
                              tickFormatter={(value: string) => value.slice(5, 10)}
                              minTickGap={40}
                            />
                            <YAxis stroke={chart.muted} tick={{ fontSize: 9 }} width={34} domain={[0, 100]} />
                            <Tooltip contentStyle={tooltipStyle} />
                            <Line type="monotone" dataKey="value" stroke={chart.series[2]} strokeWidth={1.6} dot={false} />
                          </LineChart>
                        </ResponsiveContainer>
                      </div>
                    ))}
                    {otherMetrics.length > 0 && (
                      <div className="metric-grid">
                        {otherMetrics.map((metric) => (
                          <div key={`${metric.source}-${metric.metric}`}>
                            <small>{metricLabel(metric.metric)} · {titleCase(metric.source)}</small>
                            <strong>{metricValue(metric.p95, metric.unit)}</strong>
                            <span>P95 · {metric.coveragePercent}% coverage</span>
                            {metric.aggregationMethod ? <span>{metric.aggregationMethod}</span> : null}
                          </div>
                        ))}
                      </div>
                    )}
                    <p className="performance-note">
                      Independent source evidence; Flux does not average unlike metrics · newest sample {new Date(
                        telemetry.metrics.reduce((latest, item) =>
                          (item.lastObservedAt || "") > latest ? item.lastObservedAt || latest : latest, ""),
                      ).toLocaleString()}
                    </p>
                  </>
                ) : (
                  <div className="coverage-note">
                    <strong>
                      {telemetry.azureMonitorAttempt?.status === "no_data"
                        ? "No platform metrics returned"
                        : telemetry.azureMonitorAttempt?.status === "error"
                          ? "Azure Monitor collection failed"
                          : "Awaiting Azure Monitor coverage"}
                    </strong>
                    <p>
                      {telemetry.azureMonitorAttempt
                        ? `${telemetry.azureMonitorAttempt.message} Last attempted ${new Date(telemetry.azureMonitorAttempt.observedAt).toLocaleString()}.`
                        : "This VM has not been attempted yet. Collection rolls through 200 least-recently-attempted VMs per run."}
                      {telemetryStatus ? ` ${telemetryStatus.azureMonitorCovered.toLocaleString()} of ${telemetryStatus.virtualMachineCount.toLocaleString()} VMs currently have metrics.` : ""}
                    </p>
                  </div>
                )}
                {telemetry?.rightsizingAssessment && (
                  <div className={`rightsizing-explanation rightsizing-explanation--${telemetry.rightsizingAssessment.status}`}>
                    <div>
                      <span className="eyebrow">Governed right-sizing assessment</span>
                      <strong>{titleCase(telemetry.rightsizingAssessment.status)}</strong>
                      {telemetry.rightsizingAssessment.targetSku && (
                        <small>
                          {telemetry.rightsizingAssessment.currentSku || resource.sku}
                          {" → "}
                          {telemetry.rightsizingAssessment.targetSku}
                        </small>
                      )}
                    </div>
                    <p>{telemetry.rightsizingAssessment.reason}</p>
                    <dl>
                      <div><dt>CPU p95</dt><dd>{telemetry.rightsizingAssessment.cpuP95 === null ? "Unavailable" : `${telemetry.rightsizingAssessment.cpuP95.toFixed(1)}%`}</dd></div>
                      <div><dt>Evidence window</dt><dd>{telemetry.rightsizingAssessment.evidenceWindowDays} days</dd></div>
                      <div><dt>CPU coverage</dt><dd>{telemetry.rightsizingAssessment.metricCoveragePercent === null ? "Unavailable" : `${telemetry.rightsizingAssessment.metricCoveragePercent.toFixed(1)}%`}</dd></div>
                      <div><dt>Monthly value</dt><dd>{telemetry.rightsizingAssessment.estimatedMonthlySaving === null ? "Not valued" : currency(telemetry.rightsizingAssessment.estimatedMonthlySaving, telemetry.rightsizingAssessment.currency)}</dd></div>
                    </dl>
                    <small>{titleCase(telemetry.rightsizingAssessment.telemetrySource)} · {telemetry.rightsizingAssessment.methodVersion}</small>
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function InventoryPage() {
  const initial = hashParams();
  const [data, setData] = useState<Inventory | null>(null);
  const [error, setError] = useState("");
  const [search, setSearch] = useState(initial.get("search") ?? "");
  const [type, setType] = useState(
    initial.has("resourceId") ? "" : initial.has("resourceType")
      ? initial.get("resourceType")!
      : "microsoft.compute/virtualmachines",
  );
  const [subscription, setSubscription] = useState(initial.get("subscriptionId") ?? "");
  const [region, setRegion] = useState(initial.get("region") ?? "");
  const [virtualTagKey, setVirtualTagKey] = useState(initial.get("virtualTagKey") ?? "");
  const [virtualTagValue, setVirtualTagValue] = useState(initial.get("virtualTagValue") ?? "");
  const [selected, setSelected] = useState<Resource | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);

  const params = useMemo(() => {
    const value = new URLSearchParams();
    if (search) value.set("search", search);
    if (type) value.set("resourceType", type);
    if (subscription) value.set("subscriptionId", subscription);
    if (region) value.set("region", region);
    if (virtualTagKey) value.set("virtualTagKey", virtualTagKey);
    if (virtualTagValue) value.set("virtualTagValue", virtualTagValue);
    value.set("limit", String(pageSize));
    value.set("offset", String(page * pageSize));
    return value;
  }, [search, type, subscription, region, virtualTagKey, virtualTagValue, page, pageSize]);

  useEffect(() => {
    setPage(0);
  }, [search, type, subscription, region, virtualTagKey, virtualTagValue, pageSize]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const timer = window.setTimeout(() => {
      setError("");
      api.inventory(params)
        .then((result) => {
          if (!cancelled) setData(result);
        })
        .catch((reason) => {
          if (!cancelled) setError(reason.message);
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, search ? 220 : 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [params]);

  useEffect(() => {
    const resourceId = initial.get("resourceId");
    if (resourceId && data) {
      const match = data.items.find((item) => item.resourceId.toLowerCase() === resourceId.toLowerCase());
      if (match) setSelected(match);
    }
  }, [data, initial]);

  const exportParams = new URLSearchParams(params);
  exportParams.delete("limit");
  exportParams.delete("offset");
  const maxPage = data
    ? Math.max(Math.ceil(data.total / pageSize) - 1, 0)
    : 0;
  const visibleStart = data?.total ? page * pageSize + 1 : 0;
  const visibleEnd = data
    ? Math.min(page * pageSize + data.items.length, data.total)
    : 0;

  return (
    <>
      <PageHeader
        eyebrow="Source of truth"
        title="Azure inventory"
        description="A searchable, snapshot-backed catalog of resources discovered through Azure Resource Graph."
        action={
          <span className="export-actions">
            <a
              className="button button--ghost"
              href={api.inventoryExportUrl(exportParams)}
            >
              <Download size={16} />CSV
            </a>
            <a
              className="button button--ghost"
              href={api.inventoryExportUrl(withFormat(exportParams, "xlsx"))}
            >
              <Download size={16} />Excel
            </a>
          </span>
        }
      />
      <Card aria-busy={loading}>
        <div className="filters">
          <SavedViews
            page="inventory"
            current={() => ({
              search, resourceType: type, subscriptionId: subscription, region, virtualTagKey, virtualTagValue,
            })}
            onApply={(view) => {
              setSearch(view.search ?? "");
              setType(view.resourceType ?? "");
              setSubscription(view.subscriptionId ?? "");
              setRegion(view.region ?? "");
              setVirtualTagKey(view.virtualTagKey ?? "");
              setVirtualTagValue(view.virtualTagValue ?? "");
              setPage(0);
            }}
          />
          <label className="search-field">
            <Search size={17} />
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search name, resource group, type…" />
          </label>
          <label className="select-field">
            <SlidersHorizontal size={16} />
            <select value={type} onChange={(event) => setType(event.target.value)}>
              <option value="">All resource types</option>
              {data?.facets.resourceTypes.map((value) => <option key={value} value={value}>{shortType(value)}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={subscription} onChange={(event) => setSubscription(event.target.value)}>
              <option value="">All subscriptions</option>
              {data?.facets.subscriptions.map((value) => <option key={value.id} value={value.id}>{value.name}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={region} onChange={(event) => setRegion(event.target.value)}>
              <option value="">All regions</option>
              {data?.facets.regions.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={virtualTagKey} onChange={(event) => { setVirtualTagKey(event.target.value); setVirtualTagValue(""); }}>
              <option value="">All virtual dimensions</option>
              {data?.facets.virtualTagDimensions?.filter((item) => item.status === "active").map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
            </select>
          </label>
          {virtualTagKey && <label className="select-field"><input value={virtualTagValue} onChange={(event) => setVirtualTagValue(event.target.value)} placeholder="Virtual tag value (optional)" /></label>}
          <span
            className={`result-count${loading ? " result-count--loading" : ""}`}
            role="status"
            aria-live="polite"
          >
            {loading ? (
              <><RefreshCw className="spin" size={13} />Updating results…</>
            ) : (
              <>
                {visibleStart.toLocaleString()}–{visibleEnd.toLocaleString()}
                {" of "}
                {data?.total.toLocaleString() || 0} resources
              </>
            )}
          </span>
        </div>

        {error ? <ErrorPanel message={error} /> : !data ? <Loading /> : data.items.length ? (
          <div className={`table-wrap${loading ? " table-wrap--loading" : ""}`}>
            <table>
              <thead>
                <tr>
                  <th>Resource</th>
                  <th>Type</th>
                  <th>Subscription</th>
                  <th>Region</th>
                  <th>Actual cost MTD</th>
                  <th>Signal</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((resource) => (
                  <tr key={resource.resourceId} onClick={() => setSelected(resource)}>
                    <td>
                      <div className="resource-cell">
                        <span className="resource-icon"><Box size={16} /></span>
                        <span><strong>{resource.name}</strong><small>{resource.resourceGroup || "No resource group"}</small></span>
                      </div>
                    </td>
                    <td>{shortType(resource.resourceType)}</td>
                    <td title={resource.subscriptionId}>{resource.subscriptionName || resource.subscriptionId}</td>
                    <td>{resource.region || "global"}</td>
                    <td>{currency(resource.estimatedMonthlyCost)}</td>
                    <td>{resource.opportunityKind ? <span className="pill pill--warning">{titleCase(resource.opportunityKind)}</span> : <span className="pill">Healthy</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No resources found" description="Change the filters or synchronize an Azure integration." />
        )}
      </Card>
      {data && data.total > 0 && (
        <div className="pagination">
          <button
            className="button button--ghost"
            disabled={loading || page === 0}
            onClick={() => setPage((value) => value - 1)}
          >
            <ArrowLeft size={15} />Previous
          </button>
          <span>Page {page + 1} of {maxPage + 1}</span>
          <select
            value={pageSize}
            disabled={loading}
            onChange={(event) => setPageSize(Number(event.target.value))}
          >
            <option value={25}>25 per page</option>
            <option value={50}>50 per page</option>
            <option value={100}>100 per page</option>
          </select>
          <button
            className="button button--ghost"
            disabled={loading || page >= maxPage}
            onClick={() => setPage((value) => value + 1)}
          >
            Next<ArrowRight size={15} />
          </button>
        </div>
      )}
      {selected && <ResourceModal resource={selected} onClose={() => setSelected(null)} />}
    </>
  );
}
