export type Page =
  | "overview"
  | "inventory"
  | "changes"
  | "analytics"
  | "cost-anomalies"
  | "reports"
  | "opportunities"
  | "rightsizing"
  | "intelligence"
  | "integrations";

export type PlanVm = {
  vmKey: string;
  name: string;
  subscriptionName: string;
  resourceGroup: string;
  region: string;
  sku: string;
  estimatedMonthlyCost: number | null;
  action: string;
  targetSku: string;
  cpuP95: number | null;
  coveragePercent: number | null;
  windowDays: number | null;
  estimatedMonthlySaving: number | null;
  reason: string;
  telemetrySource: string;
  noData: boolean;
};

export type PlanBucket = {
  bucketKey: string;
  boardId: string;
  region: string;
  sku: string;
  strategy: string;
  source: string;
  refQuantity: number | null;
  refMonthlyPayg: number | null;
  refMonthlyRi1y: number | null;
  refRi1yUpfront: number | null;
  refMonthlySp1y: number | null;
  refMonthlySavings: number | null;
  refReservationCheck: string;
  note: string;
  createdBy: string;
  createdAt: string | null;
  updatedAt: string | null;
};

export type RightsizingBoard = {
  id: string;
  name: string;
  description: string;
  isPrimary: boolean;
  createdBy: string;
  createdAt: string | null;
  updatedAt: string | null;
  bucketCount: number;
  assignedCount: number;
};

export type RightsizingProposalStatus = {
  board: RightsizingBoard | null;
  lastRefreshedAt: string | null;
  nextRefreshAt: string;
  due: boolean;
  cadenceDays: number;
};

export type RightsizingProposalRefresh = {
  status: "current" | "refreshed";
  boardId?: string;
  lastRefreshedAt: string | null;
  nextRefreshAt: string;
  cadenceDays: number;
  bucketCount?: number;
  placed?: number;
  review?: number;
  savingsPlan?: number;
  noData?: number;
  provisional?: number;
  waste?: number;
  modeledReservationBuckets?: number;
  modeledSavingsPlanCandidates?: number;
  modeledMonthlySavings?: number;
};

export type PlanAssignment = {
  bucketKey: string;
  decision: string;
  note: string;
  refMonthlyPayg: number | null;
  refMonthlyCommitment: number | null;
  refMonthlySavings: number | null;
  economicsStatus: string;
  updatedBy: string;
  updatedAt: string | null;
};

export type PlanLogEntry = {
  ts: string | null;
  actor: string;
  vmKey: string;
  vmName: string;
  fromLabel: string;
  toLabel: string;
  decision: string;
  note: string;
};

export type RightsizingPlanBoard = {
  boardId: string;
  boardName: string;
  boardDescription: string;
  vms: PlanVm[];
  buckets: PlanBucket[];
  assignments: Record<string, PlanAssignment>;
  importedUnmatched: (PlanAssignment & { vmKey: string; vmName: string })[];
  summary: {
    totalVms: number;
    assigned: number;
    noData: number;
    bucketCount: number;
    plannedMonthlySavings: number;
    modeledReservationBuckets: number;
    savingsPlanCandidates: number;
    modeledSavingsPlanCandidates: number;
  };
};

export type SemanticModelInfo = {
  name: string;
  displayName: string;
  description: string;
  grain: string;
  timeColumn: string | null;
  completenessLagDays: number;
  defaultFilters: Record<string, string[]>;
  available: boolean;
  dimensions: { name: string; description: string }[];
  measures: {
    name: string;
    description: string;
    format: "number" | "currency" | "percent";
    higherIs: "good" | "bad" | "neutral";
  }[];
};

export type SemanticCatalog = {
  contract: string;
  models: SemanticModelInfo[];
};

export type SemanticQueryRequest = {
  model: string;
  measures: string[];
  dimensions?: string[];
  filters?: Record<string, string[]>;
  grain?: "day" | "week" | "month" | null;
  start?: string | null;
  end?: string | null;
  limit?: number;
};

export type SemanticQueryResult = {
  columns: { name: string; kind: "time" | "dimension" | "measure"; format: string }[];
  rows: (string | number | null)[][];
  sql: string;
  rowCount: number;
  appliedDefaults?: Record<string, string[]>;
};

export type RightsizingImportReport = {
  dryRun: false;
  boardId: string;
  bucketsImported: number;
  bucketsSkipped: number;
  assignmentsImported: number;
  matched: number;
  unmatched: number;
  logImported: number;
  unmatchedSamples: string[];
  inventorySample: string[];
  inventoryVmCount: number;
};

export type RightsizingBucketFieldDiff = {
  field: string;
  before: string | number | null;
  after: string | number | null;
};

export type RightsizingBucketDiffEntry = {
  label: string;
  region: string;
  sku: string;
  fields?: RightsizingBucketFieldDiff[];
};

export type RightsizingAssignmentDiffSide = {
  bucketKey: string;
  bucketLabel: string;
  decision: string;
  note: string;
};

export type RightsizingAssignmentAdded = {
  vmKey: string;
  vmName: string;
  bucketKey: string;
  bucketLabel: string;
  decision: string;
  note: string;
  resolved: boolean;
};

export type RightsizingAssignmentChanged = {
  vmKey: string;
  vmName: string;
  before: RightsizingAssignmentDiffSide;
  after: RightsizingAssignmentDiffSide;
};

export type RightsizingImportPreview = {
  dryRun: true;
  boardId: string | null;
  newBoardName: string | null;
  buckets: {
    added: RightsizingBucketDiffEntry[];
    changed: RightsizingBucketDiffEntry[];
    unchanged: number;
    skipped: number;
  };
  assignments: {
    added: RightsizingAssignmentAdded[];
    changed: RightsizingAssignmentChanged[];
    unchanged: number;
  };
  logEntriesReplaced: number;
  logEntriesIncoming: number;
  matched: number;
  unmatched: number;
  unmatchedSamples: string[];
  inventorySample: string[];
  inventoryVmCount: number;
};

