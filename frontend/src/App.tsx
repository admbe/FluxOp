import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import { api } from "./api";
import { Shell } from "./components/Shell";
import {
  IntelligencePanel,
  IntelligenceWorkspace,
  useIntelligenceConversation,
} from "./components/IntelligenceExperience";
import { Card, EmptyState, ErrorPanel, Loading } from "./components/Ui";
import { ErrorBoundary } from "./components/ErrorBoundary";
import type { Overview, Page, Session } from "./types";

const OverviewPage = lazy(() =>
  import("./pages/OverviewPage").then((module) => ({ default: module.OverviewPage })),
);
const InventoryPage = lazy(() =>
  import("./pages/InventoryPage").then((module) => ({ default: module.InventoryPage })),
);
const ChangesPage = lazy(() =>
  import("./pages/ChangesPage").then((module) => ({ default: module.ChangesPage })),
);
const CostAnomaliesPage = lazy(() =>
  import("./pages/CostAnomaliesPage").then((module) => ({ default: module.CostAnomaliesPage })),
);
const ReportsPage = lazy(() =>
  import("./pages/ReportsPage").then((module) => ({ default: module.ReportsPage })),
);
const OpportunitiesPage = lazy(() =>
  import("./pages/OpportunitiesPage").then((module) => ({ default: module.OpportunitiesPage })),
);
const IntegrationsPage = lazy(() =>
  import("./pages/IntegrationsPage").then((module) => ({ default: module.IntegrationsPage })),
);
const AnalyticsExplorerPage = lazy(() =>
  import("./pages/AnalyticsExplorerPage").then((module) => ({ default: module.AnalyticsExplorerPage })),
);
const RightsizingPlanPage = lazy(() =>
  import("./pages/RightsizingPlanPage").then((module) => ({ default: module.RightsizingPlanPage })),
);

function pageFromHash(): Page {
  const value = window.location.hash.replace("#/", "").split("?")[0] as Page;
  return ["overview", "inventory", "changes", "cost-anomalies", "reports", "opportunities", "analytics", "rightsizing", "intelligence", "integrations"].includes(value)
    ? value
    : "overview";
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState("");
  const [intelligenceOpen, setIntelligenceOpen] = useState(false);
  const intelligence = useIntelligenceConversation(page);

  const loadOverview = useCallback(() => {
    api.overview().then(setOverview).catch((reason) => setError(reason.message));
  }, []);

  useEffect(() => {
    api.session()
      .then((value) => {
        setSession(value);
        if (value.permissions.canRead) loadOverview();
      })
      .catch((reason) => setError(reason.message));
    const onHashChange = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [loadOverview]);

  // Operational health is an Administration concern and one of the most
  // expensive endpoints, so it is fetched only by the Integrations page.
  // Loading it in the shell put a multi-second request on every page view.

  useEffect(() => {
    if (page === "integrations" && session && !session.permissions.canManageIntegrations) {
      navigate("overview");
    }
  }, [page, session]);

  useEffect(() => {
    if (!["queued", "running"].includes(overview?.latestSync?.status ?? "")) return;
    // A sync can stay queued for a long time behind a long-running collector.
    // Poll slowly: the overview costs seconds, so a tight interval stacks
    // overlapping requests instead of refreshing faster.
    const timer = window.setInterval(loadOverview, 20000);
    return () => window.clearInterval(timer);
  }, [overview?.latestSync?.status, loadOverview]);

  function navigate(nextPage: Page) {
    window.location.hash = `/${nextPage}`;
    setPage(nextPage);
  }

  if (error && !session) return <div className="auth-gate"><ErrorPanel message={error} /></div>;
  if (!session) return <div className="auth-gate"><Loading /></div>;
  if (!session.authenticated) {
    return (
      <div className="auth-gate">
        <Card>
          <EmptyState
            title="Sign in to Flux"
            description="Microsoft Entra authentication is required before cloud inventory can be viewed."
            action={<a className="button" href={session.authActions.loginPath}>Sign in with Microsoft</a>}
          />
        </Card>
      </div>
    );
  }
  if (!session.permissions.canRead) {
    return (
      <div className="auth-gate">
        <Card>
          <EmptyState
            title="Access has not been assigned"
            description="Ask a Flux administrator to assign the Flux.Reader or Flux.Admin app role."
            action={<a className="button button--secondary" href={session.authActions.logoutPath}>Sign out</a>}
          />
        </Card>
      </div>
    );
  }

  return (
    <Shell
      page={page}
      onPageChange={navigate}
      session={session}
      onOpenIntelligence={() => setIntelligenceOpen(true)}
    >
      <ErrorBoundary area="This page">
      <Suspense fallback={<Loading />}>
        {page === "overview" && (error ? <ErrorPanel message={error} /> : overview ? <OverviewPage data={overview} onNavigate={navigate} canManageIntegrations={session.permissions.canManageIntegrations} /> : <Loading />)}
        {page === "inventory" && <InventoryPage />}
        {page === "changes" && <ChangesPage />}
        {page === "cost-anomalies" && (
          <CostAnomaliesPage canManage={session.permissions.canManageIntegrations} />
        )}
        {page === "reports" && (
          <ReportsPage canManage={session.permissions.canManageIntegrations} />
        )}
        {page === "opportunities" && (
          <OpportunitiesPage
            sourceFreshness={overview?.sourceFreshness ?? []}
            canManage={session.permissions.canManageIntegrations}
          />
        )}
        {page === "analytics" && <AnalyticsExplorerPage />}
        {page === "rightsizing" && (
          <RightsizingPlanPage
            canManage={session.permissions.canManageIntegrations}
            onAskFlux={(question) => {
              setIntelligenceOpen(true);
              // Right-sizing reviews carry real money and risk; they always
              // run in Deep analysis mode.
              void intelligence.ask(question, "benchmark");
            }}
          />
        )}
        {page === "intelligence" && (
          <IntelligenceWorkspace
            controller={intelligence}
            onMinimize={() => setIntelligenceOpen(true)}
            canManageReview={session.permissions.canManageIntegrations}
          />
        )}
        {page === "integrations" && session.permissions.canManageIntegrations && (
          <IntegrationsPage onChanged={loadOverview} />
        )}
      </Suspense>
      </ErrorBoundary>
      <IntelligencePanel
        open={intelligenceOpen}
        onClose={() => setIntelligenceOpen(false)}
        onOpenWorkspace={() => {
          setIntelligenceOpen(false);
          navigate("intelligence");
        }}
        controller={intelligence}
      />
    </Shell>
  );
}
