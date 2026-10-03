import type { FleetTelemetry, AdminJob, ExpertExplorerResult, AiIntelligenceConfig, BudgetGroup, CommitmentInventory, CommitmentOptimizerHour, CommitmentOptimizerRun, CommitmentOptimizerRunDetail, CommitmentOptimizerStatus, CommitmentRunComparison, CostCoverage, FiscalOutlook, GeographyReport, PlanLogEntry, SloReport, TelemetryCoverage, RightsizingBoard, RightsizingImportPreview, RightsizingImportReport, RightsizingPlanBoard, SemanticCatalog, SemanticQueryRequest, SemanticQueryResult, SemanticSqlResult, AllocationConfig, AuditEntry, DatabaseHealth, RetentionPolicy, AllocationReport, BudgetReport, ExecutiveSummary, FocusAnalyticsReport, SavingsReport, UnitEconomicsReport, AzureIntegration, ChangeAnomalies, CostAnomalies, CostAnomaly, CostAnomalyContributor, CostHistoryStatus, CostReport, FinOpsToolkitStatus, GovernanceReport, IntelligenceResponse, IntelligenceReview, IntelligenceStatus, Inventory, InventoryChanges, OperationalHealth, Opportunities, Overview, RecommendationQuality, ResourceTelemetry, RightsizingRecommendations, Session, TagHygieneReport, TelemetryStatus, VirtualTagDimension, VirtualTagPreview, VirtualTagReport, VirtualTagRule, WorkloadReport } from "./types";

import { trackBusy } from "./busy";

const API_ROOT = "/api";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function validationPath(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value
    .filter((part) => part !== "body")
    .map(String)
    .join(".");
}

export function formatApiError(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.trim()) return value;
  if (Array.isArray(value)) {
    const messages = value
      .map((item) => {
        if (item && typeof item === "object") {
          const issue = item as { loc?: unknown; msg?: unknown };
          if (typeof issue.msg === "string") {
            const path = validationPath(issue.loc);
            return path ? `${path}: ${issue.msg}` : issue.msg;
          }
        }
        return formatApiError(item, "");
      })
      .filter(Boolean);
    return messages.length ? messages.join("; ") : fallback;
  }
  if (value && typeof value === "object") {
    const payload = value as Record<string, unknown>;
    for (const key of ["message", "detail", "error"]) {
      if (key in payload) {
        const message = formatApiError(payload[key], "");
        if (message) return message;
      }
    }
  }
  return fallback;
}

export function isAbortError(error: unknown): boolean {
  return (error instanceof DOMException && error.name === "AbortError") || (error instanceof Error && error.name === "AbortError");
}

async function performRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    signal: init?.signal,
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    throw new ApiError(
      formatApiError(payload, `Request failed (${response.status})`),
      response.status,
    );
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

/**
 * Every api.* method funnels through here, so wrapping this one function is
 * all the shared busy state needs. The *ExportUrl helpers return strings and
 * are deliberately untouched: a CSV download is a browser navigation, not a
 * governed read, and should not light the mark.
 */
function request<T>(path: string, init?: RequestInit): Promise<T> {
  return trackBusy(() => performRequest<T>(path, init));
}

