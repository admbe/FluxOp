import { useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line,
  Pie, PieChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { AlertTriangle, Layers, ShieldCheck, TrendingUp } from "lucide-react";
import { Card, EmptyState, PageHeader } from "../components/Ui";
import { useSemanticCatalog, useSemanticQuery, type SemanticQueryState } from "./useSemanticQuery";
import { currency, compactNumber, percent } from "../format";
import {
  axisLine, axisTick, gridProps, seriesColor, tooltipProps, useChartColors, type ChartColors,
} from "./chartTheme";
import {
  columnIndexByKind, columnIndexByName, modelLiteral, startDateForRange, typedRequest,
} from "./semanticTypes";
import type { SemanticQueryResult } from "../types";

type WindowDays = 30 | 60 | 90;

/** Sum a measure column across every row. */
function sumOf(result: SemanticQueryResult | null, measure: string): number | null {
  if (!result) return null;
  const idx = columnIndexByName(result.columns, measure);
  if (idx === -1) return null;
  let total = 0;
  let seen = false;
  for (const row of result.rows) {
    const v = Number(row[idx] ?? 0);
    if (!Number.isFinite(v)) continue;
    total += v;
    seen = true;
  }
  return seen ? total : null;
}

/** Collapse a time-grained result to one point per period for a measure. */
function seriesOf(
  result: SemanticQueryResult | null,
  measure: string,
): Array<{ date: string; value: number }> {
  if (!result) return [];
  const timeIdx = columnIndexByKind(result.columns, "time");
  const idx = columnIndexByName(result.columns, measure);
  if (timeIdx === -1 || idx === -1) return [];
  const byDate = new Map<string, number>();
  for (const row of result.rows) {
    // Time columns arrive as DATE strings or full timestamps depending on the
    // model's grain expression; normalise to YYYY-MM-DD so axis labels never
    // carry a time-of-day tail.
    const date = String(row[timeIdx] ?? "").slice(0, 10);
    if (!date) continue;
    const v = Number(row[idx] ?? 0);
    if (!Number.isFinite(v)) continue;
    byDate.set(date, (byDate.get(date) ?? 0) + v);
  }
  return [...byDate.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, value]) => ({ date, value }));
}

function ratio(numerator: number | null, denominator: number | null): number | null {
  if (numerator === null || denominator === null || denominator === 0) return null;
  return (100 * numerator) / denominator;
}

/** "Data through <date>" per source feeding this canvas. Staleness beyond the
 *  source's own completeness lag (+2 days of ingest slack) renders amber with
 *  the day count — production ran 4-5 days stale on every source and nothing
 *  on screen said so; a sagging line looked like a cost drop (#49/#82). */
function FreshnessStrip({ models }: { models: readonly string[] }) {
  const catalog = useSemanticCatalog(true);
  const items = useMemo(() => {
    const today = Date.UTC(
      new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate(),
    );
    return models
      .map((name) => catalog.catalog?.models.find((m) => m.name === name))
      .filter((m): m is NonNullable<typeof m> => Boolean(m?.dataThrough))
      .map((m) => {
        const through = Date.parse(`${m.dataThrough}T00:00:00Z`);
        const behindDays = Math.round((today - through) / 86_400_000);
        const trailing = behindDays > m.completenessLagDays + 2;
        return { name: m.displayName, through: m.dataThrough as string, behindDays, trailing };
      });
  }, [catalog.catalog, models]);
  if (!items.length) return null;
  return (
    <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", margin: "-6px 0 14px", fontSize: 12, color: "rgb(var(--text-muted))" }}>
      {items.map((item) => (
        <span key={item.name} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 7, height: 7, borderRadius: 999, background: item.trailing ? "rgb(var(--warning))" : "rgb(var(--success))" }} />
          {item.name} through {item.through}
          {item.trailing && (
            <strong style={{ color: "rgb(var(--warning))", fontWeight: 600 }}>· {item.behindDays}d behind</strong>
          )}
        </span>
      ))}
    </div>
  );
}

