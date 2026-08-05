/** Tiny inline trend line for table and card rows. Pure SVG, no chart library. */
export function Sparkline({
  values,
  width = 96,
  height = 26,
}: {
  values: number[];
  width?: number;
  height?: number;
}) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const points = values
    .map((value, index) => {
      const x = (index * step).toFixed(1);
      const y = (height - 2 - ((value - min) / span) * (height - 4)).toFixed(1);
      return `${x},${y}`;
    })
    .join(" ");
  const rising = values[values.length - 1] > values[0];
  return (
    <svg
      className="sparkline"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label="Recent daily trend"
    >
      <polyline
        points={points}
        fill="none"
        stroke={rising ? "rgb(var(--danger, 220 80 80))" : "rgb(var(--success, 80 170 120))"}
        strokeWidth={1.6}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
