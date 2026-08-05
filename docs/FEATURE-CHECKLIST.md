# FluxFinOps feature checklist

Last reviewed: 2026-07-31

This is the canonical product checklist. It consolidates the original focused
architecture, the intelligent-feature viability review, telemetry work, and the
Microsoft FinOps Toolkit compatibility plan.

Status meanings:

- `[x]` Shipped and deployed
- `[ ]` Accepted backlog; an item marked **In progress** has a usable foundation
- **Deferred** Deliberately excluded from the current architecture

## Current roadmap

### MVP usability — current priority

The immediate objective is a dependable, usable MVP across the core daily
workflows. New feature expansion is secondary until these acceptance items are
complete.

- [ ] Shared frontend request lifecycle with cancellation, race protection,
  consistent loading/error states, and retry behavior
- [ ] Fix report-tab failures so workload and governance errors cannot leave an
  indefinite loading state
- [ ] Clear recovered overview errors and prevent polling requests from
  presenting stale failure state
- [ ] Keep action failures local to the affected anomaly, opportunity, or card;
  preserve the surrounding results and provide retry feedback
- [ ] Complete the core keyboard-access pass: interactive rows/cards, dialogs,
  focus management, Escape handling, and visible focus states
- [ ] Replace remaining `window.prompt` flows with the established dialog
  pattern
- [ ] Define and run an MVP usability pass for Overview, Inventory, Reports,
  Cost Anomalies, Opportunities, Integrations, and Ask Flux
- [ ] Record MVP acceptance criteria for empty, loading, stale, partial-data,
  permission, and backend-error states

### Foundation strengthening — after MVP usability

Once the MVP workflows are usable and accepted, reduce operational and
architectural concentration before resuming broad feature growth.

- [ ] Roll out immutable analytics-snapshot reads as the production web read
  path, with explicit stale/no-snapshot behavior and freshness monitoring
- [ ] Split `FluxDatabase` along existing domain boundaries without changing
  report contracts
- [ ] Standardize structured backend logging, request timing, and correlation
  IDs across API and worker paths
- [ ] Change the missing `FLUX_AUTH_MODE` default to fail closed and add a
  deployment guard for unsafe auth configuration
- [ ] Generate frontend response types from FastAPI OpenAPI and fail CI on
  contract drift
- [ ] Split the four largest frontend pages along existing tab/domain seams
- [ ] Add performance and reliability gates around the heavy DuckDB test suite;
  evaluate safe parallelization after database modularization

Roadmap sequencing: **MVP usability first, foundation strengthening second,
then renewed feature expansion.**

## Focused platform foundation

- [x] React/TypeScript application shell with Entra-authenticated navigation
- [x] Dashboard with cost, inventory, utilization, opportunity, and coverage charts
- [x] Cost-source reconciliation across current, daily-history, and commitment scopes
- [x] Live Azure Resource Graph inventory across configured subscriptions
- [x] Paginated Inventory with explicit visible/total counts and full filtered CSV export
- [x] DuckDB analytical history with current, successful-source views
- [x] PostgreSQL transactional control plane for sync runs, retries, and admin coordination
- [x] Entra Reader/Admin authorization
- [x] Managed-identity ARG, Advisor, Cost Management, and Azure Monitor access
- [x] Durable singleton synchronization worker and independently scheduled jobs
- [x] Admin integration configuration with Reader mutation protection
- [x] Actual and amortized Cost Management collection with scope checkpoints
- [x] Azure Advisor collection and normalized opportunity export
- [x] Filter parity across Inventory and Opportunities
- [x] Actionable-now opportunity queue with separate portfolio, evidence, and governance review lanes
- [x] CSV export with resolved resource names and spreadsheet-injection protection
- [x] Managed-identity database backups with retention
- [x] Administrator operational health center with next-run expectations and worker-stall detection
- [x] Persisted Cost Management retry timing and explicit retry eligibility
- [x] Advisor recommendation ID, semantic-action, identity, and actionability regression checks
- [x] Production App Service, HTTPS, managed-identity, and continuous-worker deployment checks
- [x] Parameterized authenticated Reader/Admin production smoke script

## Thirteen-item reporting and reliability scope

