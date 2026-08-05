import { useEffect, useMemo, useState } from "react";

export type ThemePreference = "light" | "dark" | "system";

function initialTheme(): ThemePreference {
  const saved = window.localStorage.getItem("flux-theme");
  return saved === "light" || saved === "dark" || saved === "system"
    ? saved
    : "system";
}

function applyTheme(preference: ThemePreference): void {
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const resolved = preference === "system"
    ? (systemDark ? "dark" : "light")
    : preference;
  const root = document.documentElement;
  root.classList.toggle("dark", resolved === "dark");
  root.dataset.themePreference = preference;
  root.dataset.theme = resolved;
  root.style.colorScheme = resolved;
  window.dispatchEvent(new CustomEvent("flux-theme-change", {
    detail: { preference, resolved },
  }));
}

export function useTheme() {
  const [theme, setTheme] = useState<ThemePreference>(initialTheme);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => applyTheme(theme);
    window.localStorage.setItem("flux-theme", theme);
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  return { theme, setTheme };
}

function resolvedColor(token: string): string {
  const channels = getComputedStyle(document.documentElement)
    .getPropertyValue(token)
    .trim();
  return channels ? `rgb(${channels})` : "currentColor";
}

export type FluxChartColors = ReturnType<typeof chartColors>;

export function chartColors() {
  const series = Array.from(
    { length: 8 },
    (_, index) => resolvedColor(`--chart-${index + 1}`),
  );
  return {
    primary: resolvedColor("--primary"),
    info: resolvedColor("--info"),
    warning: resolvedColor("--warning"),
    danger: resolvedColor("--danger"),
    purple: resolvedColor("--purple"),
    background: resolvedColor("--background"),
    surface: resolvedColor("--surface-raised"),
    border: resolvedColor("--border-bright"),
    text: resolvedColor("--text"),
    muted: resolvedColor("--text-muted"),
    series,
  };
}

export function useChartColors(): FluxChartColors {
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const update = () => setRevision((value) => value + 1);
    window.addEventListener("flux-theme-change", update);
    return () => window.removeEventListener("flux-theme-change", update);
  }, []);
  return useMemo(() => chartColors(), [revision]);
}