export type IntelligenceStatus = {
  enabled: boolean;
  configured: boolean;
  authorizationRole: string;
  dataBoundary: string;
  conversationRetention: string;
  transcriptRetentionDays: number;
  usageRetentionDays: number;
  budgetUsd: number;
  stopAtUsd: number;
  remainingBeforeStopUsd: number;
  limitations: string[];
  usage: {
    requestCount: number;
    estimatedCostUsd: number;
    promptTokens: number;
    completionTokens: number;
    lastRequestAt: string | null;
    averageLatencyMs: number;
    p95LatencyMs: number;
    successfulRequestCount: number;
    failedRequestCount: number;
    averageClientEndToEndMs: number;
    p95ClientEndToEndMs: number;
    averageModelLatencyMs: number;
    averageGovernedToolLatencyMs: number;
    averageDatabaseLatencyMs: number;
    averageTransportIngressMs: number;
    averageRenderMs: number;
    retentionDays: number;
  };
  quality: IntelligenceQualityStatus;
};

export type IntelligenceQualityStatus = {
  status: "healthy" | "degraded" | "warming_up";
  retentionDays: number;
  slowRequestThresholdMs: number;
  requestCount: number;
  slowRequestCount: number;
  flaggedForReviewCount: number;
  structuredContractFailureCount: number;
  assessedCount: number;
  averageScore: number | null;
  regressionFailureCount: number;
  qualityFlags: { flag: string; count: number }[];
  helpfulCount: number;
  notHelpfulCount: number;
  unratedCount: number;
  helpfulPercent: number | null;
  responseModes: { mode: string; count: number }[];
  bottlenecks: { stage: string; count: number }[];
  openItem: string;
};

export type IntelligenceChartBlock = {
  type: "chart";
  title: string;
  chartType: "bar" | "line" | "area";
  xKey: string;
  yKeys: string[];
  data: Record<string, string | number>[];
};

export type IntelligenceResponse = {
  requestId: string;
  toolsUsed: string[];
  performance: {
    durationMs: number;
    modelMs: number;
    governedToolMs: number;
    databaseMs: number;
    validationMs: number;
    applicationMs: number;
    promptTokens: number;
    completionTokens: number;
    toolCallCount: number;
    toolCacheHits: number;
    modelCallCount: number;
    toolDurations: {
      name: string;
      durationMs: number;
      dataPath: string;
      cacheHit?: boolean;
    }[];
    clientRoundTripMs?: number;
    clientRenderMs?: number;
    clientEndToEndMs?: number;
    transportAndIngressMs?: number;
    responseMode?: string;
    rillInPath: false;
  };
  summary: string;
  blocks: (
    | { type: "markdown"; content: string }
    | { type: "mermaid"; title: string; content: string }
    | IntelligenceChartBlock
  )[];
  facts: string[];
  interpretations: string[];
  limitations: string[];
  sources: { tool: string; description: string }[];
  followUps: string[];
  actions?: { label: string; href: string; description: string }[];
  quality?: {
    score: number;
    status: "pass" | "review";
    flags: string[];
    coverageGaps: string[];
    checks: Record<string, boolean>;
  };
};

export type IntelligenceConversationEntry = {
  id: string;
  role: "user" | "assistant";
  content: string;
  response?: IntelligenceResponse;
};

export type IntelligenceReview = {
  retentionDays: number;
  items: {
    requestId: string;
    occurredAt: string | null;
    userHashPrefix: string;
    status: string;
    performance: {
      serverMs: number;
      clientEndToEndMs: number | null;
      modelMs: number;
      governedToolMs: number;
      databaseMs: number;
      validationMs: number;
      applicationMs: number;
      transportAndIngressMs: number | null;
      renderMs: number | null;
      toolDurations: {
        name: string;
        durationMs: number;
        dataPath: string;
        cacheHit?: boolean;
      }[];
      rillInPath: false;
    };
    messages: { role: string; content: string }[];
    context: Record<string, unknown>;
    response: IntelligenceResponse | null;
    rawResponse: string;
    feedback: string;
  }[];
};

export type Session = {
  authenticated: boolean;
  authMode: string;
  user: {
    id: string;
    displayName: string;
    email: string;
    tenantId: string;
    roles: string[];
    claimsSource: string;
  } | null;
  permissions: {
    canRead: boolean;
    canManageIntegrations: boolean;
    canSyncIntegrations: boolean;
  };
  authActions: {
    loginPath: string;
    logoutPath: string;
  };
  dataCurrency?: {
    mode: string;
    snapshotVersion?: number | null;
    latestVersion?: number | null;
    generatedAt?: string | null;
  };
};

export type ChartDatum = {
  name: string;
  value: number;
};

export type CostBySubscriptionDatum = {
  name: string;
  actual: number;
  amortized: number;
  subscriptionId: string;
};

export type CostDatasetStatus = {
  source:
    | "ActualCost"
    | "AmortizedCost"
    | "DailyActualCost"
    | "DailyAmortizedCost"
    | "CommitmentCoverage"
    | "FocusCost";
  label: string;
  grain: string;
  configuredScopes: number;
  availableScopes: number;
  currentPeriodScopes: number;
  failedScopes: number;
  retainedScopes: number;
  complete: boolean;
  currentPeriodComplete: boolean;
  rowCount: number;
  amount: number;
  currency: string;
  periodStart: string | null;
  periodEnd: string | null;
  lastSuccessfulAt: string | null;
  scopes: {
    subscriptionId: string;
    subscriptionName: string;
    status: string;
    available: boolean;
    currentPeriod: boolean;
    retainedLastGood: boolean;
    rowCount: number;
    amount: number | null;
    currency: string;
    periodStart: string | null;
    periodEnd: string | null;
    lastSuccessfulAt: string | null;
    lastAttemptAt: string | null;
    statusCode: number | null;
    retryAfterSeconds: number | null;
    nextRetryAt: string | null;
    message: string;
  }[];
};