export const api = {
  session: (opts?: { signal?: AbortSignal }) => request<Session>("/session", { signal: opts?.signal }),
  overview: (opts?: { signal?: AbortSignal }) => request<Overview>("/overview", { signal: opts?.signal }),
  inventory: (params = new URLSearchParams(), opts?: { signal?: AbortSignal }) =>
    request<Inventory>(`/inventory?${params.toString()}`, { signal: opts?.signal }),
  inventoryExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/inventory/export?${params.toString()}`,
  changes: (params = new URLSearchParams(), opts?: { signal?: AbortSignal }) =>
    request<InventoryChanges>(`/changes?${params.toString()}`, { signal: opts?.signal }),
  changeAnomalies: (opts?: { signal?: AbortSignal }) => request<ChangeAnomalies>("/changes/anomalies", { signal: opts?.signal }),
  costAnomalies: (params = new URLSearchParams(), opts?: { signal?: AbortSignal }) =>
    request<CostAnomalies>(`/cost/anomalies?${params.toString()}`, { signal: opts?.signal }),
  costAnomaliesExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/cost/anomalies/export?${params.toString()}`,
  reviewCostAnomaly: (
    anomaly: Pick<CostAnomaly, "runId" | "costType" | "scopeType" | "scopeId">,
    reviewStatus: CostAnomaly["reviewStatus"],
    note = "",
  ) =>
    request("/cost/anomalies/review", {
      method: "PUT",
      body: JSON.stringify({ ...anomaly, reviewStatus, note }),
    }),
  costAnomalyContributors: (
    anomaly: Pick<CostAnomaly, "runId" | "costType" | "scopeType" | "scopeId">,
  ) => {
    const params = new URLSearchParams({
      runId: anomaly.runId,
      costType: anomaly.costType,
      scopeType: anomaly.scopeType,
      scopeId: anomaly.scopeId,
    });
    return request<{ items: CostAnomalyContributor[] }>(
      `/cost/anomalies/contributors?${params.toString()}`,
    );
  },
  opportunityEvidenceUrl: (opportunityId: string) =>
    `${API_ROOT}/evidence/opportunity?opportunityId=${encodeURIComponent(opportunityId)}`,
  costAnomalyEvidenceUrl: (
    anomaly: Pick<CostAnomaly, "runId" | "costType" | "scopeType" | "scopeId">,
  ) => {
    const params = new URLSearchParams({
      runId: anomaly.runId,
      costType: anomaly.costType,
      scopeType: anomaly.scopeType,
      scopeId: anomaly.scopeId,
    });
    return `${API_ROOT}/evidence/cost-anomaly?${params.toString()}`;
  },
  costReport: (params = new URLSearchParams(), opts?: { signal?: AbortSignal }) =>
    request<CostReport>(`/reports/cost?${params.toString()}`, { signal: opts?.signal }),
  costReportExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/reports/cost/export?${params.toString()}`,
  workloadReport: (opts?: { signal?: AbortSignal }) => request<WorkloadReport>("/reports/workload", { signal: opts?.signal }),
  intelligenceStatus: () => request<IntelligenceStatus>("/intelligence/status"),
  intelligenceReview: (limit = 25) =>
    request<IntelligenceReview>(`/intelligence/review?limit=${limit}`),
  intelligenceChat: (
    messages: { role: "user" | "assistant"; content: string }[],
    context: {
      page: string;
      filters?: Record<string, string>;
      selectedResourceId?: string;
    },
    modelProfile: "fast" | "benchmark" = "fast",
  ) =>
    request<IntelligenceResponse>("/intelligence/chat", {
      method: "POST",
      body: JSON.stringify({ messages, context, modelProfile }),
    }),
  /** Streaming variant of intelligenceChat (#76): progress events arrive via
   *  onEvent as the analysis runs; resolves with the final response. Errors
   *  ride the stream as {event:"error"} frames and reject as ApiError. */
  intelligenceChatStream: (
    messages: { role: "user" | "assistant"; content: string }[],
    context: {
      page: string;
      filters?: Record<string, string>;
      selectedResourceId?: string;
    },
    modelProfile: "fast" | "benchmark",
    onEvent: (event: Record<string, unknown>) => void,
  ): Promise<IntelligenceResponse> =>
    trackBusy(async () => {
      const response = await fetch(`${API_ROOT}/intelligence/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages, context, modelProfile }),
      });
      if (!response.ok || !response.body) {
        const payload: unknown = await response.json().catch(() => null);
        throw new ApiError(
          formatApiError(payload, `Request failed (${response.status})`),
          response.status,
        );
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let result: IntelligenceResponse | null = null;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
          const data = frame
            .split("\n")
            .filter((line) => line.startsWith("data: "))
            .map((line) => line.slice(6))
            .join("");
          if (!data) continue;
          let event: Record<string, unknown>;
          try {
            event = JSON.parse(data) as Record<string, unknown>;
          } catch {
            continue;
          }
          if (event.event === "result") {
            result = event.data as IntelligenceResponse;
          } else if (event.event === "error") {
            throw new ApiError(
              String(event.detail || "Flux Intelligence failed."),
              Number(event.status) || 500,
            );
          } else {
            onEvent(event);
          }
        }
      }
      if (!result) {
        throw new ApiError("The analysis stream ended without a result.", 502);
      }
      return result;
    }),
  intelligenceConversation: (opts?: { signal?: AbortSignal }) =>
    request<{
      requestId?: string;
      occurredAt?: string;
      messages?: { role: "user" | "assistant"; content: string }[];
      response?: IntelligenceResponse;
    }>("/intelligence/conversation", { signal: opts?.signal }),
  intelligenceFeedback: (
    requestId: string,
    rating: "helpful" | "not_helpful",
    reason = "",
  ) =>
    request<void>("/intelligence/feedback", {
      method: "POST",
      body: JSON.stringify({ requestId, rating, reason }),
    }),
  intelligencePerformance: (
    requestId: string,
    clientRoundTripMs: number,
    clientRenderMs: number,
    clientEndToEndMs: number,
  ) =>
    request<void>("/intelligence/performance", {
      method: "POST",
      body: JSON.stringify({
        requestId,
        clientRoundTripMs,
        clientRenderMs,
        clientEndToEndMs,
      }),
    }),
  retirementReportExportUrl: () =>
    `${API_ROOT}/reports/workload/retirement/export`,
  governanceReport: (params = new URLSearchParams(), opts?: { signal?: AbortSignal }) =>
    request<GovernanceReport>(`/reports/governance?${params.toString()}`, { signal: opts?.signal }),
  tagHygieneReport: () => request<TagHygieneReport>("/reports/tag-hygiene"),
  allocationReport: () => request<AllocationReport>("/reports/allocation"),
  focusAnalyticsReport: () => request<FocusAnalyticsReport>("/reports/focus-analytics"),
  savingsReport: () => request<SavingsReport>("/reports/savings"),
  budgetReport: () => request<BudgetReport>("/reports/budgets"),
  commitments: () => request<CommitmentInventory>("/reports/commitments"),
  commitmentOptimizerStatus: () =>
    request<CommitmentOptimizerStatus>("/commitments/optimizer/status"),
  commitmentOptimizerRuns: (limit = 25) =>
    request<{ runs: CommitmentOptimizerRun[] }>(
      `/commitments/optimizer/runs?limit=${limit}`,
    ),
  startCommitmentOptimizerRun: (payload: {
    lookbackDays: number;
    riskProfile: "conservative" | "balanced" | "aggressive";
    term: "P1Y" | "P3Y";
  }) =>
    request<CommitmentOptimizerRunDetail>("/commitments/optimizer/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  commitmentOptimizerRun: (runId: string) =>
    request<CommitmentOptimizerRunDetail>(
      `/commitments/optimizer/runs/${encodeURIComponent(runId)}`,
    ),
  commitmentOptimizerHourly: (runId: string, limit = 2000) =>
    request<{ hours: CommitmentOptimizerHour[] }>(
      `/commitments/optimizer/runs/${encodeURIComponent(runId)}/hourly?limit=${limit}`,
    ),
  commitmentOptimizerManifestUrl: (runId: string, format: "json" | "csv") =>
    `${API_ROOT}/commitments/optimizer/runs/${encodeURIComponent(runId)}/manifest?format=${format}`,
  compareCommitmentOptimizerRuns: (base: string, compare: string) =>
    request<CommitmentRunComparison>(
      `/commitments/optimizer/runs/compare?base=${encodeURIComponent(base)}&compare=${encodeURIComponent(compare)}`,
    ),
  decideCommitmentRecommendation: (
    recommendationId: string,
    decision: "approved" | "rejected",
    note = "",
  ) =>
    request<{ recommendationId: string; decision: string; decisionBy: string }>(
      `/commitments/optimizer/recommendations/${encodeURIComponent(recommendationId)}/decision`,
      { method: "PUT", body: JSON.stringify({ decision, note }) },
    ),
  executiveExportUrl: () => `${API_ROOT}/reports/executive-summary/export`,
  budgetGroups: () => request<{ groups: BudgetGroup[] }>("/integrations/budget-groups"),
  saveBudgetGroups: (groups: {
    id?: string; name: string; annualAmount: number;
    currency?: string; subscriptionIds: string[]; virtualTagKey?: string; virtualTagValue?: string;
  }[]) =>
    request<{ groups: BudgetGroup[] }>("/integrations/budget-groups", {
      method: "PUT",
      body: JSON.stringify({ groups }),
    }),
  budgetTargets: () => request<{ targets: BudgetReport["targets"] }>("/integrations/budgets"),
  saveBudgetTargets: (targets: { scopeType: string; scopeId: string; monthlyAmount: number; currency: string }[]) =>
    request<{ targets: BudgetReport["targets"] }>("/integrations/budgets", {
      method: "PUT",
      body: JSON.stringify({ targets }),
    }),
  unitEconomicsReport: () => request<UnitEconomicsReport>("/reports/unit-economics"),
  executiveSummary: () => request<ExecutiveSummary>("/reports/executive-summary"),
  setOpportunityLifecycle: (payload: {
    opportunityId: string;
    status: "open" | "accepted" | "implemented" | "dismissed";
    note?: string;
    resourceId?: string;
    estimatedMonthlySavings?: number | null;
  }) =>
    request<{ opportunityId: string; status: string }>("/opportunities/lifecycle", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  allocationConfig: () => request<AllocationConfig>("/integrations/allocation"),
  saveAllocationConfig: (payload: { costCenterTags: string[]; sharedValues: string[]; unitTag?: string; unitLabel?: string }) =>
    request<AllocationConfig>("/integrations/allocation", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  rightsizingBoards: () =>
    request<{ boards: RightsizingBoard[] }>("/rightsizing/boards"),
  createRightsizingBoard: (name: string, description = "") =>
    request<RightsizingBoard>("/rightsizing/boards", {
      method: "POST",
      body: JSON.stringify({ name, description }),
    }),
  renameRightsizingBoard: (boardId: string, name: string, description = "") =>
    request<{ id: string; name: string; description: string }>(
      `/rightsizing/boards/${encodeURIComponent(boardId)}`,
      { method: "PUT", body: JSON.stringify({ name, description }) },
    ),
  setPrimaryRightsizingBoard: (boardId: string) =>
    request<{ id: string; isPrimary: boolean }>(
      `/rightsizing/boards/${encodeURIComponent(boardId)}/primary`,
      { method: "POST" },
    ),
  deleteRightsizingBoard: (boardId: string) =>
    request<{ removed: string; bucketsRemoved: number; assignmentsRemoved: number }>(
      `/rightsizing/boards/${encodeURIComponent(boardId)}`,
      { method: "DELETE" },
    ),
  duplicateRightsizingBoard: (boardId: string, name: string) =>
    request<{ id: string; name: string }>(
      `/rightsizing/boards/${encodeURIComponent(boardId)}/duplicate`,
      { method: "POST", body: JSON.stringify({ name, description: "" }) },
    ),
  rightsizingProposalStatus: () =>
    request<import("./types").RightsizingProposalStatus>(
      "/rightsizing/proposal/status",
    ),
  refreshRightsizingProposal: () =>
    request<import("./types").RightsizingProposalRefresh>(
      "/rightsizing/proposal/refresh",
      { method: "POST" },
    ),
  rightsizingPlan: (boardId = "") =>
    request<RightsizingPlanBoard>(
      `/rightsizing/plan${boardId ? `?boardId=${encodeURIComponent(boardId)}` : ""}`,
    ),
  rightsizingPlanLog: (boardId = "", limit = 250) =>
    request<{ entries: PlanLogEntry[] }>(
      `/rightsizing/plan/log?limit=${limit}${boardId ? `&boardId=${encodeURIComponent(boardId)}` : ""}`,
    ),
  saveRightsizingBucket: (payload: {
    boardId?: string; region: string; sku: string; strategy?: string;
    refQuantity?: number | null; refMonthlyPayg?: number | null;
    refMonthlyRi1y?: number | null; refRi1yUpfront?: number | null;
    refMonthlySp1y?: number | null; refMonthlySavings?: number | null;
    refReservationCheck?: string; note?: string;
  }) =>
    request<{ bucketKey: string; boardId: string }>("/rightsizing/plan/bucket", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  deleteRightsizingBucket: (key: string) =>
    request<{ removed: string; movedToUnassigned: number }>(
      `/rightsizing/plan/bucket?key=${encodeURIComponent(key)}`,
      { method: "DELETE" },
    ),
  moveRightsizingVms: (
    boardId: string,
    moves: {
      vmKey: string; vmName?: string; subscriptionName?: string;
      bucketKey: string; decision?: string | null; note?: string | null;
    }[],
  ) =>
    request<{ moved: number; boardId: string }>("/rightsizing/plan/assignments", {
      method: "PUT",
      body: JSON.stringify({ boardId, moves }),
    }),
  importRightsizingPlan: (
    payload: unknown,
    options: { boardId?: string; newBoardName?: string; dryRun?: boolean } = {},
  ) =>
    request<RightsizingImportReport | RightsizingImportPreview>(
      "/rightsizing/plan/import",
      {
        method: "POST",
        body: JSON.stringify({
          ...(payload as Record<string, unknown>),
          boardId: options.boardId ?? "",
          newBoardName: options.newBoardName ?? "",
          dryRun: options.dryRun ?? false,
        }),
      },
    ),
  fiscalOutlook: () => request<FiscalOutlook>("/reports/fiscal-outlook"),
  geographyReport: (opts?: { signal?: AbortSignal }) => request<GeographyReport>("/reports/geography", { signal: opts?.signal }),
  saveFiscalOutlookConfig: (payload: {
    fyStartMonth: number;
    costType: string;
    growthPercentMonthly: number;
    includePlannedSavings: boolean;
    savingsRampMonths: number;
    notes: string;
  }) =>
    request<FiscalOutlook>("/reports/fiscal-outlook/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  savePlanningAssumptions: (assumptions: {
    id?: string;
    label: string;
    monthlyAmount: number;
    direction: "saving" | "cost";
    includeInReports: boolean;
    enabled: boolean;
    notes: string;
  }[]) =>
    request<FiscalOutlook>("/reports/fiscal-outlook/assumptions", {
      method: "PUT",
      body: JSON.stringify({ assumptions }),
    }),
  semanticCatalog: (opts?: { signal?: AbortSignal }) =>
    request<SemanticCatalog>("/semantic", { signal: opts?.signal }),
  semanticQuery: (payload: SemanticQueryRequest, opts?: { signal?: AbortSignal }) =>
    request<SemanticQueryResult>("/semantic/query", {
      method: "POST",
      body: JSON.stringify(payload),
      signal: opts?.signal,
    }),
  semanticSql: (sql: string, opts?: { signal?: AbortSignal }) =>
    request<SemanticSqlResult>("/semantic/sql", {
      method: "POST",
      body: JSON.stringify({ sql }),
      signal: opts?.signal,
    }),
  databaseHealth: () => request<DatabaseHealth>("/admin/database-health"),
  adminJobs: () => request<{ jobs: AdminJob[]; activeSync: unknown }>("/admin/jobs"),
  runAdminJob: (source: string) =>
    request<{ accepted: boolean; syncId: string; source: string }>("/admin/jobs/run", {
      method: "POST",
      body: JSON.stringify({ source }),
    }),
  retentionPolicies: () => request<{ policies: RetentionPolicy[] }>("/admin/retention"),
  configurationAudit: () => request<{ entries: AuditEntry[] }>("/admin/audit"),
  aiIntelligenceConfig: () => request<AiIntelligenceConfig>("/admin/ai-config"),
  saveAiIntelligenceConfig: (payload: { provider: "deepseek" | "openrouter" | "foundry"; fastModel?: string; deepModel?: string }) =>
    request<AiIntelligenceConfig>("/admin/ai-config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  semanticExpert: (question: string, history: { question: string; sql: string }[]) =>
    request<ExpertExplorerResult>("/semantic/expert", {
      method: "POST",
      body: JSON.stringify({ question, history }),
    }),
  effectiveVirtualTags: (resourceId: string, opts?: { signal?: AbortSignal }) =>
    request<{ resourceId: string; tags: Record<string, { value: string; source: string; ruleName?: string }> }>(
      `/virtual-tags/effective?resourceId=${encodeURIComponent(resourceId)}`,
      { signal: opts?.signal },
    ),
  virtualTagDimensions: (opts?: { signal?: AbortSignal }) =>
    request<{ dimensions: VirtualTagDimension[] }>("/virtual-tags/dimensions", { signal: opts?.signal }),
  saveVirtualTagDimension: (payload: Partial<VirtualTagDimension> & { key: string; name: string }) =>
    request<{ key: string; version: number; status: string }>("/virtual-tags/dimensions", {
      method: "POST", body: JSON.stringify(payload),
    }),
  deleteVirtualTagDimension: (key: string) =>
    request(`/virtual-tags/dimensions/${encodeURIComponent(key)}`, { method: "DELETE" }),
  virtualTagRules: () => request<{ rules: VirtualTagRule[] }>("/virtual-tags/rules"),
  saveVirtualTagRule: (payload: Partial<VirtualTagRule>) =>
    request<{ ruleId: string; version: number; action: string }>("/virtual-tags/rules", {
      method: "POST", body: JSON.stringify(payload),
    }),
  previewVirtualTagRule: (payload: Partial<VirtualTagRule>) =>
    request<VirtualTagPreview>("/virtual-tags/preview", {
      method: "POST", body: JSON.stringify(payload),
    }),
  setVirtualTagRuleStatus: (ruleId: string, status: "active" | "inactive") =>
    request(`/virtual-tags/rules/${encodeURIComponent(ruleId)}/status`, {
      method: "POST", body: JSON.stringify({ status }),
    }),
  deleteVirtualTagRule: (ruleId: string) =>
    request(`/virtual-tags/rules/${encodeURIComponent(ruleId)}`, { method: "DELETE" }),
  virtualTagReport: (params = new URLSearchParams(), opts?: { signal?: AbortSignal }) =>
    request<VirtualTagReport>(`/reports/virtual-tags?${params.toString()}`, { signal: opts?.signal }),
  virtualTagReportExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/reports/virtual-tags/export?${params.toString()}`,
  resourceTelemetry: (resourceId: string) =>
    request<ResourceTelemetry>(`/telemetry/resource?resourceId=${encodeURIComponent(resourceId)}`),
  telemetryStatus: () => request<TelemetryStatus>("/telemetry/status"),
  finopsToolkitStatus: () =>
    request<FinOpsToolkitStatus>("/integrations/finops-toolkit"),
  costHistoryStatus: (opts?: { signal?: AbortSignal }) =>
    request<CostHistoryStatus>("/integrations/cost-history", { signal: opts?.signal }),
  costCoverage: () =>
    request<CostCoverage>("/integrations/cost-coverage"),
  telemetryCoverage: () =>
    request<TelemetryCoverage>("/integrations/telemetry-coverage"),
  fleetTelemetry: (params = new URLSearchParams()) =>
    request<FleetTelemetry>(`/telemetry/fleet?${params.toString()}`),
  costReconciliation: () =>
    request<Overview["costDataStatus"]>("/integrations/cost-reconciliation"),
  operationalHealth: () =>
    request<OperationalHealth>("/operations/health"),
  sloReport: () => request<SloReport>("/operations/slo"),
  recommendationQuality: () =>
    request<RecommendationQuality>("/recommendations/quality"),
  rightsizingRecommendations: (params = new URLSearchParams()) =>
    request<RightsizingRecommendations>(`/recommendations/rightsizing?${params.toString()}`),
  rightsizingExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/recommendations/rightsizing/export?${params.toString()}`,
  opportunities: (params = new URLSearchParams(), opts?: { signal?: AbortSignal }) =>
    request<Opportunities>(`/opportunities?${params.toString()}`, { signal: opts?.signal }),
  opportunitiesExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/opportunities/export?${params.toString()}`,
  azureIntegration: () => request<AzureIntegration>("/integrations/azure"),
  saveAzureIntegration: (value: AzureIntegration) =>
    request<AzureIntegration>("/integrations/azure", {
      method: "PUT",
      body: JSON.stringify(value),
    }),
  syncAzure: () =>
    request<{ accepted: boolean; syncId: string }>("/integrations/azure/sync", {
      method: "POST",
    }),
  seedDemo: () => request<void>("/dev/seed", { method: "POST" }),
  /** Client-side error telemetry; fire-and-forget so the catch never hides. */
  postClientError: (area: string, error: unknown) =>
    request<void>("/client-error", {
      method: "POST",
      body: JSON.stringify({
        area,
        message: (error instanceof Error ? error.message : String(error ?? "Unknown error")).slice(0, 2000),
        stack: (error instanceof Error ? String(error.stack ?? "") : "").slice(0, 8000),
        componentStack: "",
        url: typeof window !== "undefined" ? window.location.href.slice(0, 500) : "",
      }),
    }).catch(() => undefined),
};
