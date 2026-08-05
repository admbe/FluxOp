import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronRight,
  Eraser,
  ExternalLink,
  Maximize2,
  Minimize2,
  Send,
  Shuffle,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  X,
} from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import { announceActivity } from "../busy";
import { ErrorBoundary } from "./ErrorBoundary";
import { relativeTime, titleCase } from "../format";
import { pickSuggestions } from "../intelligencePrompts";
import { useChartColors } from "../theme";
import type {
  IntelligenceChartBlock,
  IntelligenceConversationEntry,
  IntelligenceReview,
  IntelligenceResponse,
  IntelligenceStatus,
  Page,
} from "../types";

function performanceBottleneck(performance: IntelligenceResponse["performance"]) {
  const stages = [
    ["AI analysis", performance.modelMs],
    ["Governed tools", performance.governedToolMs],
    ["DuckDB / reports", performance.databaseMs],
    ["App orchestration", performance.applicationMs],
    ["Response validation", performance.validationMs],
    ["Network + ingress", performance.transportAndIngressMs ?? 0],
    ["Browser render", performance.clientRenderMs ?? 0],
  ] as const;
  return stages.reduce((largest, current) => current[1] > largest[1] ? current : largest);
}

export type IntelligenceController = {
  page: Page;
  entries: IntelligenceConversationEntry[];
  status: IntelligenceStatus | null;
  busy: boolean;
  error: string;
  modelProfile: "fast" | "benchmark";
  setModelProfile: (value: "fast" | "benchmark") => void;
  ask: (question: string, profile?: "fast" | "benchmark") => Promise<void>;
  clear: () => void;
  feedback: (
    requestId: string,
    rating: "helpful" | "not_helpful",
  ) => Promise<void>;
};