export type Overview = {
  summary: {
    resourceCount: number;
    subscriptionCount: number;
    regionCount: number;
    tagCoveragePercent: number;
    costCoverageCount: number;
    estimatedMonthlyCost: number;
    utilizationCoverageCount: number;
    averageUtilizationPercent: number | null;
    opportunityCount: number;
    estimatedMonthlySavings: number;
    valuedOpportunityCount: number;
    monthlyGrossSavings: number;
    monthlyRiskAdjustedSavings: number;
    skuValuedOpportunityCount: number;
    costAnomalyCount: number;
    costAnomalyIncrease: number;
    costAnomalyCurrency: string;
    costAnomalyEvaluationDate: string | null;
  };
  periodComparison: {
    mtdActual: number;
    priorMtdActual: number;
    deltaPercent: number | null;
    mtdStart: string;
    mtdEnd: string;
    priorStart: string;
    priorEnd: string;
  } | null;
  resourcesByType: ChartDatum[];
  resourcesByRegion: ChartDatum[];
  costByType: ChartDatum[];
  costBySubscription: CostBySubscriptionDatum[];
  commitmentCoverage: {
    status: "ready" | "not_connected" | "reference_data_unavailable";
    eligibleCost: number;
    coveredCost: number;
    eligibleOnDemandCost: number;
    coveragePercent: number | null;
    unknownEligibilityCost: number;
    currency: string;
    periodStart: string | null;
    periodEnd: string | null;
    method: string;
  };
  commitmentCostMix: ChartDatum[];
  utilizationDistribution: ChartDatum[];
  telemetryCoverage: ChartDatum[];
  opportunitiesBySource: ChartDatum[];
  opportunitiesByKind: ChartDatum[];
  inventoryHistory: { date: string; value: number }[];
  dailyCostTrend: { date: string; amount: number }[];
  latestSync: SyncRun | null;
  sourceFreshness: SourceFreshness[];
  costDataStatus: {
    asOf: string;
    configuredSubscriptions: number;
    datasets: CostDatasetStatus[];
    source: string;
    warning: string;
  };
};

export type CostAnomaly = {
  runId: string;
  evaluatedAt: string;
  evaluationDate: string;
  costType: "ActualCost" | "AmortizedCost";
  scopeType: "subscription" | "service" | "resource";
  scopeId: string;
  subscriptionId: string;
  resourceId: string;
  resourceName: string;
  resourceType: string;
  resourceGroup: string;
  serviceName: string;
  currentAmount: number;
  baselinePoints: number;
  baselineMedian: number | null;
  mad: number | null;
  kScore: number | null;
  previousWeekAmount: number | null;
  absoluteChange: number | null;
  percentChange: number | null;
  status: "anomalous" | "warming_up";
  severity: "high" | "medium" | "none";
  currency: string;
  reason: string;
  methodVersion: string;
  reviewStatus: "new" | "investigating" | "acknowledged" | "resolved";
  reviewNote: string;
  reviewedBy: string;
  reviewedAt: string | null;
  recentDailyAmounts: number[];
};

export type CostAnomalyContributor = {
  type: "service" | "resource";
  id: string;
  name: string;
  current: number;
  previous: number;
  change: number;
  currency: string;
};

export type CostAnomalies = {
  items: CostAnomaly[];
  total: number;
  limit: number;
  offset: number;
  facets: {
    costTypes: string[];
    scopeTypes: string[];
    subscriptions: { id: string; name: string }[];
    services: string[];
    severities: string[];
  };
  summary: {
    anomalyCount: number;
    warmingCount: number;
    totalIncrease: number;
    currency: string;
    evaluatedCount: number;
    evaluationDate: string | null;
    evaluatedAt: string | null;
    methodVersion: string;
    message: string;
  };
  trend: { date: string; amount: number }[];
  trendLatencyDays?: number;
};

export type FiscalOutlookMonth = {
  month: string;
  status: "actual" | "inProgress" | "projected";
  amount: number;
  lower: number;
  upper: number;
  seasonalBasis: boolean;
};

export type CommitmentReservation = {
  reservationId: string;
  name: string;
  sku: string;
  resourceType: string;
  region: string;
  quantity: number;
  term: string;
  scopeType: string;
  expiryDate: string | null;
  daysToExpiry: number | null;
  utilization1d: number | null;
  utilization7d: number | null;
  utilization30d: number | null;
};

export type CommitmentInventory = {
  asOf: string;
  summary: {
    activeCount: number;
    totalQuantity: number;
    historicalCount: number;
    expiringWithin120Days: number;
    expiringWithin30Days: number;
    averageUtilization30d: number | null;
  };
  reservations: CommitmentReservation[];
};

export type BudgetGroup = {
  id: string;
  name: string;
  annualAmount: number;
  currency: string;
  subscriptionIds: string[];
  updatedBy?: string;
  updatedAt?: string | null;
};

export type FiscalOutlookGroup = {
  id: string;
  name: string;
  currency: string;
  annualBudget: number;
  actualToDate: number;
  fyTotal: number;
  fyLower: number;
  fyUpper: number;
  variance: number;
  status: string;
  historyMonths: number;
  memberCount: number;
  coveredMembers: number;
};

export type FiscalOutlookConfig = {
  fyStartMonth: number;
  costType: string;
  growthPercentMonthly: number;
  includePlannedSavings: boolean;
  savingsRampMonths: number;
  notes: string;
  updatedBy: string;
  updatedAt: string | null;
};

export type FiscalOutlook = {
  status: string;
  fiscalYear: string;
  fyStartMonth: number;
  fyStart: string;
  fyEnd: string;
  months: FiscalOutlookMonth[];
  actualToDate: number;
  projectedRemainder: { amount: number; lower: number; upper: number };
  fyTotal: number;
  fyLower: number;
  fyUpper: number;
  historyMonths: number;
  yoyFactor: number | null;
  backtestMape: number | null;
  assumptions: {
    growthPercentMonthly: number;
    plannedSavingsMonthly: number;
    savingsRampMonths: number;
  };
  methodVersion: string;
  seasonalComparison?: {
    methodVersion: string;
    fyTotal: number;
    fyLower: number;
    fyUpper: number;
    backtestMape: number | null;
    yoyFactor: number | null;
    reason: string;
  };
  reason: string;
  costType: string;
  currency: string;
  budgetMonthly: number | null;
  fyBudget: number | null;
  fyVarianceVsBudget: number | null;
  plannedSavingsMonthly: number;
  subscriptionCoverage: {
    covered: number;
    configured: number;
    details?: {
      subscriptionId: string;
      label: string;
      status: string;
      historyMonths: number;
      firstMonth: string | null;
      lastMonth: string | null;
      lastIngestionStatus: string | null;
      lastIngestionRowCount: number;
      lastIngestionStatusCode: number | null;
      lastIngestionMessage: string;
      lastIngestionCompletedAt: string | null;
    }[];
    uncovered?: {
      subscriptionId: string;
      label: string;
      status: string;
      historyMonths: number;
      firstMonth: string | null;
      lastMonth: string | null;
      lastIngestionStatus: string | null;
      lastIngestionRowCount: number;
      lastIngestionStatusCode: number | null;
      lastIngestionMessage: string;
      lastIngestionCompletedAt: string | null;
    }[];
  };
  groups: FiscalOutlookGroup[];
  config: FiscalOutlookConfig;
  limitations: string[];
};

