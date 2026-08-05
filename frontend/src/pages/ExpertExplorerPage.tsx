import { Check, Code2, Download, Send, Sparkles, Wand2 } from "lucide-react";
import { useState } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart,
  Legend, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { api } from "../api";
import { useChartColors } from "../theme";
import type { ExpertExplorerResult } from "../types";
import { Card, PageHeader } from "../components/Ui";

type Exchange = {
  question: string;
  result?: ExpertExplorerResult;
  error?: string;
};

function toObjects(result: ExpertExplorerResult): Record<string, unknown>[] {
  return result.rows.map((row) =>
    Object.fromEntries(result.columns.map((name, index) => [name, row[index]])),
  );
}

/** Pivot long rows (xKey, seriesKey, value) into wide rows for stacking. */
function pivotForStack(
  rows: Record<string, unknown>[],
  xKey: string,
  seriesKey: string,
  valueKey: string,
): { data: Record<string, unknown>[]; series: string[] } {
  const seriesTotals = new Map<string, number>();
  for (const row of rows) {
    const series = String(row[seriesKey] ?? "");
    seriesTotals.set(
      series,
      (seriesTotals.get(series) ?? 0) + Math.abs(Number(row[valueKey]) || 0),
    );
  }
  const top = [...seriesTotals.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([name]) => name);
  const byX = new Map<string, Record<string, unknown>>();
  for (const row of rows) {
    const x = String(row[xKey] ?? "");
    const series = String(row[seriesKey] ?? "");
    const bucket = byX.get(x) ?? { [xKey]: x };
    const key = top.includes(series) ? series : "Other";
    bucket[key] = (Number(bucket[key]) || 0) + (Number(row[valueKey]) || 0);
    byX.set(x, bucket);
  }
  const series = [...top, ...(byX.size && seriesTotals.size > top.length ? ["Other"] : [])];
  return { data: [...byX.values()], series };
}

function download(name: string, mime: string, content: string) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([content], { type: mime }));
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}

function csvOf(result: ExpertExplorerResult): string {
  const escape = (value: unknown) => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [
    result.columns.map(escape).join(","),
    ...result.rows.map((row) => row.map(escape).join(",")),
  ].join("\n");
}

