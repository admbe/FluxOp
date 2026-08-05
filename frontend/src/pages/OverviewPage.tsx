import {
  AlertTriangle,
  ArrowUpRight,
  CircleDollarSign,
  Cloud,
  DatabaseZap,
  Gauge,
  Lightbulb,
  RefreshCw,
  TrendingUp,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { compactNumber, currency, percent, relativeTime, shortType, titleCase } from "../format";
import { useChartColors } from "../theme";
import { navigateWithParams } from "../viewState";
import type { ChartDatum, Overview, Page } from "../types";
import { Card, EmptyState, PageHeader } from "../components/Ui";

function Metric({
  label,
  value,
  detail,
  icon: Icon,
  tone = "green",
  delta,
  onClick,
}: {
  label: string;
  value: string;
  detail: string;
  icon: typeof Cloud;
  tone?: string;
  delta?: { percent: number | null; title: string } | null;
  onClick?: () => void;
}) {
  return (
    <Card
      className={`metric-card ${onClick ? "metric-card--clickable" : ""}`}
      onClick={onClick}
    >
      <div className={`metric-icon metric-icon--${tone}`}>
        <Icon size={19} />
      </div>
      <span>{label}</span>
      <strong>
        {value}
        {delta && delta.percent !== null && (
          <em
            className={`metric-delta ${delta.percent > 0 ? "metric-delta--up" : "metric-delta--down"}`}
            title={delta.title}
          >
            {delta.percent > 0 ? "▲" : "▼"} {Math.abs(delta.percent).toFixed(1)}%
          </em>
        )}
      </strong>
      <small>{detail}</small>
    </Card>
  );
}
function ChartTitle({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="chart-title">
      <div>
        <h2>{title}</h2>
        <p>{detail}</p>
      </div>
      <ArrowUpRight size={18} />
    </div>
  );
}

function ChartEmpty({
  icon: Icon,
  title,
  detail,
}: {
  icon: typeof Cloud;
  title: string;
  detail: string;
}) {
  return (
    <div className="signal-empty">
      <Icon size={25} />
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}

function DonutBreakdown({
  data,
  formatter = (value) => value.toLocaleString(),
}: {
  data: ChartDatum[];
  formatter?: (value: number) => string;
}) {
  const chart = useChartColors();
  const colors = chart.series;
  const visible = data.filter((item) => item.value > 0);
  if (!visible.length) return null;
  return (
    <div className="donut-wrap">
      <ResponsiveContainer width="54%" height={230}>
        <PieChart>
          <Pie
            data={visible}
            dataKey="value"
            nameKey="name"
            innerRadius={58}
            outerRadius={88}
            paddingAngle={2}
          >
            {visible.map((_, index) => (
              <Cell key={index} fill={colors[index % colors.length]} />
            ))}
          </Pie>
          <Tooltip
            formatter={(value) => formatter(Number(value))}
            contentStyle={{ background: chart.surface, border: `1px solid ${chart.border}`, borderRadius: 10, color: chart.text }}
          />
        </PieChart>
      </ResponsiveContainer>
      <div className="legend">
        {visible.slice(0, 6).map((item, index) => (
          <div key={item.name}>
            <span style={{ background: colors[index % colors.length] }} />
            <label>{item.name}</label>
            <strong>{formatter(item.value)}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

export function OverviewPage({
  data,
  onNavigate,
  canManageIntegrations,
}: {
  data: Overview;
  onNavigate: (page: Page) => void;
  canManageIntegrations: boolean;
}) {
  const chart = useChartColors();
  const colors = chart.series;
  const { summary } = data;
  const hasData = summary.resourceCount > 0;
  const staleSources = data.sourceFreshness?.filter((source) => source.health !== "healthy") ?? [];
  const costDatasets = data.costDataStatus?.datasets ?? [];
  const actualCost = costDatasets.find((item) => item.source === "ActualCost");
  const incompleteCostDatasets = costDatasets.filter(
    (item) => !item.currentPeriodComplete || item.failedScopes > 0,
  );
  const commitmentCurrency = data.commitmentCoverage.currency;
  const commitmentMoney = (value: number) =>
    commitmentCurrency && commitmentCurrency !== "Mixed"
      ? currency(value, commitmentCurrency)
      : `${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}${commitmentCurrency === "Mixed" ? " (mixed currencies)" : ""}`;
  const anomalyMoney = (value: number) =>
    summary.costAnomalyCurrency && summary.costAnomalyCurrency !== "Mixed"
      ? currency(value, summary.costAnomalyCurrency)
      : `${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}${summary.costAnomalyCurrency === "Mixed" ? " (mixed currencies)" : ""}`;
  // Operational detail (per-source freshness, cost scope state, worker queue)
  // lives in Administration. The overview carries one compact notice, and only
  // when data is old enough to change how the numbers should be read.
  const STALE_NOTICE_HOURS = 24;
  const agedSources = staleSources.filter(
    (source) => (source.ageHours ?? 0) >= STALE_NOTICE_HOURS,
  );
  return (
    <>
      <PageHeader
        eyebrow="Azure estate"
        title="Cloud overview"
        description="A focused view of inventory, cost signals, utilization coverage, and actionable opportunities."
        action={canManageIntegrations ? (
          <button className="button button--secondary" onClick={() => onNavigate("integrations")}>
            <RefreshCw size={16} />
            {data.latestSync?.status === "running" ? "Sync running" : "Manage sync"}
          </button>
        ) : undefined}
        />

      {hasData && agedSources.length > 0 && (
        <div className="stale-notice">
          <AlertTriangle size={13} />
          <span>
            {/* Per-source ages: one shared "over N hours" figure took the
                oldest source's age and attributed it to every listed
                source (a 37h cost source read as 73h stale). */}
            {agedSources
              .map((source) => `${source.label} (${Math.floor(source.ageHours ?? 0)}h)`)
              .join(", ")}{" "}
            {agedSources.length === 1 ? "has" : "have"} not updated recently.
            Last-good data is shown.
          </span>
          {canManageIntegrations && (
            <button className="stale-notice__action" onClick={() => onNavigate("integrations")}>
              Details
            </button>
          )}
        </div>
      )}

      {!hasData ? (
        <Card>
          <EmptyState
            title="Connect your Azure estate"
            description={canManageIntegrations
              ? "Add subscription scopes in Integrations, then synchronize Azure Resource Graph. Flux will build the dashboard from the inventory it discovers."
              : "No Azure inventory is available yet. Ask a Flux administrator to configure and synchronize the Azure integration."}
            action={
              canManageIntegrations ? <div className="empty-actions">
                <button className="button" onClick={() => onNavigate("integrations")}>
                  Configure Azure
                </button>
              </div> : undefined
            }
          />
        </Card>
      ) : (
        <>
          <div className="metrics-grid">
            <Metric
              label="Resources"
              value={compactNumber(summary.resourceCount)}
              detail={`${summary.subscriptionCount} subscriptions · ${summary.regionCount} regions`}
              icon={Cloud}
              onClick={() => onNavigate("inventory")}
            />
            <Metric
              label="Actual cost MTD"
              value={summary.costCoverageCount ? currency(summary.estimatedMonthlyCost) : "Not connected"}
              delta={
                data.periodComparison
                  ? {
                      percent: data.periodComparison.deltaPercent,
                      title: `Finalized MTD ${currency(data.periodComparison.mtdActual)} vs ${currency(data.periodComparison.priorMtdActual)} for ${data.periodComparison.priorStart} – ${data.periodComparison.priorEnd}`,
                    }
                  : null
              }
              detail={
                actualCost
                  ? `${actualCost.currentPeriodScopes}/${actualCost.configuredScopes} subscription scopes · ${
                      actualCost.lastSuccessfulAt
                        ? `updated ${relativeTime(actualCost.lastSuccessfulAt)}`
                        : "never completed"
                    }`
                  : `${summary.costCoverageCount.toLocaleString()} of ${summary.resourceCount.toLocaleString()} resources priced`
              }
              icon={CircleDollarSign}
              tone="blue"
            />
            <Metric
              label="Avg. utilization"
              value={percent(summary.averageUtilizationPercent)}
              detail={`${summary.utilizationCoverageCount.toLocaleString()} resources with telemetry`}
              icon={Gauge}
              tone="purple"
            />
            <Metric
              label="Valued actions"
              value={compactNumber(summary.opportunityCount)}
              detail={
                summary.valuedOpportunityCount
                  ? `${currency(summary.monthlyRiskAdjustedSavings)} risk-adjusted monthly · ${summary.valuedOpportunityCount.toLocaleString()} valued`
                  : summary.estimatedMonthlySavings
                    ? `${currency(summary.estimatedMonthlySavings)} source-estimated monthly savings`
                    : "Awaiting governed valuation"
              }
              icon={Lightbulb}
              tone="amber"
              onClick={() => onNavigate("opportunities")}
            />
            <Metric
              label="Cost anomalies"
              value={compactNumber(summary.costAnomalyCount)}
              detail={
                summary.costAnomalyEvaluationDate
                  ? `${anomalyMoney(summary.costAnomalyIncrease)} daily increase · ${summary.costAnomalyEvaluationDate}`
                  : "Daily baseline not run yet"
              }
              icon={TrendingUp}
              tone="blue"
              onClick={() => onNavigate("cost-anomalies")}
            />
          </div>

          <div className="dashboard-grid">
            <Card className="chart-card">
              <ChartTitle title="Regional footprint" detail="Top Azure locations" />
              <DonutBreakdown data={data.resourcesByRegion} />
            </Card>

            <Card className="chart-card">
              <ChartTitle title="Resource mix" detail="Top resource types in the estate" />
              <div className="chart-area">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data.resourcesByType} layout="vertical" margin={{ left: 4, right: 12 }}>
                    <CartesianGrid horizontal={false} stroke="rgb(var(--border))" />
                    <XAxis type="number" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 11 }} />
                    <YAxis
                      type="category"
                      dataKey="name"
                      width={112}
                      tickLine={false}
                      axisLine={false}
                      tick={{ fill: "rgb(var(--text-muted-bright))", fontSize: 11 }}
                      tickFormatter={shortType}
                    />
                    <Tooltip contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                    <Bar dataKey="value" fill={chart.info} radius={[0, 6, 6, 0]} barSize={14} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <Card className="chart-card chart-card--wide">
              <ChartTitle title="Estate movement" detail="Unique resources observed by snapshot date" />
              <div className="chart-area">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={data.inventoryHistory}>
                    <defs>
                      <linearGradient id="inventoryFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={chart.primary} stopOpacity={0.38} />
                        <stop offset="100%" stopColor={chart.primary} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                    <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 11 }} />
                    <YAxis tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 11 }} />
                    <Tooltip contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                    <Area type="monotone" dataKey="value" stroke={chart.primary} strokeWidth={2.4} fill="url(#inventoryFill)" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <Card className="chart-card chart-card--wide">
              <ChartTitle title="Daily actual cost" detail="Thirty-day governed Cost Management history" />
              {data.dailyCostTrend.length ? (
                <div className="chart-area">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={data.dailyCostTrend}>
                      <defs>
                        <linearGradient id="dailyCostFill" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor={chart.info} stopOpacity={0.4} />
                          <stop offset="100%" stopColor={chart.info} stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                      <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} />
                      <YAxis tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} tickFormatter={(value) => compactNumber(Number(value))} />
                      <Tooltip formatter={(value) => anomalyMoney(Number(value))} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                      <Area type="monotone" dataKey="amount" stroke={chart.info} strokeWidth={2.4} fill="url(#dailyCostFill)" />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <ChartEmpty
                  icon={TrendingUp}
                  title="Daily cost history is warming up"
                  detail="The independent cost-history job backfills once, then refreshes a small rolling window."
                />
              )}
            </Card>

            <Card className="chart-card">
              <ChartTitle title="Utilization profile" detail="VMs grouped by 14-day average CPU" />
              {data.utilizationDistribution.length ? (
                <div className="chart-area">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={data.utilizationDistribution} layout="vertical" margin={{ left: 12, right: 24 }}>
                      <CartesianGrid horizontal={false} stroke="rgb(var(--border))" />
                      <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 11 }} />
                      <YAxis type="category" dataKey="name" width={112} tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted-bright))", fontSize: 11 }} />
                      <Tooltip formatter={(value) => `${Number(value).toLocaleString()} VMs`} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                      <Bar dataKey="value" radius={[0, 6, 6, 0]} barSize={18}>
                        {data.utilizationDistribution.map((_, index) => (
                          <Cell key={index} fill={colors[index % 4]} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <ChartEmpty
                  icon={Gauge}
                  title="No utilization samples yet"
                  detail="Azure Monitor CPU summaries will populate this distribution."
                />
              )}
            </Card>

            <Card className="chart-card">
              <ChartTitle title="Telemetry coverage" detail="Current Azure Monitor outcome by VM" />
              {data.telemetryCoverage.some((item) => item.value > 0) ? (
                <DonutBreakdown data={data.telemetryCoverage} />
              ) : (
                <ChartEmpty
                  icon={DatabaseZap}
                  title="No virtual machines discovered"
                  detail="Coverage appears after inventory and telemetry collection complete."
                />
              )}
            </Card>

            <Card className="chart-card">
              <ChartTitle title="Detected signal sources" detail="All active findings; use Opportunities for the prioritized queue" />
              {data.opportunitiesBySource.some((item) => item.value > 0) ? (
                <DonutBreakdown data={data.opportunitiesBySource} />
              ) : (
                <ChartEmpty
                  icon={Lightbulb}
                  title="No active findings"
                  detail="Advisor and Flux Signals findings will appear after synchronization."
                />
              )}
            </Card>

            <Card className="chart-card">
              <ChartTitle title="Cost visibility" detail="Actual month-to-date cost by service" />
              {data.costByType.length ? (
                <div className="chart-area">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={data.costByType}>
                      <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                      <XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} tickFormatter={shortType} />
                      <YAxis tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 11 }} />
                      <Tooltip formatter={(value) => currency(Number(value))} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                      <Bar dataKey="value" fill={chart.purple} radius={[6, 6, 0, 0]} maxBarSize={24} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="signal-empty">
                  <DatabaseZap size={25} />
                  <strong>No Cost Management rows yet</strong>
                  <p>Synchronize Azure to collect actual and amortized month-to-date cost with source lineage.</p>
                </div>
              )}
            </Card>

            <Card className="chart-card chart-card--wide">
              <ChartTitle title="Cost by subscription" detail="Actual versus amortized month-to-date cost" />
              {data.costBySubscription.length ? (
                <div className="chart-area">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={data.costBySubscription}
                      margin={{ left: 4, right: 16 }}
                      onClick={(state) => {
                        const index = state?.activeTooltipIndex;
                        const item = typeof index === "number" ? data.costBySubscription[index] : null;
                        if (item?.subscriptionId) {
                          navigateWithParams("reports", { subscriptionId: item.subscriptionId });
                        }
                      }}
                      style={{ cursor: "pointer" }}
                    >
                      <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                      <XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} interval={0} angle={-18} textAnchor="end" height={62} />
                      <YAxis tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 11 }} tickFormatter={(value) => compactNumber(Number(value))} />
                      <Tooltip formatter={(value) => currency(Number(value))} contentStyle={{ background: "rgb(var(--surface-raised))", border: "1px solid rgb(var(--border-bright))", borderRadius: 10, color: "rgb(var(--text))" }} />
                      <Legend wrapperStyle={{ color: "rgb(var(--text-muted-bright))", fontSize: 11 }} />
                      <Bar name="Actual" dataKey="actual" fill={chart.info} radius={[5, 5, 0, 0]} maxBarSize={24} />
                      <Bar name="Amortized" dataKey="amortized" fill={chart.purple} radius={[5, 5, 0, 0]} maxBarSize={24} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <ChartEmpty
                  icon={CircleDollarSign}
                  title="No subscription cost data yet"
                  detail="Cost Management actual and amortized results will populate this comparison."
                />
              )}
            </Card>

            <Card className="chart-card chart-card--wide">
              <ChartTitle
                title="Commitment-eligible cost mix"
                detail="Directional month-to-date Reservation and Savings Plan coverage"
              />
              {data.commitmentCoverage.status === "ready" && data.commitmentCostMix.length ? (
                <>
                  <div className="commitment-layout">
                    <DonutBreakdown
                      data={data.commitmentCostMix}
                      formatter={commitmentMoney}
                    />
                    <div className="commitment-summary">
                      <span>Directional coverage</span>
                      <strong>{percent(data.commitmentCoverage.coveragePercent)}</strong>
                      <small>
                        {commitmentMoney(data.commitmentCoverage.coveredCost)} covered ·{" "}
                        {commitmentMoney(data.commitmentCoverage.eligibleOnDemandCost)} eligible on-demand
                      </small>
                      {data.commitmentCoverage.unknownEligibilityCost > 0 && (
                        <small>
                          {commitmentMoney(data.commitmentCoverage.unknownEligibilityCost)} has no matching Toolkit meter.
                        </small>
                      )}
                    </div>
                  </div>
                  <p className="chart-method-note">{data.commitmentCoverage.method}</p>
                </>
              ) : (
                <ChartEmpty
                  icon={CircleDollarSign}
                  title={
                    data.commitmentCoverage.status === "reference_data_unavailable"
                      ? "Toolkit reference data is unavailable"
                      : "No commitment cost profile yet"
                  }
                  detail="The next Cost Management synchronization will collect Meter ID and pricing-model totals."
                />
              )}
            </Card>

            <Card className="chart-card chart-card--wide">
              <ChartTitle title="Opportunity signals" detail="Findings detectable from the current enrichment sources" />
              {data.opportunitiesByKind.length ? (
                <div className="opportunity-list">
                  {data.opportunitiesByKind.map((item, index) => (
                    <button key={item.name} onClick={() => onNavigate("opportunities")}>
                      <span className="opportunity-rank">{String(index + 1).padStart(2, "0")}</span>
                      <span>
                        <strong>{titleCase(item.name)}</strong>
                        <small>Review affected resources</small>
                      </span>
                      <b>{item.value}</b>
                      <ArrowUpRight size={17} />
                    </button>
                  ))}
                </div>
              ) : (
                <div className="signal-empty">
                  <Lightbulb size={25} />
                  <strong>No active signals</strong>
                  <p>Flux checks ARG inventory for unattached resources, inactive compute, and missing allocation tags.</p>
                </div>
              )}
            </Card>
          </div>

          <div className="freshness-bar">
            <span className={`status-dot status-dot--${data.latestSync?.status || "idle"}`} />
            <strong>{data.latestSync?.message || "Inventory is available."}</strong>
            <span>{relativeTime(data.latestSync?.completedAt || data.latestSync?.startedAt || null)}</span>
            <span className="freshness-spacer" />
            <span>{summary.tagCoveragePercent}% tag coverage</span>
          </div>
        </>
      )}
    </>
  );
}