export type CostReport = {
  period: {
    availableStart: string | null;
    availableEnd: string | null;
    start: string | null;
    end: string | null;
    previousStart: string | null;
    previousEnd: string | null;
  };
  summary: {
    costType: string;
    currency: string;
    currencyCount: number;
    totalCost: number;
    previousCost: number;
    changeAmount: number;
    changePercent: number | null;
    averageDailyCost: number | null;
    resourceCount: number;
    taggedResourceCount: number;
    untaggedCost: number;
  };
  costTypeComparison: {
    ActualCost?: number;
    AmortizedCost?: number;
  };
  topMovers: {
    subscriptions: CostMover[];
    services: CostMover[];
    resources: CostMover[];
  };
  daily: { date: string; amount: number; cumulative: number }[];
  bySubscription: { id: string; name: string; value: number }[];
  byService: ChartDatum[];
  byResourceGroup: ChartDatum[];
  byRegion: ChartDatum[];
  inventory: {
    resourceType: string;
    resourceCount: number;
    cost: number;
    costPerResource: number | null;
  }[];
  resources: {
    resourceId: string;
    resourceName: string;
    resourceType: string;
    resourceGroup: string;
    region: string;
    subscriptionId: string;
    cost: number;
  }[];
  forecast: {
    status: "ready" | "warming_up" | "not_connected";
    historyDays: number;
    points: {
      date: string;
      amount: number;
      lower: number;
      upper: number;
      baselinePoints: number;
    }[];
    forecastTotal: number | null;
    lowerTotal: number | null;
    upperTotal: number | null;
    backtestMape: number | null;
    backtestPoints: number;
    latencyDays: number;
    dataThrough: string | null;
    monthly: {
      month: string;
      amount: number;
      lower: number;
      upper: number;
    }[];
    trendFactor?: number;
    methodVersion: string;
    reason: string;
  };
  budgetVariance: {
    status: "blocked_missing_targets";
    variance: null;
    reason: string;
  };
  dataCoverage: {
    configuredScopes: number;
    availableScopes: number;
    completeScopes: number;
    complete: boolean;
    scopes: {
      id: string;
      name: string;
      available: boolean;
      complete: boolean;
      status: string;
      retainedLastGood: boolean;
      statusCode: number | null;
      message: string;
      availableStart: string | null;
      availableEnd: string | null;
      rowCount: number;
      observedAt: string | null;
    }[];
  };
  facets: {
    currencies: string[];
    subscriptions: { id: string; name: string }[];
    services: string[];
  };
  lineage: {
    source: string;
    grain: string;
    toolkitReference: string;
    limitations: string[];
  };
};

export type CostMover = {
  id: string;
  name: string;
  current: number;
  previous: number;
  change: number;
  changePercent: number | null;
};

export type WorkloadReport = {
  summary: Opportunities["summary"] & {
    telemetryReady: number;
    retirementCandidates: number;
    retirementRiskAdjustedValue: number;
    serviceRetirementCandidates: number;
    ownershipReady: number;
  };
  bySource: { name: string; count: number; riskAdjustedValue: number }[];
  byCategory: { name: string; count: number; riskAdjustedValue: number }[];
  byConfidence: { name: string; count: number; riskAdjustedValue: number }[];
  byAge: { name: string; count: number }[];
  coverageGaps: { status: string; count: number }[];
  savingsTrend: { date: string; currency: string; riskAdjustedValue: number }[];
  topOpportunities: Opportunity[];
  retirementCandidates: (Opportunity & {
    ownershipReady: boolean;
    ownershipTags: string[];
    costExposure: number | null;
    isServiceRetirement: boolean;
  })[];
  lineage: { sources: string[]; method: string };
};

export type SavingsReport = {
  summary: {
    acceptedCount: number;
    implementedCount: number;
    dismissedCount: number;
    estimatedAcceptedMonthly: number;
    estimatedImplementedMonthly: number;
    realizedMonthly: number;
    measuredCount: number;
  };
  items: {
    opportunityId: string;
    status: string;
    note: string;
    updatedBy: string;
    updatedAt: string | null;
    implementedAt: string | null;
    resourceId: string;
    estimatedMonthlySavings: number | null;
    baselineMonthlyCost: number | null;
    currentMonthlyCost: number | null;
    realizedMonthlySavings: number | null;
  }[];
};

export type FocusAnalyticsReport = {
  available: boolean;
  period?: { start: string | null; end: string | null; windowDays: number };
  currency?: string;
  commitment?: {
    committedEffectiveCost: number;
    onDemandEffectiveCost: number;
    coveragePercent: number | null;
    commitments: {
      id: string;
      name: string;
      type: string;
      usedCost: number;
      unusedCost: number;
      utilizationPercent: number | null;
    }[];
  };
  pricing?: {
    billedCost: number;
    effectiveCost: number;
    listCost: number;
    contractedCost: number;
    discountRealized: number;
    discountPercent: number | null;
    byService: {
      serviceName: string;
      effectiveCost: number;
      listCost: number;
      billedCost: number;
      discountPercent: number | null;
    }[];
    byPricingCategory: { name: string; value: number }[];
  };
};