function KpiCard({
  icon, label, value, note, spark, color, colors,
}: {
  icon?: ReactNode;
  label: string;
  value: string;
  note: string;
  spark: Array<{ date: string; value: number }>;
  color: string;
  colors: ChartColors;
}) {
  const gradientId = useId();
  return (
    <Card style={{ padding: 14 }}>
      <div style={{ fontSize: 11, letterSpacing: 0.6, textTransform: "uppercase", color: "rgb(var(--text-muted))", display: "flex", alignItems: "center", gap: 6 }}>
        {icon} {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 700, marginTop: 6, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      <div style={{ fontSize: 12, color: "rgb(var(--text-muted))" }}>{note}</div>
      <div style={{ height: 38, marginTop: 8 }}>
        {spark.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={spark} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={color} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <Tooltip {...tooltipProps(colors)} labelFormatter={(l: unknown) => String(l)} formatter={(v: unknown) => compactNumber(Number(v))} />
              <Area type="monotone" dataKey="value" stroke={color} fill={`url(#${gradientId})`} strokeWidth={1.8} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        ) : null}
      </div>
    </Card>
  );
}

export function CanvasPage() {
  const colors = useChartColors();
  const heroGradient = useId();
  const [windowDays, setWindowDays] = useState<WindowDays>(30);
  const start = useMemo(() => startDateForRange(windowDays), [windowDays]);

  // One FOCUS query carries the whole KPI strip and the hero chart. Every
  // measure requested is additive, so the window totals below are exact rather
  // than an average of daily ratios. total_savings is SUM(list - effective),
  // which SQL evaluates per row and skips where list is NULL — that makes it
  // the like-for-like savings numerator against list_cost as denominator.
  const focus = useSemanticQuery(true, typedRequest({
    model: modelLiteral("focus_cost"),
    measures: ["billed_cost", "effective_cost", "list_cost", "total_savings", "priced_effective_cost", "commitment_covered_cost", "on_demand_cost", "charge_count"],
    grain: "day",
    start,
    end: null,
    limit: 400,
  }));

  // Currency mix for the source-health card: >1 billing currency means the
  // window totals above silently mix units.
  const currencyQuery = useSemanticQuery(true, typedRequest({
    model: modelLiteral("focus_cost"),
    measures: ["effective_cost"],
    dimensions: ["billing_currency"],
    start,
    end: null,
    limit: 50,
  }));

  const byServiceQuery = useSemanticQuery(true, typedRequest({
    model: modelLiteral("daily_cost"),
    measures: ["total_cost"],
    dimensions: ["service_name"],
    grain: "day",
    start,
    end: null,
    limit: 5000,
  }));

  const anomalyQuery = useSemanticQuery(true, typedRequest({
    model: modelLiteral("cost_anomalies"),
    measures: ["anomaly_count"],
    grain: "day",
    start,
    end: null,
    limit: 400,
  }));

  const estateQuery = useSemanticQuery(true, typedRequest({
    model: modelLiteral("inventory"),
    measures: ["resource_count"],
    dimensions: ["resource_type"],
    limit: 200,
  }));

  const billed = sumOf(focus.result, "billed_cost");
  const effective = sumOf(focus.result, "effective_cost");
  const list = sumOf(focus.result, "list_cost");
  const listSavings = sumOf(focus.result, "total_savings");
  const pricedEffective = sumOf(focus.result, "priced_effective_cost");
  const covered = sumOf(focus.result, "commitment_covered_cost");
  const onDemand = sumOf(focus.result, "on_demand_cost");
  const chargeCount = sumOf(focus.result, "charge_count");
  const pricedShare = ratio(pricedEffective, effective);
  const coveredShare = ratio(covered, effective);

  const coverage = ratio(covered, covered === null || onDemand === null ? null : covered + onDemand);
  // ESR must be like-for-like: comparing the full effective total against a
  // list total that only a fraction of charges carry produced -965% in prod.
  // total_savings already excludes rows with no list price, so this ratio is
  // savings on priced charges over the list price of those same charges.
  const esr = list !== null && list > 0 ? ratio(listSavings, list) : null;

  const billedSeries = seriesOf(focus.result, "billed_cost");
  const effectiveSeries = seriesOf(focus.result, "effective_cost");
  const coveredSeries = seriesOf(focus.result, "commitment_covered_cost");

  /** Daily effective savings rate against list price — like-for-like via
   *  total_savings (rows without a list price contribute to neither side), and
   *  only on days that carry any list price, so a sparse source reads as a
   *  short spark rather than a fabricated line. */
  const esrSeries = useMemo(() => {
    const listByDate = new Map(seriesOf(focus.result, "list_cost").map((p) => [p.date, p.value]));
    return seriesOf(focus.result, "total_savings")
      .filter((p) => (listByDate.get(p.date) ?? 0) > 0)
      .map((p) => ({ date: p.date, value: (100 * p.value) / (listByDate.get(p.date) as number) }));
  }, [focus.result]);

  /** Actual vs amortized, both read from FOCUS. These are two real measures on
   *  the same charge rows and the same currency scale, so they belong on one
   *  axis. */
  const heroSeries = useMemo(() => {
    const amortized = new Map(effectiveSeries.map((p) => [p.date, p.value]));
    return billedSeries.map((p) => ({
      date: p.date.slice(5),
      billed: p.value,
      effective: amortized.get(p.date) ?? null,
    }));
  }, [billedSeries, effectiveSeries]);
  const heroAverage = useMemo(() => {
    const values = heroSeries.map((p) => p.billed).filter((v) => Number.isFinite(v));
    return values.length ? values.reduce((s, v) => s + v, 0) / values.length : null;
  }, [heroSeries]);

  /** Per-service totals plus a genuine last-7d vs prior-7d delta, computed
   *  from the daily rows rather than invented. */
  const services = useMemo(() => {
    const result = byServiceQuery.result;
    if (!result) return [] as Array<{ name: string; value: number; delta: number | null }>;
    const timeIdx = columnIndexByKind(result.columns, "time");
    const dimIdx = columnIndexByKind(result.columns, "dimension");
    const measIdx = columnIndexByKind(result.columns, "measure");
    if (dimIdx === -1 || measIdx === -1) return [];

    const dates = timeIdx === -1
      ? []
      : [...new Set(result.rows.map((r) => String(r[timeIdx] ?? "")))].filter(Boolean).sort();
    const recent = new Set(dates.slice(-7));
    const prior = new Set(dates.slice(-14, -7));

    const total = new Map<string, number>();
    const nowWindow = new Map<string, number>();
    const wasWindow = new Map<string, number>();
    for (const row of result.rows) {
      const key = String(row[dimIdx] ?? "—");
      const v = Number(row[measIdx] ?? 0);
      if (!Number.isFinite(v)) continue;
      total.set(key, (total.get(key) ?? 0) + v);
      const date = timeIdx === -1 ? "" : String(row[timeIdx] ?? "");
      if (recent.has(date)) nowWindow.set(key, (nowWindow.get(key) ?? 0) + v);
      else if (prior.has(date)) wasWindow.set(key, (wasWindow.get(key) ?? 0) + v);
    }

    return [...total.entries()]
      .map(([name, value]) => {
        const a = nowWindow.get(name) ?? 0;
        const b = wasWindow.get(name) ?? 0;
        // With no prior week to compare against, the delta is unknown — not zero.
        const delta = prior.size === 0 || b === 0 ? null : ((a - b) / b) * 100;
        return { name, value, delta };
      })
      .sort((x, y) => y.value - x.value)
      .slice(0, 8);
  }, [byServiceQuery.result]);

  const anomalies = useMemo(() => {
    const counts = seriesOf(anomalyQuery.result, "anomaly_count");
    return counts.map((p) => ({ date: p.date.slice(5), count: p.value }));
  }, [anomalyQuery.result]);
  const anomalyTotal = anomalies.reduce((s, p) => s + p.count, 0);

  const estate = useMemo(() => {
    const result = estateQuery.result;
    if (!result) return [] as Array<{ name: string; value: number }>;
    const dimIdx = columnIndexByKind(result.columns, "dimension");
    const measIdx = columnIndexByKind(result.columns, "measure");
    if (dimIdx === -1 || measIdx === -1) return [];
    const map = new Map<string, number>();
    for (const row of result.rows) {
      const key = String(row[dimIdx] ?? "—");
      const v = Number(row[measIdx] ?? 0);
      if (!Number.isFinite(v)) continue;
      map.set(key, (map.get(key) ?? 0) + v);
    }
    const ordered = [...map.entries()].sort((a, b) => b[1] - a[1]);
    // Six is the readable ceiling for a part-to-whole ring; the tail is one
    // "Other" slice rather than a dozen unlabelled slivers.
    const head = ordered.slice(0, 5).map(([name, value]) => ({ name, value }));
    const tail = ordered.slice(5).reduce((s, [, v]) => s + v, 0);
    return tail > 0 ? [...head, { name: "Other", value: tail }] : head;
  }, [estateQuery.result]);
  const estateKeys = estate.map((e) => e.name);
  const estateTotal = estate.reduce((s, e) => s + e.value, 0);

  const currencies = useMemo(() => {
    const result = currencyQuery.result;
    if (!result) return [] as Array<{ code: string; share: number }>;
    const dimIdx = columnIndexByKind(result.columns, "dimension");
    const measIdx = columnIndexByKind(result.columns, "measure");
    if (dimIdx === -1 || measIdx === -1) return [];
    const totals = new Map<string, number>();
    let sum = 0;
    for (const row of result.rows) {
      const code = String(row[dimIdx] ?? "").trim() || "—";
      const v = Number(row[measIdx] ?? 0);
      if (!Number.isFinite(v) || v <= 0) continue;
      totals.set(code, (totals.get(code) ?? 0) + v);
      sum += v;
    }
    return [...totals.entries()]
      .map(([code, v]) => ({ code, share: sum > 0 ? (100 * v) / sum : 0 }))
      .sort((a, b) => b.share - a.share);
  }, [currencyQuery.result]);

  const grid = gridProps(colors);
  const tick = axisTick(colors);
  const tip = tooltipProps(colors);
  const money = (v: unknown) => currency(Number(v as number));
  const loadingFocus = focus.status === "loading" && focus.result === null;

  return (
    <div className="page canvas-page">
      <PageHeader
        eyebrow="FinOps · Executive Canvas"
        title="Command Center"
        description={`One viewport: billed against amortized, commitment position, movers, anomalies and estate mix — every tile read from the governed semantic layer over the last ${windowDays} days.`}
        action={
          <div style={{ display: "flex", gap: 6 }}>
            {([30, 60, 90] as const).map((d) => (
              <button
                key={d}
                onClick={() => setWindowDays(d)}
                aria-pressed={windowDays === d}
                style={{ padding: "6px 12px", borderRadius: 999, border: "1px solid rgb(var(--border))", background: windowDays === d ? "rgb(var(--primary) / 0.14)" : "rgb(var(--surface))", cursor: "pointer", fontSize: 13, fontWeight: windowDays === d ? 600 : 400 }}
              >
                {d}d
              </button>
            ))}
          </div>
        }
      />

      <FreshnessStrip models={["focus_cost", "daily_cost", "cost_anomalies"]} />

      <div style={{ opacity: focus.status === "loading" && focus.result !== null ? 0.6 : 1, transition: "opacity 120ms ease" }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 12, marginBottom: 14 }}>
          <KpiCard
            colors={colors}
            color={colors.series[0]}
            icon={<TrendingUp size={12} />}
            label={`Billed cost · ${windowDays}d`}
            value={billed === null ? (loadingFocus ? "…" : "—") : currency(billed)}
            note={focus.status === "error" ? "source unavailable" : "FOCUS BilledCost"}
            spark={billedSeries}
          />
          <KpiCard
            colors={colors}
            color={colors.series[1]}
            label={`Effective cost · ${windowDays}d`}
            value={effective === null ? (loadingFocus ? "…" : "—") : currency(effective)}
            note={
              billed === null || effective === null
                ? "FOCUS EffectiveCost"
                : `${currency(effective - billed)} against billed`
            }
            spark={effectiveSeries}
          />
          <KpiCard
            colors={colors}
            color={colors.series[2]}
            icon={<ShieldCheck size={12} />}
            label="Commitment coverage"
            value={coverage === null ? (loadingFocus ? "…" : "—") : percent(coverage)}
            note={
              covered === null || onDemand === null
                ? "needs FOCUS commitment fields"
                : `${currency(onDemand)} still on demand`
            }
            spark={coveredSeries}
          />
          <KpiCard
            colors={colors}
            color={colors.series[3]}
            icon={<AlertTriangle size={12} />}
            label="Effective savings rate"
            value={esr === null ? (loadingFocus ? "…" : "—") : percent(esr)}
            note={
              list === null || list <= 0
                ? "list price missing at source"
                : `like-for-like · list on ${pricedShare === null ? "?" : percent(pricedShare)} of spend`
            }
            spark={esrSeries}
          />
        </div>

        <Card style={{ padding: 14, marginBottom: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
            <h3 style={{ fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
              <Layers size={14} /> Billed against effective — last {windowDays}d
              <span style={{ fontWeight: 400, color: "rgb(var(--text-muted))", fontSize: 11, fontFamily: "monospace" }}>semantic_focus_cost · day</span>
            </h3>
            <div style={{ display: "flex", gap: 14, fontSize: 12, color: "rgb(var(--text-muted))" }}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span style={{ width: 9, height: 9, borderRadius: 999, background: colors.series[0] }} /> Billed
              </span>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span style={{ width: 9, height: 9, borderRadius: 999, background: colors.series[1] }} /> Effective
              </span>
            </div>
          </div>
          {focus.status === "error" ? (
            <SourceProblem state={focus} what="FOCUS cost" />
          ) : heroSeries.length > 1 ? (
            <div style={{ width: "100%", height: 300, marginTop: 8 }}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={heroSeries} margin={{ left: 4, right: 12, top: 6, bottom: 0 }}>
                  <defs>
                    <linearGradient id={heroGradient} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={colors.series[0]} stopOpacity={0.22} />
                      <stop offset="100%" stopColor={colors.series[0]} stopOpacity={0.01} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid {...grid} />
                  <XAxis dataKey="date" tick={tick} axisLine={axisLine(colors)} tickLine={false} minTickGap={24} />
                  <YAxis tick={tick} axisLine={false} tickLine={false} tickFormatter={(v: number) => compactNumber(v)} width={54} />
                  <Tooltip {...tip} formatter={money} />
                  {heroAverage !== null && (
                    <ReferenceLine
                      y={heroAverage}
                      stroke={colors.muted}
                      strokeDasharray="5 4"
                      label={{ value: `avg ${compactNumber(heroAverage)}`, position: "insideTopRight", fill: colors.muted, fontSize: 10.5 }}
                    />
                  )}
                  <Area type="monotone" dataKey="billed" name="Billed" stroke={colors.series[0]} strokeWidth={2.2} fill={`url(#${heroGradient})`} dot={false} />
                  <Line type="monotone" dataKey="effective" name="Effective" stroke={colors.series[1]} strokeWidth={2.2} strokeLinecap="round" dot={false} connectNulls={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <EmptyState title="No FOCUS charges in this window" description={`The snapshot carries no FOCUS rows for the last ${windowDays} days.`} />
          )}
        </Card>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 12, marginBottom: 12 }}>
          <Card style={{ padding: 14 }}>
            <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Cost by service · top 8</h3>
            {byServiceQuery.status === "error" ? (
              <SourceProblem state={byServiceQuery} what="daily cost" />
            ) : services.length ? (
              <div style={{ width: "100%", height: Math.max(220, services.length * 30) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={services} layout="vertical" margin={{ left: 110, right: 16 }}>
                    <CartesianGrid {...grid} vertical horizontal={false} />
                    <XAxis type="number" tick={tick} axisLine={false} tickLine={false} tickFormatter={(v: number) => compactNumber(v)} />
                    <YAxis type="category" dataKey="name" tick={tick} axisLine={false} tickLine={false} width={108} />
                    <Tooltip {...tip} formatter={money} />
                    <Bar dataKey="value" name="Cost" fill={colors.series[0]} radius={[0, 5, 5, 0]} maxBarSize={20} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <EmptyState title="No cost rows" description="No daily cost in this window." />
            )}
          </Card>

          <Card style={{ padding: 14 }}>
            <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Biggest movers · last 7d against prior 7d</h3>
            {services.length ? (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr style={{ textAlign: "left", color: "rgb(var(--text-muted))", fontSize: 11 }}>
                    <th style={{ padding: "6px 8px" }}>Service</th>
                    <th style={{ padding: "6px 8px", textAlign: "right" }}>{windowDays}d</th>
                    <th style={{ padding: "6px 8px", textAlign: "right" }}>Δ</th>
                  </tr>
                </thead>
                <tbody>
                  {[...services]
                    .sort((a, b) => Math.abs(b.delta ?? 0) - Math.abs(a.delta ?? 0))
                    .slice(0, 6)
                    .map((row) => (
                      <tr key={row.name} style={{ borderTop: "1px solid rgb(var(--border) / 0.6)" }}>
                        <td style={{ padding: 8 }}>{row.name}</td>
                        <td style={{ padding: 8, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{currency(row.value)}</td>
                        <td style={{ padding: 8, textAlign: "right" }}>
                          {row.delta === null ? (
                            <span style={{ color: "rgb(var(--text-muted))" }}>—</span>
                          ) : (
                            <span style={{
                              display: "inline-flex", padding: "2px 8px", borderRadius: 999, fontSize: 12,
                              fontVariantNumeric: "tabular-nums",
                              background: row.delta > 6 ? "rgb(var(--danger) / 0.14)" : row.delta < -6 ? "rgb(var(--success) / 0.14)" : "rgb(var(--surface-soft))",
                              color: row.delta > 6 ? "rgb(var(--danger))" : row.delta < -6 ? "rgb(var(--success))" : "inherit",
                            }}>
                              {row.delta > 0 ? "+" : ""}{row.delta.toFixed(1)}%
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            ) : (
              <EmptyState title="No movers" description="Not enough history in the window to compare two weeks." />
            )}
            <p style={{ fontSize: 11, color: "rgb(var(--text-muted))", marginTop: 8 }}>
              Up is red because this is cost. A service with no spend in the prior week shows “—” rather than an infinite increase.
            </p>
          </Card>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))", gap: 12 }}>
          <Card style={{ padding: 14 }}>
            <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
              Anomalies detected · <span style={{ fontWeight: 400, color: "rgb(var(--text-muted))", fontSize: 11, fontFamily: "monospace" }}>semantic_cost_anomalies</span>
            </h3>
            {anomalyQuery.status === "error" ? (
              <SourceProblem state={anomalyQuery} what="cost anomalies" />
            ) : anomalies.length ? (
              <>
                <div style={{ width: "100%", height: 220 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={anomalies} margin={{ left: 4, right: 12, top: 6, bottom: 0 }}>
                      <CartesianGrid {...grid} />
                      <XAxis dataKey="date" tick={tick} axisLine={axisLine(colors)} tickLine={false} minTickGap={20} />
                      <YAxis tick={tick} axisLine={false} tickLine={false} allowDecimals={false} />
                      <Tooltip {...tip} />
                      <ReferenceLine y={0} stroke={colors.gridline} />
                      <Bar dataKey="count" name="Anomalies" fill={colors.series[4]} radius={[4, 4, 0, 0]} maxBarSize={18} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
                <p style={{ fontSize: 11, color: "rgb(var(--text-muted))", marginTop: 6 }}>
                  {anomalyTotal} detection{anomalyTotal === 1 ? "" : "s"} in the window. The detector already excludes the partial latest day.
                </p>
              </>
            ) : (
              <EmptyState title="No anomalies" description="Nothing crossed the detector's threshold in this window." />
            )}
          </Card>

          <Card style={{ padding: 14 }}>
            <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Estate mix · by resource type</h3>
            {estateQuery.status === "error" ? (
              <SourceProblem state={estateQuery} what="inventory" />
            ) : estate.length ? (
              <>
                <div style={{ width: "100%", height: 220 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={estate} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={58} outerRadius={92} paddingAngle={2} stroke={colors.background} strokeWidth={2}>
                        {estate.map((entry) => (
                          <Cell key={entry.name} fill={seriesColor(colors, estateKeys, entry.name)} />
                        ))}
                      </Pie>
                      <Tooltip {...tip} formatter={(v: unknown) => compactNumber(Number(v))} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", justifyContent: "center", fontSize: 11.5, color: "rgb(var(--text-muted))", marginTop: 4 }}>
                  {estate.map((entry) => (
                    <span key={entry.name} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                      <span style={{ width: 8, height: 8, borderRadius: 999, background: seriesColor(colors, estateKeys, entry.name) }} />
                      {entry.name}
                    </span>
                  ))}
                </div>
                <p style={{ fontSize: 11, color: "rgb(var(--text-muted))", marginTop: 6, textAlign: "center" }}>
                  {compactNumber(estateTotal)} resources · largest <strong>{estate[0]?.name ?? "—"}</strong>
                </p>
              </>
            ) : (
              <EmptyState title="No inventory" description="The snapshot carries no current resources." />
            )}
          </Card>

          <Card style={{ padding: 14 }}>
            <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
              FOCUS source health · <span style={{ fontWeight: 400, color: "rgb(var(--text-muted))", fontSize: 11, fontFamily: "monospace" }}>semantic_focus_cost</span>
            </h3>
            {effective === null || effective <= 0 ? (
              <EmptyState title="No FOCUS charges" description="Nothing to diagnose in this window." />
            ) : (
              <div style={{ display: "grid", gap: 10, fontSize: 12.5 }}>
                <HealthRow
                  label="List price present"
                  value={pricedShare === null ? "—" : percent(pricedShare)}
                  warn={pricedShare !== null && pricedShare < 50}
                  note={
                    pricedShare !== null && pricedShare < 50
                      ? "ESR and savings figures only cover this slice of spend"
                      : "share of effective spend the ESR comparison covers"
                  }
                />
                <HealthRow
                  label="Commitment fields present"
                  value={coveredShare === null ? "—" : percent(coveredShare)}
                  warn={coveredShare !== null && coveredShare < 1}
                  note={
                    coveredShare !== null && coveredShare < 1
                      ? "export carries almost no CommitmentDiscount columns — coverage reads near zero"
                      : "share of effective spend carrying a commitment discount"
                  }
                />
                <HealthRow
                  label="Billing currencies"
                  value={currencies.length ? currencies.slice(0, 3).map((c) => `${c.code} ${c.share.toFixed(0)}%`).join(" · ") : "—"}
                  warn={currencies.length > 1}
                  note={currencies.length > 1 ? "totals on this page mix currencies — filter before trusting sums" : "single-currency window"}
                />
                <HealthRow
                  label="Charge lines"
                  value={chargeCount === null ? "—" : compactNumber(chargeCount)}
                  warn={false}
                  note={`in the last ${windowDays} days`}
                />
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function HealthRow({ label, value, note, warn }: { label: string; value: string; note: string; warn: boolean }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "10px 1fr auto", gap: 8, alignItems: "baseline" }}>
      <span style={{ width: 7, height: 7, borderRadius: 999, alignSelf: "center", background: warn ? "rgb(var(--warning))" : "rgb(var(--success))" }} />
      <div>
        <div style={{ fontWeight: 600 }}>{label}</div>
        <div style={{ fontSize: 11.5, color: warn ? "rgb(var(--warning))" : "rgb(var(--text-muted))" }}>{note}</div>
      </div>
      <strong style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{value}</strong>
    </div>
  );
}

function SourceProblem({ state, what }: { state: SemanticQueryState; what: string }) {
  const catching = state.httpStatus === 503;
  return (
    <EmptyState
      title={catching ? "Data is catching up" : `Could not load ${what}`}
      description={
        catching
          ? "The analytics snapshot has not finished publishing. Reload in a moment."
          : (state.error ?? "The semantic layer refused the request.")
      }
    />
  );
}
