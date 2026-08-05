import {
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CalendarRange,
  CircleDollarSign,
  DatabaseZap,
  Download,
  Gauge,
  Lightbulb,
  RefreshCw,
  ShieldCheck,
  Tags,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  LabelList,
  Line,
  ResponsiveContainer,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api";
import { compactNumber, percent, shortType } from "../format";
import { useChartColors } from "../theme";
import type { AllocationReport, BudgetReport, CommitmentInventory, CostReport, ExecutiveSummary, FiscalOutlook, FocusAnalyticsReport, GovernanceReport, SavingsReport, TagHygieneReport, UnitEconomicsReport, WorkloadReport } from "../types";
import { VirtualTagsReport } from "../components/VirtualTagsReport";
import { Card, EmptyState, ErrorPanel, Loading, PageHeader, Tabs } from "../components/Ui";
import { announceActivity } from "../busy";
import { hashParams, navigateWithParams } from "../viewState";

type ReportTab = "cost" | "optimization" | "planning" | "governance";

const REPORT_TABS: { id: ReportTab; label: string }[] = [
  { id: "cost", label: "Cost analysis" },
  { id: "optimization", label: "Optimization" },
  { id: "planning", label: "Financial planning" },
  { id: "governance", label: "Governance & allocation" },
];

function money(value: number | null, code: string): string {
  if (value === null) return "Not available";
  return Intl.NumberFormat("en-US", {
    style: "currency",
    currency: code || "USD",
    maximumFractionDigits: 0,
  }).format(value);
}
function ReportMetric({
  label,
  value,
  detail,
  icon: Icon,
}: {
  label: string;
  value: string;
  detail: string;
  icon: typeof CircleDollarSign;
}) {
  return (
    <Card className="report-metric">
      <Icon size={18} />
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </Card>
  );
}

export function ReportsPage({ canManage = false }: { canManage?: boolean }) {
  const initial = hashParams();
  const chart = useChartColors();
  const colors = chart.series;
  const initialTab = REPORT_TABS.some((item) => item.id === initial.get("tab"))
    ? (initial.get("tab") as ReportTab)
    : "cost";
  const [tab, setTab] = useState<ReportTab>(initialTab);
  const loadedTabs = useRef(new Set<ReportTab>());
  const [data, setData] = useState<CostReport | null>(null);
  const [error, setError] = useState("");
  const [costType, setCostType] = useState("AmortizedCost");
  const [forecastDays, setForecastDays] = useState(30);
  const [outlook, setOutlook] = useState<FiscalOutlook | null>(null);
  const [outlookError, setOutlookError] = useState("");
  const [outlookSaving, setOutlookSaving] = useState(false);
  const [editAssumptions, setEditAssumptions] = useState(false);
  const [draftStartMonth, setDraftStartMonth] = useState(7);
  const [draftCostType, setDraftCostType] = useState("AmortizedCost");
  const [draftGrowth, setDraftGrowth] = useState("0");
  const [draftSavings, setDraftSavings] = useState(false);
  const [draftRamp, setDraftRamp] = useState("3");
  const [draftNotes, setDraftNotes] = useState("");
  const [currency, setCurrency] = useState("");
  const [subscription, setSubscription] = useState(initial.get("subscriptionId") ?? "");
  const [service, setService] = useState(initial.get("serviceName") ?? "");
  const [resource, setResource] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [costLoading, setCostLoading] = useState(true);
  const [workloadLoading, setWorkloadLoading] = useState(true);
  const [governanceLoading, setGovernanceLoading] = useState(true);
  const [workload, setWorkload] = useState<WorkloadReport | null>(null);
  const [governance, setGovernance] = useState<GovernanceReport | null>(null);
  const [tagHygiene, setTagHygiene] = useState<TagHygieneReport | null>(null);
  const [allocation, setAllocation] = useState<AllocationReport | null>(null);
  const [focusAnalytics, setFocusAnalytics] = useState<FocusAnalyticsReport | null>(null);
  const [savings, setSavings] = useState<SavingsReport | null>(null);
  const [commitmentsData, setCommitmentsData] = useState<CommitmentInventory | null>(null);
  const [budgets, setBudgets] = useState<BudgetReport | null>(null);
  const [unitEconomics, setUnitEconomics] = useState<UnitEconomicsReport | null>(null);
  const [executive, setExecutive] = useState<ExecutiveSummary | null>(null);
  const [governanceSubscription, setGovernanceSubscription] = useState("");
  const [governanceAssignment, setGovernanceAssignment] = useState("");
  const [governanceState, setGovernanceState] = useState("NonCompliant");

  const params = useMemo(() => {
    const value = new URLSearchParams({ costType });
    if (currency) value.set("currency", currency);
    if (subscription) value.set("subscriptionId", subscription);
    if (service) value.set("serviceName", service);
    if (resource) value.set("resourceId", resource);
    if (startDate) value.set("startDate", startDate);
    if (endDate) value.set("endDate", endDate);
    if (forecastDays !== 30) value.set("forecastDays", String(forecastDays));
    return value;
  }, [costType, currency, subscription, service, resource, startDate, endDate, forecastDays]);

  function switchTab(next: ReportTab) {
    setTab(next);
    const carried = Object.fromEntries(hashParams().entries());
    navigateWithParams("reports", { ...carried, tab: next });
  }

  const applyOutlook = (value: FiscalOutlook) => {
    setOutlook(value);
    setDraftStartMonth(value.config.fyStartMonth);
    setDraftCostType(value.config.costType);
    setDraftGrowth(String(value.config.growthPercentMonthly));
    setDraftSavings(value.config.includePlannedSavings);
    setDraftRamp(String(value.config.savingsRampMonths));
    setDraftNotes(value.config.notes);
  };

  async function saveAssumptions() {
    setOutlookSaving(true);
    setOutlookError("");
    try {
      const result = await api.saveFiscalOutlookConfig({
        fyStartMonth: draftStartMonth,
        costType: draftCostType,
        growthPercentMonthly: Number(draftGrowth) || 0,
        includePlannedSavings: draftSavings,
        savingsRampMonths: Number(draftRamp) || 0,
        notes: draftNotes,
      });
      applyOutlook(result);
      setEditAssumptions(false);
    } catch (reason) {
      setOutlookError(reason instanceof Error ? reason.message : "The assumptions could not be saved.");
    } finally {
      setOutlookSaving(false);
    }
  }

  function exportOutlookCsv() {
    if (!outlook) return;
    const lines = [
      "month,status,amount,lower,upper,currency",
      ...outlook.months.map((item) =>
        `${item.month},${item.status},${item.amount},${item.lower},${item.upper},${outlook.currency}`,
      ),
      `FY_TOTAL,${outlook.fiscalYear},${outlook.fyTotal},${outlook.fyLower},${outlook.fyUpper},${outlook.currency}`,
    ];
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `flux-fiscal-outlook-${outlook.fiscalYear}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  useEffect(() => {
    if (tab !== "cost") return;
    setError("");
    setCostLoading(true);
    announceActivity("report-cost", true);
    api.costReport(params).then((result) => {
      setData(result);
      if (!currency && result.summary.currency) {
        setCurrency(result.summary.currency);
      }
    }).catch((reason) => setError(reason.message)).finally(() => {
      setCostLoading(false);
      announceActivity("report-cost", false);
    });
  }, [tab, params, currency]);

  // Each tab's reports load on first activation instead of all ten report
  // endpoints firing on mount for sections the visit may never open.
  useEffect(() => {
    if (loadedTabs.current.has(tab)) return;
    loadedTabs.current.add(tab);
    const group: Promise<unknown>[] = [];
    if (tab === "optimization") {
      setWorkloadLoading(true);
      group.push(
        api.workloadReport().then(setWorkload).catch((reason) => setError(reason.message)).finally(() => setWorkloadLoading(false)),
        api.savingsReport().then(setSavings).catch(() => setSavings(null)),
        api.commitments().then(setCommitmentsData).catch(() => setCommitmentsData(null)),
      );
    }
    if (tab === "planning") {
      group.push(
        api.fiscalOutlook().then(applyOutlook).catch((reason) =>
          setOutlookError(reason instanceof Error ? reason.message : "The fiscal outlook could not load."),
        ),
        api.executiveSummary().then(setExecutive).catch(() => setExecutive(null)),
        api.budgetReport().then(setBudgets).catch(() => setBudgets(null)),
        api.unitEconomicsReport().then(setUnitEconomics).catch(() => setUnitEconomics(null)),
        api.focusAnalyticsReport().then(setFocusAnalytics).catch(() => setFocusAnalytics(null)),
      );
    }
    if (tab === "governance") {
      group.push(
        api.tagHygieneReport().then(setTagHygiene).catch(() => setTagHygiene(null)),
        api.allocationReport().then(setAllocation).catch(() => setAllocation(null)),
      );
    }
    if (!group.length) return;
    announceActivity(`report-${tab}`, true);
    Promise.allSettled(group).finally(() =>
      announceActivity(`report-${tab}`, false),
    );
  }, [tab]);

  useEffect(() => {
    if (tab !== "governance") return;
    setGovernanceLoading(true);
    announceActivity("report-governance", true);
    const value = new URLSearchParams();
    if (governanceSubscription) value.set("subscriptionId", governanceSubscription);
    if (governanceAssignment) value.set("assignmentId", governanceAssignment);
    if (governanceState) value.set("complianceState", governanceState);
    api.governanceReport(value).then(setGovernance).catch((reason) => setError(reason.message)).finally(() => {
      setGovernanceLoading(false);
      announceActivity("report-governance", false);
    });
  }, [tab, governanceSubscription, governanceAssignment, governanceState]);

  const code = data?.summary.currency || currency || "USD";
  const forecastChart = data ? [
    ...data.daily.slice(-30).map((point) => ({
      date: point.date,
      actual: point.amount,
      forecast: null,
      lower: null,
      upper: null,
    })),
    ...data.forecast.points.map((point) => ({
      date: point.date,
      actual: null,
      forecast: point.amount,
      lower: point.lower,
      upper: point.upper,
    })),
  ] : [];
  const outlookChart = outlook ? outlook.months.map((item) => ({
    month: item.month,
    actual: item.status === "actual" ? item.amount : null,
    projected: item.status !== "actual" ? item.amount : null,
    lower: item.status !== "actual" ? item.lower : null,
    upper: item.status !== "actual" ? item.upper : null,
    budget: outlook.budgetMonthly,
  })) : [];
  const groupChart = outlook ? outlook.groups.map((group) => ({
    name: group.name,
    projected: group.fyTotal,
    budget: group.annualBudget,
    variance: group.variance,
    over: group.variance > 0,
  })) : [];
  const taggedPercent = data?.summary.resourceCount
    ? data.summary.taggedResourceCount / data.summary.resourceCount * 100
    : null;
  const changedUp = (data?.summary.changeAmount || 0) >= 0;
  const ChangeIcon = changedUp ? ArrowUpRight : ArrowDownRight;

  return (
    <>
      <PageHeader
        eyebrow="Native Flux reporting"
        title="Cost summary"
        description="Feature-equivalent native reporting guided by Microsoft FinOps Toolkit v14, backed by governed DuckDB measures rather than Power BI."
      />

      <Tabs
        tabs={REPORT_TABS}
        active={tab}
        onChange={switchTab}
        label="Report sections"
      />

      {tab === "cost" && (<>
      <Card className="report-filter-card" aria-busy={costLoading}>
        <div className="filters filters--wrap">
          <label className="select-field">
            <select value={costType} onChange={(event) => setCostType(event.target.value)}>
              <option value="AmortizedCost">Amortized cost</option>
              <option value="ActualCost">Actual cost</option>
            </select>
          </label>
          <label className="select-field">
            <select value={currency} onChange={(event) => setCurrency(event.target.value)}>
              {(data?.facets.currencies || [currency]).filter(Boolean).map((value) => <option value={value} key={value}>{value}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={subscription} onChange={(event) => setSubscription(event.target.value)}>
              <option value="">All subscriptions</option>
              {data?.facets.subscriptions.map((value) => <option value={value.id} key={value.id}>{value.name}</option>)}
            </select>
          </label>
          <label className="select-field">
            <select value={service} onChange={(event) => setService(event.target.value)}>
              <option value="">All services</option>
              {data?.facets.services.map((value) => <option value={value} key={value}>{value}</option>)}
            </select>
          </label>
          <label className="date-field"><CalendarRange size={15} /><input type="date" value={startDate} min={data?.period.availableStart || undefined} max={endDate || data?.period.availableEnd || undefined} onChange={(event) => setStartDate(event.target.value)} /></label>
          <label className="date-field"><input type="date" value={endDate} min={startDate || data?.period.availableStart || undefined} max={data?.period.availableEnd || undefined} onChange={(event) => setEndDate(event.target.value)} /></label>
          <a className="button button--ghost" href={api.costReportExportUrl(params)}>
            <Download size={15} /> CSV
          </a>
          <a
            className="button button--ghost"
            href={api.costReportExportUrl(
              (() => { const next = new URLSearchParams(params); next.set("format", "xlsx"); return next; })(),
            )}
          >
            <Download size={15} /> Excel
          </a>
          <span className={`result-count${costLoading ? " result-count--loading" : ""}`} role="status" aria-live="polite">
            {costLoading ? <><RefreshCw className="spin" size={13} /> Updating results.</> : <>{data?.period.start || "—"} to {data?.period.end || "—"}</>}
          </span>
        </div>
        {resource && (
          <div className="inline-alert">
            <DatabaseZap size={15} />
            <span>Resource drilldown: {resource}</span>
            <button className="button button--ghost" onClick={() => setResource("")}>Clear</button>
          </div>
        )}
      </Card>

      {data && !data.dataCoverage.complete && (
        <div className="freshness-warning">
          <AlertTriangle size={16} />
          <span>
            <strong>
              Cost coverage is incomplete: {data.dataCoverage.completeScopes}/
              {data.dataCoverage.configuredScopes} subscriptions have complete {costType === "ActualCost" ? "actual" : "amortized"} history for this period.
            </strong>{" "}
            Missing or partial: {data.dataCoverage.scopes
              .filter((scope) => !scope.complete)
              .map((scope) => scope.name)
              .join(", ") || "none"}.
            Failed scopes remain visible in Integrations and are retried first.
          </span>
        </div>
      )}

      {error ? <ErrorPanel message={error} /> : !data ? <Loading /> : !data.daily.length ? (
        <Card className="report-empty-card"><EmptyState title="No daily cost rows match" description="Change the filters or allow the independent daily cost-history collector to complete more scopes." /></Card>
      ) : (
        <>
          <div className="report-metrics-grid">
            <ReportMetric
              label="Effective cost"
              value={money(data.summary.totalCost, code)}
              detail={`Actual ${money(data.costTypeComparison.ActualCost ?? null, code)} · Amortized ${money(data.costTypeComparison.AmortizedCost ?? null, code)}`}
              icon={CircleDollarSign}
            />
            <ReportMetric label="Period change" value={data.summary.changePercent === null ? "No comparison" : percent(data.summary.changePercent)} detail={`${money(data.summary.changeAmount, code)} versus prior period`} icon={ChangeIcon} />
            <ReportMetric label="Average daily" value={money(data.summary.averageDailyCost, code)} detail={`${money(data.summary.previousCost, code)} prior-period total`} icon={Gauge} />
            <ReportMetric label="Tagged coverage" value={percent(taggedPercent)} detail={`${money(data.summary.untaggedCost, code)} untagged cost`} icon={Tags} />
          </div>

          <div className="report-grid">
            <Card className="chart-card chart-card--wide">
              <div className="chart-title"><div><h2>Daily cost and running total</h2><p>Toolkit Summary and Running total equivalents</p></div><CircleDollarSign size={18} /></div>
              <div className="chart-area">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={data.daily}>
                    <defs>
                      <linearGradient id="reportDailyFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={chart.primary} stopOpacity={0.35} /><stop offset="100%" stopColor={chart.primary} stopOpacity={0} /></linearGradient>
                    </defs>
                    <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                    <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                    <YAxis yAxisId="daily" tickFormatter={(value) => compactNumber(Number(value))} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                    <YAxis yAxisId="total" orientation="right" tickFormatter={(value) => compactNumber(Number(value))} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                    <Tooltip formatter={(value) => money(Number(value), code)} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                    <Legend />
                    <Area yAxisId="daily" name="Daily cost" type="monotone" dataKey="amount" stroke={chart.primary} fill="url(#reportDailyFill)" />
                    <Line yAxisId="total" name="Running total" type="monotone" dataKey="cumulative" stroke={chart.info} dot={false} strokeWidth={2} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <Card className="chart-card chart-card--wide">
              <div className="chart-title">
                <div><h2>Operational forecast ({forecastDays} days)</h2><p>{data.forecast.reason}</p></div>
                <div className="fy-outlook__toolbar">
                  {[30, 60, 90].map((days) => (
                    <button
                      key={days}
                      className={`button button--secondary ${forecastDays === days ? "fy-horizon--active" : ""}`}
                      onClick={() => setForecastDays(days)}
                    >
                      {days}d
                    </button>
                  ))}
                  <CalendarRange size={18} />
                </div>
              </div>
              {forecastDays > 60 && (
                <p className="muted">Beyond 60 days the weekday model trends toward a run-rate; use the fiscal-year outlook above for budget horizons.</p>
              )}
              {data.forecast.status === "ready" ? (
                <>
                  <div className="forecast-summary">
                    <span><small>Expected</small><strong>{money(data.forecast.forecastTotal, code)}</strong></span>
                    <span><small>Governed range</small><strong>{money(data.forecast.lowerTotal, code)}–{money(data.forecast.upperTotal, code)}</strong></span>
                    <span><small>Backtest error</small><strong>{data.forecast.backtestMape === null ? "Warming up" : `${data.forecast.backtestMape}% MAPE (${data.forecast.backtestPoints} points)`}</strong></span>
                    {data.forecast.monthly.map((month) => <span key={month.month}><small>{month.month}</small><strong>{money(month.amount, code)} · {money(month.lower, code)}–{money(month.upper, code)}</strong></span>)}
                  </div>
                  <div className="chart-area">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={forecastChart}>
                        <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                        <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 9 }} />
                        <YAxis tickFormatter={(value) => compactNumber(Number(value))} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                        <Tooltip formatter={(value) => value === null ? "—" : money(Number(value), code)} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                        <Area name="Upper bound" type="monotone" dataKey="upper" stroke="none" fill={chart.info} fillOpacity={0.12} />
                        <Area name="Lower bound" type="monotone" dataKey="lower" stroke="none" fill="rgb(var(--background))" fillOpacity={0.7} />
                        <Line name="Actual" type="monotone" dataKey="actual" stroke={chart.primary} dot={false} strokeWidth={2} />
                        <Line name="Forecast" type="monotone" dataKey="forecast" stroke={chart.info} strokeDasharray="5 4" dot={false} strokeWidth={2} />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                </>
              ) : <div className="signal-empty"><DatabaseZap size={24} /><strong>Forecast baseline is {data.forecast.status.replace("_", " ")}</strong><p>{data.forecast.reason}</p></div>}
              <p className="chart-method-note">{data.forecast.methodVersion} · excludes {data.forecast.latencyDays} billed-latency days · data through {data.forecast.dataThrough || "—"} · {data.budgetVariance.reason}</p>
            </Card>

            {[
              ["Subscriptions", data.bySubscription],
              ["Services", data.byService],
              ["Resource groups", data.byResourceGroup],
              ["Regions", data.byRegion],
            ].map(([title, values], chartIndex) => (
              <Card className="chart-card" key={title as string}>
                <div className="chart-title"><div><h2>{title as string}</h2><p>Top contributors in the selected period</p></div><ArrowUpRight size={18} /></div>
                <div className="chart-area">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={values as { name: string; value: number }[]} layout="vertical" margin={{ left: 8, right: 18 }}>
                      <CartesianGrid horizontal={false} stroke="rgb(var(--border))" />
                      <XAxis type="number" tickFormatter={(value) => compactNumber(Number(value))} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                      <YAxis type="category" dataKey="name" width={110} tickFormatter={(value) => value.length > 18 ? `${value.slice(0, 16)}…` : value} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted-bright))", fontSize: 9 }} />
                      <Tooltip formatter={(value) => money(Number(value), code)} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                      <Bar dataKey="value" radius={[0, 5, 5, 0]} barSize={14}>
                        {(values as { name: string; value: number }[]).map((_, index) => <Cell key={index} fill={colors[(chartIndex + index) % colors.length]} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </Card>
            ))}

            <Card className="report-table-card chart-card--wide">
              <div className="chart-title"><div><h2>Top cost movers</h2><p>Largest absolute resource changes versus the matching prior period</p></div><ArrowUpRight size={18} /></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Resource</th><th>Current</th><th>Previous</th><th>Change</th><th>Change %</th></tr></thead>
                  <tbody>{data.topMovers.resources.map((item) => (
                    <tr key={item.id}>
                      <td><button className="table-link" onClick={() => setResource(item.id)}>{item.name}</button></td>
                      <td>{money(item.current, code)}</td><td>{money(item.previous, code)}</td>
                      <td>{money(item.change, code)}</td><td>{item.changePercent === null ? "New" : percent(item.changePercent)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Card>

            <Card className="report-table-card chart-card--wide">
              <div className="chart-title"><div><h2>Inventory economics</h2><p>Resource count, effective cost, and cost per resource by type</p></div><DatabaseZap size={18} /></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Resource type</th><th>Resources</th><th>Effective cost</th><th>Cost/resource</th></tr></thead>
                  <tbody>{data.inventory.map((item) => <tr key={item.resourceType}><td>{shortType(item.resourceType)}</td><td>{item.resourceCount.toLocaleString()}</td><td>{money(item.cost, code)}</td><td>{money(item.costPerResource, code)}</td></tr>)}</tbody>
                </table>
              </div>
            </Card>

            <Card className="report-table-card chart-card--wide">
              <div className="chart-title"><div><h2>Top resources</h2><p>Resource-native equivalent of the Toolkit Resources breakdown</p></div><CircleDollarSign size={18} /></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Resource</th><th>Type</th><th>Resource group</th><th>Region</th><th>Effective cost</th></tr></thead>
                  <tbody>{data.resources.slice(0, 50).map((item) => <tr key={item.resourceId}><td title={item.resourceId}><button className="table-link" onClick={() => setResource(item.resourceId)}>{item.resourceName}</button></td><td>{shortType(item.resourceType)}</td><td>{item.resourceGroup || "—"}</td><td>{item.region || "—"}</td><td>{money(item.cost, code)}</td></tr>)}</tbody>
                </table>
              </div>
            </Card>

            <Card className="report-lineage-card chart-card--wide">
              <strong>{data.lineage.toolkitReference}</strong>
              <span>{data.lineage.source} · {data.lineage.grain}</span>
              {data.lineage.limitations.map((value) => <small key={value}>{value}</small>)}
            </Card>
          </div>
        </>
      )}

      </>)}

      {tab === "optimization" && (<>
      <div className="report-section-heading">
        <div>
          <span>Workload optimization</span>
          <h2>Opportunity and retirement portfolio</h2>
          <p>Native workload reporting combines Advisor, Flux Signals, valuation, persistence, and telemetry evidence.</p>
        </div>
        <Lightbulb size={22} />
        <a className="button button--ghost" href={api.retirementReportExportUrl()}><Download size={15} /> Retirement CSV</a>
      </div>
      {!workload ? <Loading /> : (
        <>
          <div className="report-metrics-grid">
            <ReportMetric label="Opportunities" value={workload.summary.total.toLocaleString()} detail={`${workload.summary.corroborated.toLocaleString()} corroborated`} icon={Lightbulb} />
            <ReportMetric label="Risk-adjusted value" value={compactNumber(workload.summary.monthlyRiskAdjustedValue)} detail="Estimated monthly, governed confidence" icon={CircleDollarSign} />
            <ReportMetric label="Telemetry ready" value={workload.summary.telemetryReady.toLocaleString()} detail="Findings with governed performance evidence" icon={Gauge} />
            <ReportMetric label="Retirement candidates" value={workload.summary.retirementCandidates.toLocaleString()} detail={`${compactNumber(workload.summary.retirementRiskAdjustedValue)} monthly value identified`} icon={DatabaseZap} />
          </div>
          <div className="report-grid">
            <Card className="chart-card chart-card--wide">
              <div className="chart-title"><div><h2>Savings evidence trend</h2><p>Historical risk-adjusted monthly value captured by valuation snapshots</p></div><CircleDollarSign size={18} /></div>
              <div className="chart-area">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={workload.savingsTrend}>
                    <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                    <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 9 }} />
                    <YAxis tickFormatter={(value) => compactNumber(Number(value))} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                    <Tooltip formatter={(value) => compactNumber(Number(value))} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                    <Area type="monotone" dataKey="riskAdjustedValue" stroke={chart.primary} fill={chart.primary} fillOpacity={0.15} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </Card>
            <Card className="report-table-card chart-card--wide">
              <div className="chart-title"><div><h2>Coverage and candidate aging</h2><p>Telemetry gaps and persistence bands requiring attention</p></div><Gauge size={18} /></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Dimension</th><th>Status</th><th>Count</th></tr></thead>
                  <tbody>
                    {workload.coverageGaps.map((item) => <tr key={`coverage-${item.status}`}><td>Telemetry</td><td>{item.status}</td><td>{item.count.toLocaleString()}</td></tr>)}
                    {workload.byAge.map((item) => <tr key={`age-${item.name}`}><td>Candidate age</td><td>{item.name}</td><td>{item.count.toLocaleString()}</td></tr>)}
                  </tbody>
                </table>
              </div>
            </Card>
            <Card className="report-table-card chart-card--wide">
              <div className="chart-title"><div><h2>Top governed opportunities</h2><p>Ranked by risk-adjusted monthly value</p></div><Lightbulb size={18} /></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Resource</th><th>Finding</th><th>Confidence</th><th>Age</th><th>Risk-adjusted value</th></tr></thead>
                  <tbody>{workload.topOpportunities.map((item) => <tr key={item.id}><td title={item.resourceId}>{item.resourceName || item.title}</td><td>{item.title}</td><td>{item.confidence || "Review"}</td><td>{item.ageDays === null ? "—" : `${item.ageDays}d`}</td><td>{compactNumber(item.monthlyRiskAdjustedSavings || 0)}</td></tr>)}</tbody>
                </table>
              </div>
            </Card>
            <Card className="report-table-card chart-card--wide">
              <div className="chart-title"><div><h2>Retirement candidates</h2><p>Unused and orphaned assets; evidence for review, never auto-remediation</p></div><DatabaseZap size={18} /></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Resource</th><th>Type</th><th>Signal</th><th>Age</th><th>Owner tags</th><th>Cost exposure</th><th>Evidence</th></tr></thead>
                  <tbody>{workload.retirementCandidates.map((item) => <tr key={item.id}><td title={item.resourceId}>{item.resourceName || item.title}</td><td>{shortType(item.resourceType)}</td><td>{item.title}</td><td>{item.ageDays === null ? "—" : `${item.ageDays}d`}</td><td>{item.ownershipReady ? item.ownershipTags.join(", ") : "Missing"}</td><td>{item.costExposure === null ? "—" : compactNumber(item.costExposure)}</td><td><a href={api.opportunityEvidenceUrl(item.id)}>Change request</a></td></tr>)}</tbody>
                </table>
              </div>
            </Card>
          </div>
        </>
      )}

      </>)}

      {tab === "governance" && (<>
      <div className="report-section-heading">
        <div>
          <span>Governance posture</span>
          <h2>Azure Policy compliance</h2>
          <p>Assignment-level posture from Azure Resource Graph across configured subscriptions.</p>
        </div>
        <ShieldCheck size={22} />
      </div>
      {!governance ? <Loading /> : !governance.summary.observedAt ? (
        <Card className="content-module"><EmptyState title="Policy posture is ready to collect" description="Run the next Azure synchronization to populate assignment-level compliance from PolicyResources." /></Card>
      ) : (
        <>
          <Card className="report-filter-card">
            <div className="filters filters--wrap">
              <label className="select-field">
                <select value={governanceSubscription} onChange={(event) => { setGovernanceSubscription(event.target.value); setGovernanceAssignment(""); }}>
                  <option value="">All subscriptions</option>
                  {governance.bySubscription.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
                </select>
              </label>
              <label className="select-field">
                <select value={governanceAssignment} onChange={(event) => setGovernanceAssignment(event.target.value)}>
                  <option value="">All assignments</option>
                  {governance.assignments.map((item) => <option value={item.assignmentId} key={`${item.subscriptionId}-${item.assignmentId}`}>{item.assignmentName}</option>)}
                </select>
              </label>
              <label className="select-field">
                <select value={governanceState} onChange={(event) => setGovernanceState(event.target.value)}>
                  <option value="NonCompliant">Non-compliant resources</option>
                  <option value="Exempt">Exempt resources</option>
                  <option value="">All detailed states</option>
                </select>
              </label>
            </div>
          </Card>
          <div className="report-metrics-grid">
            <ReportMetric label="Compliance" value={percent(governance.summary.compliancePercent)} detail={`${governance.summary.evaluated.toLocaleString()} policy states evaluated`} icon={ShieldCheck} />
            <ReportMetric label="Non-compliant" value={governance.summary.nonCompliant.toLocaleString()} detail="Policy-state records requiring attention" icon={ArrowUpRight} />
            <ReportMetric label="Assignments" value={governance.summary.assignmentCount.toLocaleString()} detail="Across configured subscriptions" icon={DatabaseZap} />
            <ReportMetric label="Exempt" value={governance.summary.exempt.toLocaleString()} detail={`Observed ${governance.summary.observedAt.slice(0, 10)}`} icon={Tags} />
          </div>
          <Card className="report-table-card report-wide-card">
            <div className="chart-title"><div><h2>Policy assignments</h2><p>Highest non-compliant counts first</p></div><ShieldCheck size={18} /></div>
            <div className="report-table-wrap">
              <table>
                <thead><tr><th>Assignment</th><th>Subscription</th><th>Evaluated</th><th>Compliant</th><th>Non-compliant</th><th>Exempt</th></tr></thead>
                <tbody>{governance.assignments.map((item) => <tr key={`${item.subscriptionId}-${item.assignmentId}`}><td title={item.assignmentId}><button className="table-link" onClick={() => setGovernanceAssignment(item.assignmentId)}>{item.assignmentName}</button></td><td>{item.subscriptionName}</td><td>{item.evaluated.toLocaleString()}</td><td>{item.compliant.toLocaleString()}</td><td>{item.nonCompliant.toLocaleString()}</td><td>{item.exempt.toLocaleString()}</td></tr>)}</tbody>
              </table>
            </div>
          </Card>
          <Card className="report-table-card report-wide-card">
            <div className="chart-title"><div><h2>Policy resource drilldown</h2><p>Read-only non-compliant and exempt policy states from ARG</p></div><ShieldCheck size={18} /></div>
            <div className="report-table-wrap">
              <table>
                <thead><tr><th>Resource</th><th>Type</th><th>Assignment</th><th>Definition</th><th>State</th><th>Exemption</th></tr></thead>
                <tbody>{governance.resources.map((item) => (
                  <tr key={`${item.assignmentId}-${item.definitionId}-${item.resourceId}`}>
                    <td title={item.resourceId}>{item.resourceName}</td><td>{shortType(item.resourceType)}</td>
                    <td>{item.assignmentName}</td><td title={item.definitionId}>{item.definitionName || shortType(item.definitionId)}</td>
                    <td>{item.complianceState}</td><td title={item.exemptionId}>{item.exemptionId ? shortType(item.exemptionId) : "—"}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      </>)}

      {tab === "planning" && (<>
      <div className="report-grid report-grid--single">
            <Card className="chart-card chart-card--wide fy-outlook">
              <div className="chart-title">
                <div>
                  <h2>Fiscal-year outlook{outlook ? ` — ${outlook.fiscalYear}` : ""}</h2>
                  <p>{outlook ? `${outlook.costType} · governed monthly actuals plus a stated-assumption projection to ${outlook.fyEnd.slice(0, 7)}` : "Loading the fiscal-year projection…"}</p>
                </div>
                <div className="fy-outlook__toolbar">
                  <button className="button button--secondary" onClick={exportOutlookCsv} disabled={!outlook}><Download size={14} />CSV</button>
                  {canManage && (
                    <button className="button button--secondary" onClick={() => setEditAssumptions((value) => !value)} disabled={!outlook}>
                      {editAssumptions ? "Close assumptions" : "Assumptions"}
                    </button>
                  )}
                </div>
              </div>
              {outlookError && <div className="inline-alert inline-alert--error">{outlookError}</div>}
              {!outlook && !outlookError && <Loading />}
              {outlook && outlook.status === "not_connected" && (
                <div className="signal-empty"><DatabaseZap size={24} /><strong>No monthly history yet</strong><p>The next cost-history synchronization collects up to thirteen months of monthly totals from Cost Management.</p></div>
              )}
              {outlook && outlook.status !== "not_connected" && (
                <>
                  {editAssumptions && canManage && (
                    <div className="fy-editor">
                      <label>FY starts
                        <select value={draftStartMonth} onChange={(event) => setDraftStartMonth(Number(event.target.value))}>
                          {["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"].map((name, index) => (
                            <option key={name} value={index + 1}>{name}</option>
                          ))}
                        </select>
                      </label>
                      <label>Cost basis
                        <select value={draftCostType} onChange={(event) => setDraftCostType(event.target.value)}>
                          <option value="AmortizedCost">Amortized</option>
                          <option value="ActualCost">Actual</option>
                        </select>
                      </label>
                      <label>Growth %/month
                        <input value={draftGrowth} onChange={(event) => setDraftGrowth(event.target.value)} inputMode="decimal" />
                      </label>
                      <label className="fy-editor__check">
                        <input type="checkbox" checked={draftSavings} onChange={(event) => setDraftSavings(event.target.checked)} />
                        Apply plan savings ({money(outlook.plannedSavingsMonthly, outlook.currency)}/mo from the primary right-sizing board)
                      </label>
                      <label>Savings ramp (months)
                        <input value={draftRamp} onChange={(event) => setDraftRamp(event.target.value)} inputMode="numeric" />
                      </label>
                      <label className="fy-editor__notes">Notes
                        <input value={draftNotes} onChange={(event) => setDraftNotes(event.target.value)} maxLength={500} placeholder="Recorded with the assumptions" />
                      </label>
                      <button className="button" onClick={() => void saveAssumptions()} disabled={outlookSaving}>{outlookSaving ? "Saving…" : "Save"}</button>
                    </div>
                  )}
                  <div className="forecast-summary">
                    <span><small>Actual to date</small><strong>{money(outlook.actualToDate, outlook.currency)}</strong></span>
                    <span><small>Projected remainder</small><strong>{money(outlook.projectedRemainder.amount, outlook.currency)}</strong></span>
                    <span><small>{outlook.fiscalYear} total</small><strong>{money(outlook.fyTotal, outlook.currency)}</strong></span>
                    <span><small>Governed range</small><strong>{money(outlook.fyLower, outlook.currency)}–{money(outlook.fyUpper, outlook.currency)}</strong></span>
                    {outlook.fyBudget !== null && (
                      <span><small>vs budget ({money(outlook.fyBudget, outlook.currency)})</small><strong className={outlook.fyVarianceVsBudget !== null && outlook.fyVarianceVsBudget > 0 ? "fy-over" : "fy-under"}>
                        {outlook.fyVarianceVsBudget !== null && outlook.fyVarianceVsBudget > 0 ? "+" : ""}{money(outlook.fyVarianceVsBudget, outlook.currency)}
                      </strong></span>
                    )}
                  </div>
                  {outlook.groups.length > 0 && (
                    <div className="fy-groups">
                      {outlook.groups.map((group) => {
                        const over = group.variance > 0;
                        const share = group.annualBudget > 0 ? Math.min(group.fyTotal / group.annualBudget, 1.35) : 0;
                        return (
                          <div className="fy-group" key={group.id}>
                            <div className="fy-group__title">
                              <strong>{group.name}</strong>
                              <span className={over ? "fy-over" : "fy-under"}>
                                {over ? "+" : ""}{money(group.variance, group.currency)} vs {money(group.annualBudget, group.currency)}
                              </span>
                            </div>
                            <div className="fy-group__bar" role="img" aria-label={`${group.name} projected ${money(group.fyTotal, group.currency)} of ${money(group.annualBudget, group.currency)}`}>
                              <i style={{ width: `${Math.min(share, 1) * 100}%` }} className={over ? "fy-group__fill fy-group__fill--over" : "fy-group__fill"} />
                            </div>
                            <small>
                              {money(group.actualToDate, group.currency)} actual · projected {money(group.fyTotal, group.currency)} ({money(group.fyLower, group.currency)}–{money(group.fyUpper, group.currency)})
                              {group.coveredMembers < group.memberCount ? ` · ${group.coveredMembers}/${group.memberCount} subscriptions backfilled` : ""}
                            </small>
                          </div>
                        );
                      })}
                    </div>
                  )}
                  <div className="chart-area">
                    <ResponsiveContainer width="100%" height="100%">
                      <ComposedChart data={outlookChart}>
                        <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                        <XAxis dataKey="month" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 9 }} />
                        <YAxis tickFormatter={(value) => compactNumber(Number(value))} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                        <Tooltip formatter={(value) => value === null ? "—" : money(Number(value), outlook.currency)} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                        <Area name="Upper bound" type="monotone" dataKey="upper" stroke="none" fill={chart.info} fillOpacity={0.12} />
                        <Area name="Lower bound" type="monotone" dataKey="lower" stroke="none" fill="rgb(var(--background))" fillOpacity={0.7} />
                        <Bar name="Actual" dataKey="actual" fill={chart.primary} radius={[3, 3, 0, 0]} />
                        <Bar name="Projected" dataKey="projected" fill={chart.info} fillOpacity={0.55} radius={[3, 3, 0, 0]} />
                        {outlook.budgetMonthly !== null && <Line name="Monthly budget" type="monotone" dataKey="budget" stroke={chart.warning} strokeDasharray="6 4" dot={false} strokeWidth={2} />}
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                  <p className="chart-method-note">
                    {outlook.methodVersion} · {outlook.historyMonths} months of history
                    {outlook.backtestMape !== null ? ` · backtest ${outlook.backtestMape}% MAPE` : ""}
                    {outlook.seasonalComparison ? ` · seasonal comparison ${money(outlook.seasonalComparison.fyTotal, outlook.currency)} (not used for executive forecast)` : ""}
                    {outlook.config.growthPercentMonthly ? ` · growth assumption ${outlook.config.growthPercentMonthly}%/mo` : ""}
                    {outlook.plannedSavingsMonthly > 0 ? ` · plan savings applied` : ""}
                    {outlook.config.updatedBy ? ` · assumptions by ${outlook.config.updatedBy}` : ""}
                    {outlook.limitations.length ? ` · ${outlook.limitations.join(" ")}` : ""}
                  </p>
                  {(outlook.subscriptionCoverage.uncovered?.length ?? 0) > 0 && (
                    <div className="coverage-note forecast-coverage-detail">
                      <strong>Subscriptions requiring cost-history follow-up</strong>
                      <p>The coverage warning is based on these configured subscriptions:</p>
                      <ul>
                        {outlook.subscriptionCoverage.uncovered?.map((item) => (
                          <li key={item.subscriptionId}>
                            <span>{item.label}</span>
                            <small>{item.subscriptionId} · {item.lastIngestionStatus ? `${item.lastIngestionStatus} · ` : ""}{item.lastIngestionMessage}</small>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              )}
            </Card>

      </div>

      {outlook && outlook.status !== "not_connected" && (
        <div className="planning-insight-grid">
          <Card className="planning-pulse-card">
            <div className="chart-title">
              <div><span className="eyebrow">Decision lens</span><h2>Planning pulse</h2><p>Signals that change the planning conversation.</p></div>
              <Lightbulb size={18} />
            </div>
            <div className="planning-pulse-list">
              <div>
                <span>Budget headroom</span>
                <strong className={outlook.fyVarianceVsBudget !== null && outlook.fyVarianceVsBudget > 0 ? "fy-over" : "fy-under"}>
                  {outlook.fyVarianceVsBudget === null ? "Not configured" : `${outlook.fyVarianceVsBudget > 0 ? "+" : ""}${money(outlook.fyVarianceVsBudget, outlook.currency)}`}
                </strong>
                <small>{outlook.fyBudget === null ? "Add an annual target to quantify headroom." : `Projected ${money(outlook.fyTotal, outlook.currency)} against ${money(outlook.fyBudget, outlook.currency)}.`}</small>
              </div>
              <div>
                <span>Forecast uncertainty</span>
                <strong>{money(Math.max(0, outlook.fyUpper - outlook.fyLower), outlook.currency)}</strong>
                <small>Width of the governed FY range; lower is more predictable.</small>
              </div>
              <div>
                <span>Plan savings applied</span>
                <strong>{outlook.plannedSavingsMonthly > 0 ? `${money(outlook.plannedSavingsMonthly, outlook.currency)}/mo` : "None"}</strong>
                <small>{outlook.config.includePlannedSavings ? `Ramp assumption: ${outlook.config.savingsRampMonths} months.` : "Enable right-sizing savings in assumptions to model the impact."}</small>
              </div>
            </div>
          </Card>

          {groupChart.length > 0 && (
            <Card className="chart-card planning-group-chart">
              <div className="chart-title"><div><span className="eyebrow">Portfolio exposure</span><h2>Projected vs annual budget</h2><p>Budget groups ranked by their FY envelope.</p></div><CircleDollarSign size={18} /></div>
              <div className="planning-group-chart__area">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={groupChart} layout="vertical" margin={{ top: 4, right: 20, bottom: 4, left: 12 }} barCategoryGap="28%">
                    <CartesianGrid horizontal={false} stroke="rgb(var(--border))" />
                    <XAxis type="number" tickFormatter={(value) => compactNumber(Number(value))} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                    <YAxis type="category" dataKey="name" width={92} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                    <Tooltip formatter={(value) => money(Number(value), outlook.currency)} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                    <Legend />
                    <Bar name="Annual budget" dataKey="budget" fill={chart.warning} fillOpacity={0.45} radius={[0, 3, 3, 0]} />
                    <Bar name="FY projected" dataKey="projected" radius={[0, 3, 3, 0]}>
                      {groupChart.map((item) => <Cell key={item.name} fill={item.over ? chart.danger : chart.primary} />)}
                    </Bar>
                    <ReferenceLine x={0} stroke="rgb(var(--border-bright))" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
          )}
        </div>
      )}

      {executive && (
        <>
          <div className="report-section-heading section-heading">
            <div>
              <span className="eyebrow">Leadership view</span>
              <h2>Executive summary</h2>
              <p>Month-to-date position across spend, budgets, anomalies, and savings — one governed page. Generated {new Date(executive.generatedAt).toLocaleString()}.</p>
            </div>
            <div className="fy-outlook__toolbar">
              <a className="button button--ghost" href={api.executiveExportUrl()}><Download size={15} /> Excel workbook</a>
              <button className="button button--ghost" onClick={() => window.print()}><Download size={15} /> Print / PDF</button>
            </div>
          </div>
          <div className="report-metrics-grid">
            <Card>
              <small>Finalized MTD spend</small>
              <strong>
                {executive.spend ? compactNumber(executive.spend.mtdActual) : "—"}
                {executive.spend?.deltaPercent != null && (
                  <em className={`metric-delta ${executive.spend.deltaPercent > 0 ? "metric-delta--up" : "metric-delta--down"}`}>
                    {executive.spend.deltaPercent > 0 ? "▲" : "▼"} {Math.abs(executive.spend.deltaPercent).toFixed(1)}%
                  </em>
                )}
              </strong>
              <em>vs {executive.spend ? compactNumber(executive.spend.priorMtdActual) : "—"} same span last month</em>
            </Card>
            <Card>
              <small>Budget status</small>
              <strong>{executive.budgets ? executive.budgets.targets.filter((t) => t.status === "over").length + " over · " + executive.budgets.targets.filter((t) => t.status === "at_risk").length + " at risk" : "No targets set"}</strong>
              <em>{executive.budgets ? `${executive.budgets.targets.length} budget targets tracked` : "Admins set targets under Integrations"}</em>
            </Card>
            <Card>
              <small>Active cost anomalies</small>
              <strong>{executive.anomalies.count ?? 0}</strong>
              <em>{compactNumber(executive.anomalies.dailyIncrease ?? 0)} daily increase</em>
            </Card>
            <Card>
              <small>Savings</small>
              <strong>{compactNumber(executive.savings.realizedMonthly)} realized</strong>
              <em>{compactNumber(executive.savings.estimatedImplementedMonthly)} est. implemented · {executive.savings.acceptedCount} accepted</em>
            </Card>
          </div>
          {executive.serviceComposition && (
            <Card className="report-table-card report-wide-card">
              <div className="chart-title">
                <div>
                  <h2>Service composition</h2>
                  <p>Billing-service labels compared with the resource/economic view · MTD actual</p>
                </div>
              </div>
              {executive.serviceComposition.mixedSourceClassification && (
                <div className="freshness-warning report-note">
                  <AlertTriangle size={16} />
                  <span>{executive.serviceComposition.note}</span>
                </div>
              )}
              <div className="service-composition-grid">
                <div>
                  <h3>Billing-service view</h3>
                  <p className="chart-method-note">Preserves the provider/source classification used for the bill.</p>
                  <div className="report-table-wrap">
                    <table>
                      <thead><tr><th>Service</th><th>MTD actual</th></tr></thead>
                      <tbody>{executive.serviceComposition.billingServices.map((item) => (
                        <tr key={item.name}><td>{item.name}</td><td>{compactNumber(item.amount)}</td></tr>
                      ))}</tbody>
                    </table>
                  </div>
                </div>
                <div>
                  <h3>Resource/economic view</h3>
                  <p className="chart-method-note">Uses resource identity where available; unresolved charges remain labeled.</p>
                  <div className="report-table-wrap">
                    <table>
                      <thead><tr><th>Economic category</th><th>MTD actual</th></tr></thead>
                      <tbody>{executive.serviceComposition.economicCategories.map((item) => (
                        <tr key={item.name}><td>{item.name}</td><td>{compactNumber(item.amount)}</td></tr>
                      ))}</tbody>
                    </table>
                  </div>
                </div>
              </div>
              <p className="chart-method-note service-composition-source">Sources: {executive.serviceComposition.sources.map((item) => item.name).join(", ") || "No source recorded"}. {executive.serviceComposition.note}</p>
            </Card>
          )}
        </>
      )}

      {budgets?.configured && (
        <>
          <div className="report-section-heading section-heading">
            <div>
              <span className="eyebrow">Targets</span>
              <h2>Budgets</h2>
              <p>Finalized actuals vs admin-set monthly targets, with a labeled linear run-rate projection through {budgets.period?.finalizedThrough}.</p>
            </div>
          </div>
          <Card className="report-table-card report-wide-card">
            <div className="report-table-wrap">
              <table>
                <thead><tr><th>Scope</th><th>Budget</th><th>MTD actual</th><th>Burn</th><th>Projected</th><th>Status</th></tr></thead>
                <tbody>{budgets.targets.map((item) => (
                  <tr key={`${item.scopeType}-${item.scopeId}`}>
                    <td>{item.scopeType === "estate" ? "Entire estate" : item.scopeId}</td>
                    <td>{compactNumber(item.monthlyAmount)} {item.currency}</td>
                    <td>{item.mtdActual != null ? compactNumber(item.mtdActual) : "—"}</td>
                    <td>{item.burnPercent ?? "—"}%</td>
                    <td>{item.projectedMonthly != null ? compactNumber(item.projectedMonthly) : "—"} ({item.projectedPercent ?? "—"}%)</td>
                    <td><span className={`pill budget-pill budget-pill--${item.status}`}>{(item.status || "").replace("_", " ")}</span></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      {unitEconomics?.configured && unitEconomics.summary && unitEconomics.units && (
        <>
          <div className="report-section-heading section-heading">
            <div>
              <span className="eyebrow">Unit economics</span>
              <h2>Spend by {unitEconomics.summary.dimensionLabel}</h2>
              <p>Actual MTD cost attributed to the configured business dimension. {compactNumber(unitEconomics.summary.unattributedCost)} is unattributed.</p>
            </div>
          </div>
          <Card className="report-table-card report-wide-card">
            <div className="report-table-wrap">
              <table>
                <thead><tr><th>{unitEconomics.summary.dimensionLabel}</th><th>Resources</th><th>Monthly cost</th><th>% of spend</th></tr></thead>
                <tbody>{unitEconomics.units.map((item) => (
                  <tr key={item.name}>
                    <td>{item.name}</td>
                    <td>{item.resourceCount.toLocaleString()}</td>
                    <td>{compactNumber(item.monthlyCost)}</td>
                    <td>{item.percentOfTotal ?? "—"}%</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      </>)}

      {tab === "optimization" && (<>
      {commitmentsData && commitmentsData.summary.activeCount > 0 && (
        <Card className="chart-card chart-card--wide commitments-card">
          <div className="chart-title">
            <div>
              <h2>Reservation posture</h2>
              <p>Active commitments from the tenant-wide inventory, collected daily. Renewal decisions belong on the right-sizing plan board.</p>
            </div>
          </div>
          <div className="forecast-summary">
            <span><small>Active reservations</small><strong>{commitmentsData.summary.activeCount}</strong></span>
            <span><small>Instances covered</small><strong>{commitmentsData.summary.totalQuantity}</strong></span>
            <span><small>Fleet 30-day utilization</small><strong>{commitmentsData.summary.averageUtilization30d !== null ? `${commitmentsData.summary.averageUtilization30d}%` : "—"}</strong></span>
            <span><small>Expiring ≤ 120 days</small><strong className={commitmentsData.summary.expiringWithin120Days ? "fy-over" : "fy-under"}>{commitmentsData.summary.expiringWithin120Days}</strong></span>
            <span><small>Expiring ≤ 30 days</small><strong className={commitmentsData.summary.expiringWithin30Days ? "fy-over" : "fy-under"}>{commitmentsData.summary.expiringWithin30Days}</strong></span>
          </div>
          <div className="commitments-table" role="table" aria-label="Active reservations">
            <div className="commitments-row commitments-row--head" role="row">
              <span>Name</span><span>SKU</span><span>Region</span><span>Qty</span><span>Term</span><span>Scope</span><span>Util 30d</span><span>Expires</span>
            </div>
            {commitmentsData.reservations.map((item) => (
              <div className={`commitments-row${item.daysToExpiry !== null && item.daysToExpiry <= 120 ? (item.daysToExpiry <= 30 ? " commitments-row--danger" : " commitments-row--warn") : ""}`} role="row" key={item.reservationId || item.name}>
                <span>{item.name}</span>
                <span>{item.sku}</span>
                <span>{item.region}</span>
                <span>{item.quantity}</span>
                <span>{item.term}</span>
                <span>{item.scopeType}</span>
                <span>{item.utilization30d !== null ? `${item.utilization30d.toFixed(1)}%` : "—"}</span>
                <span>{item.expiryDate || "—"}{item.daysToExpiry !== null ? ` (${item.daysToExpiry}d)` : ""}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
      {savings && (savings.summary.acceptedCount + savings.summary.implementedCount + savings.summary.dismissedCount) > 0 && (
        <>
          <div className="report-section-heading section-heading">
            <div>
              <span className="eyebrow">FinOps outcomes</span>
              <h2>Savings realized</h2>
              <p>Recommendations tracked through implementation. Realized savings are measured from cost data — implementation baseline minus current run-rate — not assumed from estimates.</p>
            </div>
          </div>
          <div className="report-metrics-grid">
            <Card><small>Accepted</small><strong>{savings.summary.acceptedCount}</strong><em>{compactNumber(savings.summary.estimatedAcceptedMonthly)} est. monthly</em></Card>
            <Card><small>Implemented</small><strong>{savings.summary.implementedCount}</strong><em>{compactNumber(savings.summary.estimatedImplementedMonthly)} est. monthly</em></Card>
            <Card><small>Realized monthly</small><strong>{compactNumber(savings.summary.realizedMonthly)}</strong><em>measured on {savings.summary.measuredCount} resources</em></Card>
            <Card><small>Dismissed</small><strong>{savings.summary.dismissedCount}</strong><em>excluded from pipeline</em></Card>
          </div>
          <Card className="report-table-card report-wide-card">
            <div className="chart-title"><div><h2>Tracked recommendations</h2><p>Baseline vs current actual monthly cost</p></div></div>
            <div className="report-table-wrap">
              <table>
                <thead><tr><th>Recommendation</th><th>Status</th><th>Estimated</th><th>Baseline</th><th>Current</th><th>Realized</th><th>By</th></tr></thead>
                <tbody>{savings.items.map((item) => (
                  <tr key={item.opportunityId}>
                    <td title={item.opportunityId}>{item.resourceId ? item.resourceId.split("/").slice(-1)[0] : item.opportunityId.slice(0, 40)}</td>
                    <td>{item.status}</td>
                    <td>{item.estimatedMonthlySavings != null ? compactNumber(item.estimatedMonthlySavings) : "—"}</td>
                    <td>{item.baselineMonthlyCost != null ? compactNumber(item.baselineMonthlyCost) : "—"}</td>
                    <td>{item.currentMonthlyCost != null ? compactNumber(item.currentMonthlyCost) : "—"}</td>
                    <td><strong>{item.realizedMonthlySavings != null ? compactNumber(item.realizedMonthlySavings) : "—"}</strong></td>
                    <td>{item.updatedBy || "—"}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      </>)}

      {tab === "planning" && (<>
      {focusAnalytics?.available && focusAnalytics.commitment && focusAnalytics.pricing && (
        <>
          <div className="report-section-heading section-heading">
            <div>
              <span className="eyebrow">Charge-level FOCUS analysis</span>
              <h2>Commitments and pricing</h2>
              <p>Reservation and Savings Plan utilization plus realized discounts, computed from the governed FOCUS charge ledger ({focusAnalytics.period?.windowDays}-day window).</p>
            </div>
          </div>
          <div className="report-metrics-grid">
            <Card><small>Commitment coverage</small><strong>{focusAnalytics.commitment.coveragePercent ?? "—"}%</strong><em>{compactNumber(focusAnalytics.commitment.committedEffectiveCost)} committed · {compactNumber(focusAnalytics.commitment.onDemandEffectiveCost)} on-demand</em></Card>
            <Card><small>Discount realized</small><strong>{compactNumber(focusAnalytics.pricing.discountRealized)}</strong><em>{focusAnalytics.pricing.discountPercent ?? "—"}% below list</em></Card>
            <Card><small>Effective cost</small><strong>{compactNumber(focusAnalytics.pricing.effectiveCost)}</strong><em>billed {compactNumber(focusAnalytics.pricing.billedCost)}</em></Card>
            <Card><small>Active commitments</small><strong>{focusAnalytics.commitment.commitments.length}</strong><em>with charge-level utilization</em></Card>
          </div>
          <div className="report-grid">
            <Card className="report-table-card">
              <div className="chart-title"><div><h2>Commitment utilization</h2><p>Used vs unused effective cost per commitment</p></div></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Commitment</th><th>Type</th><th>Used</th><th>Unused</th><th>Utilization</th></tr></thead>
                  <tbody>{focusAnalytics.commitment.commitments.map((item) => (
                    <tr key={item.id}>
                      <td title={item.id}>{item.name}</td>
                      <td>{item.type || "—"}</td>
                      <td>{compactNumber(item.usedCost)}</td>
                      <td>{compactNumber(item.unusedCost)}</td>
                      <td>{item.utilizationPercent ?? "—"}%</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Card>
            <Card className="report-table-card">
              <div className="chart-title"><div><h2>Discount realization by service</h2><p>List vs effective cost</p></div></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Service</th><th>List</th><th>Effective</th><th>Discount</th></tr></thead>
                  <tbody>{focusAnalytics.pricing.byService.map((item) => (
                    <tr key={item.serviceName}>
                      <td>{item.serviceName}</td>
                      <td>{compactNumber(item.listCost)}</td>
                      <td>{compactNumber(item.effectiveCost)}</td>
                      <td>{item.discountPercent ?? "—"}%</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Card>
          </div>
        </>
      )}

      </>)}

      {tab === "governance" && (<>
      <VirtualTagsReport costType={costType} startDate={startDate} endDate={endDate} />
      {allocation && (
        <>
          <div className="report-section-heading section-heading">
            <div>
              <span className="eyebrow">Showback</span>
              <h2>Cost allocation</h2>
              <p>Actual month-to-date spend attributed to cost centers by tag, with shared spend prorated in proportion to direct usage.</p>
            </div>
          </div>
          {allocation.configured && allocation.summary && allocation.centers ? (
            <>
              <div className="report-metrics-grid">
                <Card><small>Allocated</small><strong>{allocation.summary.allocatedPercent ?? "—"}%</strong><em>of {compactNumber(allocation.summary.totalMonthlyCost)} MTD actual</em></Card>
                <Card><small>Cost centers</small><strong>{allocation.summary.centerCount}</strong><em>tag keys: {allocation.config.costCenterTags.join(", ")}</em></Card>
                <Card><small>Shared pool prorated</small><strong>{compactNumber(allocation.summary.sharedPool)}</strong><em>{allocation.config.sharedValues.join(", ") || "no shared values configured"}</em></Card>
                <Card><small>Unallocated</small><strong>{compactNumber(allocation.summary.unallocatedCost)}</strong><em>{allocation.summary.unallocatedResourceCount.toLocaleString()} untagged resources — see Tag hygiene</em></Card>
              </div>
              <Card className="report-table-card report-wide-card">
                <div className="chart-title"><div><h2>Cost centers</h2><p>Direct spend plus prorated shared allocation</p></div></div>
                <div className="report-table-wrap">
                  <table>
                    <thead><tr><th>Cost center</th><th>Resources</th><th>Direct</th><th>Shared allocation</th><th>Total</th><th>% of spend</th></tr></thead>
                    <tbody>{allocation.centers.map((item) => (
                      <tr key={item.name}>
                        <td>{item.name}</td>
                        <td>{item.resourceCount.toLocaleString()}</td>
                        <td>{compactNumber(item.directCost)}</td>
                        <td>{compactNumber(item.sharedAllocation)}</td>
                        <td><strong>{compactNumber(item.totalCost)}</strong></td>
                        <td>{item.percentOfTotal ?? "—"}%</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </Card>
            </>
          ) : (
            <Card>
              <EmptyState
                title="Allocation is not configured"
                description="An administrator sets the cost-center tag keys (and optional shared values) under Integrations. Until then, spend cannot be attributed to cost centers."
              />
            </Card>
          )}
        </>
      )}

      {tagHygiene && (
        <>
          <div className="report-section-heading section-heading">
            <div>
              <span className="eyebrow">Allocation readiness</span>
              <h2>Tag hygiene</h2>
              <p>Spend can only be allocated to cost centers when the resources carrying it are tagged. Untagged spend is unallocatable spend.</p>
            </div>
          </div>
          <div className="report-metrics-grid">
            <Card><small>Tag coverage</small><strong>{tagHygiene.summary.taggedPercent ?? "—"}%</strong><em>{tagHygiene.summary.resourceCount.toLocaleString()} resources assessed</em></Card>
            <Card><small>Tagged spend</small><strong>{tagHygiene.summary.taggedCostPercent ?? "—"}%</strong><em>of {compactNumber(tagHygiene.summary.totalMonthlyCost)} MTD actual</em></Card>
            <Card><small>Unallocatable spend</small><strong>{compactNumber(tagHygiene.summary.untaggedMonthlyCost)}</strong><em>on untagged resources</em></Card>
            <Card>
              <small>Required-tag compliance</small>
              <strong>{tagHygiene.summary.compliantPercent ?? "—"}%</strong>
              <em>{tagHygiene.summary.requiredTags.length ? tagHygiene.summary.requiredTags.join(", ") : "No required tags configured"}</em>
            </Card>
          </div>
          <div className="report-grid">
            <Card className="report-table-card">
              <div className="chart-title"><div><h2>By subscription</h2><p>Ordered by unallocatable spend</p></div></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Subscription</th><th>Resources</th><th>Tagged</th><th>Compliant</th><th>Untagged spend</th></tr></thead>
                  <tbody>{tagHygiene.bySubscription.map((item) => (
                    <tr key={item.subscriptionId}>
                      <td>{item.subscriptionName}</td>
                      <td>{item.resources.toLocaleString()}</td>
                      <td>{item.taggedPercent ?? "—"}%</td>
                      <td>{item.compliantPercent ?? "—"}%</td>
                      <td>{compactNumber(item.untaggedCost)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Card>
            <Card className="report-table-card">
              <div className="chart-title"><div><h2>Costliest untagged resources</h2><p>Tag these first to raise allocation coverage fastest</p></div></div>
              <div className="report-table-wrap">
                <table>
                  <thead><tr><th>Resource</th><th>Type</th><th>Monthly cost</th></tr></thead>
                  <tbody>{tagHygiene.topUntagged.map((item) => (
                    <tr key={item.resourceId}>
                      <td title={item.resourceId}>{item.name}</td>
                      <td>{shortType(item.resourceType)}</td>
                      <td>{compactNumber(item.monthlyCost)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Card>
          </div>
        </>
      )}
      </>)}

    </>
  );
}
