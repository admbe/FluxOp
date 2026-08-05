import { Download } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api } from "../api";
import { compactNumber } from "../format";
import type { VirtualTagReport } from "../types";
import { Card, EmptyState, Loading } from "./Ui";

export function VirtualTagsReport({ costType, startDate, endDate }: { costType: string; startDate: string; endDate: string }) {
  const [dimension, setDimension] = useState("");
  const [selectedValue, setSelectedValue] = useState("");
  const [report, setReport] = useState<VirtualTagReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const params = useMemo(() => {
    const value = new URLSearchParams({ costType });
    if (dimension) value.set("dimension", dimension);
    if (selectedValue) value.set("value", selectedValue);
    if (startDate) value.set("startDate", startDate);
    if (endDate) value.set("endDate", endDate);
    return value;
  }, [costType, dimension, selectedValue, startDate, endDate]);

  useEffect(() => {
    setLoading(true); setError("");
    api.virtualTagReport(params).then((value) => {
      setReport(value);
      if (!dimension && value.dimension) setDimension(value.dimension);
    }).catch((reason) => setError(reason.message)).finally(() => setLoading(false));
  }, [params]);

  const monthly = useMemo(() => {
    if (!report) return [];
    const result = new Map<string, Record<string, string | number>>();
    report.monthly.forEach((item) => {
      const row = result.get(item.month) ?? { month: item.month };
      row[item.value] = item.cost;
      result.set(item.month, row);
    });
    return [...result.values()];
  }, [report]);

  if (loading && !report) return <Loading />;
  if (error) return <div className="inline-alert inline-alert--error">{error}</div>;
  if (!report?.dimensions.length) return <Card><EmptyState title="No virtual-tag dimensions" description="An administrator can create dimensions and rules under Administration → Configuration." /></Card>;
  const series = report.values.filter((item) => item.value !== "Unclassified").slice(0, 6).map((item) => item.value);
  return <>
    <div className="report-section-heading section-heading">
      <div><span className="eyebrow">Business dimensions</span><h2>Virtual tag showback</h2><p>Historical and current spend grouped by Flux-effective tags, with assignment provenance and explicit unclassified cost.</p></div>
      <div className="form-actions">
        <select aria-label="Virtual tag dimension" value={dimension} onChange={(event) => { setDimension(event.target.value); setSelectedValue(""); }}>{report.dimensions.filter((item) => item.status === "active").map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}</select>
        <select aria-label="Virtual tag value" value={selectedValue} onChange={(event) => setSelectedValue(event.target.value)}><option value="">All values</option>{report.values.map((item) => <option key={item.value} value={item.value}>{item.value}</option>)}</select>
        <a className="button button--ghost" href={api.virtualTagReportExportUrl(params)}><Download size={15} />CSV</a>
      </div>
    </div>
    <div className="report-metrics-grid">
      <Card><small>Classified spend</small><strong>{report.summary.classifiedPercent ?? "—"}%</strong><em>{compactNumber(report.summary.classifiedCost)} of {compactNumber(report.summary.totalCost)}</em></Card>
      <Card><small>Dimension values</small><strong>{report.summary.valueCount}</strong><em>{report.dimension}</em></Card>
      <Card><small>Mapped resources</small><strong>{report.summary.resourceCount.toLocaleString()}</strong><em>{report.costType}</em></Card>
      <Card><small>Precedence</small><strong>Governed</strong><em>manual › imported › rule › native</em></Card>
    </div>
    <div className="report-grid">
      <Card className="chart-card"><div className="chart-title"><div><h3>Cost by value</h3><p>{report.currency}</p></div></div><div className="chart-frame"><ResponsiveContainer width="100%" height="100%"><BarChart data={report.values.slice(0, 10)} layout="vertical" margin={{ left: 20, right: 20 }}><CartesianGrid strokeDasharray="3 3" horizontal={false} /><XAxis type="number" tickFormatter={compactNumber} /><YAxis type="category" dataKey="value" width={105} /><Tooltip formatter={(value) => compactNumber(Number(value))} /><Bar dataKey="cost" fill="var(--flux)" radius={[0, 4, 4, 0]} /></BarChart></ResponsiveContainer></div></Card>
      <Card className="chart-card"><div className="chart-title"><div><h3>Monthly history</h3><p>Current effective classification</p></div></div><div className="chart-frame"><ResponsiveContainer width="100%" height="100%"><LineChart data={monthly}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="month" /><YAxis tickFormatter={compactNumber} /><Tooltip formatter={(value) => compactNumber(Number(value))} /><Legend />{series.map((name, index) => <Line key={name} dataKey={name} type="monotone" stroke={["#198f72", "#4f8fe8", "#e7b84b", "#9b6bd3", "#e66d65", "#4aa7a4"][index]} dot={false} />)}</LineChart></ResponsiveContainer></div></Card>
    </div>
    <Card className="report-table-card report-wide-card">
      <div className="chart-title"><div><h3>Resources</h3><p>{report.resourcesTruncated ? "Top 500 by cost; export for the complete result" : "Complete filtered result"}</p></div></div>
      <div className="report-table-wrap"><table><thead><tr><th>Resource</th><th>Value</th><th>Source</th><th>Subscription</th><th>Resource group</th><th>Cost</th></tr></thead><tbody>{report.resources.map((item) => <tr key={`${item.resourceId}-${item.value}`}><td><a className="table-link" href={`#/inventory?resourceId=${encodeURIComponent(item.resourceId)}`}>{item.name}</a><small>{item.resourceType}</small></td><td>{item.value}</td><td>{item.source}</td><td>{item.subscriptionName}</td><td>{item.resourceGroup || "—"}</td><td>{compactNumber(item.cost)}</td></tr>)}</tbody></table></div>
      <p className="muted">{report.lineage.limitation}</p>
    </Card>
  </>;
}