- [x] **1. Cost-history reliability and scope observability**
  - All-process DuckDB lease, bounded lock retries, independently staggered jobs
  - Durable request attempts, retry delays, HTTP failures, scope coverage, and last-good state
  - Failed/unfinished scopes first; health visible in Integrations and Cost Anomalies
  - Automated monthly Cost Details fallback with per-period checkpoints for failed Query API scopes
- [x] **2. Native Toolkit reporting parity matrix**
  - Canonical page classification plus measures, filters, drilldowns, exports, and acceptance tests
- [x] **3. Native Cost Summary reporting**
  - Actual/amortized comparison, trends, prior-period deltas, all primary breakdowns
  - Subscription/service/resource movers, resource drilldown, and complete CSV
- [x] **4. Cost forecasting**
  - Daily and calendar-month forecasts, robust bounds, billed-latency cutoff, MAPE, and warm-up
  - Estate/subscription/service/resource scope; budget variance explicitly blocked without targets
- [x] **5. Cost anomaly investigation**
  - Previous-week comparison, service/resource contributors, review workflow, CSV, and change-request evidence
- [x] **6. Additional Toolkit/ARG rules**
  - Versioned disk, storage, App Service, VMSS, SQL, orphan, and official service-retirement review rules
- [x] **7. Azure Policy governance posture**
  - Assignment totals, subscription/assignment/state filters, exemptions, and resource drilldown
- [x] **8. Deterministic investigator evidence packs**
  - Inventory/cost/telemetry/confidence/freshness/uncertainty plus implementation, validation, and rollback draft
- [x] **9. Governed reporting catalog**
  - Approved measures, dimensions, joins, filters, sources, request validation, and arbitrary-SQL rejection
- [x] **10. Toolkit upstream-drift tooling**
  - Release, commit, dataset, measure, rule, and report-feature review checklist; never auto-imports behavior
- [x] **11. Native workload-optimization reporting**
  - Savings trend, confidence and aging distribution, telemetry gaps, valuation, pricing, and candidate evidence
- [x] **12. Optional Rill semantic layer**
  - Read-only cost, anomaly, workload, and Policy models compiled and parity-tested against native measures
  - Runtime remains optional and is not hosted by the Flux App Service
- [x] **13. Resource retirement reporting**
  - Aged/orphaned/stopped/unattached/retired-service candidates with ownership readiness, cost, CSV, and evidence

## Intelligent feature viability list

- [x] **1. Inventory drift and change anomalies** `[Rules + Statistics]`
  - Exact create/delete/resize/retier/retag/move/reconfiguration diffs
  - Median/MAD anomaly baselines with warming-up state
- [x] **2. Opportunity persistence and confidence** `[Statistics / heuristic]`
  - First/last seen, consecutive observations, recurrence, corroboration, freshness
- [x] **3. Opportunity valuation** `[Rules]`
  - Advisor estimates and eligible amortized-cost run rates
  - Gross and confidence-adjusted values with source lineage
- [x] **4. Utilization right-sizing and idle detection** `[Rules / Statistics]`
  - Azure Monitor collection, LogicMonitor baseline, coverage states, memory guardrail
  - Multi-source conflict review and Advisor-only target SKU
  - [x] Incremental checkpointed LogicMonitor metric collection
  - [x] Governed SKU price-difference valuation
- [ ] **5. Cost anomaly detection** `[Statistics — time series]` **In progress**
  - [x] Checkpointed daily actual and amortized resource/service cost history
  - [x] Matching-weekday median/MAD baseline with explicit two-day billed-data latency
  - [x] Subscription, service, and resource findings with warming-up state and method lineage
  - [x] Dedicated Cost anomalies page, filters, trend, and dashboard signal
  - [x] Admin review workflow, filtered CSV, and deterministic evidence export
  - [x] Governed 30-day matching-weekday forecast with robust confidence bounds
  - [ ] Usage/rate decomposition
  - [ ] Budget trajectory (requires approved organizational targets)
