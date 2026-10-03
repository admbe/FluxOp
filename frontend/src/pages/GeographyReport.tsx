import { Globe2, Percent, TrendingUp } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api, isAbortError } from "../api";
import { compactNumber } from "../format";
import { useChartColors } from "../theme";
import type { GeographyReport as GeographyReportData } from "../types";
import { Card, EmptyState, ErrorPanel, Loading } from "../components/Ui";

function money(value: number | null | undefined, code: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: code || "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

const AXIS_TICK = { fill: "rgb(var(--text-muted))", fontSize: 10 } as const;
const TOOLTIP_STYLE = {
  background: "rgb(var(--surface-raised))",
  border: "1px solid rgb(var(--border-bright))",
  borderRadius: 10,
  color: "rgb(var(--text))",
} as const;

/** Axis label helpers so every chart states its bearings (report feedback
 *  2026-08-10: charts shipped with no X/Y orientation at all). */
const xLabel = (text: string) => ({
  value: text,
  position: "insideBottom" as const,
  offset: -2,
  fill: "rgb(var(--text-muted))",
  fontSize: 10,
});
const yLabel = (text: string) => ({
  value: text,
  angle: -90,
  position: "insideLeft" as const,
  fill: "rgb(var(--text-muted))",
  fontSize: 10,
});

export function GeographyReport({ active }: { active: boolean }) {
  const chart = useChartColors();
  const [data, setData] = useState<GeographyReportData | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!active || loaded) return;
    setLoaded(true);
    const ac = new AbortController();
    api
      .geographyReport({ signal: ac.signal })
      .then((value) => {
        if (ac.signal.aborted) return;
        setData(value);
      })
      .catch((reason) => {
        if (isAbortError(reason)) return;
        setError(
          reason instanceof Error
            ? reason.message
            : "The geography report could not be loaded.",
        );
      });
    return () => ac.abort();
  }, [active, loaded]);

  const geoColor = useMemo(() => {
    const palette: Record<string, string> = {};
    (data?.geographies ?? []).forEach((geo, index) => {
      palette[geo.key] = chart.series[index % chart.series.length];
    });
    return palette;
  }, [data, chart]);

  const geoLabel = useMemo(
    () =>
      Object.fromEntries(
        (data?.geographies ?? []).map((geo) => [geo.key, geo.label]),
      ) as Record<string, string>,
    [data],
  );

  if (error) return <ErrorPanel message={error} />;
  if (!data) return <Loading />;
  if (data.status !== "ok") {
    return (
      <EmptyState
        title="No FOCUS charge data yet"
        description="The geography report reads charge-level FOCUS exports; run a cost-details synchronization to populate it."
      />
    );
  }

  const code = data.currency;
  const activeGeos = data.geographies.filter((geo) =>
    data.trend.some((row) => (row.byGeo[geo.key] ?? 0) > 0.005),
  );
  const trendRows = data.trend.map((row) => ({ ...row.byGeo, month: row.month }));
  const shareRows = activeGeos
    .map((geo) => ({
      label: geo.label,
      key: geo.key,
      amount: data.share[geo.key] ?? 0,
    }))
    .sort((a, b) => b.amount - a.amount);
  const shareTotal = shareRows.reduce((sum, row) => sum + row.amount, 0);
  const esrRows = activeGeos
    .map((geo) => ({
      label: geo.label,
      key: geo.key,
      esr: data.pricingByGeo[geo.key]?.esr ?? null,
      pricedShare: data.pricingByGeo[geo.key]?.pricedShare ?? null,
    }))
    .filter((row) => row.esr !== null)
    .sort((a, b) => (b.esr ?? 0) - (a.esr ?? 0));
  const pricingRows = activeGeos.map((geo) => ({
    label: geo.label,
    key: geo.key,
    List: data.pricingByGeo[geo.key]?.list ?? 0,
    Contracted: data.pricingByGeo[geo.key]?.contracted ?? 0,
    Effective: data.pricingByGeo[geo.key]?.effective ?? 0,
  }));
  const moverRows = data.movers
    .filter((row) => row.previous > 0.005 || row.current > 0.005)
    .map((row) => ({ ...row, label: geoLabel[row.geo] ?? row.geo }));
  const domainLabel = Object.fromEntries(
    data.domains.map((domain) => [domain.key, domain.label]),
  ) as Record<string, string>;
  const activeDomains = data.domains.filter((domain) =>
    activeGeos.some((geo) => (data.matrix[geo.key]?.[domain.key] ?? 0) > 0.005),
  );
  const compositionRows = activeGeos
    .map((geo) => {
      const byDomain = data.matrix[geo.key] ?? {};
      const total = Object.values(byDomain).reduce((sum, value) => sum + value, 0);
      const row: Record<string, number | string> = { label: geo.label };
      for (const domain of activeDomains) {
        row[domain.key] = total > 0 ? ((byDomain[domain.key] ?? 0) / total) * 100 : 0;
      }
      return { ...row, total };
    })
    .filter((row) => (row.total as number) > 0.005);
  const domainRows = data.domains
    .map((domain) => ({
      label: domain.label,
      key: domain.key,
      effective: data.pricingByDomain[domain.key]?.effective ?? 0,
      esr: data.pricingByDomain[domain.key]?.esr ?? null,
    }))
    .filter((row) => row.effective > 0.005)
    .sort((a, b) => b.effective - a.effective);

  return (
    <>
      <div className="report-metrics-grid">
        <Card className="report-metric">
          <Globe2 size={18} />
          <span>Effective spend · {data.months.length} months</span>
          <strong>{money(data.totals.effective, code)}</strong>
          <small>{activeGeos.length} active geographies</small>
        </Card>
        <Card className="report-metric">
          <Percent size={18} />
          <span>Effective Savings Rate</span>
          <strong>{data.totals.esr !== null ? `${data.totals.esr}%` : "—"}</strong>
          <small>covers {data.totals.pricedShare ?? 0}% of spend with a list price</small>
        </Card>
        <Card className="report-metric">
          <TrendingUp size={18} />
          <span>{data.shareMonth} spend</span>
          <strong>{money(shareTotal, code)}</strong>
          <small>
            {data.latestMonthComplete
              ? "latest complete month"
              : `latest complete month (${data.latestMonth} still collecting)`}
          </small>
        </Card>
      </div>

      <div className="report-grid">
        <Card className="chart-card chart-card--wide">
          <div className="chart-title">
            <div>
              <h2>Geographic cost trend</h2>
              <p>Monthly effective cost, stacked by geography.</p>
            </div>
            <Globe2 size={18} />
          </div>
          <div className="chart-area">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={trendRows} margin={{ bottom: 14, left: 8 }}>
                <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                <XAxis
                  dataKey="month"
                  tickLine={false}
                  axisLine={false}
                  tick={AXIS_TICK}
                  label={xLabel("Month")}
                />
                <YAxis
                  tickFormatter={(value) => compactNumber(Number(value))}
                  tickLine={false}
                  axisLine={false}
                  tick={AXIS_TICK}
                  label={yLabel(`Effective cost (${code})`)}
                />
                <Tooltip
                  formatter={(value, name) => [money(Number(value), code), geoLabel[String(name)] ?? name]}
                  contentStyle={TOOLTIP_STYLE}
                />
                <Legend formatter={(value) => geoLabel[String(value)] ?? value} />
                {activeGeos.map((geo) => (
                  <Bar
                    key={geo.key}
                    dataKey={geo.key}
                    stackId="trend"
                    fill={geoColor[geo.key]}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="chart-card">
          <div className="chart-title">
            <div>
              <h2>Share by geography — {data.shareMonth}</h2>
              <p>Latest complete month of effective cost.</p>
            </div>
          </div>
          <div className="chart-area">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={shareRows} layout="vertical" margin={{ bottom: 14, left: 14 }}>
                <CartesianGrid horizontal={false} stroke="rgb(var(--border))" />
                <XAxis
                  type="number"
                  tickFormatter={(value) => compactNumber(Number(value))}
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  label={xLabel(`Effective cost (${code})`)}
                />
                <YAxis type="category" dataKey="label" width={120} tick={AXIS_TICK} tickLine={false} axisLine={false} />
                <Tooltip
                  formatter={(value) => [
                    `${money(Number(value), code)} · ${shareTotal > 0 ? ((Number(value) / shareTotal) * 100).toFixed(1) : 0}%`,
                    "Effective cost",
                  ]}
                  contentStyle={TOOLTIP_STYLE}
                />
                <Bar dataKey="amount">
                  {shareRows.map((row) => (
                    <Cell key={row.key} fill={geoColor[row.key]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="chart-card">
          <div className="chart-title">
            <div>
              <h2>Effective Savings Rate by geography</h2>
              <p>1 − effective/list over charges carrying a list price; hover for coverage.</p>
            </div>
          </div>
          <div className="chart-area">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={esrRows} layout="vertical" margin={{ bottom: 14, left: 14 }}>
                <CartesianGrid horizontal={false} stroke="rgb(var(--border))" />
                <XAxis
                  type="number"
                  domain={[0, "auto"]}
                  tickFormatter={(value) => `${value}%`}
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  label={xLabel("ESR (% off list)")}
                />
                <YAxis type="category" dataKey="label" width={120} tick={AXIS_TICK} tickLine={false} axisLine={false} />
                <Tooltip
                  formatter={(value, _name, item) => [
                    `${Number(value).toFixed(1)}% · list price covers ${item?.payload?.pricedShare ?? "—"}% of this geography's spend`,
                    "ESR",
                  ]}
                  contentStyle={TOOLTIP_STYLE}
                />
                <Bar dataKey="esr" fill={chart.primary} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="chart-card chart-card--wide">
          <div className="chart-title">
            <div>
              <h2>List vs Contracted vs Effective</h2>
              <p>What the estate would cost at list, at negotiated rates, and what it actually costs.</p>
            </div>
          </div>
          <div className="chart-area">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={pricingRows} margin={{ bottom: 14, left: 8 }}>
                <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={false} label={xLabel("Geography")} />
                <YAxis
                  tickFormatter={(value) => compactNumber(Number(value))}
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  label={yLabel(`Cost, ${data.months.length}-month window (${code})`)}
                />
                <Tooltip formatter={(value) => money(Number(value), code)} contentStyle={TOOLTIP_STYLE} />
                <Legend />
                <Bar dataKey="List" fill={chart.muted} />
                <Bar dataKey="Contracted" fill={chart.info} />
                <Bar dataKey="Effective" fill={chart.primary} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="chart-card">
          <div className="chart-title">
            <div>
              <h2>Biggest geographic movers</h2>
              <p>
                {data.movers.length >= 2
                  ? "Month-over-month change, latest two complete months."
                  : "Needs two complete months of charges."}
              </p>
            </div>
          </div>
          <div className="chart-area">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={moverRows} layout="vertical" margin={{ bottom: 14, left: 14 }}>
                <CartesianGrid horizontal={false} stroke="rgb(var(--border))" />
                <XAxis
                  type="number"
                  tickFormatter={(value) => compactNumber(Number(value))}
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  label={xLabel(`Δ effective cost, MoM (${code})`)}
                />
                <YAxis type="category" dataKey="label" width={120} tick={AXIS_TICK} tickLine={false} axisLine={false} />
                <Tooltip
                  formatter={(value, _name, item) => [
                    `${money(Number(value), code)} (${item?.payload?.deltaPercent ?? "—"}%)`,
                    "Change",
                  ]}
                  contentStyle={TOOLTIP_STYLE}
                />
                <Bar dataKey="delta">
                  {moverRows.map((row) => (
                    <Cell key={row.geo} fill={row.delta >= 0 ? chart.danger : chart.primary} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="chart-card chart-card--wide">
          <div className="chart-title">
            <div>
              <h2>Cost domain composition — {data.shareMonth}</h2>
              <p>Each geography's spend as a 100% stack of its cost domains.</p>
            </div>
          </div>
          <div className="chart-area">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={compositionRows} margin={{ bottom: 14, left: 8 }}>
                <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={false} label={xLabel("Geography")} />
                <YAxis
                  domain={[0, 100]}
                  tickFormatter={(value) => `${value}%`}
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  label={yLabel("Share of geography spend (%)")}
                />
                <Tooltip
                  formatter={(value, name) => [
                    `${Number(value).toFixed(1)}%`,
                    domainLabel[String(name)] ?? name,
                  ]}
                  contentStyle={TOOLTIP_STYLE}
                />
                <Legend formatter={(value) => domainLabel[String(value)] ?? value} />
                {activeDomains.map((domain, index) => (
                  <Bar
                    key={domain.key}
                    dataKey={domain.key}
                    stackId="mix"
                    fill={chart.series[index % chart.series.length]}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="report-table-card chart-card--wide">
          <div className="chart-title">
            <div>
              <h2>Cost domain × efficiency</h2>
              <p>Where the spend concentrates, and how well each domain is bought.</p>
            </div>
          </div>
          <div className="report-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Cost domain</th>
                  <th>Effective cost</th>
                  <th>Share</th>
                  <th>ESR</th>
                </tr>
              </thead>
              <tbody>
                {domainRows.map((row) => (
                  <tr key={row.key}>
                    <td>{row.label}</td>
                    <td>{money(row.effective, code)}</td>
                    <td>
                      {data.totals.effective > 0
                        ? `${((row.effective / data.totals.effective) * 100).toFixed(1)}%`
                        : "—"}
                    </td>
                    <td>{row.esr !== null ? `${row.esr}%` : "no list price"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card className="report-table-card chart-card--wide">
          <div className="chart-title">
            <div>
              <h2>Geography × cost domain — {data.shareMonth}</h2>
              <p>Effective cost for the latest complete month.</p>
            </div>
          </div>
          <div className="report-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Geography</th>
                  {data.domains.map((domain) => (
                    <th key={domain.key}>{domain.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {activeGeos.map((geo) => (
                  <tr key={geo.key}>
                    <td>{geo.label}</td>
                    {data.domains.map((domain) => {
                      const value = data.matrix[geo.key]?.[domain.key] ?? 0;
                      return (
                        <td key={domain.key}>
                          {value > 0.005 ? money(value, code) : "—"}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </>
  );
}
