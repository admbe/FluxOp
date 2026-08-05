import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  CalendarClock,
  CircleDollarSign,
  Download,
  Search,
  SlidersHorizontal,
  TrendingUp,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Sparkline } from "../components/Sparkline";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api";
import { compactNumber, relativeTime, shortType, titleCase } from "../format";
import { useChartColors } from "../theme";
import type { CostAnomalies, CostAnomalyContributor, CostHistoryStatus } from "../types";
import {
  Card,
  EmptyState,
  ErrorPanel,
  Loading,
  PageHeader,
} from "../components/Ui";

function money(value: number | null, currencyCode: string): string {
  if (value === null) return "Not available";
  if (!currencyCode || currencyCode === "Mixed") {
    const suffix = currencyCode === "Mixed" ? " (mixed currencies)" : "";
    return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
  }
  return Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currencyCode,
    maximumFractionDigits: 2,
  }).format(value);
}
function changePercent(value: number | null): string {
  return value === null ? "new spend" : `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
}

export function CostAnomaliesPage({ canManage = false }: { canManage?: boolean }) {
  const chart = useChartColors();
  const [data, setData] = useState<CostAnomalies | null>(null);
  const [costHistory, setCostHistory] = useState<CostHistoryStatus | null>(null);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [costType, setCostType] = useState("AmortizedCost");
  const [scopeType, setScopeType] = useState("");
  const [subscription, setSubscription] = useState("");
  const [service, setService] = useState("");
  const [severity, setSeverity] = useState("");
  const [status, setStatus] = useState("anomalous");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [reviewing, setReviewing] = useState("");
  const [contributorLoading, setContributorLoading] = useState("");
  const [contributors, setContributors] = useState<Record<string, CostAnomalyContributor[]>>({});

  const params = useMemo(() => {
    const value = new URLSearchParams();
    if (search) value.set("search", search);
    if (costType) value.set("costType", costType);
    if (scopeType) value.set("scopeType", scopeType);
    if (subscription) value.set("subscriptionId", subscription);
    if (service) value.set("serviceName", service);
    if (severity) value.set("severity", severity);
    if (status) value.set("status", status);
    value.set("limit", String(pageSize));
    value.set("offset", String(page * pageSize));
    return value;
  }, [
    search,
    costType,
    scopeType,
    subscription,
    service,
    severity,
    status,
    page,
    pageSize,
  ]);

  useEffect(() => {
    setPage(0);
  }, [
    search,
    costType,
    scopeType,
    subscription,
    service,
    severity,
    status,
    pageSize,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setError("");
      api.costAnomalies(params).then(setData).catch((reason) => setError(reason.message));
    }, search ? 220 : 0);
    return () => window.clearTimeout(timer);
  }, [params, search]);

  useEffect(() => {
    api.costHistoryStatus().then(setCostHistory).catch(() => undefined);
  }, []);

  const maxPage = data ? Math.max(Math.ceil(data.total / pageSize) - 1, 0) : 0;
  const summaryCurrency = data?.summary.currency || "";

  function updateReview(
    anomaly: CostAnomalies["items"][number],
    reviewStatus: CostAnomalies["items"][number]["reviewStatus"],
  ) {
    const key = `${anomaly.scopeType}-${anomaly.scopeId}-${anomaly.costType}`;
    setReviewing(key);
    setError("");
    api.reviewCostAnomaly(anomaly, reviewStatus, anomaly.reviewNote)
      .then(() => api.costAnomalies(params))
      .then(setData)
      .catch((reason) => setError(reason.message))
      .finally(() => setReviewing(""));
  }

  function loadContributors(anomaly: CostAnomalies["items"][number]) {
    const key = `${anomaly.runId}-${anomaly.costType}-${anomaly.scopeType}-${anomaly.scopeId}`;
    if (contributors[key]) {
      setContributors((value) => {
        const next = { ...value };
        delete next[key];
        return next;
      });
      return;
    }
    setContributorLoading(key);
    api.costAnomalyContributors(anomaly)
      .then((result) => setContributors((value) => ({ ...value, [key]: result.items })))
      .catch((reason) => setError(reason.message))
      .finally(() => setContributorLoading(""));
  }

  return (
    <>
      <PageHeader
        eyebrow="Spend intelligence"
        title="Cost anomalies"
        description="Daily actual and amortized cost compared with a governed matching-weekday median and median absolute deviation baseline."
      />

      {costHistory?.latestRun && (
        <div className={`inline-alert ${
          costHistory.latestRun.status === "succeeded"
            ? ""
            : "inline-alert--error"
        }`}>
          <CalendarClock size={16} />
          <div>
            <strong>
              Cost history {costHistory.latestRun.status} ·{" "}
              {costHistory.latestRun.completedScopes}/{costHistory.latestRun.expectedScopes} scopes
            </strong>
            <p>
              {costHistory.latestRun.message}
              {costHistory.latestRun.failedScopes
                ? ` ${costHistory.latestRun.failedScopes} scopes will be retried first.`
                : ""}
            </p>
          </div>
        </div>
      )}

      {data && (
        <div className="opportunity-summary-grid cost-anomaly-summary-grid">
          <Card>
            <small>Active anomalies</small>
            <strong>{data.summary.anomalyCount.toLocaleString()}</strong>
          </Card>
          <Card>
            <small>Daily increase</small>
            <strong>{money(data.summary.totalIncrease, summaryCurrency)}</strong>
          </Card>
          <Card>
            <small>Scopes evaluated</small>
            <strong>{data.summary.evaluatedCount.toLocaleString()}</strong>
          </Card>
          <Card>
            <small>Evaluation date</small>
            <strong>{data.summary.evaluationDate || "Not run"}</strong>
            <em>{relativeTime(data.summary.evaluatedAt)}</em>
          </Card>
        </div>
      )}

      {!!data?.summary.warmingCount && (
        <div className="inline-alert cost-anomaly-baseline-note">
          <CalendarClock size={16} />
          {data.summary.warmingCount.toLocaleString()} subscription or service baseline
          {data.summary.warmingCount === 1 ? " is" : "s are"} warming up. Resource-level
          warming records are intentionally suppressed.
        </div>
      )}

      {data?.summary.evaluationDate && (
        <Card className="cost-anomaly-trend-card">
          <div className="chart-title">
            <div>
              <h2>Daily {titleCase(costType)} trend</h2>
              <p>
                Estate total.{" "}
                {(data.trendLatencyDays ?? 0) > 0
                  ? `The most recent ${data.trendLatencyDays} day${data.trendLatencyDays === 1 ? "" : "s"} are hidden here and excluded from anomaly evaluation: Cost Management has not finalized them, so charting them would show a false drop.`
                  : "All retained days are shown, including days Cost Management may not have finalized."}
              </p>
            </div>
            <TrendingUp size={18} />
          </div>
          <div className="chart-area">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data.trend}>
                <defs>
                  <linearGradient id="costAnomalyFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={chart.info} stopOpacity={0.4} />
                    <stop offset="100%" stopColor={chart.info} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                <YAxis tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} tickFormatter={(value) => compactNumber(Number(value))} />
                <Tooltip
                  formatter={(value) => money(Number(value), summaryCurrency)}
                  contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }}
                />
                <Area type="monotone" dataKey="amount" stroke={chart.info} strokeWidth={2.4} fill="url(#costAnomalyFill)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}

      <Card className="opportunity-filter-card">
        <div className="filters filters--wrap">
          <label className="search-field">
            <Search size={17} />
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search resource, group, service…" />
          </label>
          <label className="select-field">
            <SlidersHorizontal size={16} />
            <select value={costType} onChange={(event) => setCostType(event.target.value)}>
              <option value="AmortizedCost">Amortized cost</option>
              <option value="ActualCost">Actual cost</option>
            </select>
          </label>
          <label className="select-field">
            <select value={scopeType} onChange={(event) => setScopeType(event.target.value)}>
              <option value="">All scopes</option>
              <option value="subscription">Subscriptions</option>
              <option value="service">Services</option>
              <option value="resource">Resources</option>
            </select>
          </label>
          <label className="select-field">
            <select value={subscription} onChange={(event) => setSubscription(event.target.value)}>
              <option value="">All subscriptions</option>
              {data?.facets.subscriptions.map((value) => <option key={value.id} value={value.id}>{value.name}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={service} onChange={(event) => setService(event.target.value)}>
              <option value="">All services</option>
              {data?.facets.services.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={severity} onChange={(event) => setSeverity(event.target.value)}>
              <option value="">All severities</option>
              {data?.facets.severities.map((value) => <option key={value} value={value}>{titleCase(value)}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="anomalous">Anomalies</option>
              <option value="warming_up">Warming up</option>
              <option value="">All statuses</option>
            </select>
          </label>
          <span className="result-count">{data?.total.toLocaleString() || 0} results</span>
          <a className="button button--ghost" href={api.costAnomaliesExportUrl(params)}>
            <Download size={15} />
            Export CSV
          </a>
        </div>
      </Card>

      {error ? <ErrorPanel message={error} /> : !data ? <Loading /> : data.items.length ? (
        <>
          <div className="cost-anomaly-list">
            {data.items.map((anomaly) => {
              const contributorKey = `${anomaly.runId}-${anomaly.costType}-${anomaly.scopeType}-${anomaly.scopeId}`;
              return (
              <Card className="cost-anomaly-card" key={`${anomaly.scopeType}-${anomaly.scopeId}-${anomaly.costType}`}>
                <div className="cost-anomaly-card__signal">
                  <span className={`pill cost-anomaly-pill cost-anomaly-pill--${anomaly.status === "warming_up" ? "warming" : anomaly.severity}`}>
                    {anomaly.status === "warming_up" ? "Warming up" : titleCase(anomaly.severity)}
                  </span>
                  <small>{titleCase(anomaly.scopeType)} scope</small>
                </div>
                <div className="cost-anomaly-card__identity">
                  <h2>{anomaly.resourceName || anomaly.scopeId}</h2>
                  <p>
                    {anomaly.resourceType ? `${shortType(anomaly.resourceType)} · ` : ""}
                    {anomaly.serviceName ? `${anomaly.serviceName} · ` : ""}
                    {anomaly.resourceGroup || anomaly.subscriptionId}
                  </p>
                  <span>{anomaly.reason}</span>
                </div>
                <div className="cost-anomaly-card__numbers">
                  {anomaly.recentDailyAmounts?.length > 1 && (
                    <span className="cost-anomaly-sparkline" title="Daily spend, last 14 finalized days">
                      <Sparkline values={anomaly.recentDailyAmounts} />
                    </span>
                  )}
                  <span><small>Current day</small><strong>{money(anomaly.currentAmount, anomaly.currency)}</strong></span>
                  <span><small>Seasonal baseline</small><strong>{money(anomaly.baselineMedian, anomaly.currency)}</strong></span>
                  <span><small>Previous week</small><strong>{money(anomaly.previousWeekAmount, anomaly.currency)}</strong></span>
                  <span><small>Increase</small><strong>{money(anomaly.absoluteChange, anomaly.currency)}</strong></span>
                  <span><small>Change</small><strong>{changePercent(anomaly.percentChange)}</strong></span>
                </div>
                <div className="cost-anomaly-card__evidence">
                  <span>{anomaly.baselinePoints} weekday points</span>
                  {anomaly.kScore !== null && <span>{anomaly.kScore.toFixed(1)} robust score</span>}
                  <span>{titleCase(anomaly.reviewStatus)}</span>
                  <time>{anomaly.evaluationDate}</time>
                </div>
                <div className="cost-anomaly-card__review">
                  <a
                    className="button button--ghost"
                    href={api.costAnomalyEvidenceUrl(anomaly)}
                  >
                    <Download size={14} />
                    Evidence
                  </a>
                  <button
                    className="button button--ghost"
                    disabled={contributorLoading === contributorKey}
                    onClick={() => loadContributors(anomaly)}
                  >
                    {contributors[contributorKey] ? "Hide contributors" : "Contributors"}
                  </button>
                  {canManage && anomaly.status === "anomalous" && (
                    <>
                    {(["investigating", "acknowledged", "resolved"] as const).map((value) => (
                      <button
                        className="button button--ghost"
                        disabled={reviewing === `${anomaly.scopeType}-${anomaly.scopeId}-${anomaly.costType}`}
                        key={value}
                        onClick={() => updateReview(anomaly, value)}
                      >
                        {titleCase(value)}
                      </button>
                    ))}
                    </>
                  )}
                </div>
                {contributors[contributorKey] && (
                  <div className="cost-anomaly-contributors">
                    <strong>Top contributors versus previous week</strong>
                    {contributors[contributorKey].length ? contributors[contributorKey].map((item) => (
                      <span key={item.id}>
                        <b>{item.name}</b>
                        {money(item.current, item.currency)} · {money(item.change, item.currency)} change
                      </span>
                    )) : <span>No lower-grain contributors were available.</span>}
                  </div>
                )}
              </Card>
            )})}
          </div>
          <div className="pagination">
            <button className="button button--ghost" disabled={page === 0} onClick={() => setPage((value) => value - 1)}><ArrowLeft size={15} />Previous</button>
            <span>Page {page + 1} of {maxPage + 1}</span>
            <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}><option value={25}>25 per page</option><option value={50}>50 per page</option><option value={100}>100 per page</option></select>
            <button className="button button--ghost" disabled={page >= maxPage} onClick={() => setPage((value) => value + 1)}>Next<ArrowRight size={15} /></button>
          </div>
        </>
      ) : (
        <Card>
          <EmptyState
            title={data.summary.evaluatedAt ? "No cost anomalies match" : "Cost baseline has not run yet"}
            description={data.summary.evaluatedAt
              ? "No findings match the active filters. A quiet result is expected when daily cost remains inside its seasonal baseline."
              : "The independent daily cost job will backfill history and publish the first governed baseline."}
            action={data.summary.evaluatedAt ? undefined : <span className="muted"><CircleDollarSign size={15} /> Scheduled daily at 12:30 UTC</span>}
          />
        </Card>
      )}
    </>
  );
}