export type AllocationConfig = {
  costCenterTags: string[];
  sharedValues: string[];
  unitTag: string;
  unitLabel: string;
  updatedAt: string | null;
};

export type DatabaseHealth = {
  generatedAt: string;
  controlPlane: {
    engine: string;
    reachable: boolean;
    latencyMs: number | null;
    error?: string;
  };
  analytical: {
    engine: string;
    path: string;
    exists: boolean;
    sizeBytes: number;
    modifiedAt: string | null;
    ageSeconds: number | null;
    writerLeaseHeld: boolean | null;
    note: string;
    error?: string;
  };
};

export type AdminJob = {
  source: string;
  label: string;
  schedule: string;
  observedAt: string | null;
  rowCount: number;
  health?: string;
  stale: boolean;
  nextExpectedAt: string | null;
  lastAttemptStatus?: string;
  lastAttemptMessage?: string;
  triggerSource: string;
};

export type RetentionPolicy = {
  name: string;
  days: number | null;
  setting: string;
  note: string;
};

export type AuditEntry = {
  surface: string;
  scope: string;
  updatedBy: string;
  updatedAt: string | null;
};

export type AiIntelligenceConfig = {
  provider: "deepseek" | "openrouter" | "foundry";
  fastModel: string;
  deepModel: string;
  overrideActive: boolean;
  updatedBy: string;
  updatedAt: string | null;
  keys: {
    deepseek: { configured: boolean; masked: string };
    openrouter: { configured: boolean; masked: string };
    foundry: { configured: boolean; masked: string };
  };
};

export type BudgetReport = {
  configured: boolean;
  period?: {
    monthStart: string;
    finalizedThrough: string;
    daysInMonth: number;
    elapsedFinalizedDays: number;
  };
  targets: {
    scopeType: string;
    scopeId: string;
    monthlyAmount: number;
    currency: string;
    mtdActual?: number;
    projectedMonthly?: number;
    burnPercent?: number | null;
    projectedPercent?: number | null;
    status?: string;
  }[];
};

export type UnitEconomicsReport = {
  configured: boolean;
  config?: AllocationConfig;
  summary?: {
    dimensionLabel: string;
    unitCount: number;
    totalMonthlyCost: number;
    unattributedCost: number;
    attributedPercent: number | null;
  };
  units?: {
    name: string;
    resourceCount: number;
    monthlyCost: number;
    percentOfTotal: number | null;
  }[];
};

export type ExecutiveSummary = {
  generatedAt: string;
  spend: Overview["periodComparison"];
  topServices: { name: string; mtdActual: number }[];
  serviceComposition: {
    periodStart: string;
    currency: string;
    billingServices: { name: string; amount: number }[];
    economicCategories: { name: string; amount: number }[];
    sources: { name: string; amount: number }[];
    mixedSourceClassification: boolean;
    note: string;
  };
  budgets: BudgetReport | null;
  anomalies: { count?: number; dailyIncrease?: number };
  savings: SavingsReport["summary"];
  allocation: { allocatedPercent: number | null; unallocatedCost: number } | null;
};

export type AllocationReport = {
  configured: boolean;
  config: AllocationConfig;
  summary?: {
    totalMonthlyCost: number;
    allocatedPercent: number | null;
    sharedPool: number;
    unallocatedCost: number;
    unallocatedResourceCount: number;
    centerCount: number;
  };
  centers?: {
    name: string;
    resourceCount: number;
    directCost: number;
    sharedAllocation: number;
    totalCost: number;
    percentOfTotal: number | null;
  }[];
};

export type TagHygieneReport = {
  summary: {
    resourceCount: number;
    excludedCount: number;
    taggedPercent: number | null;
    compliantPercent: number | null;
    totalMonthlyCost: number;
    taggedCostPercent: number | null;
    untaggedMonthlyCost: number;
    requiredTags: string[];
  };
  missingByRequiredTag: { tag: string; missingCount: number }[];
  bySubscription: {
    subscriptionId: string;
    subscriptionName: string;
    resources: number;
    tagged: number;
    compliant: number;
    cost: number;
    untaggedCost: number;
    taggedPercent: number | null;
    compliantPercent: number | null;
  }[];
  topUntagged: {
    resourceId: string;
    name: string;
    resourceType: string;
    subscriptionId: string;
    monthlyCost: number;
  }[];
};

export type GovernanceReport = {
  summary: {
    evaluated: number;
    compliant: number;
    nonCompliant: number;
    exempt: number;
    unknown: number;
    assignmentCount: number;
    compliancePercent: number | null;
    observedAt: string | null;
  };
  bySubscription: {
    id: string;
    name: string;
    evaluated: number;
    compliant: number;
    nonCompliant: number;
    exempt: number;
    assignmentCount: number;
    compliancePercent: number | null;
  }[];
  assignments: {
    subscriptionId: string;
    subscriptionName: string;
    assignmentId: string;
    assignmentName: string;
    evaluated: number;
    compliant: number;
    nonCompliant: number;
    exempt: number;
    unknown: number;
    resourceCount: number;
    definitionCount: number;
  }[];
  resources: {
    subscriptionId: string;
    subscriptionName: string;
    assignmentId: string;
    assignmentName: string;
    definitionId: string;
    definitionName: string;
    complianceState: string;
    resourceId: string;
    resourceName: string;
    resourceType: string;
    region: string;
    exemptionId: string;
    evaluatedAt: string;
  }[];
  lineage: { source: string; scope: string; limitation: string };
};

export type SourceRun = {
  source: string;
  scopeId: string;
  startedAt: string;
  completedAt: string | null;
  status: string;
  attemptCount: number;
  rowCount: number;
  retainedLastGood: boolean;
  message: string;
  lastAttemptAt: string | null;
  statusCode: number | null;
  retryAfterSeconds: number | null;
  nextRetryAt: string | null;
};

export type SyncRun = {
  id: string;
  provider: string;
  startedAt: string;
  completedAt: string | null;
  status: "queued" | "running" | "succeeded" | "failed";
  resourceCount: number;
  message: string;
  trigger: string;
  stage: string;
  stageMessage: string;
  claimedAt: string | null;
  requestedSources: string[];
  sourceRuns: SourceRun[];
};

