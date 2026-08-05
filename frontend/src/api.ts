import type { AdminJob, ExpertExplorerResult, AiIntelligenceConfig, BudgetGroup, CommitmentInventory, CostCoverage, FiscalOutlook, PlanLogEntry, SloReport, TelemetryCoverage, RightsizingBoard, RightsizingImportPreview, RightsizingImportReport, RightsizingPlanBoard, SemanticCatalog, SemanticQueryRequest, SemanticQueryResult, AllocationConfig, AuditEntry, DatabaseHealth, RetentionPolicy, AllocationReport, BudgetReport, ExecutiveSummary, FocusAnalyticsReport, SavingsReport, UnitEconomicsReport, AzureIntegration, ChangeAnomalies, CostAnomalies, CostAnomaly, CostAnomalyContributor, CostHistoryStatus, CostReport, FinOpsToolkitStatus, GovernanceReport, IntelligenceResponse, IntelligenceReview, IntelligenceStatus, Inventory, InventoryChanges, OperationalHealth, Opportunities, Overview, RecommendationQuality, ResourceTelemetry, RightsizingRecommendations, Session, TagHygieneReport, TelemetryStatus, VirtualTagDimension, VirtualTagPreview, VirtualTagReport, VirtualTagRule, WorkloadReport } from "./types";

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

async function performRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
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
  session: () => request<Session>("/session"),
  overview: () => request<Overview>("/overview"),
  inventory: (params = new URLSearchParams()) =>
    request<Inventory>(`/inventory?${params.toString()}`),
  inventoryExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/inventory/export?${params.toString()}`,
  changes: (params = new URLSearchParams()) =>
    request<InventoryChanges>(`/changes?${params.toString()}`),
  changeAnomalies: () => request<ChangeAnomalies>("/changes/anomalies"),
  costAnomalies: (params = new URLSearchParams()) =>
    request<CostAnomalies>(`/cost/anomalies?${params.toString()}`),
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
  costReport: (params = new URLSearchParams()) =>
    request<CostReport>(`/reports/cost?${params.toString()}`),
  costReportExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/reports/cost/export?${params.toString()}`,
  workloadReport: () => request<WorkloadReport>("/reports/workload"),
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
  governanceReport: (params = new URLSearchParams()) =>
    request<GovernanceReport>(`/reports/governance?${params.toString()}`),
  tagHygieneReport: () => request<TagHygieneReport>("/reports/tag-hygiene"),
  allocationReport: () => request<AllocationReport>("/reports/allocation"),
  focusAnalyticsReport: () => request<FocusAnalyticsReport>("/reports/focus-analytics"),
  savingsReport: () => request<SavingsReport>("/reports/savings"),
  budgetReport: () => request<BudgetReport>("/reports/budgets"),
  commitments: () => request<CommitmentInventory>("/reports/commitments"),
  executiveExportUrl: () => `${API_ROOT}/reports/executive-summary/export`,
  budgetGroups: () => request<{ groups: BudgetGroup[] }>("/integrations/budget-groups"),
  saveBudgetGroups: (groups: {
    id?: string; name: string; annualAmount: number;
    currency?: string; subscriptionIds: string[];
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
  semanticCatalog: () => request<SemanticCatalog>("/semantic"),
  semanticQuery: (payload: SemanticQueryRequest) =>
    request<SemanticQueryResult>("/semantic/query", {
      method: "POST",
      body: JSON.stringify(payload),
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
  effectiveVirtualTags: (resourceId: string) =>
    request<{ resourceId: string; tags: Record<string, { value: string; source: string; ruleName?: string }> }>(
      `/virtual-tags/effective?resourceId=${encodeURIComponent(resourceId)}`,
    ),
  virtualTagDimensions: () =>
    request<{ dimensions: VirtualTagDimension[] }>("/virtual-tags/dimensions"),
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
  virtualTagReport: (params = new URLSearchParams()) =>
    request<VirtualTagReport>(`/reports/virtual-tags?${params.toString()}`),
  virtualTagReportExportUrl: (params = new URLSearchParams()) =>
    `${API_ROOT}/reports/virtual-tags/export?${params.toString()}`,
  resourceTelemetry: (resourceId: string) =>
    request<ResourceTelemetry>(`/telemetry/resource?resourceId=${encodeURIComponent(resourceId)}`),
  telemetryStatus: () => request<TelemetryStatus>("/telemetry/status"),
  finopsToolkitStatus: () =>
    request<FinOpsToolkitStatus>("/integrations/finops-toolkit"),
  costHistoryStatus: () =>
    request<CostHistoryStatus>("/integrations/cost-history"),
  costCoverage: () =>
    request<CostCoverage>("/integrations/cost-coverage"),
  telemetryCoverage: () =>
    request<TelemetryCoverage>("/integrations/telemetry-coverage"),
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
  opportunities: (params = new URLSearchParams()) =>
    request<Opportunities>(`/opportunities?${params.toString()}`),
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
};
