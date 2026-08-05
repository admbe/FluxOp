import { Plus, Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "../api";
import { currency } from "../format";
import type { VirtualTagCondition, VirtualTagDimension, VirtualTagPreview, VirtualTagRule } from "../types";
import { Card, Loading } from "./Ui";

const emptyCondition: VirtualTagCondition = { field: "subscriptionId", operator: "equals", value: "" };

export function VirtualTagsAdmin() {
  const [dimensions, setDimensions] = useState<VirtualTagDimension[]>([]);
  const [rules, setRules] = useState<VirtualTagRule[]>([]);
  const [dimensionDraft, setDimensionDraft] = useState({ key: "", name: "", description: "" });
  const [draft, setDraft] = useState<Partial<VirtualTagRule>>({
    name: "", tagKey: "", tagValue: "", priority: 100, effect: "include",
    status: "active", conditions: { combinator: "and", conditions: [{ ...emptyCondition }], groups: [] },
  });
  const [preview, setPreview] = useState<VirtualTagPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => Promise.all([api.virtualTagDimensions(), api.virtualTagRules()])
    .then(([dimensionResult, ruleResult]) => {
      setDimensions(dimensionResult.dimensions);
      setRules(ruleResult.rules);
      if (!draft.tagKey && dimensionResult.dimensions.length) {
        setDraft((value) => ({ ...value, tagKey: dimensionResult.dimensions[0].key }));
      }
    })
    .catch((reason) => setError(reason.message));

  useEffect(() => { void load(); }, []);

  const conditions = draft.conditions?.conditions ?? [];
  function updateCondition(index: number, patch: Partial<VirtualTagCondition>) {
    const next = conditions.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item);
    setDraft({ ...draft, conditions: { ...draft.conditions, conditions: next } });
  }

  async function saveDimension() {
    setBusy(true); setError("");
    try {
      await api.saveVirtualTagDimension({ ...dimensionDraft, status: "active" });
      setDimensionDraft({ key: "", name: "", description: "" });
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Dimension save failed."); }
    finally { setBusy(false); }
  }

  async function runRule(action: "preview" | "save") {
    setBusy(true); setError("");
    try {
      if (action === "preview") setPreview(await api.previewVirtualTagRule(draft));
      else { await api.saveVirtualTagRule(draft); setPreview(null); await load(); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Rule operation failed."); }
    finally { setBusy(false); }
  }

  function editRule(rule: VirtualTagRule) {
    const generalized = Array.isArray(rule.conditions.conditions);
    setDraft(generalized ? rule : { ...rule, conditions: { combinator: "and", conditions: [{ ...emptyCondition }], groups: [] } });
    setPreview(null);
  }

  return (
    <div className="report-wide-card">
      <div className="section-heading">
        <div><span className="eyebrow">Flux metadata</span><h2>Virtual tags</h2><p>Govern dimensions and evaluated assignments without changing Azure resources. Precedence is manual, imported, rule, then native.</p></div>
      </div>
      {error && <div className="inline-alert inline-alert--error">{error}</div>}
      <div className="report-grid">
        <Card className="integration-form">
          <div className="chart-title"><div><h3>Dimensions</h3><p>Reusable business axes for reports and allocation</p></div></div>
          <div className="form-grid">
            <label><span>Key</span><input value={dimensionDraft.key} onChange={(event) => setDimensionDraft({ ...dimensionDraft, key: event.target.value })} placeholder="Region" /></label>
            <label><span>Name</span><input value={dimensionDraft.name} onChange={(event) => setDimensionDraft({ ...dimensionDraft, name: event.target.value })} placeholder="Business region" /></label>
            <label className="full"><span>Description</span><input value={dimensionDraft.description} onChange={(event) => setDimensionDraft({ ...dimensionDraft, description: event.target.value })} /></label>
          </div>
          <button className="button" disabled={busy || !dimensionDraft.key || !dimensionDraft.name} onClick={saveDimension}><Plus size={15} />Add dimension</button>
          <div className="report-table-wrap"><table><thead><tr><th>Dimension</th><th>Status</th><th /></tr></thead><tbody>
            {dimensions.map((item) => <tr key={item.key}><td><strong>{item.name}</strong><small>{item.key}{item.implicit ? " · discovered" : ""}</small></td><td>{item.status}</td><td><button className="icon-button" aria-label={`Disable ${item.name}`} disabled={busy || item.implicit || item.status === "inactive"} onClick={async () => { await api.deleteVirtualTagDimension(item.key); await load(); }}><Trash2 size={14} /></button></td></tr>)}
          </tbody></table></div>
        </Card>

        <Card className="integration-form">
          <div className="chart-title"><div><h3>Rule editor</h3><p>Preview affected resources and monthly cost before saving</p></div></div>
          <div className="form-grid">
            <label><span>Rule name</span><input value={draft.name ?? ""} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
            <label><span>Dimension</span><select value={draft.tagKey ?? ""} onChange={(event) => setDraft({ ...draft, tagKey: event.target.value })}><option value="">Select…</option>{dimensions.filter((item) => item.status === "active").map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}</select></label>
            <label><span>Effect</span><select value={draft.effect ?? "include"} onChange={(event) => setDraft({ ...draft, effect: event.target.value as "include" | "exclude" })}><option value="include">Assign value</option><option value="exclude">Exclude assignment</option></select></label>
            <label><span>Value</span><input value={draft.tagValue ?? ""} disabled={draft.effect === "exclude"} onChange={(event) => setDraft({ ...draft, tagValue: event.target.value })} /></label>
            <label><span>Priority</span><input type="number" min="1" max="1000" value={draft.priority ?? 100} onChange={(event) => setDraft({ ...draft, priority: Number(event.target.value) })} /></label>
            <label><span>Group logic</span><select value={draft.conditions?.combinator ?? "and"} onChange={(event) => setDraft({ ...draft, conditions: { ...draft.conditions, combinator: event.target.value as "and" | "or" } })}><option value="and">All conditions (AND)</option><option value="or">Any condition (OR)</option></select></label>
          </div>
          {conditions.map((item, index) => <div className="form-grid" key={index}>
            <label><span>Field</span><select value={item.field} onChange={(event) => updateCondition(index, { field: event.target.value })}>{["subscriptionId", "subscriptionName", "resourceGroup", "resourceType", "region", "name", "serviceName", "meterCategory", "billingScope", "nativeTag"].map((field) => <option key={field}>{field}</option>)}</select></label>
            {item.field === "nativeTag" && <label><span>Native tag key</span><input value={item.key ?? ""} onChange={(event) => updateCondition(index, { key: event.target.value })} /></label>}
            <label><span>Operator</span><select value={item.operator} onChange={(event) => updateCondition(index, { operator: event.target.value })}>{["equals", "not_equals", "contains", "starts_with", "in", "exists", "not_exists"].map((operator) => <option key={operator}>{operator}</option>)}</select></label>
            {!(["exists", "not_exists"].includes(item.operator)) && <label><span>Value{item.operator === "in" ? "s (comma separated)" : ""}</span><input value={item.value ?? (item.values ?? []).join(", ")} onChange={(event) => updateCondition(index, item.operator === "in" ? { value: undefined, values: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) } : { value: event.target.value, values: undefined })} /></label>}
            <button className="icon-button" aria-label="Remove condition" disabled={conditions.length === 1} onClick={() => setDraft({ ...draft, conditions: { ...draft.conditions, conditions: conditions.filter((_, itemIndex) => itemIndex !== index) } })}><Trash2 size={14} /></button>
          </div>)}
          <div className="form-actions">
            <button className="button button--ghost" onClick={() => setDraft({ ...draft, conditions: { ...draft.conditions, conditions: [...conditions, { ...emptyCondition }] } })}><Plus size={15} />Condition</button>
            <button className="button button--ghost" disabled={busy} onClick={() => runRule("preview")}>Preview</button>
            <button className="button" disabled={busy} onClick={() => runRule("save")}><Save size={15} />Save rule</button>
          </div>
          {busy && <Loading />}
          {preview && <div className="inline-alert"><strong>{preview.matchedCount.toLocaleString()}</strong> of {preview.totalResources.toLocaleString()} resources · {currency(preview.matchedMonthlyCost, "USD")} current monthly cost</div>}
        </Card>
      </div>
      <Card className="report-table-card">
        <div className="chart-title"><div><h3>Rules</h3><p>Versioned assignments and exclusions</p></div></div>
        <div className="report-table-wrap"><table><thead><tr><th>Name</th><th>Assignment</th><th>Priority</th><th>Status</th><th>Version</th><th /></tr></thead><tbody>
          {rules.map((rule) => <tr key={rule.ruleId}><td><button className="table-link" onClick={() => editRule(rule)}>{rule.name}</button></td><td>{rule.effect === "exclude" ? "Exclude" : `${rule.tagKey} = ${rule.tagValue}`}</td><td>{rule.priority}</td><td><button className="table-link" onClick={async () => { await api.setVirtualTagRuleStatus(rule.ruleId, rule.status === "active" ? "inactive" : "active"); await load(); }}>{rule.status}</button></td><td>{rule.version}</td><td><button className="icon-button" aria-label={`Disable ${rule.name}`} disabled={rule.status === "inactive"} onClick={async () => { await api.deleteVirtualTagRule(rule.ruleId); await load(); }}><Trash2 size={14} /></button></td></tr>)}
        </tbody></table></div>
      </Card>
    </div>
  );
}