function ExchangeChart({ result }: { result: ExpertExplorerResult }) {
  const chart = useChartColors();
  const rows = toObjects(result);
  const tooltipStyle = {
    background: chart.surface,
    border: `1px solid ${chart.border}`,
    borderRadius: 8,
    color: chart.text,
    fontSize: 11,
  };
  if (result.chartType === "table" || !result.xKey || !result.yKeys.length) {
    return null;
  }
  const axes = (
    <>
      <CartesianGrid stroke={chart.border} vertical={false} />
      <XAxis dataKey={result.xKey} stroke={chart.muted} tick={{ fontSize: 10 }} minTickGap={30} />
      <YAxis stroke={chart.muted} tick={{ fontSize: 10 }} width={68} />
      <Tooltip contentStyle={tooltipStyle} />
      <Legend wrapperStyle={{ fontSize: 10 }} />
    </>
  );
  if (result.chartType === "stacked-bar" && result.seriesKey) {
    const { data, series } = pivotForStack(
      rows, result.xKey, result.seriesKey, result.yKeys[0],
    );
    return (
      <ResponsiveContainer width="100%" height={260}>
        <BarChart data={data}>
          {axes}
          {series.map((name, index) => (
            <Bar key={name} dataKey={name} stackId="stack" fill={chart.series[index % chart.series.length]} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    );
  }
  if (result.chartType === "bar") {
    return (
      <ResponsiveContainer width="100%" height={260}>
        <BarChart data={rows}>
          {axes}
          {result.yKeys.map((key, index) => (
            <Bar key={key} dataKey={key} fill={chart.series[index % chart.series.length]} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    );
  }
  if (result.chartType === "area") {
    return (
      <ResponsiveContainer width="100%" height={260}>
        <AreaChart data={rows}>
          {axes}
          {result.yKeys.map((key, index) => (
            <Area key={key} type="monotone" dataKey={key} stroke={chart.series[index % chart.series.length]} fill={chart.series[index % chart.series.length]} fillOpacity={0.14} />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={rows}>
        {axes}
        {result.yKeys.map((key, index) => (
          <Line key={key} type="monotone" dataKey={key} stroke={chart.series[index % chart.series.length]} strokeWidth={1.8} dot={false} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

export function ExpertExplorerPage({ onBuilder }: { onBuilder: () => void }) {
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [copiedSql, setCopiedSql] = useState("");

  async function ask() {
    const trimmed = question.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setQuestion("");
    const history = exchanges
      .filter((item) => item.result)
      .slice(-4)
      .map((item) => ({ question: item.question, sql: item.result!.sql }));
    setExchanges((current) => [...current, { question: trimmed }]);
    try {
      const result = await api.semanticExpert(trimmed, history);
      setExchanges((current) =>
        current.map((item, index) =>
          index === current.length - 1 ? { ...item, result } : item,
        ),
      );
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "The request failed.";
      setExchanges((current) =>
        current.map((item, index) =>
          index === current.length - 1 ? { ...item, error: message } : item,
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="FinOps"
        title="Expert explorer"
        description="Ask in plain language. Flux generates governed, validated, read-only SQL over the semantic views, runs it with row and time limits, and shows the query, results, chart, and assumptions."
        action={
          <button className="button button--secondary" onClick={onBuilder}>
            Back to builder
          </button>
        }
      />
      <Card>
        <div className="expert-composer">
          <Wand2 size={16} />
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter") void ask(); }}
            placeholder="e.g. Monthly amortized cost by service for the last 3 months, stacked"
            disabled={busy}
          />
          <button className="button" onClick={() => void ask()} disabled={busy || !question.trim()}>
            <Send size={15} />{busy ? "Working…" : "Ask"}
          </button>
        </div>
        {!exchanges.length && (
          <p className="muted expert-hint">
            Try: “Which subscriptions grew fastest month over month?” · “Top 15
            resource groups by amortized cost this month, bar chart” · “Daily
            actual vs amortized trend for the last 60 days”. Refine
            conversationally — follow-ups keep the previous query as context.
          </p>
        )}
        {exchanges.map((exchange, index) => (
          <div className="expert-exchange" key={index}>
            <div className="expert-question"><Sparkles size={13} /> {exchange.question}</div>
            {exchange.error && <div className="inline-alert inline-alert--error">{exchange.error}</div>}
            {!exchange.error && !exchange.result && <p className="muted">Generating and validating SQL…</p>}
            {exchange.result && (
              <>
                <p className="expert-explanation">{exchange.result.explanation}</p>
                {exchange.result.assumptions.length > 0 && (
                  <ul className="expert-assumptions">
                    {exchange.result.assumptions.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                )}
                <ExchangeChart result={exchange.result} />
                <details className="expert-sql">
                  <summary><Code2 size={13} /> SQL · {exchange.result.rows.length.toLocaleString()} rows{exchange.result.truncated ? ` (truncated at ${exchange.result.rowLimit.toLocaleString()})` : ""} · {exchange.result.durationMs}ms</summary>
                  <pre>{exchange.result.sql}</pre>
                  <div className="expert-actions">
                    <button
                      className="button button--ghost"
                      onClick={() => {
                        void navigator.clipboard.writeText(exchange.result!.sql);
                        setCopiedSql(exchange.question);
                        window.setTimeout(() => setCopiedSql(""), 1500);
                      }}
                    >
                      {copiedSql === exchange.question ? <Check size={14} /> : <Code2 size={14} />} Copy SQL
                    </button>
                    <button className="button button--ghost" onClick={() => download("flux-expert-query.sql", "text/plain", exchange.result!.sql)}>
                      <Download size={14} /> SQL
                    </button>
                    <button className="button button--ghost" onClick={() => download("flux-expert-results.csv", "text/csv", csvOf(exchange.result!))}>
                      <Download size={14} /> CSV
                    </button>
                  </div>
                </details>
                <div className="table-wrap expert-table">
                  <table>
                    <thead><tr>{exchange.result.columns.map((name) => <th key={name}>{name}</th>)}</tr></thead>
                    <tbody>
                      {toObjects(exchange.result).slice(0, 50).map((row, rowIndex) => (
                        <tr key={rowIndex}>
                          {exchange.result!.columns.map((name) => (
                            <td key={name}>{row[name] === null || row[name] === undefined ? "—" : String(row[name])}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        ))}
      </Card>
    </>
  );
}