export function useIntelligenceConversation(page: Page): IntelligenceController {
  const [entries, setEntries] = useState<IntelligenceConversationEntry[]>([]);
  const [status, setStatus] = useState<IntelligenceStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [modelProfile, setModelProfile] = useState<"fast" | "benchmark">("fast");

  function refreshStatus() {
    api.intelligenceStatus().then(setStatus).catch(() => undefined);
  }

  useEffect(refreshStatus, []);

  async function ask(question: string, profile?: "fast" | "benchmark") {
    const content = question.trim();
    if (!content || busy) return;
    // A caller-supplied profile (Review with Flux forces Deep analysis)
    // must apply to THIS request; setModelProfile alone would only affect
    // the next one. The state update keeps the toolbar select honest.
    const effectiveProfile = profile ?? modelProfile;
    if (profile && profile !== modelProfile) setModelProfile(profile);
    const userEntry: IntelligenceConversationEntry = {
      id: crypto.randomUUID(),
      role: "user",
      content,
    };
    const next = [...entries, userEntry];
    setEntries(next);
    setBusy(true);
    announceActivity("intelligence", true);
    setError("");
    const clientStarted = performance.now();
    try {
      const response = await api.intelligenceChat(
        next.map((entry) => ({
          role: entry.role,
          content: entry.response?.summary || entry.content,
        })),
        { page },
        effectiveProfile,
      );
      const responseReceived = performance.now();
      const clientRoundTripMs = Math.round(responseReceived - clientStarted);
      response.performance.clientRoundTripMs = clientRoundTripMs;
      response.performance.transportAndIngressMs = Math.max(
        0,
        clientRoundTripMs - response.performance.durationMs,
      );
      setEntries((current) => [
        ...current,
        {
          id: response.requestId,
          role: "assistant",
          content: response.summary,
          response,
        },
      ]);
      requestAnimationFrame(() => {
        const clientRenderMs = Math.round(performance.now() - responseReceived);
        const clientEndToEndMs = Math.round(performance.now() - clientStarted);
        setEntries((current) => current.map((entry) => {
          if (entry.id !== response.requestId || !entry.response) return entry;
          return {
            ...entry,
            response: {
              ...entry.response,
              performance: {
                ...entry.response.performance,
                clientRenderMs,
                clientEndToEndMs,
              },
            },
          };
        }));
        api.intelligencePerformance(
          response.requestId,
          clientRoundTripMs,
          clientRenderMs,
          clientEndToEndMs,
        ).then(refreshStatus).catch(() => undefined);
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Flux Intelligence could not answer.");
    } finally {
      setBusy(false);
      announceActivity("intelligence", false);
    }
  }

  async function feedback(
    requestId: string,
    rating: "helpful" | "not_helpful",
  ) {
    try {
      await api.intelligenceFeedback(requestId, rating);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Feedback could not be saved.");
    }
  }

  return {
    page,
    entries,
    status,
    busy,
    error,
    modelProfile,
    setModelProfile,
    ask,
    clear: () => {
      setEntries([]);
      setError("");
    },
    feedback,
  };
}
function IntelligenceChart({ block }: { block: IntelligenceChartBlock }) {
  const chart = useChartColors();
  const colors = chart.series;
  const common = (
    <>
      <CartesianGrid stroke="rgb(var(--border))" vertical={false} />
      <XAxis dataKey={block.xKey} stroke="rgb(var(--text-muted))" tick={{ fontSize: 10 }} />
      <YAxis stroke="rgb(var(--text-muted))" tick={{ fontSize: 10 }} width={55} />
      <Tooltip
        contentStyle={{
          background: "rgb(var(--surface-raised))",
          border: "1px solid rgb(var(--border-bright))",
          borderRadius: 8,
          color: "rgb(var(--text))",
          fontSize: 11,
        }}
      />
    </>
  );
  return (
    <section className="intelligence-chart">
      <h4>{block.title}</h4>
      <div>
        <ResponsiveContainer width="100%" height="100%">
          {block.chartType === "line" ? (
            <LineChart data={block.data}>
              {common}
              {block.yKeys.map((key, index) => (
                <Line key={key} dataKey={key} stroke={colors[index % colors.length]} strokeWidth={2} dot={false} />
              ))}
            </LineChart>
          ) : block.chartType === "area" ? (
            <AreaChart data={block.data}>
              {common}
              {block.yKeys.map((key, index) => (
                <Area key={key} dataKey={key} stroke={colors[index % colors.length]} fill={colors[index % colors.length]} fillOpacity={0.12} />
              ))}
            </AreaChart>
          ) : (
            <BarChart data={block.data}>
              {common}
              {block.yKeys.map((key, index) => (
                <Bar key={key} dataKey={key} fill={colors[index % colors.length]} radius={[4, 4, 0, 0]} maxBarSize={24} />
              ))}
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
    </section>
  );
}

function MermaidDiagram({ title, content }: { title: string; content: string }) {
  const chart = useChartColors();
  const id = `flux-mermaid-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    import("mermaid")
      .then(async ({ default: mermaid }) => {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "base",
          themeVariables: {
            background: chart.background,
            primaryColor: chart.surface,
            primaryTextColor: chart.text,
            primaryBorderColor: chart.primary,
            secondaryColor: chart.series[1],
            tertiaryColor: chart.series[2],
            lineColor: chart.muted,
            textColor: chart.text,
            edgeLabelBackground: chart.surface,
            fontFamily: "Inter, Segoe UI Variable, Segoe UI, sans-serif",
          },
          flowchart: { htmlLabels: false },
        });
        const result = await mermaid.render(id, content);
        if (active && ref.current) ref.current.innerHTML = result.svg;
      })
      .catch(() => {
        if (active) setError("This diagram could not be rendered.");
      });
    return () => {
      active = false;
    };
  }, [chart, content, id]);

  return (
    <section className="intelligence-mermaid">
      <h4>{title}</h4>
      {error ? <p>{error}</p> : <div ref={ref} />}
    </section>
  );
}

function ResponseContent({
  response,
  onAsk,
  onFeedback,
}: {
  response: IntelligenceResponse;
  onAsk: (value: string) => void;
  onFeedback: (rating: "helpful" | "not_helpful") => void;
}) {
  const [rated, setRated] = useState(false);
  const endToEndMs = (
    response.performance.clientEndToEndMs
    ?? response.performance.clientRoundTripMs
    ?? response.performance.durationMs
  );
  // Expected, self-resolving gaps first: telemetry evidence windows still
  // accumulating and billing-finalization lag are working as designed, and
  // labelling them "degraded" tells the reader something is broken when
  // nothing needs their attention. Only unmatched coverage language falls
  // through to the needs-attention tone.
  const expectedLimitations = response.limitations.filter((item) =>
    /\b(warming[ -]?up|not finalized|finaliz\w* (24|48|hour|late)|lands? 24|24-48 hours|evidence window|more evidence|accumulat)\b/i.test(item),
  );
  const criticalLimitations = response.limitations.filter(
    (item) =>
      !expectedLimitations.includes(item)
      && /\b(429|coverage|failed|missing|partial|stale|throttl|unavailable|retained)\b/i.test(item),
  );
  const reviewLimitations = response.limitations.filter((item) =>
    /^review flag:/i.test(item),
  );
  const otherLimitations = response.limitations.filter(
    (item) =>
      !expectedLimitations.includes(item)
      && !criticalLimitations.includes(item)
      && !reviewLimitations.includes(item),
  );
  const bottleneck = performanceBottleneck(response.performance);
  const limitationPanel = (
    items: string[],
    tone: "standard" | "critical" | "review" | "expected" = "standard",
  ) => items.length > 0 && (
    <div className={`intelligence-limitations intelligence-limitations--${tone}`}>
      <AlertTriangle size={14} />
      <div>
        <strong>
          {tone === "critical"
            ? "Coverage needs attention"
            : tone === "review"
              ? "Flagged for review"
              : tone === "expected"
                ? "Expected data lag · no action needed"
                : "Limitations"}
        </strong>
        {items.map((item) => <span key={item}>{item}</span>)}
      </div>
    </div>
  );
  return (
    <>
      {endToEndMs >= 20_000 && (
        <div className="intelligence-slow-notice">
          <AlertTriangle size={14} />
          <span>
            <strong>Slow response detected</strong>
            {bottleneck[0]} was the largest measured stage at {(bottleneck[1] / 1000).toFixed(1)}s of {(endToEndMs / 1000).toFixed(1)}s end to end.
          </span>
        </div>
      )}
      {limitationPanel(criticalLimitations, "critical")}
      {limitationPanel(expectedLimitations, "expected")}
      {limitationPanel(reviewLimitations, "review")}
      {response.blocks.map((block, index) => {
        if (block.type === "chart") return <IntelligenceChart key={index} block={block} />;
        if (block.type === "mermaid") {
          return <MermaidDiagram key={index} title={block.title} content={block.content} />;
        }
        return (
          <div className="intelligence-markdown" key={index}>
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ children, ...props }) => (
                  <a {...props} target="_blank" rel="noreferrer">{children}</a>
                ),
              }}
            >
              {block.content}
            </ReactMarkdown>
          </div>
        );
      })}
      {(response.facts.length > 0 || response.interpretations.length > 0) && (
        <div className="intelligence-evidence">
          {response.facts.length > 0 && (
            <div>
              <strong>Retrieved facts</strong>
              <ul>{response.facts.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          )}
          {response.interpretations.length > 0 && (
            <div>
              <strong>Interpretation</strong>
              <ul>{response.interpretations.map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          )}
        </div>
      )}
      {limitationPanel(otherLimitations)}
      {response.sources.length > 0 && (
        <details className="intelligence-sources">
          <summary>Governed sources ({response.sources.length})</summary>
          {response.sources.map((source, index) => (
            <p key={`${source.tool}-${index}`}><code>{source.tool}</code> {source.description}</p>
          ))}
        </details>
      )}
      {(response.actions || []).length > 0 && (
        <div className="intelligence-actions">
          {(response.actions || []).map((action) => (
            <a key={`${action.href}-${action.label}`} href={action.href}>
              <span><strong>{action.label}</strong>{action.description}</span>
              <ExternalLink size={14} />
            </a>
          ))}
        </div>
      )}
      {response.followUps.length > 0 && (
        <div className="intelligence-followups">
          {response.followUps.map((item) => (
            <button key={item} onClick={() => onAsk(item)}>
              {item}<ChevronRight size={13} />
            </button>
          ))}
        </div>
      )}
      <details className="intelligence-performance">
        <summary>Performance · {(endToEndMs / 1000).toFixed(1)}s end to end</summary>
        <div>
          <span><strong>AI analysis</strong>{(response.performance.modelMs / 1000).toFixed(2)}s · {response.performance.modelCallCount} call{response.performance.modelCallCount === 1 ? "" : "s"}</span>
          <span><strong>Governed tools</strong>{(response.performance.governedToolMs / 1000).toFixed(2)}s · {response.performance.toolCallCount} call{response.performance.toolCallCount === 1 ? "" : "s"}</span>
          <span><strong>Tool cache</strong>{response.performance.toolCacheHits || 0} hit{response.performance.toolCacheHits === 1 ? "" : "s"}</span>
          <span><strong>DuckDB / reports</strong>{(response.performance.databaseMs / 1000).toFixed(2)}s</span>
          <span><strong>App orchestration</strong>{(response.performance.applicationMs / 1000).toFixed(2)}s</span>
          <span><strong>Response validation</strong>{(response.performance.validationMs / 1000).toFixed(2)}s</span>
          <span><strong>Network + ingress</strong>{response.performance.transportAndIngressMs === undefined ? "Collecting…" : `${(response.performance.transportAndIngressMs / 1000).toFixed(2)}s`}</span>
          <span><strong>Browser render</strong>{response.performance.clientRenderMs === undefined ? "Collecting…" : `${(response.performance.clientRenderMs / 1000).toFixed(2)}s`}</span>
          <span><strong>Response format</strong>{({
            structured: "Structured",
            structured_repair: "Structured after one repair",
            plain_text: "Plain-text fallback",
            plain_text_review: "Plain text · flagged for review",
            structured_review: "Structured · flagged for review",
          } as Record<string, string>)[response.performance.responseMode ?? "structured"] ?? "Structured"}</span>
          <span><strong>Rill</strong>Not in this request path</span>
        </div>
        {response.performance.toolDurations.length > 0 && (
          <ul>
            {response.performance.toolDurations.map((item, index) => (
              <li key={`${item.name}-${index}`}><code>{item.name}</code> {(item.durationMs / 1000).toFixed(2)}s · {item.dataPath}{item.cacheHit ? " · cached" : ""}</li>
            ))}
          </ul>
        )}
      </details>
      <div className="intelligence-rating">
        <span>
          {(endToEndMs / 1000).toFixed(1)}s
          {" · "}
          {response.performance.toolCallCount} governed tool
          {response.performance.toolCallCount === 1 ? "" : "s"}
        </span>
        {!rated ? (
          <>
            <button aria-label="Helpful response" onClick={() => { onFeedback("helpful"); setRated(true); }}><ThumbsUp size={13} /></button>
            <button aria-label="Not helpful response" onClick={() => { onFeedback("not_helpful"); setRated(true); }}><ThumbsDown size={13} /></button>
          </>
        ) : <span><Check size={12} /> Feedback saved</span>}
      </div>
    </>
  );
}

export function IntelligenceConversation({
  controller,
  compact = false,
}: {
  controller: IntelligenceController;
  compact?: boolean;
}) {
  const [question, setQuestion] = useState("");
  // Drawn once per mount so the set stays put while the user reads it, and
  // redrawn on clear or on demand.
  const [suggestions, setSuggestions] = useState(() => pickSuggestions(controller.page));
  const endRef = useRef<HTMLDivElement>(null);
  // Braced body, not an implicit return: scrollIntoView's return value must
  // not become the effect cleanup. Browsers with a patched scrollIntoView
  // (smooth-scroll extensions return a Promise) made React call that value
  // as the cleanup when entries.length changed -- i.e. the moment a first
  // message was added -- crashing the workspace with "V is not a function"
  // at commitHookEffectListUnmount (prod incident 2026-08-01).
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [
    controller.entries.length,
    controller.busy,
  ]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = question;
    setQuestion("");
    controller.ask(value);
  }

  return (
    <div className={`intelligence-conversation ${compact ? "intelligence-conversation--compact" : ""}`}>
      <div className="intelligence-toolbar">
        <label>
          Model
          <select
            value={controller.modelProfile}
            onChange={(event) => controller.setModelProfile(event.target.value as "fast" | "benchmark")}
            disabled={controller.busy}
          >
            <option value="fast">Fast · default</option>
            <option value="benchmark">Deep analysis</option>
          </select>
        </label>
        <button
          className="icon-button"
          onClick={() => {
            controller.clear();
            setSuggestions(pickSuggestions(controller.page));
          }}
          title="Clear visible conversation"
        >
          <Eraser size={16} />
        </button>
      </div>
      <div className="intelligence-thread">
        {controller.entries.length === 0 && (
          <div className="intelligence-welcome">
            <span><Sparkles size={19} /></span>
            <h3>Ask Flux about your cloud estate</h3>
            <p>Answers use authenticated, governed Flux APIs. The model cannot query Azure, Rill, DuckDB, or SQL directly.</p>
            <div>
              {suggestions.map((item) => (
                <button key={item} onClick={() => controller.ask(item)}>
                  {item}<ChevronRight size={13} />
                </button>
              ))}
            </div>
            <button
              className="intelligence-shuffle"
              onClick={() => setSuggestions(pickSuggestions(controller.page))}
            >
              <Shuffle size={12} />Show me other questions
            </button>
          </div>
        )}
        {controller.entries.map((entry) => (
          <article key={entry.id} className={`intelligence-message intelligence-message--${entry.role}`}>
            <header>
              {entry.role === "assistant" ? <Sparkles size={14} /> : <span>You</span>}
              {entry.role === "assistant" && <span>Flux Intelligence</span>}
            </header>
            {entry.response ? (
              <ResponseContent
                response={entry.response}
                onAsk={controller.ask}
                onFeedback={(rating) => controller.feedback(entry.response!.requestId, rating)}
              />
            ) : <p>{entry.content}</p>}
          </article>
        ))}
        {controller.busy && (
          <article className="intelligence-message intelligence-message--assistant intelligence-thinking">
            <header><Bot size={14} /><span>Flux Intelligence</span></header>
            <p>Retrieving governed evidence and analyzing it…</p>
          </article>
        )}
        {controller.error && <div className="inline-alert inline-alert--error">{controller.error}</div>}
        <div ref={endRef} />
      </div>
      <form className="intelligence-composer" onSubmit={submit}>
        <textarea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="Ask about cost, usage, anomalies, governance, or opportunities…"
          maxLength={12000}
          rows={compact ? 2 : 3}
          disabled={controller.busy || !controller.status?.configured}
        />
        <button type="submit" disabled={controller.busy || !question.trim() || !controller.status?.configured} aria-label="Send">
          <Send size={17} />
        </button>
      </form>
      <footer className="intelligence-disclaimer">
        AI may make mistakes · read-only · prompts and replies retained for {controller.status?.transcriptRetentionDays || 30} days
      </footer>
    </div>
  );
}

export function IntelligencePanel({
  open,
  onClose,
  onOpenWorkspace,
  controller,
}: {
  open: boolean;
  onClose: () => void;
  onOpenWorkspace: () => void;
  controller: IntelligenceController;
}) {
  if (!open) return null;
  return (
    <>
      <button className="intelligence-panel-scrim" onClick={onClose} aria-label="Close Flux Intelligence" />
      <aside className="intelligence-panel">
        <header>
          <div><Sparkles size={17} /><strong>Ask Flux</strong><span>Read-only</span></div>
          <div>
            <button className="icon-button" onClick={onOpenWorkspace} title="Open Intelligence Workspace"><Maximize2 size={16} /></button>
            <button className="icon-button" onClick={onClose} title="Close"><X size={18} /></button>
          </div>
        </header>
        {!controller.status?.configured && (
          <div className="intelligence-notice">
            <AlertTriangle size={15} />
            <span>Flux Intelligence is not configured. Governed reporting remains available elsewhere in Flux.</span>
          </div>
        )}
        {/* Scoped to the conversation content, not the whole drawer: a
            render crash here (a malformed chart or table in a response)
            must not take the header/close button down with it, or the
            entire overlay appears to vanish with no way to dismiss it. */}
        <ErrorBoundary area="Ask Flux">
          <IntelligenceConversation controller={controller} compact />
        </ErrorBoundary>
      </aside>
    </>
  );
}

export function IntelligenceWorkspace({
  controller,
  onMinimize,
  canManageReview = false,
}: {
  controller: IntelligenceController;
  onMinimize: () => void;
  canManageReview?: boolean;
}) {
  const [review, setReview] = useState<IntelligenceReview | null>(null);
  const [reviewError, setReviewError] = useState("");
  useEffect(() => {
    if (!canManageReview) return;
    api.intelligenceReview(25)
      .then(setReview)
      .catch((reason) => setReviewError(reason instanceof Error ? reason.message : "Quality review could not load."));
  }, [canManageReview]);
  const quality = controller.status?.quality;
  return (
    <div className="intelligence-workspace">
      <div className="page-header">
        <div>
          <span className="eyebrow">Governed AI analysis</span>
          <h1>Flux Intelligence</h1>
          <p>Investigate cloud cost, usage, anomalies, governance, and optimization through authenticated, governed Flux data.</p>
        </div>
        <div className="page-actions">
          <button className="button button--secondary" onClick={onMinimize}><Minimize2 size={16} /> Open as panel</button>
        </div>
      </div>
      <div className="intelligence-status-strip card">
        <span><strong>Access</strong>{controller.status?.authorizationRole || "Flux.Reader"}</span>
        <span><strong>Data boundary</strong>Governed Flux APIs</span>
        <span><strong>Conversation</strong>{controller.status?.conversationRetention || "30 days"} review retention</span>
        <span><strong>Recent performance</strong>{controller.status?.usage.requestCount ? `${((controller.status.usage.averageClientEndToEndMs || controller.status.usage.averageLatencyMs) / 1000).toFixed(1)}s avg · ${((controller.status.usage.p95ClientEndToEndMs || controller.status.usage.p95LatencyMs) / 1000).toFixed(1)}s p95` : "No requests yet"}</span>
        <span><strong>Quality</strong>{quality ? `${titleCase(quality.status)} · ${quality.averageScore === null ? "warming up" : `${quality.averageScore}/100 avg`}` : "Collecting"}</span>
      </div>
      {canManageReview && quality && (
        <details className="intelligence-quality-review card">
          <summary>
            <span><strong>Quality and performance review</strong>{quality.requestCount} retained requests · {quality.slowRequestCount} slow · {quality.flaggedForReviewCount} flagged</span>
            <span>{titleCase(quality.status)}</span>
          </summary>
          <div className="intelligence-quality-summary">
            <span><small>Helpful</small><strong>{quality.helpfulPercent === null ? "No ratings" : `${quality.helpfulPercent}%`}</strong></span>
            <span><small>Average score</small><strong>{quality.averageScore === null ? "Warming up" : `${quality.averageScore}/100`}</strong></span>
            <span><small>Regression reviews</small><strong>{quality.regressionFailureCount}</strong></span>
            <span><small>Slow responses</small><strong>{quality.slowRequestCount}</strong></span>
            <span><small>Primary bottleneck</small><strong>{quality.bottlenecks[0] ? titleCase(quality.bottlenecks[0].stage) : "Warming up"}</strong></span>
          </div>
          <p className="muted">{quality.openItem}</p>
          {reviewError && <div className="inline-alert inline-alert--error">{reviewError}</div>}
          {review && (
            <div className="intelligence-review-list">
              {review.items.map((item) => {
                const prompt = [...item.messages].reverse().find((message) => message.role === "user")?.content || "No user prompt retained.";
                const duration = item.performance.clientEndToEndMs ?? item.performance.serverMs;
                const stage = performanceBottleneck({
                  durationMs: item.performance.serverMs,
                  modelMs: item.performance.modelMs,
                  governedToolMs: item.performance.governedToolMs,
                  databaseMs: item.performance.databaseMs,
                  validationMs: item.performance.validationMs,
                  applicationMs: item.performance.applicationMs,
                  promptTokens: 0,
                  completionTokens: 0,
                  toolCallCount: item.performance.toolDurations.length,
                  toolCacheHits: item.performance.toolDurations.filter((tool) => tool.cacheHit).length,
                  modelCallCount: 0,
                  toolDurations: item.performance.toolDurations,
                  clientEndToEndMs: item.performance.clientEndToEndMs ?? undefined,
                  transportAndIngressMs: item.performance.transportAndIngressMs ?? undefined,
                  clientRenderMs: item.performance.renderMs ?? undefined,
                  rillInPath: false,
                });
                return (
                  <details key={item.requestId}>
                    <summary>
                      <span><strong>{prompt}</strong>{relativeTime(item.occurredAt)} · {(duration / 1000).toFixed(1)}s · {stage[0]}{item.response?.quality ? ` · ${item.response.quality.score}/100` : ""}</span>
                      <span>{item.feedback ? titleCase(item.feedback) : titleCase(item.status)}</span>
                    </summary>
                    <p><b>Reply:</b> {item.response?.summary || item.rawResponse || "No validated response retained."}</p>
                    <p><b>Timing:</b> AI {(item.performance.modelMs / 1000).toFixed(2)}s · tools {(item.performance.governedToolMs / 1000).toFixed(2)}s · database {(item.performance.databaseMs / 1000).toFixed(2)}s · network {((item.performance.transportAndIngressMs || 0) / 1000).toFixed(2)}s</p>
                    {item.response?.quality?.flags.length ? <p><b>Quality flags:</b> {item.response.quality.flags.map(titleCase).join(", ")}</p> : null}
                  </details>
                );
              })}
              {!review.items.length && <p className="muted">No retained conversations are available yet.</p>}
            </div>
          )}
        </details>
      )}
      <div className="intelligence-workspace-surface">
        <IntelligenceConversation controller={controller} />
      </div>
    </div>
  );
}
