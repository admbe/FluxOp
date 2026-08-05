import { AlertTriangle, ArrowLeft, ArrowRight, GitCompareArrows, Search, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { relativeTime, shortType, titleCase } from "../format";
import type { ChangeAnomalies, InventoryChanges } from "../types";
import { Card, EmptyState, ErrorPanel, Loading, PageHeader } from "../components/Ui";

function detailValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "none";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ChangesPage() {
  const [data, setData] = useState<InventoryChanges | null>(null);
  const [anomalies, setAnomalies] = useState<ChangeAnomalies | null>(null);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [changeType, setChangeType] = useState("");
  const [subscription, setSubscription] = useState("");
  const [resourceGroup, setResourceGroup] = useState("");
  const [windowDays, setWindowDays] = useState(7);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);

  const params = useMemo(() => {
    const value = new URLSearchParams();
    if (search) value.set("search", search);
    if (changeType) value.set("changeType", changeType);
    if (subscription) value.set("subscriptionId", subscription);
    if (resourceGroup) value.set("resourceGroup", resourceGroup);
    value.set("windowDays", String(windowDays));
    value.set("limit", String(pageSize));
    value.set("offset", String(page * pageSize));
    return value;
  }, [search, changeType, subscription, resourceGroup, windowDays, page, pageSize]);

  useEffect(() => {
    setPage(0);
  }, [search, changeType, subscription, resourceGroup, windowDays, pageSize]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setError("");
      Promise.all([api.changes(params), api.changeAnomalies()])
        .then(([changes, anomalyData]) => {
          setData(changes);
          setAnomalies(anomalyData);
        })
        .catch((reason) => setError(reason.message));
    }, search ? 220 : 0);
    return () => window.clearTimeout(timer);
  }, [params, search]);

  const maxPage = data ? Math.max(Math.ceil(data.total / pageSize) - 1, 0) : 0;

  return (
    <>
      <PageHeader
        eyebrow="Operational evidence"
        title="Inventory changes"
        description="Exact differences between the two latest complete Azure inventory snapshots. Change-volume baselines use median and MAD."
      />
      {data && (
        <div className="opportunity-summary-grid change-summary-grid">
          <Card><small>Latest changes</small><strong>{data.summary.total.toLocaleString()}</strong></Card>
          <Card><small>Created</small><strong>{data.summary.created.toLocaleString()}</strong></Card>
          <Card><small>Deleted</small><strong>{data.summary.deleted.toLocaleString()}</strong></Card>
          <Card><small>Configuration</small><strong>{data.summary.configuration.toLocaleString()}</strong></Card>
        </div>
      )}
      {anomalies?.warmingUp && (
        <div className="inline-alert change-baseline-note">
          <GitCompareArrows size={16} />
          Change anomaly baseline is warming up. Flux will require the configured number of completed drift intervals before flagging unusual volume.
        </div>
      )}
      {!!anomalies?.anomalyCount && (
        <div className="inline-alert inline-alert--error change-baseline-note">
          <AlertTriangle size={16} />
          {anomalies.anomalyCount.toLocaleString()} unusual change scope{anomalies.anomalyCount === 1 ? "" : "s"} detected in the latest interval.
        </div>
      )}
      <Card className="opportunity-filter-card">
        <div className="filters filters--wrap">
          <label className="search-field"><Search size={17} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search resource, group, type…" /></label>
          <label className="select-field"><SlidersHorizontal size={16} /><select value={changeType} onChange={(event) => setChangeType(event.target.value)}><option value="">All changes</option>{data?.facets.changeTypes.map((value) => <option key={value} value={value}>{titleCase(value)}</option>)}</select></label>
          <label className="select-field"><select value={windowDays} onChange={(event) => setWindowDays(Number(event.target.value))} aria-label="Change window"><option value={1}>Last 24 hours</option><option value={7}>Last 7 days</option><option value={14}>Last 14 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option><option value={0}>All retained history</option></select></label>
          <label className="select-field"><select value={subscription} onChange={(event) => setSubscription(event.target.value)}><option value="">All subscriptions</option>{data?.facets.subscriptions.map((value) => <option key={value.id} value={value.id}>{value.name}</option>)}</select></label>
          <label className="select-field"><select value={resourceGroup} onChange={(event) => setResourceGroup(event.target.value)}><option value="">All resource groups</option>{data?.facets.resourceGroups.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <span className="result-count">{data?.total.toLocaleString() || 0} changes</span>
        </div>
      </Card>
      {error ? <ErrorPanel message={error} /> : !data ? <Loading /> : data.items.length ? (
        <>
          <div className="change-list">
            {data.items.map((change, index) => (
              <Card className="change-card" key={`${change.resourceId}-${change.changeType}-${index}`}>
                <span className={`pill change-pill change-pill--${change.changeType}`}>{titleCase(change.changeType)}</span>
                <div className="change-card__identity">
                  <h2>{change.resourceName || change.resourceId}</h2>
                  <p>{shortType(change.resourceType)} · {change.subscriptionName || change.subscriptionId} · {change.resourceGroup || "subscription scope"}</p>
                </div>
                <div className="change-card__details">
                  {Object.entries(change.details).map(([field, value]) => (
                    <span key={field}><small>{titleCase(field)}</small>{detailValue(value.from)} <ArrowRight size={11} /> {detailValue(value.to)}</span>
                  ))}
                  {!Object.keys(change.details).length && <span><small>Observation</small>{titleCase(change.changeType)} in latest snapshot</span>}
                </div>
                <time>{relativeTime(change.computedAt)}</time>
              </Card>
            ))}
          </div>
          <div className="pagination">
            <button className="button button--ghost" disabled={page === 0} onClick={() => setPage((value) => value - 1)}><ArrowLeft size={15} />Previous</button>
            <span>Page {page + 1} of {maxPage + 1}</span>
            <select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}><option value={25}>25 per page</option><option value={50}>50 per page</option><option value={100}>100 per page</option></select>
            <button className="button button--ghost" disabled={page >= maxPage} onClick={() => setPage((value) => value + 1)}>Next<ArrowRight size={15} /></button>
          </div>
        </>
      ) : (
        <Card className="content-module"><EmptyState title="No inventory changes" description="Synchronize Azure again to compare two complete inventory snapshots, or change the active filters." /></Card>
      )}
    </>
  );
}