export type SourceFreshness = {
  source: string;
  label: string;
  observedAt: string | null;
  rowCount: number;
  stale: boolean;
  ageHours: number | null;
  staleAfterHours: number;
  health: "healthy" | "stale" | "degraded";
  schedule: string;
  nextExpectedAt?: string | null;
  lastAttemptAt?: string;
  lastAttemptStatus?: string;
  lastAttemptMessage?: string;
  scopeTotal?: number;
  scopeSucceeded?: number;
  retainedLastGood?: boolean;
};

export type Resource = {
  resourceId: string;
  name: string;
  resourceType: string;
  subscriptionId: string;
  subscriptionName: string;
  resourceGroup: string;
  region: string;
  kind: string;
  sku: string;
  provisioningState: string;
  managedBy: string;
  tags: Record<string, string>;
  estimatedMonthlyCost: number | null;
  amortizedMonthlyCost: number | null;
  costCurrency: string;
  costSource: string | null;
  utilizationPercent: number | null;
  utilizationSource: string | null;
  opportunityKind: string | null;
  opportunityReason: string | null;
  estimatedMonthlySavings: number | null;
  observedAt: string;
  effectiveVirtualTags?: Record<string, { value: string; source: string; ruleName?: string }>;
};

export type InventoryChange = {
  snapshotId: string;
  previousSnapshotId: string;
  computedAt: string;
  resourceId: string;
  resourceName: string;
  resourceType: string;
  subscriptionId: string;
  subscriptionName: string;
  resourceGroup: string;
  region: string;
  changeType: string;
  details: Record<string, { from: unknown; to: unknown }>;
  methodVersion: string;
};

export type InventoryChanges = {
  items: InventoryChange[];
  total: number;
  limit: number;
  offset: number;
  facets: {
    changeTypes: string[];
    subscriptions: { id: string; name: string }[];
    resourceGroups: string[];
  };
  summary: {
    total: number;
    created: number;
    deleted: number;
    configuration: number;
  };
};

export type ChangeAnomalies = {
  items: {
    snapshotId: string;
    computedAt: string;
    scopeType: string;
    scopeId: string;
    subscriptionId: string;
    resourceGroup: string;
    changeType: string;
    changeCount: number;
    baselinePoints: number;
    baselineMedian: number | null;
    mad: number | null;
    kScore: number | null;
    thresholdK: number;
    status: "warming_up" | "normal" | "anomalous";
    methodVersion: string;
  }[];
  anomalyCount: number;
  warmingUp: boolean;
};

export type Opportunity = {
  id: string;
  lifecycleStatus: "open" | "accepted" | "implemented" | "dismissed";
  source: "inventory_rule" | "azure_advisor" | "flux_intelligence";
  kind: string;
  category: string;
  impact: string;
  confidence: string;
  title: string;
  reason: string;
  resourceId: string;
  relatedResourceId: string;
  resourceName: string;
  resourceType: string;
  subscriptionId: string;
  subscriptionName: string;
  resourceGroup: string;
  region: string;
  estimatedMonthlySavings: number | null;
  annualSavingsAmount: number | null;
  savingsCurrency: string;
  actualMonthlyCost: number | null;
  currentSku: string;
  recommendedSku: string;
  lastUpdated: string | null;
  learnMoreLink: string;
  observedAt: string;
  isCorroborated: boolean;
  corroboratedSources: string[];
  family: string;
  confidenceScore: number | null;
  firstSeen: string | null;
  lastSeen: string | null;
  ageDays: number | null;
  consecutiveCount: number | null;
  reappearedAfterRemediation: boolean;
  confidenceFactors: {
    factors: Record<string, number>;
    weights: Record<string, number>;
    contributions: Record<string, number>;
    telemetryApplicable: boolean;
    telemetryStatus: string;
    sourceCount: number;
    ageDays: number;
    evidenceFreshnessAt: string;
    methodVersion: string;
  } | null;
  confidenceMethodVersion: string;
  valuationStatus: string;
  monthlyGrossSavings: number | null;
  monthlyRiskAdjustedSavings: number | null;
  valuationCurrency: string;
  valuationSource: string;
  valuationBasis: string;
  valuationCostSnapshotId: string;
  valuationCostType: string;
  valuationPeriodStart: string | null;
  valuationPeriodEnd: string | null;
  valuationMethodVersion: string;
  valuationComputedAt: string | null;
  currentMonthlyCostRunRate: number | null;
  targetMonthlyRetailCost: number | null;
  currentCostBasis: string;
  targetPriceBasis: string;
  targetPriceSnapshotId: string;
  targetPriceStatus: string;
  targetHourlyPrice: number | null;
  targetHoursPerMonth: number | null;
  targetMeterId: string;
  targetMeterName: string;
  targetProductName: string;
  targetPriceEffectiveStart: string | null;
  priceOperatingSystem: string;
  priceLicenseModel: string;
  actionability:
    | "actionable_now"
    | "portfolio_review"
    | "evidence_needed"
    | "governance_review";
  actionabilityReason: string;
  recommendationStatus: "candidate" | "evidence_needed" | "validated" | "financially_qualified" | "dismissed" | "suppressed";
  executionStatus: "not_requested" | "owner_approval_needed" | "prechecks_needed" | "change_approval_needed" | "execution_ready" | "executed" | "verified" | "rolled_back" | "suppressed";
  executionBlockers: string[];
};

export type Opportunities = {
  items: Opportunity[];
  total: number;
  limit: number;
  offset: number;
  facets: {
    resourceTypes: string[];
    subscriptions: { id: string; name: string }[];
    regions: string[];
    sources: string[];
    categories: string[];
    confidences: string[];
    actionabilities: string[];
  };
  summary: {
    total: number;
    highImpact: number;
    highConfidenceFlux: number;
    estimatedMonthlySavings: number;
    annualSavings: number;
    corroborated: number;
    distinctResources: number;
    subscriptionScoped: number;
    costExposure: number;
    valuedCount: number;
    monthlyGrossValue: number;
    monthlyRiskAdjustedValue: number;
    skuValuedCount: number;
    skuCurrentMonthlyCost: number;
    skuTargetMonthlyCost: number;
    portfolio: {
      detected: number;
      actionableNow: number;
      portfolioReview: number;
      evidenceNeeded: number;
      governanceReview: number;
      valued: number;
      corroborated: number;
      telemetryReady: number;
      actionableBySource: {
        source: string;
        count: number;
      }[];
    };
  };
  diagnostics: {
    sourceRows: {
      source: string;
      rawRows: number;
      uniqueIds: number;
      duplicates: number;
    }[];
    visibleActions: number;
    distinctResources: number;
    subscriptionScoped: number;
  };
};

