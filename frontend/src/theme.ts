import { useEffect, useMemo, useState } from "react";

export type ThemePreference = "light" | "dark";
export type ThemeVariant = "graphite-deep" | "vibe-code";

const VARIANTS: ThemeVariant[] = ["graphite-deep", "vibe-code"];

export const themeVariants: { id: ThemeVariant; label: string }[] = [
  { id: "graphite-deep", label: "Graphite deep" },
  { id: "vibe-code", label: "Vibe code" },
];

function initialTheme(): ThemePreference {
  const saved = window.localStorage.getItem("flux-theme");
  // Light is Flux's default. Treat the retired "system" preference as light
  // so existing users are returned to the white theme on their next visit.
  return saved === "dark" ? "dark" : "light";
}

function initialVariant(): ThemeVariant {
  const saved = window.localStorage.getItem("flux-theme-variant");
  return VARIANTS.includes(saved as ThemeVariant)
    ? (saved as ThemeVariant)
    : "graphite-deep";
}

function applyTheme(preference: ThemePreference, variant: ThemeVariant): void {
  const resolved = preference;
  const root = document.documentElement;
  root.classList.toggle("dark", resolved === "dark");
  root.dataset.themePreference = preference;
  root.dataset.theme = resolved;
  // Graphite deep is the plain .dark block, so it needs no attribute — and
  // leaving it off keeps the DOM honest about which themes are overrides.
  if (variant === "graphite-deep") {
    delete root.dataset.variant;
  } else {
    root.dataset.variant = variant;
  }
  root.style.colorScheme = resolved;
  // Kept in step with the boot script in index.html, or the mobile browser
  // chrome keeps whichever colour the page loaded with.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta instanceof HTMLMetaElement) {
    meta.content = resolved === "dark"
      ? (variant === "vibe-code" ? "#0d0e15" : "#08090c")
      : "#fcfdfc";
  }
  // useChartColors listens for this and re-reads the --chart-* tokens, which
  // matters here: the variants ship different chart series.
  window.dispatchEvent(new CustomEvent("flux-theme-change", {
    detail: { preference, resolved, variant },
  }));
}

export function useTheme() {
  const [theme, setTheme] = useState<ThemePreference>(initialTheme);
  const [variant, setVariant] = useState<ThemeVariant>(initialVariant);

  useEffect(() => {
    const apply = () => applyTheme(theme, variant);
    window.localStorage.setItem("flux-theme", theme);
    window.localStorage.setItem("flux-theme-variant", variant);
    apply();
  }, [theme, variant]);

  return { theme, setTheme, variant, setVariant };
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
    // One step off the surface — gridlines carry no data and must stay
    // recessive; --border-bright above is for card edges.
    gridline: resolvedColor("--border"),
    text: resolvedColor("--text"),
    muted: resolvedColor("--text-muted"),
    series,
    // Tail bucket: series past slot 8 fold into one grey rather than a
    // generated ninth hue indistinguishable under CVD.
    other: resolvedColor("--chart-other"),
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
