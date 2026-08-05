import {
  ArrowDown, ArrowUp, Check, Code2, Download, GitCompareArrows,
  Image as ImageIcon, LineChart as LineChartIcon, Link2, Table2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { api } from "../api";
import { announceActivity } from "../busy";
import { compactNumber, currency } from "../format";
import { useChartColors } from "../theme";
import type {
  SemanticCatalog, SemanticModelInfo, SemanticQueryRequest,
  SemanticQueryResult,
} from "../types";
import { Card, EmptyState, ErrorPanel, Loading, PageHeader } from "../components/Ui";
import { ExpertExplorerPage } from "./ExpertExplorerPage";

type ChartForm = "auto" | "line" | "area" | "bar" | "table";

const RANGE_PRESETS = [
  { key: "30d", label: "30 days", days: 30 },
  { key: "90d", label: "90 days", days: 90 },
  { key: "180d", label: "180 days", days: 180 },
  { key: "all", label: "All time", days: 0 },
];

/** At most this many series are drawn; the rest fold into "Other". */
const MAX_SERIES = 7;
const OTHER_SERIES = "Other";
/** Synthetic series key for the previous-period overlay line. */
const PREVIOUS_KEY = "__previous__";

const DAY_MILLISECONDS = 86_400_000;

function isoDaysAgo(days: number): string {
  const value = new Date();
  value.setDate(value.getDate() - days);
  return value.toISOString().slice(0, 10);
}

function isoShift(iso: string, days: number): string {
  return new Date(Date.parse(iso) + days * DAY_MILLISECONDS)
    .toISOString()
    .slice(0, 10);
}

function daysBetween(startIso: string, endIso: string): number {
  return Math.round((Date.parse(endIso) - Date.parse(startIso)) / DAY_MILLISECONDS);
}

function formatValue(value: unknown, format: string): string {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  if (format === "currency") return currency(value);
  if (format === "percent") return `${value.toFixed(1)}%`;
  return Math.abs(value) >= 1000 || Number.isInteger(value)
    ? value.toLocaleString()
    : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** Axis ticks stay short ($1.2M / 45K); the tooltip and table carry full precision. */
function compactValue(value: number, format: string): string {
  if (format === "percent") return `${Number(value.toFixed(1))}%`;
  if (format === "currency") {
    return `${value < 0 ? "-" : ""}$${compactNumber(Math.abs(value))}`;
  }
  return compactNumber(value);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatPeriodTick(iso: string, grain: string, multiYear: boolean): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (!year || !month) return iso;
  const monthName = MONTHS[month - 1] ?? "";
  if (grain === "month") return multiYear ? `${monthName} ’${String(year).slice(2)}` : monthName;
  const base = `${monthName} ${day}`;
  return multiYear ? `${base} ’${String(year).slice(2)}` : base;
}

function formatPeriodFull(iso: string, grain: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (!year || !month) return iso;
  const monthName = MONTHS[month - 1] ?? "";
  if (grain === "month") return `${monthName} ${year}`;
  if (grain === "week") return `Week of ${monthName} ${day}, ${year}`;
  return `${monthName} ${day}, ${year}`;
}

/** Measures whose per-bucket values must be averaged, never summed. */
function isMeanLike(name: string, format: string): boolean {
  return (
    format === "percent"
    || /average|avg|rate|percent|utilization/i.test(name)
  );
}

/** Encode the current selection into the location hash for shareable links. */
function encodeState(state: Record<string, string>): void {
  const params = new URLSearchParams(
    Object.fromEntries(Object.entries(state).filter(([, value]) => value !== "")),
  );
  window.history.replaceState(null, "", `#/analytics?${params.toString()}`);
}

function decodeState(): Record<string, string> {
  const raw = window.location.hash.split("?")[1] ?? "";
  return Object.fromEntries(new URLSearchParams(raw).entries());
}

/** Persistent member→palette-slot assignment so a filter change never
 *  repaints surviving series: color follows the entity, not its rank.
 *  Slots hand out monotonically and wrap past the palette length — a
 *  documented trade for stability over uniqueness on long-lived scopes. */
function loadSeriesSlots(scope: string): Record<string, number> {
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(`flux-series-slots:${scope}`) ?? "{}",
    );
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function assignSeriesSlots(
  scope: string,
  names: string[],
): Record<string, number> {
  const slots = loadSeriesSlots(scope);
  let next = Object.values(slots).reduce(
    (max, value) => (typeof value === "number" ? Math.max(max, value + 1) : max),
    0,
  );
  let changed = false;
  for (const name of names) {
    if (name === OTHER_SERIES) continue;
    if (!(name in slots)) {
      slots[name] = next;
      next += 1;
      changed = true;
    }
  }
  if (changed) {
    try {
      window.localStorage.setItem(
        `flux-series-slots:${scope}`,
        JSON.stringify(slots),
      );
    } catch {
      // Quota or privacy mode: colors fall back to per-render order.
    }
  }
  return slots;
}

/** Resolve `var(--token)` references so a serialized SVG renders standalone. */
function inlineCssVariables(svg: string): string {
  const rootStyle = getComputedStyle(document.documentElement);
  const channels = (name: string) =>
    rootStyle.getPropertyValue(name).trim().split(/\s+/).join(", ");
  return svg
    .replace(
      /rgb\(var\((--[\w-]+)\)\s*\/\s*([0-9.]+)\)/g,
      (_, name: string, alpha: string) => `rgba(${channels(name)}, ${alpha})`,
    )
    .replace(/var\((--[\w-]+)\)/g, (_, name: string) => channels(name));
}

type TooltipEntry = {
  dataKey?: string | number;
  name?: string | number;
  value?: number | string;
  color?: string;
  stroke?: string;
  fill?: string;
};

/** Values lead, names follow; identity comes from a colored key beside the
 *  text, never from coloring the text itself. */
function ExplorerTooltip({
  active, payload, label, format, grain, hidden, timeAxis, showTotal, clickHint,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string | number;
  format: string;
  grain: string;
  hidden: Set<string>;
  timeAxis: boolean;
  showTotal: boolean;
  clickHint: boolean;
}) {
  if (!active || !payload?.length) return null;
  const rows = payload
    .filter((entry) => !hidden.has(String(entry.dataKey ?? "")))
    .sort((a, b) => Number(b.value ?? 0) - Number(a.value ?? 0));
  if (!rows.length) return null;
  const total = rows
    .filter((entry) => entry.dataKey !== PREVIOUS_KEY)
    .reduce((sum, entry) => sum + Number(entry.value ?? 0), 0);
  const title = timeAxis
    ? formatPeriodFull(String(label ?? ""), grain)
    : String(label ?? "");
  return (
    <div className="sx-tip" role="status">
      <p className="sx-tip-title">{title}</p>
      {rows.map((entry) => {
        const key = String(entry.dataKey ?? entry.name ?? "");
        const name = key === PREVIOUS_KEY
          ? "Previous period"
          : String(entry.name ?? key).replaceAll("_", " ");
        return (
          <div className="sx-tip-row" key={key}>
            <span
              className="sx-swatch sx-swatch--line"
              style={{ background: entry.color ?? entry.stroke ?? entry.fill }}
              aria-hidden
            />
            <span className="sx-tip-name">{name}</span>
            <strong className="sx-tip-value">
              {formatValue(Number(entry.value ?? 0), format)}
            </strong>
          </div>
        );
      })}
      {showTotal && rows.length > 1 && (
        <div className="sx-tip-row sx-tip-row--total">
          <span className="sx-swatch sx-swatch--line" aria-hidden />
          <span className="sx-tip-name">Total</span>
          <strong className="sx-tip-value">{formatValue(total, format)}</strong>
        </div>
      )}
      {clickHint && <p className="sx-tip-hint">Click a bar to filter to it</p>}
    </div>
  );
}

function BuilderExplorer({ onExpert }: { onExpert: () => void }) {
  const chart = useChartColors();
  const initial = useRef(decodeState());
  const chartAreaRef = useRef<HTMLDivElement | null>(null);
  const [catalog, setCatalog] = useState<SemanticCatalog | null>(null);
  const [error, setError] = useState("");
  const [modelName, setModelName] = useState(initial.current.model ?? "");
  const [measures, setMeasures] = useState<string[]>(
    initial.current.measures ? initial.current.measures.split(",") : [],
  );
  const [dimension, setDimension] = useState(initial.current.dim ?? "");
  const [grain, setGrain] = useState(initial.current.grain ?? "day");
  const [range, setRange] = useState(initial.current.range ?? "90d");
  const [customStart, setCustomStart] = useState(initial.current.cstart ?? "");
  const [customEnd, setCustomEnd] = useState(initial.current.cend ?? "");
  const [compare, setCompare] = useState(initial.current.cmp === "1");
  const [filterDim, setFilterDim] = useState(initial.current.fdim ?? "");
  const [filterValues, setFilterValues] = useState<string[]>(() => {
    try { const parsed = JSON.parse(initial.current.fvals ?? "[]"); return Array.isArray(parsed) ? parsed.map(String) : []; } catch { return []; }
  });
  const [filterOptions, setFilterOptions] = useState<string[]>([]);
  const [form, setForm] = useState<ChartForm>(() => {
    const saved = initial.current.form;
    return saved === "line" || saved === "area" || saved === "bar" || saved === "table"
      ? saved
      : "auto";
  });
  const [result, setResult] = useState<SemanticQueryResult | null>(null);
  const [previousResult, setPreviousResult] = useState<SemanticQueryResult | null>(null);
  const [running, setRunning] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [showSql, setShowSql] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [highlighted, setHighlighted] = useState("");
  const [sort, setSort] = useState<{ index: number; direction: "asc" | "desc" } | null>(null);
  const [linkCopied, setLinkCopied] = useState(false);

  const model: SemanticModelInfo | null = useMemo(
    () => catalog?.models.find((entry) => entry.name === modelName) ?? null,
    [catalog, modelName],
  );

  useEffect(() => {
    api.semanticCatalog()
      .then((value) => {
        setCatalog(value);
        const available = value.models.filter((entry) => entry.available);
        if (!initial.current.model && available.length) {
          const first = available[0];
          setModelName(first.name);
          setMeasures([first.measures[0]?.name].filter(Boolean) as string[]);
        }
      })
      .catch((reason) => setError(reason.message));
  }, []);

  function switchModel(name: string) {
    const next = catalog?.models.find((entry) => entry.name === name);
    setModelName(name);
    setMeasures(next?.measures[0] ? [next.measures[0].name] : []);
    setDimension("");
    setFilterDim("");
    setFilterValues([]);
    setGrain(next?.timeColumn ? "day" : "");
    setResult(null);
    setPreviousResult(null);
  }

  // A new grouping or measure set is a new set of series; stale hidden or
  // highlighted names must not silently suppress the fresh ones.
  useEffect(() => {
    setHidden(new Set());
    setHighlighted("");
  }, [modelName, dimension, measures.join(",")]);

  // Populate filter values with the dimension's top members by the first
  // measure, so the picker offers realistic choices without free text.
  useEffect(() => {
    if (!model || !filterDim || !model.measures.length) {
      setFilterOptions([]);
      return;
    }
    let active = true;
    api.semanticQuery({
      model: model.name,
      measures: [model.measures[0].name],
      dimensions: [filterDim],
      limit: 50,
    })
      .then((value) => {
        if (!active) return;
        setFilterOptions(
          value.rows
            .map((row) => String(row[0] ?? ""))
            .filter((entry) => entry !== ""),
        );
      })
      .catch(() => setFilterOptions([]));
    return () => {
      active = false;
    };
  }, [model, filterDim]);

  // The explicit window behind the current selection. `end` is null when the
  // server's completeness clamp should decide; `finalEnd` is the client's view
  // of where the window effectively ends, used to derive the previous window.
  const window_ = useMemo(() => {
    if (!model?.timeColumn) return null;
    if (range === "custom") {
      const start = customStart || null;
      const end = customEnd || null;
      const finalEnd = end
        ?? (model.completenessLagDays > 0 ? isoDaysAgo(model.completenessLagDays) : isoDaysAgo(0));
      return start ? { start, end, finalEnd, days: daysBetween(start, finalEnd) + 1 } : null;
    }
    const preset = RANGE_PRESETS.find((entry) => entry.key === range);
    if (!preset || preset.days === 0) return null;
    const start = isoDaysAgo(preset.days);
    const finalEnd = model.completenessLagDays > 0
      ? isoDaysAgo(model.completenessLagDays)
      : isoDaysAgo(0);
    return { start, end: null, finalEnd, days: daysBetween(start, finalEnd) + 1 };
  }, [model, range, customStart, customEnd]);

  const compareReady = Boolean(compare && window_ && window_.days > 0);

  const runQuery = useCallback(() => {
    if (!model || !measures.length) return;
    const timed = Boolean(model.timeColumn) && grain !== "";
    setRunning(true);
    announceActivity("analytics-query", true);
    setQueryError("");
    const request: SemanticQueryRequest = {
      model: model.name,
      measures,
      dimensions: dimension ? [dimension] : [],
      filters:
        filterDim && filterValues.length
          ? { [filterDim]: filterValues }
          : {},
      grain: timed ? (grain as "day" | "week" | "month") : null,
      start: window_?.start ?? null,
      end: window_?.end ?? null,
      limit: 2000,
    };
    // The previous window mirrors the request exactly (same grouping, grain,
    // filters, and limit) so both windows suffer identical caps and clamps
    // and the comparison stays honest.
    const previousRequest: SemanticQueryRequest | null =
      compareReady && window_
        ? {
          ...request,
          start: isoShift(window_.start, -window_.days),
          end: isoShift(window_.start, -1),
        }
        : null;
    Promise.all([
      api.semanticQuery(request),
      previousRequest ? api.semanticQuery(previousRequest) : Promise.resolve(null),
    ])
      .then(([current, previous]) => {
        setResult(current);
        setPreviousResult(previous);
      })
      .catch((reason) => {
        setQueryError(
          reason instanceof Error ? reason.message : "The query failed.",
        );
        setResult(null);
        setPreviousResult(null);
      })
      .finally(() => {
        setRunning(false);
        announceActivity("analytics-query", false);
      });
  }, [
    model, measures, dimension, grain, range, customStart, customEnd,
    compareReady, window_, filterDim, filterValues,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(runQuery, 250);
    return () => window.clearTimeout(timer);
  }, [runQuery]);

  // The shareable link tracks every selection, including view-only state
  // like the chart form, without triggering a refetch.
  useEffect(() => {
    if (!model) return;
    encodeState({
      model: model.name,
      measures: measures.join(","),
      dim: dimension,
      grain,
      range,
      cstart: range === "custom" ? customStart : "",
      cend: range === "custom" ? customEnd : "",
      cmp: compare ? "1" : "",
      form: form === "auto" ? "" : form,
      fdim: filterDim,
      fvals: filterValues.length ? JSON.stringify(filterValues) : "",
    });
  }, [
    model, measures, dimension, grain, range, customStart, customEnd,
    compare, form, filterDim, filterValues,
  ]);

  const measureInfo = useMemo(
    () =>
      Object.fromEntries(
        (model?.measures ?? []).map((entry) => [entry.name, entry]),
      ),
    [model],
  );

  // Shape query rows for the chart. With a time grain and a dimension the
  // rows pivot wide (one series per dimension member, top N + Other); with
  // only a grain each measure is a series; with only a dimension the rows
  // chart as categories.
  const shaped = useMemo(() => {
    if (!result || !model) return null;
    const timeIndex = result.columns.findIndex((c) => c.kind === "time");
    const dimIndex = result.columns.findIndex((c) => c.kind === "dimension");
    const measureIndexes = result.columns
      .map((column, index) => ({ column, index }))
      .filter((entry) => entry.column.kind === "measure");
    if (!measureIndexes.length) return null;
    const primary = measureIndexes[0];

    if (timeIndex >= 0 && dimIndex >= 0) {
      const totals = new Map<string, number>();
      for (const row of result.rows) {
        const key = String(row[dimIndex] ?? "—");
        totals.set(
          key,
          (totals.get(key) ?? 0) + Number(row[primary.index] ?? 0),
        );
      }
      const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]);
      const kept = ranked.slice(0, MAX_SERIES).map(([key]) => key);
      const folded = ranked.length > MAX_SERIES;
      const points = new Map<string, Record<string, number | string>>();
      for (const row of result.rows) {
        const period = String(row[timeIndex] ?? "").slice(0, 10);
        const point = points.get(period) ?? { period };
        const key = String(row[dimIndex] ?? "—");
        const series = kept.includes(key) ? key : OTHER_SERIES;
        point[series] =
          Number(point[series] ?? 0) + Number(row[primary.index] ?? 0);
        points.set(period, point);
      }
      // Fixed hue order follows the entity name, not its rank, so a filter
      // change never repaints surviving series.
      const seriesNames = [...kept].sort((a, b) => a.localeCompare(b));
      if (folded) seriesNames.push(OTHER_SERIES);
      // A member with no rows in a period genuinely spent zero there; leave
      // no holes for lines to break on.
      const data = [...points.values()].sort((a, b) =>
        String(a.period).localeCompare(String(b.period)),
      );
      for (const point of data) {
        for (const name of seriesNames) {
          if (!(name in point)) point[name] = 0;
        }
      }
      return {
        kind: "time" as const,
        data,
        series: seriesNames,
        format: primary.column.format,
        primaryName: primary.column.name,
      };
    }
    if (timeIndex >= 0) {
      const sameFormat = measureIndexes.filter(
        (entry) => entry.column.format === primary.column.format,
      );
      return {
        kind: "time" as const,
        data: result.rows.map((row) => {
          const point: Record<string, number | string> = {
            period: String(row[timeIndex] ?? "").slice(0, 10),
          };
          for (const entry of sameFormat) {
            point[entry.column.name] = Number(row[entry.index] ?? 0);
          }
          return point;
        }),
        series: sameFormat.map((entry) => entry.column.name),
        format: primary.column.format,
        primaryName: primary.column.name,
      };
    }
    if (dimIndex >= 0) {
      return {
        kind: "category" as const,
        data: result.rows.slice(0, 20).map((row) => ({
          label: String(row[dimIndex] ?? "—"),
          value: Number(row[primary.index] ?? 0),
        })),
        series: [primary.column.name],
        format: primary.column.format,
        primaryName: primary.column.name,
      };
    }
    return {
      kind: "stat" as const,
      data: measureIndexes.map((entry) => ({
        label: entry.column.name,
        value: result.rows[0]?.[entry.index] ?? null,
        format: entry.column.format,
      })),
      series: [],
      format: primary.column.format,
      primaryName: primary.column.name,
    };
  }, [result, model]);

  // Previous-period overlay: index-aligned per bucket, drawn only without a
  // dimension split (overlaying a second family of series is noise).
  const overlay = useMemo(() => {
    if (!previousResult || !shaped || shaped.kind !== "time" || dimension) return null;
    const timeIndex = previousResult.columns.findIndex((c) => c.kind === "time");
    const primaryIndex = previousResult.columns.findIndex(
      (c) => c.kind === "measure" && c.name === shaped.primaryName,
    );
    if (timeIndex < 0 || primaryIndex < 0) return null;
    const buckets = previousResult.rows
      .map((row) => ({
        period: String(row[timeIndex] ?? "").slice(0, 10),
        value: Number(row[primaryIndex] ?? 0),
      }))
      .sort((a, b) => a.period.localeCompare(b.period));
    if (!buckets.length) return null;
    return shaped.data.map((point, index) => ({
      ...point,
      [PREVIOUS_KEY]: buckets[index]?.value ?? null,
    }));
  }, [previousResult, shaped, dimension]);

  // Recharts renders `null` as a gap, but its data type does not admit it;
  // the cast is confined here.
  const chartData = useMemo(() => {
    if (!shaped || shaped.kind !== "time") return [] as Record<string, string | number>[];
    return (overlay ?? shaped.data) as Record<string, string | number>[];
  }, [shaped, overlay]);

  const multiYear = useMemo(() => {
    if (!shaped || shaped.kind !== "time" || !shaped.data.length) return false;
    const first = String(shaped.data[0].period ?? "").slice(0, 4);
    const last = String(shaped.data[shaped.data.length - 1].period ?? "").slice(0, 4);
    return first !== last;
  }, [shaped]);

  // Headline stats for the KPI strip. Per-bucket grouping happens first so a
  // dimension split cannot double-count; mean-like measures (rates,
  // utilization) average instead of summing.
  function headlineStats(source: SemanticQueryResult | null) {
    if (!source) return null;
    const timeIndex = source.columns.findIndex((c) => c.kind === "time");
    const primary = source.columns.findIndex((c) => c.kind === "measure");
    if (primary < 0 || !source.rows.length) return null;
    const primaryColumn = source.columns[primary];
    const meanLike = isMeanLike(primaryColumn.name, primaryColumn.format);
    if (timeIndex < 0) {
      const values = source.rows.map((row) => Number(row[primary] ?? 0));
      const total = values.reduce((sum, value) => sum + value, 0);
      return {
        meanLike,
        headline: meanLike ? total / values.length : total,
        average: total / values.length,
        peak: null as { period: string; value: number } | null,
        buckets: values.length,
      };
    }
    const perBucket = new Map<string, { sum: number; count: number }>();
    for (const row of source.rows) {
      const period = String(row[timeIndex] ?? "").slice(0, 10);
      const bucket = perBucket.get(period) ?? { sum: 0, count: 0 };
      bucket.sum += Number(row[primary] ?? 0);
      bucket.count += 1;
      perBucket.set(period, bucket);
    }
    const buckets = [...perBucket.entries()].map(([period, bucket]) => ({
      period,
      value: meanLike ? bucket.sum / bucket.count : bucket.sum,
    }));
    const total = buckets.reduce((sum, bucket) => sum + bucket.value, 0);
    const peak = buckets.reduce(
      (best, bucket) => (bucket.value > (best?.value ?? -Infinity) ? bucket : best),
      null as { period: string; value: number } | null,
    );
    return {
      meanLike,
      headline: meanLike ? total / buckets.length : total,
      average: total / buckets.length,
      peak,
      buckets: buckets.length,
    };
  }

  const stats = useMemo(() => headlineStats(result), [result]);
  const previousStats = useMemo(() => headlineStats(previousResult), [previousResult]);

  const effectiveForm: ChartForm = useMemo(() => {
    if (form !== "auto") return form;
    if (!shaped) return "table";
    if (shaped.kind === "time") return shaped.series.length > 1 ? "line" : "area";
    if (shaped.kind === "category") return "bar";
    return "table";
  }, [form, shaped]);

  // Reset the table sort when the column set changes; a stale index would
  // sort by an unrelated column.
  const columnsKey = result?.columns.map((column) => column.name).join("|") ?? "";
  useEffect(() => {
    setSort(null);
  }, [columnsKey]);

  const sortedRows = useMemo(() => {
    if (!result) return [];
    // A stale sort index can outlive a column-set change for one render
    // (the reset effect runs after); fall back to server order until then.
    if (!sort || !result.columns[sort.index]) return result.rows;
    const column = result.columns[sort.index];
    const factor = sort.direction === "asc" ? 1 : -1;
    return [...result.rows].sort((a, b) => {
      const left = a[sort.index];
      const right = b[sort.index];
      if (column.kind === "measure") {
        return (Number(left ?? 0) - Number(right ?? 0)) * factor;
      }
      return String(left ?? "").localeCompare(String(right ?? "")) * factor;
    });
  }, [result, sort]);

  function cycleSort(index: number) {
    setSort((current) => {
      const kind = result?.columns[index]?.kind;
      const first: "asc" | "desc" = kind === "measure" ? "desc" : "asc";
      const second: "asc" | "desc" = first === "desc" ? "asc" : "desc";
      if (current?.index !== index) return { index, direction: first };
      if (current.direction === first) return { index, direction: second };
      return null;
    });
  }

  // Dimension members keep their slot across filter changes (persisted per
  // model+dimension); measure series color by selection order, which is
  // already stable for a given selection.
  const seriesSlots = useMemo(() => {
    if (!shaped || shaped.kind !== "time" || !dimension) return null;
    return assignSeriesSlots(`${modelName}:${dimension}`, shaped.series);
  }, [shaped, dimension, modelName]);

  /** Color follows the entity: real series take persistent palette slots,
   *  the folded tail always wears the de-emphasis gray. */
  const seriesColor = useCallback(
    (name: string, index: number) => {
      if (name === OTHER_SERIES) return `rgb(${getOtherChannels()})`;
      const slot = seriesSlots?.[name] ?? index;
      return chart.series[slot % chart.series.length];
    },
    [chart, seriesSlots],
  );

  function getOtherChannels(): string {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue("--chart-other")
      .trim();
    return value ? value.split(/\s+/).join(", ") : "145, 161, 153";
  }

  function toggleSeries(name: string, isolate: boolean) {
    setHidden((current) => {
      const visible = (shaped?.series ?? []).filter((entry) => !current.has(entry));
      if (isolate) {
        // Shift-click isolates; on an already-isolated series it restores all.
        if (visible.length === 1 && visible[0] === name) return new Set();
        return new Set((shaped?.series ?? []).filter((entry) => entry !== name));
      }
      const next = new Set(current);
      if (next.has(name)) {
        next.delete(name);
      } else {
        if (visible.length === 1) return current;
        next.add(name);
      }
      return next;
    });
  }

  function drillTo(member: string) {
    if (!dimension || !member) return;
    setFilterDim(dimension);
    setFilterValues([member]);
  }

  function downloadCsv() {
    if (!result) return;
    const header = result.columns.map((column) => column.name).join(",");
    const lines = result.rows.map((row) =>
      row
        .map((value) => {
          const text = value === null || value === undefined ? "" : String(value);
          return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
        })
        .join(","),
    );
    const blob = new Blob([[header, ...lines].join("\n")], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `flux-${modelName}-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  function downloadPng() {
    const svg = chartAreaRef.current?.querySelector("svg");
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const clone = svg.cloneNode(true) as SVGSVGElement;
    clone.setAttribute("width", String(rect.width));
    clone.setAttribute("height", String(rect.height));
    clone.setAttribute("font-family", getComputedStyle(document.body).fontFamily);
    const source = inlineCssVariables(new XMLSerializer().serializeToString(clone));
    const url = URL.createObjectURL(
      new Blob([source], { type: "image/svg+xml;charset=utf-8" }),
    );
    const image = new Image();
    image.onload = () => {
      const scale = 2;
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(rect.width * scale);
      canvas.height = Math.round(rect.height * scale);
      const context = canvas.getContext("2d");
      if (!context) return;
      context.fillStyle = chart.surface;
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.scale(scale, scale);
      context.drawImage(image, 0, 0);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob) => {
        if (!blob) return;
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `flux-${modelName}-${new Date().toISOString().slice(0, 10)}.png`;
        link.click();
        URL.revokeObjectURL(link.href);
      });
    };
    image.src = url;
  }

  function copyLink() {
    navigator.clipboard.writeText(window.location.href).then(() => {
      setLinkCopied(true);
      window.setTimeout(() => setLinkCopied(false), 1600);
    });
  }

  if (error) return <ErrorPanel message={error} />;
  if (!catalog) return <Loading />;
  if (!catalog.models.some((entry) => entry.available)) {
    return (
      <EmptyState
        title="The semantic views are not in the current snapshot yet"
        description="This deployment added the semantic layer, but the analytics snapshot being served predates it. Models appear automatically after the next data synchronization publishes."
      />
    );
  }
  if (!model) return <Loading />;

  const tickStyle = { fill: "rgb(var(--text-muted))", fontSize: 11 };
  const axisFormat = (value: number) => compactValue(value, shaped?.format ?? "number");
  const primaryMeasure = measureInfo[shaped?.primaryName ?? ""] ?? null;
  const stacked = effectiveForm === "bar" && shaped?.kind === "time" && Boolean(dimension);
  const legendMark: "line" | "rect" = effectiveForm === "bar" ? "rect" : "line";
  const dimOpacity = (name: string) =>
    highlighted && highlighted !== name ? 0.25 : 1;
  const includesUnfinalized =
    range === "custom"
    && Boolean(customEnd)
    && model.completenessLagDays > 0
    && customEnd > isoDaysAgo(model.completenessLagDays);

  const deltaTile = (() => {
    if (!compareReady || !stats || !previousStats) return null;
    const current = stats.headline;
    const previous = previousStats.headline;
    if (!Number.isFinite(current) || !Number.isFinite(previous)) return null;
    const higherIs = primaryMeasure?.higherIs ?? "neutral";
    const rising = current >= previous;
    const tone = higherIs === "neutral"
      ? "neutral"
      : (rising ? higherIs === "good" : higherIs === "bad")
        ? "good"
        : "bad";
    const percentFormat = (shaped?.format ?? "number") === "percent";
    const text = percentFormat
      ? `${current - previous >= 0 ? "+" : ""}${(current - previous).toFixed(1)}pp`
      : previous === 0
        ? "new"
        : `${current - previous >= 0 ? "+" : ""}${(((current - previous) / Math.abs(previous)) * 100).toFixed(1)}%`;
    return { tone, text, rising, previous };
  })();

  const kpis = (() => {
    if (!shaped || !stats || shaped.kind === "stat") return [];
    const format = shaped.format;
    const tiles: { label: string; value: string; sub?: string }[] = [];
    if (shaped.kind === "time") {
      const grainNoun = grain === "month" ? "Monthly" : grain === "week" ? "Weekly" : "Daily";
      tiles.push({
        label: stats.meanLike ? "Window average" : "Window total",
        value: formatValue(stats.headline, format),
        sub: `${stats.buckets.toLocaleString()} ${grain || "day"} buckets`,
      });
      if (!stats.meanLike && stats.average !== undefined) {
        tiles.push({
          label: `${grainNoun} average`,
          value: formatValue(stats.average, format),
        });
      }
      if (stats.peak) {
        tiles.push({
          label: "Peak",
          value: formatValue(stats.peak.value, format),
          sub: formatPeriodFull(stats.peak.period, grain),
        });
      }
    } else {
      tiles.push({
        label: stats.meanLike ? "Average across members" : "Total",
        value: formatValue(stats.headline, format),
        sub: `${result?.rowCount.toLocaleString()} members`,
      });
      const top = shaped.data[0] as { label: string; value: number } | undefined;
      if (top && !stats.meanLike && stats.headline > 0) {
        tiles.push({
          label: "Top member",
          value: formatValue(top.value, format),
          sub: `${top.label} · ${((top.value / stats.headline) * 100).toFixed(1)}% of total`,
        });
      } else if (top) {
        tiles.push({ label: "Top member", value: formatValue(top.value, format), sub: top.label });
      }
    }
    return tiles;
  })();

  return (
    <>
      <PageHeader
        eyebrow="FinOps"
        title="Analytics explorer"
        description="Slice governed metrics by any approved dimension. Every measure comes from the semantic layer, so numbers here match the rest of Flux and Ask Flux."
        action={
          <div className="rz-header-actions">
            <button className="button button--secondary" onClick={onExpert}>
              Expert with Flux
            </button>
            {result ? (<>
            <button className="button button--secondary" onClick={copyLink}>
              {linkCopied ? <Check size={15} /> : <Link2 size={15} />}
              {linkCopied ? "Copied" : "Copy link"}
            </button>
            <button className="button button--secondary" onClick={() => setShowSql((v) => !v)}>
              <Code2 size={15} />{showSql ? "Hide SQL" : "View SQL"}
            </button>
            {shaped && shaped.kind !== "stat" && effectiveForm !== "table" && (
              <button className="button button--secondary" onClick={downloadPng}>
                <ImageIcon size={15} />PNG
              </button>
            )}
            <button className="button button--secondary" onClick={downloadCsv}>
              <Download size={15} />CSV
            </button>
            </>) : null}
          </div>
        }
      />

      <Card className="opportunity-filter-card sx-controls">
        <div className="filters filters--wrap">
          <label className="select-field" title={model.description}>
            <select value={modelName} onChange={(event) => switchModel(event.target.value)} aria-label="Semantic model">
              {catalog.models.filter((entry) => entry.available).map((entry) => (
                <option key={entry.name} value={entry.name}>{entry.displayName}</option>
              ))}
            </select>
          </label>
          <label className="select-field">
            <select value={dimension} onChange={(event) => setDimension(event.target.value)} aria-label="Group by">
              <option value="">No grouping</option>
              {model.dimensions.map((entry) => (
                <option key={entry.name} value={entry.name}>by {entry.name}</option>
              ))}
            </select>
          </label>
          {model.timeColumn && (
            <label className="select-field">
              <select value={grain} onChange={(event) => setGrain(event.target.value)} aria-label="Time grain">
                <option value="day">Daily</option>
                <option value="week">Weekly</option>
                <option value="month">Monthly</option>
                <option value="">No time axis</option>
              </select>
            </label>
          )}
          {model.timeColumn && grain !== "" && (
            <div className="sx-presets" role="group" aria-label="Time range">
              {RANGE_PRESETS.map((preset) => (
                <button
                  key={preset.key}
                  className={range === preset.key ? "active" : ""}
                  onClick={() => setRange(preset.key)}
                >
                  {preset.label}
                </button>
              ))}
              <button
                className={range === "custom" ? "active" : ""}
                onClick={() => setRange("custom")}
              >
                Custom
              </button>
            </div>
          )}
          {model.timeColumn && grain !== "" && range === "custom" && (
            <div className="sx-daterange" role="group" aria-label="Custom date range">
              <input
                type="date"
                value={customStart}
                max={customEnd || undefined}
                onChange={(event) => setCustomStart(event.target.value)}
                aria-label="Start date"
              />
              <span aria-hidden>–</span>
              <input
                type="date"
                value={customEnd}
                min={customStart || undefined}
                onChange={(event) => setCustomEnd(event.target.value)}
                aria-label="End date"
              />
            </div>
          )}
          {model.timeColumn && (
            <button
              className={`sx-chip sx-chip--compare ${compare ? "sx-chip--on" : ""}`}
              aria-pressed={compare}
              disabled={!window_}
              title={window_
                ? "Compare this window against the equal-length window immediately before it"
                : "Pick a bounded time range to compare periods"}
              onClick={() => setCompare((value) => !value)}
            >
              <GitCompareArrows size={13} />vs previous period
            </button>
          )}
          <label className="select-field">
            <select value={filterDim} onChange={(event) => { setFilterDim(event.target.value); setFilterValues([]); }} aria-label="Filter dimension">
              <option value="">No filter</option>
              {model.dimensions.map((entry) => (
                <option key={entry.name} value={entry.name}>filter {entry.name}</option>
              ))}
            </select>
          </label>
        </div>
        <div className="sx-measures" role="group" aria-label="Measures">
          {model.measures.map((entry) => {
            const selected = measures.includes(entry.name);
            return (
              <button
                key={entry.name}
                className={`sx-chip ${selected ? "sx-chip--on" : ""}`}
                title={entry.description || entry.name}
                aria-pressed={selected}
                onClick={() =>
                  setMeasures((current) =>
                    selected
                      ? current.filter((name) => name !== entry.name)
                      : [...current, entry.name],
                  )
                }
              >
                {entry.name.replaceAll("_", " ")}
              </button>
            );
          })}
        </div>
        {filterDim && filterOptions.length > 0 && (
          <div className="sx-measures" role="group" aria-label="Filter values">
            {filterOptions.slice(0, 24).map((value) => {
              const selected = filterValues.includes(value);
              return (
                <button
                  key={value}
                  className={`sx-chip sx-chip--filter ${selected ? "sx-chip--on" : ""}`}
                  aria-pressed={selected}
                  onClick={() =>
                    setFilterValues((current) =>
                      selected
                        ? current.filter((entry) => entry !== value)
                        : [...current, value],
                    )
                  }
                >
                  {value}
                </button>
              );
            })}
          </div>
        )}
      </Card>

      {queryError && <div className="inline-alert inline-alert--error">{queryError}</div>}

      {showSql && result && (
        <Card className="sx-sql"><pre>{result.sql}</pre></Card>
      )}

      {kpis.length > 0 && (
        <div className="sx-kpis">
          {kpis.map((tile) => (
            <div className="sx-kpi" key={tile.label}>
              <span>{tile.label}</span>
              <strong>{tile.value}</strong>
              {tile.sub && <small>{tile.sub}</small>}
            </div>
          ))}
          {deltaTile && (
            <div className="sx-kpi">
              <span>vs previous period</span>
              <strong className={`sx-delta sx-delta--${deltaTile.tone}`}>
                {deltaTile.rising ? <ArrowUp size={15} /> : <ArrowDown size={15} />}
                {deltaTile.text}
              </strong>
              <small>
                previous {stats?.meanLike ? "average" : "total"} {formatValue(deltaTile.previous, shaped?.format ?? "number")}
              </small>
            </div>
          )}
        </div>
      )}

      <Card className="chart-card sx-chart-card">
        <div className="sx-chart-head">
          <div>
            <h2>{measures.map((name) => name.replaceAll("_", " ")).join(", ") || "Pick a measure"}</h2>
            <p className="muted">
              {model.displayName}
              {dimension ? ` by ${dimension}` : ""}
              {model.timeColumn && grain ? ` · ${grain}` : ""}
              {result ? ` · ${result.rowCount.toLocaleString()} rows` : ""}
              {compareReady && previousResult
                ? ` · vs previous period${overlay ? " (dashed)" : ""}`
                : ""}
              {running && result ? " · updating…" : ""}
              {result?.appliedDefaults && Object.entries(result.appliedDefaults).map(([name, values]) => (
                ` · ${name} = ${values.join("/")} (governed default — filter or group by ${name} to change)`
              )).join("")}
              {includesUnfinalized
                ? " · includes days Azure has not finalized yet"
                : model.completenessLagDays > 0 && model.timeColumn && grain !== ""
                  && !(range === "custom" && customEnd)
                  ? ` · excludes the last ${model.completenessLagDays} days while Cost Management data finishes arriving`
                  : ""}
            </p>
          </div>
          <div className="rz-tabswitch" role="group" aria-label="Chart form">
            {(["auto", "line", "area", "bar", "table"] as ChartForm[]).map((entry) => (
              <button
                key={entry}
                className={form === entry ? "active" : ""}
                onClick={() => setForm(entry)}
              >
                {entry === "table" ? <Table2 size={12} /> : entry === "auto" ? <LineChartIcon size={12} /> : null}
                {entry}
              </button>
            ))}
          </div>
        </div>

        {running && !result && <Loading />}
        {!running && !measures.length && (
          <EmptyState title="Pick at least one measure" description="Measures are the governed calculations of the semantic layer; choose one or more chips above." />
        )}

        {shaped && shaped.kind === "stat" && form !== "table" && (
          <div className="metrics-grid sx-stats">
            {shaped.data.map((entry: any) => (
              <Card className="metric-card" key={entry.label}>
                <span>{entry.label.replaceAll("_", " ")}</span>
                <strong>{formatValue(entry.value, entry.format)}</strong>
                <small>{measureInfo[entry.label]?.description || model.grain}</small>
              </Card>
            ))}
          </div>
        )}

        {shaped && shaped.kind !== "stat" && effectiveForm !== "table" && (
          <>
            <div
              className={`chart-area sx-chart-area ${running ? "sx-refetching" : ""}`}
              ref={chartAreaRef}
            >
              <ResponsiveContainer width="100%" height="100%">
                {effectiveForm === "bar" || shaped.kind === "category" ? (
                  <BarChart data={shaped.kind === "category" ? shaped.data : chartData} barCategoryGap="24%">
                    <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                    <XAxis
                      dataKey={shaped.kind === "category" ? "label" : "period"}
                      tickLine={false}
                      axisLine={false}
                      tick={tickStyle}
                      tickMargin={6}
                      interval="preserveStartEnd"
                      tickFormatter={(value: string) =>
                        shaped.kind === "category"
                          ? (value.length > 16 ? `${value.slice(0, 15)}…` : value)
                          : formatPeriodTick(value, grain, multiYear)}
                    />
                    <YAxis tickLine={false} axisLine={false} tick={tickStyle} tickFormatter={axisFormat} width={58} />
                    <Tooltip
                      content={(
                        <ExplorerTooltip
                          format={shaped.format}
                          grain={grain}
                          hidden={hidden}
                          timeAxis={shaped.kind !== "category"}
                          showTotal={Boolean(dimension) && shaped.kind === "time"}
                          clickHint={shaped.kind === "category"}
                        />
                      )}
                      cursor={{ fill: "rgb(var(--border) / 0.35)" }}
                    />
                    {shaped.kind === "category" ? (
                      <Bar
                        isAnimationActive={false}
                        dataKey="value"
                        name={shaped.primaryName}
                        fill={chart.series[0]}
                        radius={[4, 4, 0, 0]}
                        maxBarSize={24}
                        cursor={dimension ? "pointer" : undefined}
                        onClick={(entry: any) => drillTo(String(entry?.payload?.label ?? entry?.label ?? ""))}
                      />
                    ) : (
                      shaped.series.map((name, index) => (
                        <Bar
                          isAnimationActive={false}
                          key={name}
                          dataKey={name}
                          hide={hidden.has(name)}
                          stackId={stacked ? "stack" : undefined}
                          fill={seriesColor(name, index)}
                          fillOpacity={dimOpacity(name)}
                          stroke={stacked ? chart.surface : undefined}
                          strokeWidth={stacked ? 2 : 0}
                          radius={stacked ? 0 : [4, 4, 0, 0]}
                          maxBarSize={24}
                        />
                      ))
                    )}
                  </BarChart>
                ) : effectiveForm === "area" ? (
                  <AreaChart data={chartData}>
                    <defs>
                      <linearGradient id="sxFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={chart.series[0]} stopOpacity={0.16} />
                        <stop offset="100%" stopColor={chart.series[0]} stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                    <XAxis
                      dataKey="period"
                      tickLine={false}
                      axisLine={false}
                      tick={tickStyle}
                      tickMargin={6}
                      interval="preserveStartEnd"
                      tickFormatter={(value: string) => formatPeriodTick(value, grain, multiYear)}
                    />
                    <YAxis tickLine={false} axisLine={false} tick={tickStyle} tickFormatter={axisFormat} width={58} />
                    <Tooltip
                      content={(
                        <ExplorerTooltip
                          format={shaped.format}
                          grain={grain}
                          hidden={hidden}
                          timeAxis
                          showTotal={Boolean(dimension)}
                          clickHint={false}
                        />
                      )}
                    />
                    {shaped.series.map((name, index) => (
                      <Area
                        isAnimationActive={false}
                        key={name}
                        type="monotone"
                        dataKey={name}
                        hide={hidden.has(name)}
                        stroke={seriesColor(name, index)}
                        strokeWidth={2}
                        strokeOpacity={dimOpacity(name)}
                        fill={index === 0 ? "url(#sxFill)" : "transparent"}
                        fillOpacity={dimOpacity(name)}
                        activeDot={{ r: 4, stroke: chart.surface, strokeWidth: 2 }}
                      />
                    ))}
                    {overlay && (
                      <Area
                        isAnimationActive={false}
                        type="monotone"
                        dataKey={PREVIOUS_KEY}
                        stroke={`rgb(${getOtherChannels()})`}
                        strokeWidth={2}
                        strokeDasharray="5 4"
                        fill="transparent"
                        dot={false}
                        activeDot={{ r: 4, stroke: chart.surface, strokeWidth: 2 }}
                      />
                    )}
                  </AreaChart>
                ) : (
                  <LineChart data={chartData}>
                    <CartesianGrid vertical={false} stroke="rgb(var(--border))" />
                    <XAxis
                      dataKey="period"
                      tickLine={false}
                      axisLine={false}
                      tick={tickStyle}
                      tickMargin={6}
                      interval="preserveStartEnd"
                      tickFormatter={(value: string) => formatPeriodTick(value, grain, multiYear)}
                    />
                    <YAxis tickLine={false} axisLine={false} tick={tickStyle} tickFormatter={axisFormat} width={58} />
                    <Tooltip
                      content={(
                        <ExplorerTooltip
                          format={shaped.format}
                          grain={grain}
                          hidden={hidden}
                          timeAxis
                          showTotal={Boolean(dimension)}
                          clickHint={false}
                        />
                      )}
                    />
                    {shaped.series.map((name, index) => (
                      <Line
                        isAnimationActive={false}
                        key={name}
                        type="monotone"
                        dataKey={name}
                        hide={hidden.has(name)}
                        stroke={seriesColor(name, index)}
                        strokeWidth={2}
                        strokeOpacity={dimOpacity(name)}
                        dot={false}
                        activeDot={{ r: 4, stroke: chart.surface, strokeWidth: 2 }}
                      />
                    ))}
                    {overlay && (
                      <Line
                        isAnimationActive={false}
                        type="monotone"
                        dataKey={PREVIOUS_KEY}
                        stroke={`rgb(${getOtherChannels()})`}
                        strokeWidth={2}
                        strokeDasharray="5 4"
                        dot={false}
                        activeDot={{ r: 4, stroke: chart.surface, strokeWidth: 2 }}
                      />
                    )}
                  </LineChart>
                )}
              </ResponsiveContainer>
            </div>
            {shaped.series.length > 1 && (
              <div className="sx-legend" role="group" aria-label="Series">
                {shaped.series.map((name, index) => {
                  const isHidden = hidden.has(name);
                  return (
                    <button
                      key={name}
                      aria-pressed={!isHidden}
                      className={isHidden ? "sx-legend-item sx-legend-item--off" : "sx-legend-item"}
                      title="Click to toggle; shift-click to isolate"
                      onClick={(event) => toggleSeries(name, event.shiftKey)}
                      onMouseEnter={() => setHighlighted(name)}
                      onMouseLeave={() => setHighlighted("")}
                      onFocus={() => setHighlighted(name)}
                      onBlur={() => setHighlighted("")}
                    >
                      <span
                        className={`sx-swatch sx-swatch--${legendMark}`}
                        style={isHidden ? { borderColor: seriesColor(name, index) } : { background: seriesColor(name, index) }}
                        aria-hidden
                      />
                      {name}
                    </button>
                  );
                })}
                {overlay && (
                  <span className="sx-legend-item sx-legend-item--static">
                    <span className="sx-swatch sx-swatch--dashed" aria-hidden />
                    Previous period
                  </span>
                )}
              </div>
            )}
            {shaped.series.length <= 1 && overlay && (
              <div className="sx-legend" role="group" aria-label="Series">
                <span className="sx-legend-item sx-legend-item--static">
                  <span className="sx-swatch sx-swatch--dashed" aria-hidden />
                  Previous period
                </span>
              </div>
            )}
          </>
        )}
      </Card>

      {result && (
        <Card className="sx-table-card">
          <div className="section-heading"><div><h2>Result rows</h2><p>The exact rows behind the chart — the accessible, copyable view. Click a header to sort.</p></div></div>
          <div className={`sx-table ${running ? "sx-refetching" : ""}`}>
            <table>
              <thead>
                <tr>
                  {result.columns.map((column, index) => (
                    <th
                      key={column.name}
                      className={column.kind === "measure" ? "sx-num" : ""}
                      aria-sort={sort?.index === index
                        ? (sort.direction === "asc" ? "ascending" : "descending")
                        : undefined}
                    >
                      <button className="sx-sort" onClick={() => cycleSort(index)}>
                        {column.name.replaceAll("_", " ")}
                        {sort?.index === index && (
                          sort.direction === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />
                        )}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sortedRows.slice(0, 200).map((row, index) => (
                  <tr key={index}>
                    {row.map((value, cellIndex) => {
                      const column = result.columns[cellIndex];
                      return (
                        <td key={cellIndex} className={column.kind === "measure" ? "sx-num" : ""}>
                          {column.kind === "measure"
                            ? formatValue(value, column.format)
                            : String(value ?? "—").slice(0, 120)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            {result.rows.length > 200 && (
              <p className="muted">Showing 200 of {result.rows.length.toLocaleString()} rows — download the CSV for the full result.</p>
            )}
          </div>
        </Card>
      )}
    </>
  );
}

export function AnalyticsExplorerPage() {
  const [mode, setMode] = useState<"builder" | "expert">("builder");
  return mode === "expert" ? (
    <ExpertExplorerPage onBuilder={() => setMode("builder")} />
  ) : (
    <BuilderExplorer onExpert={() => setMode("expert")} />
  );
}