export type Inventory = {
  items: Resource[];
  total: number;
  limit: number;
  offset: number;
  facets: {
    resourceTypes: string[];
    subscriptions: { id: string; name: string }[];
    regions: string[];
    virtualTagDimensions: VirtualTagDimension[];
  };
};

export type TelemetryMetric = {
  source: string;
  metric: string;
  unit: string;
  windowStart: string;
  windowEnd: string;
  sampleCount: number;
  coveragePercent: number;
  average: number | null;
  p95: number | null;
  maximum: number | null;
  lastValue: number | null;
  lastObservedAt: string | null;
  aggregationMethod: string;
  lineage: Record<string, unknown>;
};

export type ExpertExplorerResult = {
  question: string;
  sql: string;
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
  rowLimit: number;
  durationMs: number;
  chartType: string;
  xKey: string;
  yKeys: string[];
  seriesKey: string | null;
  explanation: string;
  assumptions: string[];
};

export type ResourceTelemetry = {
  resourceId: string;
  costDaily: {
    date: string;
    actual: number | null;
    amortized: number | null;
    currency: string;
  }[];
  sampleSeries: {
    source: string;
    metric: string;
    unit: string;
    points: { t: string; value: number }[];
  }[];
  metrics: TelemetryMetric[];
  azureMonitorAttempt: {
    status: "covered" | "no_data" | "error";
    metricCount: number;
    message: string;
    observedAt: string;
  } | null;
  logicMonitorAttempt: {
    status: "covered" | "no_data" | "error";
    metricCount: number;
    message: string;
    observedAt: string;
  } | null;
  matches: {
    source: string;
    sourceResourceId: string;
    sourceName: string;
    status: string;
    method: string;
    confidence: string;
    observedAt: string;
  }[];
  rightsizingAssessment: {
    computedAt: string;
    kind: string;
    status: string;
    currentSku: string;
    targetSku: string;
    evidenceWindowDays: number;
    coverageFlag: string;
    telemetrySource: string;
    cpuP95: number | null;
    cpuMaximum: number | null;
    networkInP95: number | null;
    networkOutP95: number | null;
    metricCoveragePercent: number | null;
    estimatedMonthlySaving: number | null;
    currency: string;
    valueSource: string;
    reason: string;
    evidence: Record<string, unknown>;
    methodVersion: string;
  } | null;
};

export type RightsizingRecommendations = {
  items: {
    runId: string;
    computedAt: string;
    resourceId: string;
    resourceName: string;
    subscriptionId: string;
    subscriptionName: string;
    resourceGroup: string;
    region: string;
    kind: string;
    status: string;
    currentSku: string;
    targetSku: string;
    evidenceWindowDays: number;
    coverageFlag: string;
    telemetrySource: string;
    cpuP95: number | null;
    cpuMaximum: number | null;
    networkInP95: number | null;
    networkOutP95: number | null;
    metricCoveragePercent: number | null;
    estimatedMonthlySaving: number | null;
    currency: string;
    valueSource: string;
    reason: string;
    evidence: {
      logicMonitorMatched?: boolean;
      logicMonitorMetricsUsed?: boolean;
    };
    methodVersion: string;
  }[];
  total: number;
  limit: number;
  offset: number;
  summary: {
    virtualMachines: number;
    candidates: number;
    warmingUp: number;
    needsReview: number;
    insufficient: number;
    covered: number;
    estimatedMonthlySaving: number;
  };
};

export type TelemetryStatus = {
  virtualMachineCount: number;
  azureMonitorCovered: number;
  azureMonitorAttempted: number;
  azureMonitorNoData: number;
  azureMonitorErrors: number;
  logicMonitorMatched: number;
  logicMonitorAmbiguous: number;
  logicMonitorUnmatched: number;
  logicMonitorMetricCovered: number;
  logicMonitorCheckpointed: number;
  logicMonitorOldestCheckpoint: string | null;
  logicMonitorNewestCheckpoint: string | null;
  bySubscription: {
    subscriptionId: string;
    subscriptionName: string;
    virtualMachines: number;
    azureMonitorAttempted: number;
    azureMonitorCovered: number;
    azureMonitorNoData: number;
    azureMonitorErrors: number;
    logicMonitorMatched: number;
    logicMonitorCovered: number;
    candidates: number;
    warmingUp: number;
    insufficient: number;
  }[];
  runs: {
    source: string;
    id: string;
    trigger: string;
    startedAt: string;
    completedAt: string | null;
    status: string;
    processedCount: number;
    message: string;
  }[];
};

export type FinOpsToolkitStatus = {
  datasets: {
    dataset: string;
    toolkitVersion: string;
    upstreamCommit: string;
    sourceUrl: string;
    sha256: string;
    importedAt: string;
    rowCount: number;
    license: string;
  }[];
};

