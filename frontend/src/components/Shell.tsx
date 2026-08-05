import {
  Boxes,
  ChartNoAxesCombined,
  Compass,
  ChevronRight,
  CloudCog,
  GitCompareArrows,
  LayoutDashboard,
  Lightbulb,
  LogOut,
  Menu,
  Monitor,
  Moon,
  Palette,
  Scaling,
  Sparkles,
  Sun,
  TrendingUp,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { absoluteTime, relativeTime } from "../format";
import { useTheme, themeVariants, type ThemePreference, type ThemeVariant } from "../theme";
import { useBusy } from "../useBusy";
import type { Page, Session } from "../types";
import { Logo } from "./Logo";

type NavigationItem = { id: Page; label: string; icon: typeof Boxes };

const navigation: { label: string; items: NavigationItem[] }[] = [
  {
    label: "Workspace",
    items: [
      { id: "overview", label: "Overview", icon: LayoutDashboard },
      { id: "inventory", label: "Inventory", icon: Boxes },
      { id: "changes", label: "Changes", icon: GitCompareArrows },
    ],
  },
  {
    label: "FinOps",
    items: [
      { id: "cost-anomalies", label: "Cost anomalies", icon: TrendingUp },
      { id: "reports", label: "Reports", icon: ChartNoAxesCombined },
      { id: "analytics", label: "Explore", icon: Compass },
      { id: "opportunities", label: "Opportunities", icon: Lightbulb },
      { id: "rightsizing", label: "Right-sizing plan", icon: Scaling },
    ],
  },
  {
    label: "Intelligence",
    items: [
      { id: "intelligence", label: "Flux Intelligence", icon: Sparkles },
    ],
  },
  {
    label: "Administration",
    items: [
      { id: "integrations", label: "Administration", icon: CloudCog },
    ],
  },
];

const pageSections: Record<Page, { label: string; selector: string }[]> = {
  overview: [
    { label: "Estate summary", selector: ".metrics-grid" },
    { label: "Operational health", selector: ".operations-health-card" },
    { label: "Trends and distribution", selector: ".dashboard-grid" },
    { label: "Data freshness", selector: ".freshness-bar" },
  ],
  inventory: [
    { label: "Inventory filters", selector: ".filters" },
    { label: "Azure resources", selector: ".table-wrap" },
  ],
  changes: [
    { label: "Change summary", selector: ".change-summary-grid" },
    { label: "Recent changes", selector: ".change-list" },
  ],
  "cost-anomalies": [
    { label: "Anomaly summary", selector: ".cost-anomaly-summary-grid" },
    { label: "Spend trend", selector: ".cost-anomaly-trend-card" },
    { label: "Anomaly findings", selector: ".cost-anomaly-list" },
  ],
  reports: [
    { label: "Report sections", selector: ".admin-tabs" },
    { label: "Summary", selector: ".report-metrics-grid" },
    { label: "Charts", selector: ".report-grid" },
  ],
  opportunities: [
    { label: "Opportunity summary", selector: ".opportunity-summary-grid" },
    { label: "Right-sizing", selector: ".rightsizing-panel" },
    { label: "Findings", selector: ".findings-grid" },
  ],
  analytics: [
    { label: "Query", selector: ".sx-controls" },
    { label: "Chart", selector: ".sx-chart-card" },
    { label: "Rows", selector: ".sx-table-card" },
  ],
  rightsizing: [
    { label: "Plan summary", selector: ".rz-summary" },
    { label: "Board", selector: ".rz-board" },
    { label: "Decision log", selector: ".rz-log" },
  ],
  intelligence: [
    { label: "Access and performance", selector: ".intelligence-status-strip" },
    { label: "Conversation", selector: ".intelligence-workspace-surface" },
  ],
  // Administration navigates by tab, not by scrolling to anchors, so the
  // rail stays empty rather than linking to sections that may be on a tab
  // the user is not currently viewing.
  integrations: [],
};

const pageLabels = Object.fromEntries(
  navigation.flatMap((group) => group.items.map((item) => [item.id, item.label])),
) as Record<Page, string>;

function ThemeIcon({ theme }: { theme: ThemePreference }) {
  if (theme === "light") return <Sun size={15} />;
  if (theme === "dark") return <Moon size={15} />;
  return <Monitor size={15} />;
}

export function Shell({
  page,
  onPageChange,
  children,
  session,
  onOpenIntelligence,
}: {
  page: Page;
  onPageChange: (page: Page) => void;
  children: ReactNode;
  session: Session;
  onOpenIntelligence: () => void;
}) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { theme, setTheme, variant, setVariant } = useTheme();
  const busy = useBusy();
  // The variant only redefines tokens inside .dark, so the picker is hidden
  // in light mode rather than offering a control that does nothing.
  const darkActive = theme === "dark"
    || (theme === "system"
      && window.matchMedia("(prefers-color-scheme: dark)").matches);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [page]);

  function navigate(nextPage: Page) {
    onPageChange(nextPage);
    setMobileOpen(false);
  }

  function scrollTo(selector: string) {
    document.querySelector(selector)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }

  return (
    <div className="shell">
      <header className="app-header">
        <div className="app-header-brand">
          <button
            className="icon-button mobile-nav-trigger"
            onClick={() => setMobileOpen(true)}
            aria-label="Open navigation"
          >
            <Menu size={19} />
          </button>
          <Logo busy={busy} />
        </div>
        <div className="app-header-context">
          <span>Cloud operations</span>
          <ChevronRight size={13} />
          <strong>{pageLabels[page]}</strong>
          {session.dataCurrency?.mode === "snapshot" && session.dataCurrency.generatedAt && (
            <span
              className="data-currency-chip"
              title={`Analytical snapshot v${session.dataCurrency.snapshotVersion ?? "…"} — published ${new Date(session.dataCurrency.generatedAt).toLocaleString()}`}
            >
              <span className="status-dot" />
              Data as of {absoluteTime(session.dataCurrency.generatedAt)} ({relativeTime(session.dataCurrency.generatedAt)})
            </span>
          )}
        </div>
        <div className="app-header-actions">
          <button
            className="ask-flux-button"
            onClick={onOpenIntelligence}
            aria-label="Open Ask Flux"
          >
            <Sparkles size={16} />
            <span>Ask Flux</span>
          </button>
          <label className="theme-selector" title="Color theme">
            <ThemeIcon theme={theme} />
            <select
              value={theme}
              onChange={(event) => setTheme(event.target.value as ThemePreference)}
              aria-label="Color theme"
            >
              <option value="system">System</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </label>
          {darkActive && (
            <label className="theme-selector" title="Dark theme">
              <Palette size={15} />
              <select
                value={variant}
                onChange={(event) => setVariant(event.target.value as ThemeVariant)}
                aria-label="Dark theme"
              >
                {themeVariants.map((option) => (
                  <option key={option.id} value={option.id}>{option.label}</option>
                ))}
              </select>
            </label>
          )}
          <div className="header-user">
            <span className="user-avatar"><UserRound size={14} /></span>
            <div>
              <strong>{session.user?.displayName || "Flux user"}</strong>
              <span>{session.user?.roles.join(" · ") || "No assigned role"}</span>
            </div>
          </div>
          {session.authMode !== "mock" && (
            <a className="logout-button" href={session.authActions.logoutPath} title="Sign out">
              <LogOut size={15} />
            </a>
          )}
        </div>
      </header>

      {mobileOpen && <button className="mobile-scrim" onClick={() => setMobileOpen(false)} aria-label="Close navigation" />}
      <aside className={`sidebar ${mobileOpen ? "sidebar--open" : ""}`}>
        <div className="sidebar-mobile-header">
          <Logo />
          <button className="icon-button" onClick={() => setMobileOpen(false)} aria-label="Close navigation">
            <X size={18} />
          </button>
        </div>
        <nav className="nav" aria-label="Primary navigation">
          {navigation.map((group) => {
            const items = group.items.filter(
              (item) => item.id !== "integrations" || session.permissions.canManageIntegrations,
            );
            if (!items.length) return null;
            return (
              <section className="nav-group" key={group.label}>
                <span className="nav-group-label">{group.label}</span>
                {items.map((item) => {
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.id}
                      className={`nav-item ${page === item.id ? "nav-item--active" : ""}`}
                      onClick={() => navigate(item.id)}
                    >
                      <Icon size={17} strokeWidth={1.8} />
                      <span>{item.label}</span>
                    </button>
                  );
                })}
              </section>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <span className="status-dot" />
          <div>
            <strong>Flux services</strong>
            <span>Authenticated and read-only</span>
          </div>
        </div>
      </aside>

      <main className="content">
        <div className="page-content">{children}</div>
        <footer className="app-footer">
          <span>FluxFinOps</span>
          <span>Azure FinOps and CloudOps intelligence</span>
        </footer>
      </main>

      <aside className="toc-rail" aria-label="On this page">
        <span className="toc-title">On this page</span>
        <button onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
          {pageLabels[page]}
        </button>
        {pageSections[page].map((section) => (
          <button key={section.label} onClick={() => scrollTo(section.selector)}>
            {section.label}
          </button>
        ))}
        <div className="toc-assistant">
          <Sparkles size={15} />
          <strong>Need another angle?</strong>
          <span>Ask Flux using the context from this page.</span>
          <button onClick={onOpenIntelligence}>Ask a question</button>
        </div>
      </aside>
    </div>
  );
}
