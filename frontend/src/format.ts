export function compactNumber(value: number): string {
  return Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function currency(value: number | null, currencyCode = "USD"): string {
  if (value === null) return "Not connected";
  return Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currencyCode || "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

export function percent(value: number | null): string {
  return value === null ? "Not connected" : `${value.toFixed(1)}%`;
}

export function shortType(value: string): string {
  const parts = value.split("/");
  return parts.at(-1)?.replaceAll("_", " ") || value;
}

export function relativeTime(value: string | null): string {
  if (!value) return "Never";
  const milliseconds = new Date(value).getTime() - Date.now();
  const minutes = Math.round(milliseconds / 60_000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
}

/** Absolute local timestamp including the date and timezone abbreviation.
 *  A bare clock time is ambiguous: a value from a previous day reads as if
 *  it were from today, which misrepresents how current the data actually is.
 */
export function absoluteTime(value: string | null): string {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Unknown";
  return parsed.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

export function titleCase(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}
