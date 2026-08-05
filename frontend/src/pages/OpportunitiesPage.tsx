import {
  AlertTriangle, ArrowLeft, ArrowRight, CircleDollarSign, Download, Lightbulb,
  RefreshCw, Search, SlidersHorizontal,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { currency, shortType, titleCase } from "../format";
import type { Opportunities, Opportunity, RightsizingRecommendations, SourceFreshness } from "../types";
import { Card, EmptyState, ErrorPanel, Loading, PageHeader } from "../components/Ui";
import { hashParams, navigateWithParams, SavedViews } from "../viewState";

function withFormat(params: URLSearchParams, format: string): URLSearchParams {
  const next = new URLSearchParams(params);
  next.set("format", format);
  return next;
}

function sourceLabel(source: string): string {
  if (source === "flux_intelligence") return "Flux Signals";
  if (source === "azure_advisor") return "Azure Advisor";
  if (source === "inventory_rule") return "Inventory";
  return titleCase(source);
}

export function OpportunitiesPage({
  sourceFreshness,
  canManage = false,
}: {
  sourceFreshness: SourceFreshness[];
  canManage?: boolean;
}) {
  const [data, setData] = useState<Opportunities | null>(null);
  const [rightsizing, setRightsizing] = useState<RightsizingRecommendations | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const initial = hashParams();
  const [search, setSearch] = useState(initial.get("search") ?? "");
  const [type, setType] = useState(initial.get("resourceType") ?? "");
  const [subscription, setSubscription] = useState(initial.get("subscriptionId") ?? "");
  const [region, setRegion] = useState(initial.get("region") ?? "");
  const [source, setSource] = useState(initial.get("source") ?? "");
  const [category, setCategory] = useState(initial.get("category") ?? "");
  const [confidence, setConfidence] = useState(initial.get("confidence") ?? "");
  const [actionability, setActionability] = useState(
    initial.get("actionability") ?? "actionable_now",
  );
  const [includeGovernance, setIncludeGovernance] = useState(false);
  const [sort, setSort] = useState("impact");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const [lifecycleOverrides, setLifecycleOverrides] = useState<Record<string, Opportunity["lifecycleStatus"]>>({});
  const [rightsizingExpanded, setRightsizingExpanded] = useState(false);

  function updateLifecycle(
    opportunity: Opportunity,
    status: Opportunity["lifecycleStatus"],
  ) {
    setLifecycleOverrides((current) => ({ ...current, [opportunity.id]: status }));
    api.setOpportunityLifecycle({
      opportunityId: opportunity.id,
      status,
      resourceId: opportunity.resourceId,
      estimatedMonthlySavings:
        opportunity.monthlyRiskAdjustedSavings ?? opportunity.estimatedMonthlySavings ?? null,
    }).catch(() => {
      setLifecycleOverrides((current) => {
        const next = { ...current };
        delete next[opportunity.id];
        return next;
      });
    });
  }

  const params = useMemo(() => {
    const value = new URLSearchParams();
    if (search) value.set("search", search);
    if (type) value.set("resourceType", type);
    if (subscription) value.set("subscriptionId", subscription);
    if (region) value.set("region", region);
    if (source) value.set("source", source);
    if (category) value.set("category", category);
    if (confidence) value.set("confidence", confidence);
    if (actionability) value.set("actionability", actionability);
    if (includeGovernance || actionability === "governance_review") {
      value.set("includeGovernance", "true");
    }
    value.set("sort", sort);
    value.set("direction", sort === "resource" ? "asc" : "desc");
    value.set("limit", String(pageSize));
    value.set("offset", String(page * pageSize));
    return value;
  }, [search, type, subscription, region, source, category, confidence, actionability,
    includeGovernance, sort, page, pageSize]);

  useEffect(() => {
    setPage(0);
  }, [search, type, subscription, region, source, category, confidence, actionability,
    includeGovernance, sort, pageSize]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const timer = window.setTimeout(() => {
      setError("");
      const rightsizingParams = new URLSearchParams({ status: "candidate", limit: rightsizingExpanded ? "2000" : "6" });
      if (subscription) rightsizingParams.set("subscriptionId", subscription);
      Promise.all([
        api.opportunities(params),
        api.rightsizingRecommendations(rightsizingParams),
      ]).then(([opportunities, recommendations]) => {
        if (cancelled) return;
        setData(opportunities);
        setRightsizing(recommendations);
      }).catch((reason) => {
        if (!cancelled) setError(reason.message);
      }).finally(() => {
        if (!cancelled) setLoading(false);
      });
    }, search ? 220 : 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [params, search, subscription, rightsizingExpanded]);

  const exportParams = new URLSearchParams(params);
  exportParams.delete("limit");
  exportParams.delete("offset");
  const maxPage = data ? Math.max(Math.ceil(data.total / pageSize) - 1, 0) : 0;
  const staleEvidence = sourceFreshness.filter((item) =>
    item.health !== "healthy" && ["AzureAdvisor", "FluxIntelligence", "ActualCost", "AmortizedCost", "CommitmentCoverage", "AzureRetailPrices", "azure_monitor"].includes(item.source),
  );

  return (
    <>
      <PageHeader
        eyebrow="Action layer"
        title="Opportunities"
        description="A prioritized work queue. Actionable resource findings are shown first; portfolio and evidence-deficient signals remain available as separate views."
        action={
          <span className="export-actions">
            <a className="button button--ghost" href={api.opportunitiesExportUrl(exportParams)}>
              <Download size={16} />CSV
            </a>
            <a className="button button--ghost" href={api.opportunitiesExportUrl(withFormat(exportParams, "xlsx"))}>
              <Download size={16} />Excel
            </a>
            {canManage && (
              <>
                <a
                  className="button button--ghost"
                  href="/api/remediation/servicenow-package?format=csv&minMonthlyCost=5"
                  title="ServiceNow-ready planned remediation tasks (CSV) for high-confidence unattached-disk signals costing at least $5/month. Each row includes a pre-filled Planned task form link. Re-downloading is safe; only tasks reconciled as filed are suppressed."
                >
                  <Download size={16} />ServiceNow tasks
                </a>
                <a
                  className="button button--ghost"
                  href="/api/remediation/servicenow-package?format=script&minMonthlyCost=5"
                  title="Batch-create script: paste into the DevTools console of a signed-in ServiceNow tab. Confirms the count first, skips tasks that already exist, creates records as you, and prints the reconcile payload for Flux."
                >
                  <Download size={16} />ServiceNow batch
                </a>
              </>
            )}
          </span>
        }
      />
      {data && (
        <div className="savings-pipeline" role="group" aria-label="Savings pipeline">
          <span className="savings-pipeline__stage">
            <small>Identified</small>
            <strong>{data.summary.portfolio.detected.toLocaleString()} signals</strong>
          </span>
          <span className="savings-pipeline__arrow">→</span>
          <span className="savings-pipeline__stage">
            <small>Actionable now</small>
            <strong>{data.summary.portfolio.actionableNow.toLocaleString()}</strong>
          </span>
          <span className="savings-pipeline__arrow">→</span>
          <span className="savings-pipeline__stage savings-pipeline__stage--value">
            <small>Risk-adjusted monthly value</small>
            <strong>{currency(data.summary.monthlyRiskAdjustedValue, "USD")}</strong>
          </span>
        </div>
      )}
      {staleEvidence.length > 0 && (
        <div className="freshness-warning">
          <AlertTriangle size={16} />
          <span>
            <strong>Recommendation evidence may be stale.</strong>{" "}
            {staleEvidence.map((item) => item.label).join(", ")}{" "}
            {staleEvidence.length === 1 ? "is" : "are"} stale or degraded.
          </span>
        </div>
      )}
      {data && (
        <div className="opportunity-summary-grid">
          <Card><small>Detected FinOps signals</small><strong>{data.summary.portfolio.detected.toLocaleString()}</strong></Card>
          <Card><small>Actionable now</small><strong>{data.summary.portfolio.actionableNow.toLocaleString()}</strong><em>Default work queue</em></Card>
          <Card><small>Portfolio review</small><strong>{data.summary.portfolio.portfolioReview.toLocaleString()}</strong><em>Subscription-level actions</em></Card>
          <Card><small>Needs evidence</small><strong>{data.summary.portfolio.evidenceNeeded.toLocaleString()}</strong></Card>
          <Card><small>Telemetry ready</small><strong>{data.summary.portfolio.telemetryReady.toLocaleString()}</strong></Card>
          <Card><small>Corroborated</small><strong>{data.summary.portfolio.corroborated.toLocaleString()}</strong></Card>
          <Card><small>Risk-adjusted monthly</small><strong>{currency(data.summary.monthlyRiskAdjustedValue, "USD")}</strong></Card>
          <Card><small>Duplicate IDs suppressed</small><strong>{data.diagnostics.sourceRows.reduce((total, item) => total + item.duplicates, 0).toLocaleString()}</strong></Card>
        </div>
      )}
      {rightsizing && (
        <Card className="rightsizing-panel">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Multi-source telemetry</span>
              <h2>Right-sizing and idle candidates</h2>
              <p>Recommendations use governed Azure Monitor and checkpointed LogicMonitor evidence. Material disagreement between sources is routed to review.</p>
            </div>
            <div className="rightsizing-summary">
              <span><small>Candidates</small><strong>{rightsizing.summary.candidates.toLocaleString()}</strong></span>
              <span><small>Covered VMs</small><strong>{rightsizing.summary.covered.toLocaleString()}</strong></span>
              <span><small>Needs review</small><strong>{rightsizing.summary.needsReview.toLocaleString()}</strong></span>
              <span><small>Warming up</small><strong>{rightsizing.summary.warmingUp.toLocaleString()}</strong></span>
              <span><small>Insufficient</small><strong>{rightsizing.summary.insufficient.toLocaleString()}</strong></span>
            </div>
            <div className="export-actions">
              <a className="button button--ghost" href={api.rightsizingExportUrl(new URLSearchParams({ status: "candidate", ...(subscription ? { subscriptionId: subscription } : {}) }))}><Download size={15} />CSV</a>
              <a className="button button--ghost" href={api.rightsizingExportUrl(new URLSearchParams({ status: "candidate", format: "xlsx", ...(subscription ? { subscriptionId: subscription } : {}) }))}><Download size={15} />Excel</a>
              {rightsizing.total > 6 && <button className="button button--ghost" onClick={() => setRightsizingExpanded((value) => !value)}>{rightsizingExpanded ? "Show top 6" : `Show all ${rightsizing.total.toLocaleString()}`}</button>}
            </div>
          </div>
          {rightsizing.total > rightsizing.items.length && <p className="muted">Showing {rightsizing.items.length.toLocaleString()} of {rightsizing.total.toLocaleString()} candidates.</p>}
          {rightsizing.items.length ? (
            <div className="rightsizing-list">
              {rightsizing.items.map((recommendation) => (
                <div key={recommendation.resourceId}>
                  <span className="pill pill--flux">{titleCase(recommendation.kind)}</span>
                  <span><button className="link-button" onClick={() => navigateWithParams("inventory", { resourceId: recommendation.resourceId, search: recommendation.resourceId })}><strong>{recommendation.resourceName}</strong></button><small>{recommendation.currentSku} <ArrowRight size={11} /> {recommendation.targetSku}</small><small className="rightsizing-reason" title={recommendation.reason}>{recommendation.reason}</small></span>
                  <span><strong>CPU p95 {recommendation.cpuP95?.toFixed(1)}%</strong><small>{recommendation.evidenceWindowDays}d · {recommendation.metricCoveragePercent?.toFixed(0)}% coverage</small></span>
                  <span><strong>{recommendation.estimatedMonthlySaving === null ? "Value unavailable" : currency(recommendation.estimatedMonthlySaving, recommendation.currency)}</strong><small>{titleCase(recommendation.valueSource)}</small></span>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted rightsizing-empty">
              No actionable telemetry candidates yet. {rightsizing.summary.needsReview.toLocaleString()} VMs have conflicting source evidence, {rightsizing.summary.warmingUp.toLocaleString()} are warming up, and {rightsizing.summary.insufficient.toLocaleString()} lack sufficient metrics.
            </p>
          )}
        </Card>
      )}
      <Card className="opportunity-filter-card" aria-busy={loading}>
        <div className="filters filters--wrap">
          <SavedViews
            page="opportunities"
            current={() => ({
              search, resourceType: type, subscriptionId: subscription, region,
              source, category, confidence, actionability,
            })}
            onApply={(view) => {
              setSearch(view.search ?? "");
              setType(view.resourceType ?? "");
              setSubscription(view.subscriptionId ?? "");
              setRegion(view.region ?? "");
              setSource(view.source ?? "");
              setCategory(view.category ?? "");
              setConfidence(view.confidence ?? "");
              setActionability(view.actionability ?? "actionable_now");
              setPage(0);
            }}
          />
          <label className="search-field"><Search size={17} /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search resource, recommendation, group, type…" /></label>
          <label className="select-field"><SlidersHorizontal size={16} /><select value={type} onChange={(e) => setType(e.target.value)}><option value="">All resource types</option>{data?.facets.resourceTypes.map((v) => <option key={v} value={v}>{shortType(v)}</option>)}</select></label>
          <label className="select-field"><select value={subscription} onChange={(e) => setSubscription(e.target.value)}><option value="">All subscriptions</option>{data?.facets.subscriptions.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}</select></label>
          <label className="select-field"><select value={region} onChange={(e) => setRegion(e.target.value)}><option value="">All regions</option>{data?.facets.regions.map((v) => <option key={v} value={v}>{v}</option>)}</select></label>
          <label className="select-field"><select value={source} onChange={(e) => setSource(e.target.value)}><option value="">All sources</option>{data?.facets.sources.map((v) => <option key={v} value={v}>{sourceLabel(v)}</option>)}</select></label>
          <label className="select-field"><select value={category} onChange={(e) => setCategory(e.target.value)}><option value="">All action categories</option>{data?.facets.categories.map((v) => <option key={v} value={v}>{v}</option>)}</select></label>
          <label className="select-field"><select value={confidence} onChange={(e) => setConfidence(e.target.value)}><option value="">All confidence</option>{data?.facets.confidences.map((v) => <option key={v} value={v}>{v}</option>)}</select></label>
          <label className="select-field">
            <select value={actionability} onChange={(e) => setActionability(e.target.value)}>
              <option value="actionable_now">Actionable now</option>
              <option value="">All FinOps signals</option>
              <option value="portfolio_review">Portfolio review</option>
              <option value="evidence_needed">Needs evidence</option>
              <option value="governance_review">Governance review</option>
            </select>
          </label>
          <label className="select-field"><select value={sort} onChange={(e) => setSort(e.target.value)}><option value="impact">Highest impact</option><option value="valuation">Highest risk-adjusted value</option><option value="savings">Highest Advisor savings</option><option value="cost">Highest cost exposure</option><option value="confidence">Highest confidence</option><option value="updated">Newest</option><option value="resource">Resource name</option></select></label>
          <label className="filter-check"><input type="checkbox" checked={includeGovernance} onChange={(e) => setIncludeGovernance(e.target.checked)} />Include governance and non-FinOps Advisor</label>
          <span className={`result-count${loading ? " result-count--loading" : ""}`} role="status" aria-live="polite">
            {loading ? <><RefreshCw className="spin" size={13} />Updating results…</> : (
              <>
                {data?.total.toLocaleString() || 0} unique actions
                {data ? ` · ${data.summary.distinctResources.toLocaleString()} resources` : ""}
              </>
            )}
          </span>
        </div>
      </Card>

      {error ? <ErrorPanel message={error} /> : !data ? <Loading /> : data.items.length ? (
        <>
          <div className={`findings-grid${loading ? " findings-grid--loading" : ""}`} aria-busy={loading}>
            {data.items.map((opportunity) => (
              <Card className="finding-card" key={opportunity.id}>
                <div className="finding-card__top">
                  <span className="finding-icon"><Lightbulb size={18} /></span>
                  <div className="finding-card__badges">
                    {opportunity.impact && <span className="pill">{titleCase(opportunity.impact)} impact</span>}
                    {opportunity.confidence && <span className="pill">{titleCase(opportunity.confidence)} confidence</span>}
                    {opportunity.resourceId === `/subscriptions/${opportunity.subscriptionId}` && (
                      <span className="pill pill--warning">Subscription scope</span>
                    )}
                    <span className={`pill ${opportunity.isCorroborated || opportunity.source === "flux_intelligence" ? "pill--flux" : "pill--warning"}`}>
                      {opportunity.isCorroborated ? "Advisor + Flux" : sourceLabel(opportunity.source)}
                    </span>
                    <span className={`pill ${opportunity.actionability === "actionable_now" ? "pill--flux" : "pill--warning"}`}>
                      {titleCase(opportunity.actionability)}
                    </span>
                  </div>
                </div>
                <h2>
                  {opportunity.resourceId === `/subscriptions/${opportunity.subscriptionId}` ? opportunity.title : (
                    <button className="link-button link-button--heading" onClick={() => navigateWithParams("inventory", { resourceId: opportunity.resourceId, search: opportunity.resourceId, resourceType: opportunity.resourceType })}>{opportunity.resourceName || opportunity.title}</button>
                  )}
                </h2>
                <p>{opportunity.reason}</p>
                <p className="finding-actionability">{opportunity.actionabilityReason}</p>
                <div className="finding-context"><span>{shortType(opportunity.resourceType) || opportunity.category}</span><span title={opportunity.subscriptionId}>{opportunity.subscriptionName || opportunity.subscriptionId}</span><span>{opportunity.region || "global"}</span></div>
                {opportunity.currentSku && opportunity.recommendedSku && <div className="finding-context"><span>{opportunity.currentSku}</span><ArrowRight size={14} /><span>{opportunity.recommendedSku}</span></div>}
                {opportunity.currentMonthlyCostRunRate !== null && opportunity.targetMonthlyRetailCost !== null && (
                  <div className="finding-price-bridge">
                    <span><small>Observed run rate</small><strong>{currency(opportunity.currentMonthlyCostRunRate, opportunity.valuationCurrency)}</strong></span>
                    <ArrowRight size={15} />
                    <span><small>Target retail model</small><strong>{currency(opportunity.targetMonthlyRetailCost, opportunity.valuationCurrency)}</strong></span>
                  </div>
                )}
                {opportunity.confidenceScore !== null && (
                  <div className="finding-confidence">
                    <span>
                      <small>Evidence confidence</small>
                      <strong>{Math.round(opportunity.confidenceScore * 100)}%</strong>
                    </span>
                    <p>
                      {opportunity.consecutiveCount?.toLocaleString()} consecutive sync{opportunity.consecutiveCount === 1 ? "" : "s"}
                      {opportunity.ageDays !== null ? ` · ${opportunity.ageDays}d observed` : ""}
                      {opportunity.confidenceFactors?.telemetryApplicable
                        ? ` · telemetry ${titleCase(opportunity.confidenceFactors.telemetryStatus)}`
                        : ""}
                      {opportunity.reappearedAfterRemediation ? " · reappeared" : ""}
                    </p>
                  </div>
                )}
                <div className="finding-value">
                  <CircleDollarSign size={17} />
                  <span>
                    <small>{opportunity.monthlyRiskAdjustedSavings !== null ? "Risk-adjusted monthly value" : opportunity.monthlyGrossSavings !== null ? "Gross monthly value" : "Valuation status"}</small>
                    <strong>
                      {opportunity.monthlyRiskAdjustedSavings !== null
                        ? currency(opportunity.monthlyRiskAdjustedSavings, opportunity.valuationCurrency)
                        : opportunity.monthlyGrossSavings !== null
                          ? currency(opportunity.monthlyGrossSavings, opportunity.valuationCurrency)
                          : titleCase(opportunity.valuationStatus)}
                    </strong>
                    <em>
                      {opportunity.monthlyGrossSavings !== null && opportunity.monthlyRiskAdjustedSavings !== null
                        ? `${currency(opportunity.monthlyGrossSavings, opportunity.valuationCurrency)} gross · ${titleCase(opportunity.valuationSource)}`
                        : opportunity.valuationBasis}
                    </em>
                    {opportunity.targetPriceBasis && <em>{opportunity.targetPriceBasis}</em>}
                    {!opportunity.targetPriceBasis && opportunity.recommendedSku && (
                      <em>Target price: {titleCase(opportunity.targetPriceStatus || "not collected")}</em>
                    )}
                  </span>
                  <div className="finding-actions">
                    {canManage && (
                      <select
                        className="lifecycle-select"
                        value={lifecycleOverrides[opportunity.id] ?? opportunity.lifecycleStatus}
                        onChange={(event) => updateLifecycle(opportunity, event.target.value as Opportunity["lifecycleStatus"])}
                        aria-label="Recommendation lifecycle status"
                        title="Track this recommendation through implementation; realized savings are measured from cost data"
                      >
                        <option value="open">Open</option>
                        <option value="accepted">Accepted</option>
                        <option value="implemented">Implemented</option>
                        <option value="dismissed">Dismissed</option>
                      </select>
                    )}
                    {!canManage && opportunity.lifecycleStatus !== "open" && (
                      <span className={`pill lifecycle-pill lifecycle-pill--${opportunity.lifecycleStatus}`}>{titleCase(opportunity.lifecycleStatus)}</span>
                    )}
                    <a className="finding-link" href={api.opportunityEvidenceUrl(opportunity.id)} aria-label="Download evidence package" title="Download evidence package"><Download size={17} /></a>
                    {opportunity.learnMoreLink ? <a className="finding-link" href={opportunity.learnMoreLink} target="_blank" rel="noreferrer" aria-label="Open Azure recommendation guidance"><ArrowRight size={18} /></a> : <ArrowRight size={18} />}
                  </div>
                </div>
              </Card>
            ))}
          </div>
          <div className="pagination">
            <button className="button button--ghost" disabled={loading || page === 0} onClick={() => setPage((v) => v - 1)}><ArrowLeft size={15} />Previous</button>
            <span>Page {page + 1} of {maxPage + 1}</span>
            <select value={pageSize} disabled={loading} onChange={(e) => setPageSize(Number(e.target.value))}><option value={25}>25 per page</option><option value={50}>50 per page</option><option value={100}>100 per page</option></select>
            <button className="button button--ghost" disabled={loading || page >= maxPage} onClick={() => setPage((v) => v + 1)}>Next<ArrowRight size={15} /></button>
          </div>
        </>
      ) : <Card className="content-module"><EmptyState title="No opportunities found" description="Change the filters or synchronize Azure to refresh findings." /></Card>}
    </>
  );
}
