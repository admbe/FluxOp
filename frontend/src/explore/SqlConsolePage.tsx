import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import {
  AlertTriangle, Bookmark, BookOpen, ChevronDown, ChevronRight, Clock,
  Database, Download, Link2, Loader2, Play, Send, Sparkles, X,
} from "lucide-react";
import { ApiError, api, isAbortError } from "../api";
import { Card, EmptyState, PageHeader } from "../components/Ui";
import { compactNumber, currency } from "../format";
import type { ExpertExplorerResult, SemanticModelInfo, SemanticSqlResult } from "../types";
import { useSemanticCatalog } from "./useSemanticQuery";
import {
  OTHER_LABEL, axisLine, axisTick, foldSeriesKeys, gridProps, seriesColor,
  tooltipProps, useChartColors, type ChartColors,
} from "./chartTheme";

type SqlCell = string | number | boolean | null;

/** One rendered run, whether it came from handwritten SQL or from Ask Flux.
 *  Both endpoints execute through the same validator and watchdog, so one
 *  result panel tells the truth for both. */
type RunResult = {
  source: "sql" | "ask";
  sql: string;
  columns: string[];
  types: string[];
  rows: SqlCell[][];
  durationMs: number;
  truncated: boolean;
  rowLimit: number;
  question?: string;
  explanation?: string;
  assumptions?: string[];
  hints?: { chartType: string; xKey: string; yKeys: string[]; seriesKey: string | null };
};

type RunError = { status: number | null; message: string; from: "sql" | "ask" };

type HistoryEntry = { sql: string; at: string; rows: number | null };

type SavedQuery = { name: string; sql: string; at: string };

const SAVED_KEY = "flux.sqlConsole.saved";