export type CostHistoryStatus = {
  latestRun: {
    runId: string;
    startedAt: string;
    completedAt: string | null;
    status: "running" | "succeeded" | "partial" | "failed";
    expectedScopes: number;
    completedScopes: number;
    failedScopes: number;
    rowCount: number;
    message: string;
  } | null;
  scopes: {
    runId: string;
    subscriptionId: string;
    subscriptionName: string;
    costType: "ActualCost" | "AmortizedCost";
    startedAt: string;
    completedAt: string | null;
    status: "running" | "succeeded" | "failed";
    queryStart: string;
    queryEnd: string;
    rowCount: number;
    retainedLastGood: boolean;
    statusCode: number | null;
    message: string;
    attemptCount: number;
    retryCount: number;
    lastAttemptAt: string | null;
    nextRetryAt: string | null;
    retryAfterSeconds: number | null;
  }[];
  backfill: {
    completedPeriods: number;
    failedPeriods: number;
    runningPeriods: number;
    periods: {
      subscriptionId: string;
      subscriptionName: string;
      costType: "ActualCost" | "AmortizedCost";
      periodStart: string;
      periodEnd: string;
      status: "running" | "succeeded" | "failed" | "unsupported";
      attemptCount: number;
      rowCount: number;
      firstAttemptAt: string | null;
      lastAttemptAt: string | null;
      completedAt: string | null;
      nextRetryAt: string | null;
      statusCode: number | null;
      message: string;
      source: string;
    }[];
  };
};

export type CostCoverage = {
  windowStart: string;
  windowEnd: string;
  scopeCount: number;
  completeScopes: number;
  expectedScopeDays: number;
  ingestedScopeDays: number;
  coveragePercent: number | null;
  scopes: {
    subscriptionId: string;
    subscriptionName: string;
    costType: "ActualCost" | "AmortizedCost";
    windowStart: string;
    windowEnd: string;
    expectedDays: number;
    ingestedDays: number;
    missingDays: number;
    coveragePercent: number | null;
    firstIngestedDay: string | null;
    missingRanges: { start: string; end: string }[];
    missingRangesTruncated: boolean;
  }[];
};

export type TelemetryCoverage = {
  totalVms: number;
  coveredVms: number;
  coveredPercent: number | null;
  lowCoverageVms: number;
  lowCoverageThresholdPercent: number;
  totalMonthlyCost: number;
  uncoveredMonthlyCost: number;
  coveredMonthlyCost: number;
  bySource: {
    source: string;
    vmCount: number;
    averageCoveragePercent: number;
  }[];
  uncovered: {
    resourceId: string;
    name: string;
    subscriptionName: string;
    estimatedMonthlyCost: number;
  }[];
  uncoveredTruncated: boolean;
};

export type SloReport = {
  generatedAt: string;
  worstState: "ok" | "unknown" | "warn" | "breach";
  objectives: {
    key: string;
    label: string;
    description: string;
    unit: string;
    direction: "above" | "below";
    warn: number;
    breach: number;
    runbook: string;
    value: number | null;
    state: "ok" | "unknown" | "warn" | "breach";
    tracked: {
      state: string;
      since: string | null;
      lastNotified: string | null;
    } | null;
  }[];
};

export type RecommendationQuality = {
  status: "healthy" | "review" | "failed";
  asOf: string;
  storedActive: number;
  uniqueIds: number;
  semanticActions: number;
  duplicateIds: number;
  semanticDuplicates: number;
  subscriptionScoped: number;
  resolvedResources: number;
  unresolvedResources: number;
  estateResources: number;
  recommendationsPerResource: number;
  portfolio: {
    detected: number;
    actionableNow: number;
    portfolioReview: number;
    evidenceNeeded: number;
    governanceReview: number;
    valued: number;
    corroborated: number;
    telemetryReady: number;
    actionableBySource: { source: string; count: number }[];
  };
  checks: {
    name: string;
    status: "passed" | "review" | "failed";
    value: number;
    message: string;
  }[];
  method: string;
};

export type OperationalHealth = {
  status: "healthy" | "degraded" | "critical";
  asOf: string;
  summary: {
    healthySources: number;
    staleSources: number;
    degradedSources: number;
    incompleteCostDatasets: number;
    queuedRuns: number;
    runningRuns: number;
  };
  worker: {
    status: "ready" | "busy" | "stalled";
    queuedRuns: number;
    runningRuns: number;
    oldestActiveAt: string | null;
    activeAgeMinutes: number | null;
    latestRun: SyncRun | null;
  };
  sources: SourceFreshness[];
  costDatasets: CostDatasetStatus[];
  recommendations: RecommendationQuality;
};

export type AzureIntegration = {
  name: string;
  tenantId: string;
  enabled: boolean;
  authMode: "local_powershell" | "managed_identity";
  subscriptions: { subscriptionId: string; label: string }[];
  lastSyncAt: string | null;
  lastSyncStatus: string;
  lastSyncMessage: string;
  updatedAt: string | null;
  latestSync?: SyncRun | null;
  sourceFreshness?: SourceFreshness[];
};

export type VirtualTagDimension = {
  key: string;
  name: string;
  description: string;
  status: "active" | "inactive";
  version: number;
  updatedBy: string;
  updatedAt: string | null;
  implicit: boolean;
};

export type VirtualTagCondition = {
  field: string;
  operator: string;
  key?: string;
  value?: string;
  values?: string[];
};

export type VirtualTagRule = {
  ruleId: string;
  name: string;
  tagKey: string;
  tagValue: string;
  priority: number;
  effect: "include" | "exclude";
  conditions: { combinator?: "and" | "or"; conditions?: VirtualTagCondition[]; groups?: unknown[]; [key: string]: unknown };
  status: "active" | "inactive";
  effectiveFrom: string | null;
  effectiveTo: string | null;
  version: number;
  updatedBy: string;
  updatedAt: string | null;
};

export type VirtualTagPreview = {
  matchedCount: number;
  totalResources: number;
  matchedMonthlyCost: number;
  sample: { resourceId: string; name: string; resourceGroup: string; region: string; monthlyCost: number }[];
};

export type VirtualTagReport = {
  dimension: string;
  dimensions: VirtualTagDimension[];
  costType: string;
  currency: string;
  summary: { totalCost: number; classifiedCost: number; classifiedPercent: number | null; valueCount: number; resourceCount: number };
  values: { value: string; cost: number; resourceCount: number; percentOfTotal: number | null; sources: Record<string, number> }[];
  monthly: { month: string; value: string; cost: number }[];
  resources: { resourceId: string; name: string; subscriptionName: string; resourceGroup: string; resourceType: string; value: string; source: string; cost: number }[];
  resourcesTruncated: boolean;
  lineage: { costSource: string; tagEvaluation: string; precedence: string; limitation: string };
};
