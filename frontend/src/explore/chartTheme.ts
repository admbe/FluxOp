/**
 * explore/chartTheme.ts — shared chart chrome for the Explore surface.
 *
 * Every colour here comes from the `--chart-*` / surface tokens in tokens.css
 * via `useChartColors()`, so the light, Graphite-deep and Vibe-code themes all
 * get their own validated palette instead of one hard-coded hex set. Nothing in
 * explore/ should name a literal colour.
 */
import { useChartColors, type FluxChartColors } from "../theme";

export type ChartColors = FluxChartColors;

/** Token ceiling. tokens.css ships --chart-1..8 and one --chart-other. */
export const MAX_SERIES = 8;
export const OTHER_LABEL = "Other";

export { useChartColors };

/**
 * Choose which dimension values get their own colour.
 *
 * Slots are assigned in sequence and never cycled: a ninth generated hue is not
 * distinguishable from an existing slot under colour-vision deficiency, so the
 * tail folds into a single "Other" bucket instead.
 */
export function foldSeriesKeys(
  totals: ReadonlyMap<string, number>,
  max: number = MAX_SERIES,
): string[] {
  const ordered = [...totals.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([key]) => key);
  if (ordered.length <= max) return ordered;
  return [...ordered.slice(0, max - 1), OTHER_LABEL];
}

/** Map a dimension value onto its slot colour. "Other" always takes the grey. */
export function seriesColor(
  colors: ChartColors,
  keys: readonly string[],
  key: string,
): string {
  if (key === OTHER_LABEL) return colors.other;
  const index = keys.indexOf(key);
  if (index < 0 || index >= colors.series.length) return colors.other;
  return colors.series[index] ?? colors.other;
}

/** Fold a raw dimension value onto one of the chosen keys. */
export function bucketFor(keys: readonly string[], value: string): string {
  return keys.includes(value) ? value : OTHER_LABEL;
}

/**
 * Recharts chrome. Gridlines are solid hairlines — dashing reads as
 * "projection" or "threshold" when it is only a grid — and horizontal only,
 * because vertical rules duplicate the category ticks.
 */
export function gridProps(colors: ChartColors) {
  return {
    stroke: colors.gridline,
    strokeWidth: 1,
    vertical: false,
  } as const;
}

export function axisTick(colors: ChartColors) {
  return { fontSize: 11, fill: colors.muted } as const;
}

export function axisLine(colors: ChartColors) {
  return { stroke: colors.gridline } as const;
}

/**
 * The default Recharts tooltip is a white box with a grey border, which is
 * unreadable on either dark theme. Bind it to the surface tokens instead.
 */
export function tooltipProps(colors: ChartColors) {
  return {
    contentStyle: {
      background: colors.surface,
      border: `1px solid ${colors.border}`,
      borderRadius: 10,
      fontSize: 12,
      color: colors.text,
      boxShadow: "0 8px 24px rgb(0 0 0 / 0.14)",
      padding: "8px 10px",
    },
    labelStyle: { color: colors.muted, fontSize: 11, marginBottom: 4 },
    itemStyle: { color: colors.text, padding: "1px 0" },
    cursor: { fill: colors.gridline, fillOpacity: 0.35 },
  } as const;
}

/** Legend swatches, rendered as plain HTML so identity never rides on the
 *  chart alone. */
export function legendItems(
  colors: ChartColors,
  keys: readonly string[],
): Array<{ key: string; color: string }> {
  return keys.map((key) => ({ key, color: seriesColor(colors, keys, key) }));
}