function loadSaved(): SavedQuery[] {
  try {
    const raw = window.localStorage.getItem(SAVED_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.slice(0, 20) : [];
  } catch {
    return [];
  }
}

function persistSaved(entries: SavedQuery[]): void {
  try {
    window.localStorage.setItem(SAVED_KEY, JSON.stringify(entries.slice(0, 20)));
  } catch {
    // Storage full or blocked — saving is best-effort.
  }
}

/** base64url of UTF-8 SQL for shareable #/analytics?tab=sql&q= links. */
function encodeSqlParam(sql: string): string {
  const bytes = new TextEncoder().encode(sql);
  let binary = "";
  bytes.forEach((b) => { binary += String.fromCharCode(b); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeSqlParam(value: string): string | null {
  try {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
    const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    const sql = new TextDecoder().decode(bytes);
    return sql.trim() ? sql : null;
  } catch {
    return null;
  }
}

function sharedSqlFromHash(): string | null {
  const match = window.location.hash.match(/[?&]q=([A-Za-z0-9_-]+)/);
  return match?.[1] ? decodeSqlParam(match[1]) : null;
}

/** Name a saved query from its leading `-- comment` or its first words. */
function queryName(sql: string): string {
  const comment = sql.match(/^\s*--\s*(.+)$/m)?.[1]?.trim();
  if (comment) return comment.slice(0, 48);
  return sql.replace(/\s+/g, " ").trim().slice(0, 48);
}

function downloadCsv(result: RunResult): void {
  const escapeCell = (value: SqlCell): string => {
    let s = value === null ? "" : String(value);
    // Formula-injection defence, same posture as the server-side exports.
    if (/^[=+\-@]/.test(s)) s = `'${s}`;
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const text = [
    result.columns.join(","),
    ...result.rows.map((row) => row.map(escapeCell).join(",")),
  ].join("\n");
  // BOM so Excel opens the UTF-8 file with the right encoding.
  const blob = new Blob(["\ufeff" + text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "flux-query.csv";
  anchor.click();
  URL.revokeObjectURL(url);
}

const EXAMPLES: Array<{ label: string; sql: string }> = [
  {
    label: "Daily spend · 30d",
    sql: "SELECT usage_date, SUM(amount) AS cost\nFROM semantic_daily_cost\nWHERE cost_type = 'ActualCost'\n  AND usage_date >= current_date - INTERVAL 30 DAY\nGROUP BY 1\nORDER BY 1",
  },
  {
    label: "Top services",
    sql: "SELECT service_name, SUM(amount) AS cost\nFROM semantic_daily_cost\nWHERE cost_type = 'ActualCost'\n  AND usage_date >= current_date - INTERVAL 30 DAY\nGROUP BY 1\nORDER BY 2 DESC\nLIMIT 10",
  },
  {
    label: "Service trend · stacked",
    sql: "SELECT usage_date, service_name, SUM(amount) AS cost\nFROM semantic_daily_cost\nWHERE cost_type = 'ActualCost'\n  AND usage_date >= current_date - INTERVAL 30 DAY\nGROUP BY 1, 2\nORDER BY 1",
  },
  {
    label: "Billed against effective",
    sql: "SELECT CAST(charge_period_start AS DATE) AS day,\n       SUM(billed_cost) AS billed,\n       SUM(effective_cost) AS effective\nFROM semantic_focus_cost\nGROUP BY 1\nORDER BY 1",
  },
  {
    label: "Estate mix",
    sql: "SELECT resource_type, COUNT(*) AS resources\nFROM semantic_inventory\nGROUP BY 1\nORDER BY 2 DESC\nLIMIT 12",
  },
  {
    label: "Month over month (CTE)",
    sql: "WITH monthly AS (\n  SELECT date_trunc('month', usage_date) AS month, SUM(amount) AS cost\n  FROM semantic_daily_cost\n  WHERE cost_type = 'ActualCost'\n  GROUP BY 1\n)\nSELECT month, cost\nFROM monthly\nORDER BY month",
  },
];

const SUGGESTED_QUESTIONS = [
  "Which services drove spend over the last 30 days?",
  "How is daily cost trending this month?",
  "How much of our effective cost is covered by commitments?",
  "Which resource types dominate the estate?",
];

// ---------------------------------------------------------------------------
// Column classification — DuckDB type names first, value sniffing as fallback
// for expert responses produced by an older backend without `types`.
// ---------------------------------------------------------------------------

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}/;

function isTimeType(type: string | undefined, name: string, sample: SqlCell): boolean {
  if (type && /^(DATE|TIMESTAMP)/i.test(type)) return true;
  if (type) return false;
  return /date|day|week|month|period/i.test(name) && typeof sample === "string" && ISO_DATE_RE.test(sample);
}

function isNumericType(type: string | undefined, sample: SqlCell): boolean {
  if (type) return /INT|DOUBLE|FLOAT|DECIMAL|REAL|NUMERIC/i.test(type);
  return typeof sample === "number";
}

type Shape = { timeIdx: number; numericIdxs: number[]; dimIdx: number };

function classify(columns: string[], types: string[] | undefined, rows: SqlCell[][]): Shape {
  const sampleFor = (i: number): SqlCell => rows.find((r) => r[i] !== null)?.[i] ?? null;
  let timeIdx = -1;
  const numericIdxs: number[] = [];
  let dimIdx = -1;
  columns.forEach((name, i) => {
    const type = types?.[i];
    const sample = sampleFor(i);
    if (timeIdx === -1 && isTimeType(type, name, sample)) {
      timeIdx = i;
    } else if (isNumericType(type, sample)) {
      numericIdxs.push(i);
    } else if (dimIdx === -1) {
      dimIdx = i;
    }
  });
  return { timeIdx, numericIdxs, dimIdx };
}

/** Column-name heuristic for value formatting; raw SQL carries no format
 *  metadata, so cost-looking names read as dollars and rate-looking names as
 *  percentages. The table always shows the raw value. */
function formatterFor(name: string): (v: number) => string {
  if (/percent|rate|pct|esr|utili[sz]ation/i.test(name)) return (v) => `${v.toFixed(1)}%`;
  if (/cost|amount|spend|saving|billed|effective|price|charge/i.test(name)) return (v) => currency(v);
  return (v) => compactNumber(v);
}

function shortDate(value: string): string {
  return ISO_DATE_RE.test(value) ? value.slice(5, 10) : value;
}

// ---------------------------------------------------------------------------
// Auto chart — picks an encoding from the result shape. Hints from Ask Flux
// override when they reference real columns.
// ---------------------------------------------------------------------------

function AutoChart({ result, colors }: { result: RunResult; colors: ChartColors }) {
  const gradientId = useId();
  const shape = useMemo(
    () => classify(result.columns, result.types.length ? result.types : undefined, result.rows),
    [result],
  );

  const hinted = result.hints ?? null;
  const hintX = hinted ? result.columns.indexOf(hinted.xKey) : -1;
  const hintYs = hinted ? hinted.yKeys.map((k) => result.columns.indexOf(k)).filter((i) => i !== -1) : [];

  const timeIdx = hintX !== -1 && isTimeType(result.types[hintX], result.columns[hintX] ?? "", result.rows[0]?.[hintX] ?? null)
    ? hintX
    : shape.timeIdx;
  const numericIdxs = hintYs.length ? hintYs : shape.numericIdxs;
  const dimIdx = hinted?.seriesKey ? result.columns.indexOf(hinted.seriesKey) : shape.dimIdx;

  const grid = gridProps(colors);
  const tick = axisTick(colors);
  const tip = tooltipProps(colors);

  // ---- time series ----------------------------------------------------------
  const timeData = useMemo(() => {
    if (timeIdx === -1 || numericIdxs.length === 0) return null;

    if (dimIdx !== -1 && numericIdxs.length >= 1) {
      // date × dimension × measure → stacked bands, tail folded to Other.
      const measureIdx = numericIdxs[0] ?? 0;
      const totals = new Map<string, number>();
      for (const row of result.rows) {
        const key = String(row[dimIdx] ?? "—");
        const v = Number(row[measureIdx] ?? 0);
        if (Number.isFinite(v)) totals.set(key, (totals.get(key) ?? 0) + v);
      }
      const keys = foldSeriesKeys(totals);
      const byDate = new Map<string, Record<string, number>>();
      for (const row of result.rows) {
        const date = String(row[timeIdx] ?? "");
        if (!date) continue;
        const v = Number(row[measureIdx] ?? 0);
        if (!Number.isFinite(v)) continue;
        const raw = String(row[dimIdx] ?? "—");
        const key = keys.includes(raw) ? raw : OTHER_LABEL;
        const slot = byDate.get(date) ?? {};
        slot[key] = (slot[key] ?? 0) + v;
        byDate.set(date, slot);
      }
      const points = [...byDate.entries()]
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([date, slot]) => {
          const point: Record<string, number | string> = { date: shortDate(date) };
          for (const key of keys) point[key] = slot[key] ?? 0;
          return point;
        });
      return { mode: "stacked" as const, points, keys, measureIdx };
    }

    // date × 1..4 measures → lines (single measure gets a gradient area).
    const byDate = new Map<string, Record<string, number>>();
    const chosen = numericIdxs.slice(0, 4);
    for (const row of result.rows) {
      const date = String(row[timeIdx] ?? "");
      if (!date) continue;
      const slot = byDate.get(date) ?? {};
      for (const idx of chosen) {
        const v = Number(row[idx] ?? 0);
        if (!Number.isFinite(v)) continue;
        const name = result.columns[idx] ?? `col${idx}`;
        slot[name] = (slot[name] ?? 0) + v;
      }
      byDate.set(date, slot);
    }
    const points = [...byDate.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([date, slot]) => ({ date: shortDate(date), ...slot }));
    return { mode: "lines" as const, points, keys: chosen.map((i) => result.columns[i] ?? `col${i}`), measureIdx: chosen[0] ?? 0 };
  }, [result, timeIdx, numericIdxs, dimIdx]);

  // ---- categorical ----------------------------------------------------------
  const categoryData = useMemo(() => {
    if (timeIdx !== -1 || dimIdx === -1 || numericIdxs.length === 0) return null;
    const measureIdx = numericIdxs[0] ?? 0;
    const totals = new Map<string, number>();
    for (const row of result.rows) {
      const key = String(row[dimIdx] ?? "—");
      const v = Number(row[measureIdx] ?? 0);
      if (Number.isFinite(v)) totals.set(key, (totals.get(key) ?? 0) + v);
    }
    const items = [...totals.entries()]
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 12);
    return items.length >= 2 ? { items, measureIdx } : null;
  }, [result, timeIdx, dimIdx, numericIdxs]);

  if (timeData && timeData.points.length > 1) {
    const fmt = formatterFor(result.columns[timeData.measureIdx] ?? "");
    if (timeData.mode === "stacked") {
      return (
        <ChartBox>
          <AreaChart data={timeData.points} margin={{ left: 4, right: 12, top: 6, bottom: 0 }}>
            <CartesianGrid {...grid} />
            <XAxis dataKey="date" tick={tick} axisLine={axisLine(colors)} tickLine={false} minTickGap={24} />
            <YAxis tick={tick} axisLine={false} tickLine={false} tickFormatter={(v: number) => compactNumber(v)} width={52} />
            <Tooltip {...tip} formatter={(v: unknown) => fmt(Number(v))} />
            {timeData.keys.map((key) => {
              const c = seriesColor(colors, timeData.keys, key);
              return <Area key={key} type="monotone" dataKey={key} stackId="a" stroke={c} fill={c} fillOpacity={0.28} strokeWidth={1.4} dot={false} />;
            })}
          </AreaChart>
        </ChartBox>
      );
    }
    if (timeData.keys.length === 1) {
      const key = timeData.keys[0] ?? "value";
      return (
        <ChartBox>
          <AreaChart data={timeData.points} margin={{ left: 4, right: 12, top: 6, bottom: 0 }}>
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={colors.series[0]} stopOpacity={0.32} />
                <stop offset="100%" stopColor={colors.series[0]} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid {...grid} />
            <XAxis dataKey="date" tick={tick} axisLine={axisLine(colors)} tickLine={false} minTickGap={24} />
            <YAxis tick={tick} axisLine={false} tickLine={false} tickFormatter={(v: number) => compactNumber(v)} width={52} />
            <Tooltip {...tip} formatter={(v: unknown) => fmt(Number(v))} />
            <Area type="monotone" dataKey={key} stroke={colors.series[0]} strokeWidth={2.2} fill={`url(#${gradientId})`} dot={false} />
          </AreaChart>
        </ChartBox>
      );
    }
    return (
      <>
        <ChartBox>
          <LineChart data={timeData.points} margin={{ left: 4, right: 12, top: 6, bottom: 0 }}>
            <CartesianGrid {...grid} />
            <XAxis dataKey="date" tick={tick} axisLine={axisLine(colors)} tickLine={false} minTickGap={24} />
            <YAxis tick={tick} axisLine={false} tickLine={false} tickFormatter={(v: number) => compactNumber(v)} width={52} />
            <Tooltip {...tip} formatter={(v: unknown) => fmt(Number(v))} />
            {timeData.keys.map((key, i) => (
              <Line key={key} type="monotone" dataKey={key} stroke={colors.series[i % colors.series.length]} strokeWidth={2.1} strokeLinecap="round" dot={false} />
            ))}
          </LineChart>
        </ChartBox>
        <ChartLegend keys={timeData.keys} colors={colors} />
      </>
    );
  }

  if (categoryData) {
    const measureName = result.columns[categoryData.measureIdx] ?? "value";
    const fmt = formatterFor(measureName);
    return (
      <div style={{ width: "100%", height: Math.max(180, categoryData.items.length * 30), marginTop: 10 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={categoryData.items} layout="vertical" margin={{ left: 110, right: 16 }}>
            <CartesianGrid {...grid} vertical horizontal={false} />
            <XAxis type="number" tick={tick} axisLine={false} tickLine={false} tickFormatter={(v: number) => compactNumber(v)} />
            <YAxis type="category" dataKey="name" tick={tick} axisLine={false} tickLine={false} width={106} />
            <Tooltip {...tip} formatter={(v: unknown) => fmt(Number(v))} />
            <Bar dataKey="value" name={measureName} fill={colors.series[0]} radius={[0, 5, 5, 0]} maxBarSize={20} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    );
  }

  // Single row of numbers → KPI strip instead of a one-bar chart.
  if (result.rows.length === 1 && numericIdxs.length >= 1) {
    const row = result.rows[0] ?? [];
    return (
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10, marginTop: 10 }}>
        {numericIdxs.slice(0, 6).map((idx) => {
          const name = result.columns[idx] ?? `col${idx}`;
          const v = Number(row[idx] ?? 0);
          return (
            <div key={idx} style={{ padding: "10px 12px", borderRadius: 10, border: "1px solid rgb(var(--border))", background: "rgb(var(--surface))" }}>
              <div style={{ fontSize: 11, color: "rgb(var(--text-muted))", fontFamily: "monospace" }}>{name}</div>
              <div style={{ fontSize: 20, fontWeight: 700, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>
                {Number.isFinite(v) ? formatterFor(name)(v) : String(row[idx] ?? "—")}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  return null;
}

function ChartBox({ children }: { children: ReactElement }) {
  return (
    <div style={{ width: "100%", height: 260, marginTop: 10 }}>
      <ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer>
    </div>
  );
}

function ChartLegend({ keys, colors }: { keys: readonly string[]; colors: ChartColors }) {
  return (
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 11.5, color: "rgb(var(--text-muted))", marginTop: 6 }}>
      {keys.map((key, i) => (
        <span key={key} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ width: 8, height: 8, borderRadius: 999, background: colors.series[i % colors.series.length] }} />
          {key}
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Result panel — table + chart + provenance for the last run.
// ---------------------------------------------------------------------------

function ResultPanel({ result, colors }: { result: RunResult; colors: ChartColors }) {
  const [visibleRows, setVisibleRows] = useState(25);
  useEffect(() => setVisibleRows(25), [result]);
  const rows = result.rows.slice(0, visibleRows);

  return (
    <div data-testid="sql-result" style={{ marginTop: 14 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 12, color: "rgb(var(--text-muted))" }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 9px", borderRadius: 999, background: "rgb(var(--success) / 0.14)", color: "rgb(var(--text))", fontWeight: 600 }}>
          {result.rows.length}{result.truncated ? "+" : ""} row{result.rows.length === 1 ? "" : "s"}
        </span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><Clock size={12} /> {result.durationMs} ms</span>
        {result.truncated && <span>capped at {result.rowLimit.toLocaleString()} rows</span>}
        <span style={{ fontFamily: "monospace", fontSize: 11 }}>read from the published snapshot</span>
        {result.rows.length > 0 && (
          <button
            onClick={() => downloadCsv(result)}
            title="Download the full result as CSV"
            style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 10px", borderRadius: 8, border: "1px solid rgb(var(--border))", background: "rgb(var(--surface))", cursor: "pointer", fontSize: 12, color: "rgb(var(--text))" }}
          >
            <Download size={13} /> CSV
          </button>
        )}
      </div>

      {result.source === "ask" && result.explanation && (
        <div style={{ marginTop: 10, padding: "12px 14px", borderRadius: 10, background: "rgb(var(--primary) / 0.07)", border: "1px solid rgb(var(--primary) / 0.2)" }}>
          <div style={{ fontSize: 12, fontWeight: 700, display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
            <Sparkles size={13} /> Flux
          </div>
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.55 }}>{result.explanation}</p>
          {result.assumptions && result.assumptions.length > 0 && (
            <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 12, color: "rgb(var(--text-muted))", lineHeight: 1.5 }}>
              {result.assumptions.map((item, i) => <li key={i}>{item}</li>)}
            </ul>
          )}
        </div>
      )}

      {result.rows.length === 0 ? (
        <EmptyState title="No rows" description="The query is valid but nothing in the current snapshot matches it. Widen the date range or drop a filter." />
      ) : (
        <>
          <AutoChart result={result} colors={colors} />
          <div style={{ overflowX: "auto", marginTop: 12, border: "1px solid rgb(var(--border))", borderRadius: 10 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr>
                  {result.columns.map((c, i) => (
                    <th key={c} style={{ textAlign: "left", padding: "8px 10px", borderBottom: "1px solid rgb(var(--border))", background: "rgb(var(--surface-soft))", fontFamily: "monospace", whiteSpace: "nowrap" }}>
                      {c}
                      {result.types[i] && <span style={{ fontWeight: 400, color: "rgb(var(--text-muted))", marginLeft: 6, fontSize: 10.5 }}>{result.types[i]}</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i} style={{ borderTop: i === 0 ? "none" : "1px solid rgb(var(--border) / 0.55)" }}>
                    {row.map((value, j) => (
                      <td key={j} style={{ padding: "7px 10px", whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums", maxWidth: 340, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {value === null ? <span style={{ color: "rgb(var(--text-muted))" }}>—</span> : String(value)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.rows.length > visibleRows && (
            <button className="button button--secondary" onClick={() => setVisibleRows((n) => n + 50)} style={{ marginTop: 10 }}>
              Show 50 more ({result.rows.length - visibleRows} remaining)
            </button>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Schema browser — the governed views, from the same catalog the layer serves.
// ---------------------------------------------------------------------------

function SchemaBrowser({ onInsert }: { onInsert: (sql: string) => void }) {
  const catalog = useSemanticCatalog(true);
  const [open, setOpen] = useState<string | null>(null);

  if (catalog.status === "error") {
    return <p style={{ fontSize: 12, color: "rgb(var(--text-muted))" }}>Catalog unavailable: {catalog.error}</p>;
  }
  const models = catalog.catalog?.models ?? [];
  if (!models.length) {
    return <p style={{ fontSize: 12, color: "rgb(var(--text-muted))" }}>Loading the governed view catalog…</p>;
  }
  return (
    <div style={{ display: "grid", gap: 2 }}>
      {models.map((model: SemanticModelInfo) => {
        const view = `semantic_${model.name}`;
        const expanded = open === model.name;
        return (
          <div key={model.name}>
            <button
              onClick={() => setOpen(expanded ? null : model.name)}
              style={{ width: "100%", display: "flex", alignItems: "center", gap: 6, padding: "6px 8px", borderRadius: 8, border: 0, background: expanded ? "rgb(var(--surface-soft))" : "transparent", cursor: "pointer", textAlign: "left" }}
            >
              {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              <span style={{ fontFamily: "monospace", fontSize: 12 }}>{view}</span>
              <span style={{ marginLeft: "auto", width: 7, height: 7, borderRadius: 999, background: model.available ? "rgb(var(--success))" : "rgb(var(--warning))" }} title={model.available ? "in the current snapshot" : "not in the current snapshot yet"} />
            </button>
            {expanded && (
              <div style={{ padding: "4px 8px 10px 27px", fontSize: 11.5, color: "rgb(var(--text-muted))", lineHeight: 1.5 }}>
                <p style={{ margin: "0 0 6px" }}>{model.description}</p>
                {model.timeColumn && (
                  <p style={{ margin: "0 0 6px" }}>
                    time: <code>{model.timeColumn}</code>
                    {model.completenessLagDays > 0 ? ` · trails ${model.completenessLagDays}d` : ""}
                    {model.dataThrough ? ` · data through ${model.dataThrough}` : ""}
                  </p>
                )}
                <p style={{ margin: "0 0 4px" }}>
                  <strong style={{ color: "rgb(var(--text))" }}>dimensions</strong> {model.dimensions.map((d) => d.name).join(", ") || "—"}
                </p>
                <p style={{ margin: "0 0 8px" }}>
                  <strong style={{ color: "rgb(var(--text))" }}>measures</strong> {model.measures.map((m) => m.name).join(", ") || "—"}
                </p>
                <button
                  className="button button--secondary"
                  style={{ fontSize: 12, padding: "4px 10px" }}
                  onClick={() => {
                    const dim = model.dimensions[0]?.name;
                    const starter = model.timeColumn
                      ? `SELECT ${model.timeColumn}${dim ? `, ${dim}` : ""}, COUNT(*) AS rows\nFROM ${view}\nGROUP BY ${dim ? "1, 2" : "1"}\nORDER BY 1\nLIMIT 100`
                      : `SELECT ${dim ? `${dim}, ` : ""}COUNT(*) AS rows\nFROM ${view}\n${dim ? "GROUP BY 1\nORDER BY 2 DESC\n" : ""}LIMIT 100`;
                    onInsert(starter);
                  }}
                >
                  Insert starter query
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page.
// ---------------------------------------------------------------------------

export function SqlConsolePage() {
  const colors = useChartColors();
  const [sql, setSql] = useState<string>(() => sharedSqlFromHash() ?? EXAMPLES[0]?.sql ?? "");
  const [question, setQuestion] = useState("");
  const [running, setRunning] = useState<"sql" | "ask" | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<RunError | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [saved, setSaved] = useState<SavedQuery[]>(loadSaved);
  const [linkCopied, setLinkCopied] = useState(false);
  const [exampleIdx, setExampleIdx] = useState(0);
  const askAbort = useRef<AbortController | null>(null);
  const runAbort = useRef<AbortController | null>(null);
  const askTurns = useRef<{ question: string; sql: string }[]>([]);
  const sharedRan = useRef(false);

  const pushHistory = useCallback((entry: HistoryEntry) => {
    setHistory((h) => [entry, ...h.filter((x) => x.sql !== entry.sql)].slice(0, 8));
  }, []);

  const runSql = useCallback((statement?: string) => {
    const text = (statement ?? sql).trim();
    if (!text) return;
    runAbort.current?.abort();
    const ac = new AbortController();
    runAbort.current = ac;
    setRunning("sql");
    setError(null);
    api
      .semanticSql(text, { signal: ac.signal })
      .then((r: SemanticSqlResult) => {
        if (ac.signal.aborted) return;
        setResult({
          source: "sql",
          sql: r.sql,
          columns: r.columns,
          types: r.types ?? [],
          rows: r.rows as SqlCell[][],
          durationMs: r.durationMs,
          truncated: r.truncated,
          rowLimit: r.rowLimit,
        });
        pushHistory({ sql: text, at: new Date().toLocaleTimeString(), rows: r.rows.length });
        setRunning(null);
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted || isAbortError(e)) return;
        setError({
          status: e instanceof ApiError ? e.status : null,
          message: e instanceof Error ? e.message : String(e),
          from: "sql",
        });
        pushHistory({ sql: text, at: new Date().toLocaleTimeString(), rows: null });
        setRunning(null);
      });
  }, [sql, pushHistory]);

  const ask = useCallback((q?: string) => {
    const text = (q ?? question).trim();
    if (text.length < 3) return;
    askAbort.current?.abort();
    const ac = new AbortController();
    askAbort.current = ac;
    setRunning("ask");
    setError(null);
    setQuestion(text);
    api
      .semanticExpert(text, askTurns.current.slice(-4))
      .then((r: ExpertExplorerResult) => {
        if (ac.signal.aborted) return;
        setSql(r.sql);
        setResult({
          source: "ask",
          sql: r.sql,
          columns: r.columns,
          types: r.types ?? [],
          rows: r.rows as SqlCell[][],
          durationMs: r.durationMs,
          truncated: r.truncated,
          rowLimit: r.rowLimit,
          question: r.question,
          explanation: r.explanation,
          assumptions: r.assumptions,
          hints: { chartType: r.chartType, xKey: r.xKey, yKeys: r.yKeys, seriesKey: r.seriesKey },
        });
        askTurns.current = [...askTurns.current, { question: text, sql: r.sql }].slice(-6);
        pushHistory({ sql: r.sql, at: new Date().toLocaleTimeString(), rows: r.rows.length });
        setRunning(null);
        setQuestion("");
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted || isAbortError(e)) return;
        setError({
          status: e instanceof ApiError ? e.status : null,
          message: e instanceof Error ? e.message : String(e),
          from: "ask",
        });
        setRunning(null);
      });
  }, [question, pushHistory]);

  // A shared link (?q=) runs itself once, so the recipient lands on the
  // answer rather than an editor full of someone else's SQL.
  useEffect(() => {
    if (sharedRan.current) return;
    sharedRan.current = true;
    const shared = sharedSqlFromHash();
    if (shared) runSql(shared);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const saveCurrent = useCallback(() => {
    const text = sql.trim();
    if (!text) return;
    setSaved((entries) => {
      const next = [
        { name: queryName(text), sql: text, at: new Date().toLocaleDateString() },
        ...entries.filter((entry) => entry.sql !== text),
      ].slice(0, 20);
      persistSaved(next);
      return next;
    });
  }, [sql]);

  const removeSaved = useCallback((target: SavedQuery) => {
    setSaved((entries) => {
      const next = entries.filter((entry) => entry.sql !== target.sql);
      persistSaved(next);
      return next;
    });
  }, []);

  const copyLink = useCallback(() => {
    const url = `${window.location.origin}${window.location.pathname}#/analytics?tab=sql&q=${encodeSqlParam(sql.trim())}`;
    void navigator.clipboard?.writeText(url).then(() => {
      setLinkCopied(true);
      window.setTimeout(() => setLinkCopied(false), 1600);
    });
  }, [sql]);

  const errorCopy = useMemo<{ title: string; body: string } | null>(() => {
    if (!error) return null;
    if (error.status === 503 && error.from === "ask") {
      return {
        title: "Ask Flux needs an AI provider",
        body: "No AI provider is configured, so natural-language SQL is off. An admin can set one under Integrations → AI configuration. The console itself works without it — write SQL and Run.",
      };
    }
    if (error.status === 503) {
      return { title: "Data is catching up", body: "The analytics snapshot has not finished publishing. Nothing is wrong with the query — run it again in a moment." };
    }
    if (error.status === 402) {
      return { title: "AI budget exhausted", body: error.message };
    }
    if (error.from === "ask") {
      return { title: "Ask Flux could not answer that", body: error.message };
    }
    return { title: "The governed layer refused the query", body: error.message };
  }, [error]);

  return (
    <div className="page sql-console">
      <PageHeader
        eyebrow="FinOps · Analytics · SQL Console"
        title="Query the semantic layer"
        description="Write SQL against the governed semantic views — the same ones every dashboard reads — or ask Flux in plain English and it writes, runs, and explains the query. Read-only, row-capped, published snapshot."
      />

      <div className="sql-console-grid" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 360px", gap: 14, alignItems: "start" }}>
        {/* ---- left: editor + result ---- */}
        <Card style={{ padding: 14 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
            <strong style={{ fontSize: 13, display: "inline-flex", alignItems: "center", gap: 7 }}>
              <Database size={14} /> SQL
              <span style={{ fontWeight: 400, color: "rgb(var(--text-muted))" }}>· governed views · read-only</span>
            </strong>
            <span style={{ fontSize: 11, color: "rgb(var(--text-muted))" }}>Ctrl+Enter runs</span>
          </div>

          <div style={{ border: "1px solid rgb(var(--border))", borderRadius: 10, background: "rgb(var(--code-bg))", overflow: "hidden" }}>
            <textarea
              value={sql}
              onChange={(e) => setSql(e.target.value)}
              onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runSql(); }}
              spellCheck={false}
              aria-label="SQL editor"
              style={{ width: "100%", minHeight: 150, padding: "12px 14px", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 13, lineHeight: 1.55, color: "rgb(var(--text))", background: "transparent", border: 0, outline: 0, resize: "vertical", display: "block" }}
            />
          </div>

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 10 }}>
            <button
              onClick={() => runSql()}
              disabled={running !== null}
              style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "8px 16px", borderRadius: 9, border: "1px solid rgb(var(--primary))", background: "rgb(var(--primary))", color: "white", cursor: running ? "wait" : "pointer", fontWeight: 600, fontSize: 13, opacity: running ? 0.7 : 1 }}
            >
              {running === "sql" ? <Loader2 size={14} className="spin" /> : <Play size={14} />} Run
            </button>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {EXAMPLES.map((example, i) => (
                <button
                  key={example.label}
                  onClick={() => { setExampleIdx(i); setSql(example.sql); }}
                  aria-pressed={exampleIdx === i && sql === example.sql}
                  style={{ padding: "5px 10px", borderRadius: 999, border: "1px solid rgb(var(--border))", background: sql === example.sql ? "rgb(var(--primary) / 0.12)" : "rgb(var(--surface))", cursor: "pointer", fontSize: 12 }}
                >
                  {example.label}
                </button>
              ))}
            </div>
            <span style={{ marginLeft: "auto", display: "inline-flex", gap: 6 }}>
              <button
                onClick={saveCurrent}
                title="Save this query on this browser"
                style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 10px", borderRadius: 8, border: "1px solid rgb(var(--border))", background: "rgb(var(--surface))", cursor: "pointer", fontSize: 12 }}
              >
                <Bookmark size={13} /> Save
              </button>
              <button
                onClick={copyLink}
                title="Copy a link that opens this query and runs it"
                style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 10px", borderRadius: 8, border: "1px solid rgb(var(--border))", background: linkCopied ? "rgb(var(--success) / 0.14)" : "rgb(var(--surface))", cursor: "pointer", fontSize: 12 }}
              >
                <Link2 size={13} /> {linkCopied ? "Copied" : "Copy link"}
              </button>
            </span>
          </div>

          {errorCopy && (
            <div style={{ display: "flex", gap: 10, alignItems: "start", marginTop: 12, padding: "12px 14px", borderRadius: 10, background: "rgb(var(--warning) / 0.1)", border: "1px solid rgb(var(--warning) / 0.35)" }}>
              <AlertTriangle size={15} style={{ flex: "none", marginTop: 1, color: "rgb(var(--warning))" }} />
              <div>
                <strong style={{ fontSize: 12.5 }}>{errorCopy.title}</strong>
                <p style={{ margin: "3px 0 0", fontSize: 12.5, lineHeight: 1.5, color: "rgb(var(--text-muted))" }}>{errorCopy.body}</p>
              </div>
            </div>
          )}

          {running === "ask" && (
            <p style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: "rgb(var(--text-muted))", marginTop: 12 }}>
              <Loader2 size={14} className="spin" /> Flux is writing and validating the SQL…
            </p>
          )}

          {result && running !== "ask" && <ResultPanel result={result} colors={colors} />}
        </Card>

        {/* ---- right: Ask Flux + views + history ---- */}
        <div className="sql-console-side" style={{ display: "grid", gap: 12, position: "sticky", top: "calc(var(--header-height) + 12px)" }}>
          <Card style={{ padding: 14, background: "linear-gradient(150deg, rgb(var(--primary) / 0.09), transparent 55%)" }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, display: "flex", alignItems: "center", gap: 7 }}>
              <Sparkles size={14} /> Ask Flux
            </div>
            <p style={{ fontSize: 12.5, lineHeight: 1.5, margin: "6px 0 10px", color: "rgb(var(--text-muted))" }}>
              Plain English in — governed SQL out. The query lands in the editor, runs under the same validator, and Flux explains what came back.
            </p>
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } }}
              placeholder="e.g. Which services drove spend last month?"
              aria-label="Ask Flux a question"
              rows={2}
              style={{ width: "100%", padding: "9px 11px", borderRadius: 9, border: "1px solid rgb(var(--border))", background: "rgb(var(--surface))", color: "rgb(var(--text))", fontSize: 13, lineHeight: 1.5, resize: "vertical", outline: 0, display: "block" }}
            />
            <button
              onClick={() => ask()}
              disabled={running !== null || question.trim().length < 3}
              style={{ marginTop: 8, width: "100%", display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "9px 12px", borderRadius: 9, border: "1px solid rgb(var(--primary))", background: "rgb(var(--primary))", color: "white", cursor: "pointer", fontWeight: 600, fontSize: 13, opacity: running !== null || question.trim().length < 3 ? 0.55 : 1 }}
            >
              {running === "ask" ? <Loader2 size={14} className="spin" /> : <Send size={14} />} Ask
            </button>
            <div style={{ display: "grid", gap: 5, marginTop: 10 }}>
              {SUGGESTED_QUESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  onClick={() => ask(suggestion)}
                  disabled={running !== null}
                  style={{ textAlign: "left", fontSize: 12, padding: "6px 9px", borderRadius: 8, border: "1px solid rgb(var(--border))", background: "rgb(var(--surface))", cursor: "pointer", color: "rgb(var(--text-muted))", lineHeight: 1.4 }}
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </Card>

          <Card style={{ padding: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 700, display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
              <BookOpen size={13} /> Governed views
            </div>
            <SchemaBrowser onInsert={(starter) => setSql(starter)} />
          </Card>

          {saved.length > 0 && (
            <Card style={{ padding: 12 }}>
              <div style={{ fontSize: 12, fontWeight: 700, display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                <Bookmark size={13} /> Saved
              </div>
              {saved.map((entry) => (
                <div key={entry.sql.slice(0, 80)} style={{ display: "flex", alignItems: "center", gap: 4 }}>
                  <button
                    onClick={() => setSql(entry.sql)}
                    title="Restore into the editor"
                    style={{ flex: 1, minWidth: 0, textAlign: "left", padding: "7px 9px", borderRadius: 8, border: "1px solid transparent", background: "transparent", cursor: "pointer" }}
                  >
                    <div style={{ fontSize: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", color: "rgb(var(--text))" }}>{entry.name}</div>
                    <div style={{ fontSize: 10.5, color: "rgb(var(--text-muted))", marginTop: 1 }}>{entry.at}</div>
                  </button>
                  <button
                    onClick={() => removeSaved(entry)}
                    aria-label={`Delete saved query ${entry.name}`}
                    style={{ flex: "none", border: 0, background: "transparent", cursor: "pointer", color: "rgb(var(--text-muted))", padding: 4 }}
                  >
                    <X size={13} />
                  </button>
                </div>
              ))}
            </Card>
          )}

          {history.length > 0 && (
            <Card style={{ padding: 12 }}>
              <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 6 }}>History</div>
              {history.map((entry, i) => (
                <button
                  key={`${entry.at}-${i}`}
                  onClick={() => setSql(entry.sql)}
                  title="Restore into the editor"
                  style={{ width: "100%", textAlign: "left", padding: "7px 9px", borderRadius: 8, border: "1px solid transparent", background: "transparent", cursor: "pointer", display: "block" }}
                >
                  <div style={{ fontFamily: "monospace", fontSize: 11, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", color: "rgb(var(--text))" }}>
                    {entry.sql.replace(/\s+/g, " ").slice(0, 64)}{entry.sql.length > 64 ? "…" : ""}
                  </div>
                  <div style={{ fontSize: 10.5, color: "rgb(var(--text-muted))", marginTop: 1 }}>
                    {entry.at} · {entry.rows === null ? "failed" : `${entry.rows} rows`}
                  </div>
                </button>
              ))}
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