- [x] **6. Natural-language visualization** `[LLM — governed read-only]`
  - [x] Governed measure/dimension catalog; no text-to-SQL
  - [x] Native React/Recharts report outputs
  - [x] Swappable model-service adapter with fast and deep-analysis profiles
  - [x] OpenRouter provider adapter (default: `google/gemini-2.5-flash-lite` + `openai/gpt-4.1-mini`)
  - [x] Contextual Ask Flux panel and full Intelligence Workspace
  - [x] Markdown, governed chart, and strict Mermaid output contract
  - [x] Configurable 30-day admin transcript review; model reasoning excluded
  - [x] Browser-to-model stage timing with average/p95 monitoring and per-tool breakdown
  - [x] Flux.Reader authorization, bounded governed tools, and no direct data access
  - [x] Evaluation budget/stop ceiling, feedback, seed evaluation suite, and limitations
  - [x] Admin transcript-quality review, response-mode regression, slow-request counts, and stage bottlenecks
  - [x] Governed composite daily-history and FOCUS charge-level cost investigation
  - [x] Deterministic per-answer quality score and retained regression visibility
  - [x] Server-generated Flux evidence links and bounded read-tool caching
  - [ ] Stakeholder-authored evaluation questions and acceptance thresholds
  - [ ] Production hardening, model-service procurement, and steady-state governance
  - **Current boundary:** read-only internal alpha
- [x] **7. Investigator and evidence packs** `[Deterministic foundation]`
  - Curated read-only opportunity and cost-anomaly evidence with uncertainty
  - Markdown and JSON payloads suitable for a later approved LLM drafting step
  - No autonomous cloud mutation or mixed-risk bulk approval

## Telemetry

- [x] Azure Monitor rolling VM platform summaries
- [x] Subscription-level telemetry coverage and per-VM governed right-sizing rationale
- [x] LogicMonitor identity discovery and Azure resource matching
- [x] Historical Azure Monitor and LogicMonitor bootstrap import
- [x] Per-metric source, aggregation, collection window, and lineage
- [x] Conservative source reconciliation and disagreement review
- [x] Incremental LogicMonitor CPU/memory/disk/network collector
- [x] Governed 30-day raw-sample retention and rolling 14-day summaries
- [ ] AMA/Log Analytics adapter when approved data becomes available

## Microsoft FinOps Toolkit compatibility

- [x] MIT attribution and upstream version manifest
- [x] Checksum-pinned v14 open-data downloader and DuckDB loader
- [x] Resource type, region, service, and pricing-unit reference tables
- [x] Reservation/Savings Plan meter eligibility reference table
- [x] Weekly independent open-data import job
- [x] Windows Server Azure Hybrid Benefit eligibility review rule
- [x] SQL VM Azure Hybrid Benefit eligibility review rule
- [x] Join commitment eligibility to Meter ID/Pricing Model cost summaries
- [x] Directional Reservation and Savings Plan eligible-cost dashboard
- [ ] Charge-level Reservation and Savings Plan utilization dashboard
- [x] Port non-duplicative VMSS, SQL, App Service, disk, storage, and orphan rules
- [x] Add report-only upstream-drift review tooling for new Toolkit releases
- [x] FOCUS v1.0 charge-level model, idempotent Blob ingestion, and curated daily views
- [x] Use Toolkit Power BI/workbook measures as native-report acceptance references
- **Deferred:** Full Azure Optimization Engine deployment
  - Deferred because it duplicates Flux and adds Automation, SQL, Log Analytics,
    and storage infrastructure.
- **Deferred:** FinOps Hubs deployment
  - Retained as a scale-out option when DuckDB/App Service thresholds are reached.
- **Deferred:** Automated FinOps Alerts remediation
  - Deferred until governed notification and approval workflows exist.

## Planned FinOps and CloudOps extensions

- [x] Scoped Azure Retail Prices adapter for explicit VM target SKUs
- [ ] Contracted Azure price adapter with explicit agreement provenance
- [ ] Cost allocation rules, shared-cost allocation, and showback/chargeback
- [ ] Budgets and commitment planning
- [x] Governed billed-cost forecast
- [x] Resource retirement candidate reporting
- [x] Azure Policy compliance and governance posture
- [x] Optional local Rill semantic layer over curated DuckDB models
- [x] Read-only Flux Intelligence over the governed catalog
- **Deferred:** Power BI workspace publishing (native Flux output is the target)

## Guardrails that apply to every feature

- [x] One cross-process DuckDB writer lease
- [x] Last-good source data survives partial failures
- [x] Missing telemetry is never classified as idle
- [x] Recommendations retain source and method-version evidence
- [x] Readers cannot start or configure synchronization
- [x] LLM prerequisites expose curated catalogs and never execute raw generated SQL
- [x] Remediation remains human-approved and separate from evidence generation
