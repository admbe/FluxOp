import { PlayCircle, Download, ShieldCheck, ShieldAlert, ShieldQuestion, GitCompareArrows } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api";
import { Card, EmptyState, ErrorPanel, Loading, PageHeader, Tabs } from "../components/Ui";
import { currency, percent, titleCase } from "../format";
import type {
  CommitmentOptimizerHour,
  CommitmentOptimizerRun,
  CommitmentOptimizerRunDetail,
  CommitmentOptimizerStatus,
  CommitmentRunComparison,
} from "../types";

const PORTFOLIO_LABELS: Record<string, string> = {
  payg: "PAYG baseline",
  existing_commitments: "Existing commitments",
  ri_only: "Reservation only",
  sp_only: "Savings Plan only",
  blended: "Blended RI + SP",
};

const READINESS_ICONS: Record<string, typeof ShieldCheck> = {
  PURCHASE_READY: ShieldCheck,
  REVIEW_REQUIRED: ShieldQuestion,
  DIRECTIONAL_ONLY: ShieldQuestion,
  BLOCKED: ShieldAlert,
};

export function CommitmentOptimizerPage({ canManage }: { canManage: boolean }) {
  const [status, setStatus] = useState<CommitmentOptimizerStatus | null>(null);
  const [detail, setDetail] = useState<CommitmentOptimizerRunDetail | null>(null);
  const [hours, setHours] = useState<CommitmentOptimizerHour[]>([]);
  const [runs, setRuns] = useState<CommitmentOptimizerRun[]>([]);
  const [comparison, setComparison] = useState<CommitmentRunComparison | null>(null);
  const [compareBase, setCompareBase] = useState("");
  const [compareTarget, setCompareTarget] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"summary" | "scenarios" | "hourly" | "gates" | "runs">("summary");
  const [lookbackDays, setLookbackDays] = useState(30);
  const [riskProfile, setRiskProfile] = useState<"conservative" | "balanced" | "aggressive">("balanced");
  const [term, setTerm] = useState<"P1Y" | "P3Y">("P1Y");

  const load = useCallback(() => {
    api
      .commitmentOptimizerStatus()
      .then((value) => {
        setStatus(value);
        api.commitmentOptimizerRuns(50).then((list) => setRuns(list.runs));
        if (value.latestRun) {
          return api.commitmentOptimizerRun(value.latestRun.runId).then((run) => {
            setDetail(run);
            return api.commitmentOptimizerHourly(value.latestRun!.runId).then((hourly) => {
              setHours(hourly.hours);
            });
          });
        }
        setDetail(null);
        setHours([]);
      })
      .catch((reason) => setError(reason.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function runOptimizer() {
    setBusy(true);
    api
      .startCommitmentOptimizerRun({ lookbackDays, riskProfile, term })
      .then((value) => {
        setDetail(value);
        if (value.run?.runId) {
          api.commitmentOptimizerHourly(value.run.runId).then((hourly) => setHours(hourly.hours));
        }
        return api.commitmentOptimizerStatus().then(setStatus);
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setBusy(false));
  }

  function decide(recommendationId: string, decision: "approved" | "rejected") {
    api
      .decideCommitmentRecommendation(recommendationId, decision)
      .then(() => detail && detail.run?.runId ? api.commitmentOptimizerRun(detail.run.runId).then(setDetail) : undefined)
      .catch((reason) => setError(reason.message));
  }

  function compareRuns() {
    if (!compareBase || !compareTarget) return;
    api
      .compareCommitmentOptimizerRuns(compareBase, compareTarget)
      .then(setComparison)
      .catch((reason) => setError(reason.message));
  }

  // The run-start response can come back without a run at all (the
  // author already guards value.run?.runId on that path), so every read
  // through detail.run has to tolerate its absence -- otherwise the
  // first render after clicking Run optimizer throws and the error
  // boundary replaces the whole page.
  const summary = detail?.run?.summary;
  const portfolios = summary?.portfolios ?? {};
  const recommended = summary?.recommendedPortfolio ?? "";
  const runCurrency = detail?.run?.currency || summary?.currency || "USD";
  const readiness = detail?.run?.readiness || "";
  const ReadinessIcon = READINESS_ICONS[readiness] ?? ShieldQuestion;
  const reviewEvents = summary?.reviewEvents ?? [];
  const backtest = summary?.backtest ?? null;

  const chartData = useMemo(
    () =>
      hours.map((hour) => ({
        hour: hour.hour.slice(5, 16).replace("T", " "),
        riCovered: Number(hour.riCoveredCost.toFixed(2)),
        spCovered: Number(hour.spCoveredCost.toFixed(2)),
        payg: Number(hour.paygCost.toFixed(2)),
        waste: Number(hour.spWaste.toFixed(2)),
      })),
    [hours],
  );

  if (error && !status) return <ErrorPanel message={error} />;
  if (!status) return <Loading />;

  if (!status.enabled) {
    return (
      <div>
        <PageHeader
          eyebrow="FinOps"
          title="Commitment optimizer"
          description="Hourly Reservation and Savings Plan portfolio optimization over governed FOCUS evidence."
        />
        <Card>
          <EmptyState
            title="Optimizer disabled"
            description="Set FLUX_COMMITMENT_OPTIMIZER_ENABLED=true to enable the Azure Commitment Purchase Optimizer. Until then the right-sizing plan keeps producing directional candidates only."
          />
        </Card>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        eyebrow="FinOps"
        title="Commitment optimizer"
        description="Exact, explainable Reservation and Savings Plan purchase portfolios simulated hour by hour from governed FOCUS evidence. Flux never purchases commitments automatically."
        action={
          canManage ? (
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <select
                className="input"
                value={lookbackDays}
                onChange={(event) => setLookbackDays(Number(event.target.value))}
                aria-label="Lookback days"
              >
                <option value={7}>7-day lookback</option>
                <option value={30}>30-day lookback</option>
                <option value={60}>60-day lookback</option>
              </select>
              <select
                className="input"
                value={riskProfile}
                onChange={(event) => setRiskProfile(event.target.value as typeof riskProfile)}
                aria-label="Risk profile"
              >
                <option value="conservative">Conservative</option>
                <option value="balanced">Balanced</option>
                <option value="aggressive">Aggressive</option>
              </select>
              <select
                className="input"
                value={term}
                onChange={(event) => setTerm(event.target.value as typeof term)}
                aria-label="Term"
              >
                <option value="P1Y">1-year term</option>
                <option value="P3Y">3-year term</option>
              </select>
              <button className="button" onClick={runOptimizer} disabled={busy}>
                <PlayCircle size={15} /> {busy ? "Optimizing…" : "Run optimizer"}
              </button>
              {detail?.run?.runId && readiness !== "BLOCKED" && readiness !== "DIRECTIONAL_ONLY" && (
                <a
                  className="button button--secondary"
                  href={api.commitmentOptimizerManifestUrl(detail.run!.runId, "csv")}
                >
                  <Download size={15} /> Manifest CSV
                </a>
              )}
            </div>
          ) : undefined
        }
      />

      {error ? <ErrorPanel message={error} /> : null}

      {!detail ? (
        <Card className="co-status">
          <EmptyState
            title="No optimization run yet"
            description="Run the optimizer to build purchase-ready portfolios from the latest governed FOCUS evidence, negotiated price sheet, and commitment inventory."
          />
        </Card>
      ) : (
        <>
          <Card className="co-status">
            <div className="metrics-grid" style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))" }}>
              <div>
                <span className="eyebrow">Purchase readiness</span>
                <strong style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <ReadinessIcon size={16} /> {readiness.replaceAll("_", " ")}
                </strong>
              </div>
              <div>
                <span className="eyebrow">Recommended portfolio</span>
                <strong>{PORTFOLIO_LABELS[recommended] ?? recommended}</strong>
              </div>
              <div>
                <span className="eyebrow">Annual cost (PAYG)</span>
                <strong>{currency(portfolios.payg?.annualizedCost ?? null, runCurrency)}</strong>
              </div>
              <div>
                <span className="eyebrow">Annual cost (recommended)</span>
                <strong>{currency(portfolios[recommended]?.annualizedCost ?? null, runCurrency)}</strong>
              </div>
              <div>
                <span className="eyebrow">Annual savings</span>
                <strong>{currency(portfolios[recommended]?.annualizedSavings ?? null, runCurrency)}</strong>
              </div>
              <div>
                <span className="eyebrow">Contracted ESR</span>
                <strong>{percent((portfolios[recommended]?.contractedEsr ?? 0) * 100)}</strong>
              </div>
              <div>
                <span className="eyebrow">Coverage</span>
                <strong>{percent((portfolios[recommended]?.coverage ?? 0) * 100)}</strong>
              </div>
              <div>
                <span className="eyebrow">Commitment waste</span>
                <strong>{currency(portfolios[recommended]?.waste ?? null, runCurrency)}</strong>
              </div>
            </div>
            {summary?.directionalPricing ? (
              <p style={{ marginTop: 12 }}>
                Directional pricing in use: the customer price sheet is unavailable or incomplete, so
                retail rates are reference-only and no purchase manifest is produced.
              </p>
            ) : null}
            {detail.run?.error ? <p style={{ marginTop: 12 }}>{detail.run?.error}</p> : null}
            {backtest ? (
              <p style={{ marginTop: 12 }}>
                Backtest (train {backtest.trainHours}h / holdout {backtest.holdoutHours}h): in-sample
                savings {currency(backtest.inSampleSavings, runCurrency)}, holdout savings{" "}
                {currency(backtest.holdoutSavings, runCurrency)}, holdout waste{" "}
                {currency(backtest.holdoutWaste, runCurrency)} —{" "}
                {backtest.stable
                  ? "stable across windows."
                  : "holdout performance degrades materially; treat savings as upper-bound."}
              </p>
            ) : null}
            {reviewEvents.length > 0 ? (
              <div style={{ marginTop: 12 }}>
                <span className="eyebrow">Review events</span>
                <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {reviewEvents.map((event, index) => (
                    <li key={`${event.type}-${index}`}>
                      <strong>{titleCase(event.type)}:</strong> {event.detail}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </Card>

          <Tabs
            tabs={[
              { id: "summary", label: "Purchase plan" },
              { id: "scenarios", label: "Portfolios", badge: detail.scenarios.length },
              { id: "hourly", label: "Hourly evidence", badge: hours.length },
              { id: "gates", label: "Data-quality gates" },
              { id: "runs", label: "Run history", badge: runs.length },
            ]}
            active={tab}
            onChange={setTab}
            label="Commitment optimizer views"
          />

          {tab === "summary" && (
            <Card className="co-recommendations">
              <h2>Exact purchase recommendations</h2>
              {detail.recommendations.length === 0 ? (
                <EmptyState title="No recommendations" description="This run produced no commitment recommendations." />
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Action</th>
                        <th>Type</th>
                        <th>SKU / commitment</th>
                        <th>Region</th>
                        <th>Qty</th>
                        <th>Term</th>
                        <th>Expected cost</th>
                        <th>Expected savings</th>
                        <th>Status</th>
                        {canManage ? <th>Decision</th> : null}
                      </tr>
                    </thead>
                    <tbody>
                      {detail.recommendations.map((rec) => (
                        <tr key={rec.recommendationId}>
                          <td>{titleCase(rec.action)}</td>
                          <td>{rec.commitmentType.replaceAll("_", " ")}</td>
                          <td>
                            {rec.commitmentType === "savings_plan"
                              ? `${currency(rec.hourlyCommitment, runCurrency)}/hour`
                              : rec.sku || "—"}
                          </td>
                          <td>{rec.region || "—"}</td>
                          <td>{rec.quantity || "—"}</td>
                          <td>{rec.term || "—"}</td>
                          <td>{currency(rec.expectedCost, runCurrency)}</td>
                          <td>{currency(rec.expectedSavings, runCurrency)}</td>
                          <td>{rec.status}</td>
                          {canManage ? (
                            <td>
                              {rec.decisionBy ? (
                                <span>{rec.status} by {rec.decisionBy}</span>
                              ) : rec.action === "buy_now" || rec.action === "review" ? (
                                <div style={{ display: "flex", gap: 6 }}>
                                  <button className="button button--secondary" onClick={() => decide(rec.recommendationId, "approved")}>
                                    Approve
                                  </button>
                                  <button className="button button--secondary" onClick={() => decide(rec.recommendationId, "rejected")}>
                                    Reject
                                  </button>
                                </div>
                              ) : (
                                <span>—</span>
                              )}
                            </td>
                          ) : null}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          )}

          {tab === "scenarios" && (
            <Card className="co-portfolios">
              <h2>Portfolio comparison</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Portfolio</th>
                      <th>Total cost</th>
                      <th>Savings</th>
                      <th>Contracted ESR</th>
                      <th>Coverage</th>
                      <th>RI utilization</th>
                      <th>SP utilization</th>
                      <th>Waste</th>
                      <th>Downside savings</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(portfolios).map(([name, p]) => (
                      <tr key={name} style={name === recommended ? { fontWeight: 600 } : undefined}>
                        <td>{PORTFOLIO_LABELS[name] ?? name}{name === recommended ? " (recommended)" : ""}</td>
                        <td>{currency(p.totalCost, runCurrency)}</td>
                        <td>{currency(p.savings, runCurrency)}</td>
                        <td>{percent(p.contractedEsr * 100)}</td>
                        <td>{percent(p.coverage * 100)}</td>
                        <td>{percent(p.riUtilization * 100)}</td>
                        <td>{percent(p.spUtilization * 100)}</td>
                        <td>{currency(p.waste, runCurrency)}</td>
                        <td>{currency(p.downsideSavings, runCurrency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          {tab === "hourly" && (
            <Card className="co-hourly">
              <h2>Hourly benefit waterfall</h2>
              {hours.length === 0 ? (
                <EmptyState title="No hourly evidence" description="This run has no stored hourly evidence." />
              ) : (
                <ResponsiveContainer width="100%" height={320}>
                  <AreaChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border))" />
                    <XAxis dataKey="hour" tickLine={false} axisLine={false} tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} minTickGap={40} />
                    <YAxis tick={{ fill: "rgb(var(--text-muted))", fontSize: 10 }} tickFormatter={(value) => currency(Number(value), runCurrency)} />
                    <Tooltip formatter={(value) => currency(Number(value), runCurrency)} />
                    <Legend />
                    <Area type="monotone" dataKey="riCovered" name="RI covered" stackId="1" stroke="#0f9d8c" fill="#0f9d8c" fillOpacity={0.5} />
                    <Area type="monotone" dataKey="spCovered" name="SP covered" stackId="1" stroke="#4f8ef7" fill="#4f8ef7" fillOpacity={0.5} />
                    <Area type="monotone" dataKey="payg" name="PAYG remainder" stackId="1" stroke="#f7b84f" fill="#f7b84f" fillOpacity={0.5} />
                    <Area type="monotone" dataKey="waste" name="SP waste" stackId="1" stroke="#e05d5d" fill="#e05d5d" fillOpacity={0.35} />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </Card>
          )}

          {tab === "gates" && (
            <Card className="co-gates">
              <h2>Data-quality gates</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Gate</th>
                      <th>Severity</th>
                      <th>Result</th>
                      <th>Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(detail.run?.gates ?? []).map((gate) => (
                      <tr key={gate.name}>
                        <td>{gate.name.replaceAll("_", " ")}</td>
                        <td>{gate.severity}</td>
                        <td>{gate.passed ? "pass" : "fail"}</td>
                        <td>{gate.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          {tab === "runs" && (
            <Card className="co-runs">
              <h2>Run history and comparison</h2>
              {runs.length === 0 ? (
                <EmptyState title="No runs yet" description="Run the optimizer to create the first versioned run." />
              ) : (
                <>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
                    <select className="input" value={compareBase} onChange={(e) => setCompareBase(e.target.value)} aria-label="Base run">
                      <option value="">Base run…</option>
                      {runs.map((run) => (
                        <option key={run.runId} value={run.runId}>
                          {run.startedAt ?? run.runId} — {run.readiness || run.status}
                        </option>
                      ))}
                    </select>
                    <select className="input" value={compareTarget} onChange={(e) => setCompareTarget(e.target.value)} aria-label="Comparison run">
                      <option value="">Compare with…</option>
                      {runs.map((run) => (
                        <option key={run.runId} value={run.runId}>
                          {run.startedAt ?? run.runId} — {run.readiness || run.status}
                        </option>
                      ))}
                    </select>
                    <button
                      className="button button--secondary"
                      onClick={compareRuns}
                      disabled={!compareBase || !compareTarget || compareBase === compareTarget}
                    >
                      <GitCompareArrows size={15} /> Compare
                    </button>
                  </div>
                  {comparison ? (
                    <div className="table-wrap" style={{ marginBottom: 12 }}>
                      <table>
                        <thead>
                          <tr>
                            <th>Metric</th>
                            <th>Base</th>
                            <th>Compare</th>
                            <th>Delta</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr>
                            <td>Recommended portfolio</td>
                            <td>{PORTFOLIO_LABELS[comparison.base.recommendedPortfolio] ?? comparison.base.recommendedPortfolio}</td>
                            <td>{PORTFOLIO_LABELS[comparison.compare.recommendedPortfolio] ?? comparison.compare.recommendedPortfolio}</td>
                            <td>{comparison.recommendationChanged ? "changed" : "unchanged"}</td>
                          </tr>
                          <tr>
                            <td>Annualized cost</td>
                            <td>{currency(comparison.base.annualizedCost, comparison.base.currency)}</td>
                            <td>{currency(comparison.compare.annualizedCost, comparison.compare.currency)}</td>
                            <td>{currency(comparison.deltas.annualizedCost, comparison.compare.currency)}</td>
                          </tr>
                          <tr>
                            <td>Annualized savings</td>
                            <td>{currency(comparison.base.annualizedSavings, comparison.base.currency)}</td>
                            <td>{currency(comparison.compare.annualizedSavings, comparison.compare.currency)}</td>
                            <td>{currency(comparison.deltas.annualizedSavings, comparison.compare.currency)}</td>
                          </tr>
                          <tr>
                            <td>Contracted ESR</td>
                            <td>{percent(comparison.base.contractedEsr * 100)}</td>
                            <td>{percent(comparison.compare.contractedEsr * 100)}</td>
                            <td>{percent(comparison.deltas.contractedEsr * 100)}</td>
                          </tr>
                          <tr>
                            <td>Coverage</td>
                            <td>{percent(comparison.base.coverage * 100)}</td>
                            <td>{percent(comparison.compare.coverage * 100)}</td>
                            <td>{percent(comparison.deltas.coverage * 100)}</td>
                          </tr>
                          <tr>
                            <td>Purchase lines</td>
                            <td>{comparison.base.purchaseLines}</td>
                            <td>{comparison.compare.purchaseLines}</td>
                            <td>{comparison.deltas.purchaseLines}</td>
                          </tr>
                        </tbody>
                      </table>
                      <p style={{ marginTop: 8 }}>
                        Savings drift {comparison.savingsDriftPercent}% against a{" "}
                        {comparison.materialityThresholdPercent}% materiality threshold —{" "}
                        {comparison.materialChange ? "material change; review the newer run." : "no material change."}
                      </p>
                    </div>
                  ) : null}
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Started</th>
                          <th>Readiness</th>
                          <th>Portfolio</th>
                          <th>Risk profile</th>
                          <th>Term</th>
                          <th>Lookback</th>
                          <th>Algorithm</th>
                        </tr>
                      </thead>
                      <tbody>
                        {runs.map((run) => (
                          <tr key={run.runId} style={run.runId === detail?.run?.runId ? { fontWeight: 600 } : undefined}>
                            <td>{run.startedAt ?? "—"}</td>
                            <td>{(run.readiness || run.status).replaceAll("_", " ")}</td>
                            <td>{PORTFOLIO_LABELS[run.recommendedPortfolio] ?? run.recommendedPortfolio ?? "—"}</td>
                            <td>{titleCase(run.riskProfile)}</td>
                            <td>{run.term}</td>
                            <td>{run.lookbackDays}d</td>
                            <td>{run.algorithmVersion}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </Card>
          )}
        </>
      )}
    </div>
  );
}
