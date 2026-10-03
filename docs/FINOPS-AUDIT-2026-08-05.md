# Flux FinOps Accounting and Calculation Audit

Audit date: 2026-08-05  
Addendum: 2026-08-09 — two-plane inventory and doc-sync updates (no change to audit findings)
Scope: Azure cost ingestion, FOCUS normalization, allocation/showback, budgets,
forecasting, anomaly detection, opportunity valuation, realized savings,
rightsizing, Reservations, and Savings Plans.

## Audit opinion

Flux is suitable for directional FinOps analysis after the defects corrected in
this audit, but it is not yet an audit-grade subledger. The principal remaining
control gap is that persisted money uses binary `DOUBLE` values and several
executive surfaces still need an explicit one-currency measure contract. The
Savings Plan board is an estimate/candidate generator, not a purchase optimizer:
Azure optimizes a shared dollar-per-hour commitment against hourly eligible PAYG
usage, while Flux currently prices candidate VMs as monthly equivalents.

Accounting-source inventory reviewed:

- `costs_current`: current Cost Management resource snapshots (ActualCost and
  AmortizedCost).
- `daily_cost_history`: daily resource/service ledger used by reports, budgets,
  forecasts, and anomalies.
- `monthly_cost_history`: subscription-month ledger used by fiscal outlook.
- `focus_cost_charges` / `focus_cost_current`: charge-level billed, effective,
  contracted, and list cost evidence.
- Advisor recommendations, reservation recommendations/inventory, retail-price
  snapshots, opportunity valuation snapshots, rightsizing plan values, and
  lifecycle baselines.


> **Addendum 2026-08-09 — two-plane clarification.** This audit predates the PostgreSQL + DuckDB interim plan
> (`docs/POSTGRES-DUCKDB-INTERIM-SCALING-PLAN.md`). Its `DOUBLE` vs `DECIMAL` and currency-contract findings still
> apply identically on both planes (operational PostgreSQL uses `DOUBLE PRECISION` via `OperationalStore._translate_sql`).
> Operational tables referenced in this audit (`cost_anomaly_reviews`, `focus_import_runs`, etc.) now live on the
> operational plane; analytical tables (`daily_cost_history`, `cost_anomaly_snapshots`, `focus_cost_charges`, …) remain
> on the analytical plane and are snapshot-published. Nothing in this addendum changes the audited defects or severities.

## 1. Defects and calculation bugs

### Corrected in this audit

1. **Mixed-currency virtual-tag totals (critical).** The report summed every
   currency and then attached the dominant currency code. USD 100 plus EUR 100
   could therefore be reported as USD 200. It now chooses or accepts one
   currency before aggregation and lists excluded currencies in lineage.

2. **Currency-unsafe budgets (critical).** Budget MTD grouped only by
   subscription, so spend in every currency was compared with a target in one
   currency. Budget math now keys balances by `(subscription, currency)` and
   normalizes configured currency codes to uppercase.

3. **Virtual-tag fiscal lane failure (critical).** `YYYY-MM` strings were passed
   to a forecaster requiring `date` keys, causing a runtime type error as soon as
   a tag-driven group had history. Keys are now converted to month dates.

4. **Virtual-tag MTD understatement (high).** An incomplete tagged month was
   treated as the full-month estimate. It is now calendar-normalized through the
   same T-2 finalized horizon used by budgets.

5. **False “covered” status after a 401 (critical).** A failed scope row was a
   truthy object and was therefore labeled covered even with zero monthly rows.
   Coverage now requires at least one historical month; a failed 401 without a
   retained last-good row is isolated and marked for administrator action.

6. **Critical warning path could execute unrelated DDL (critical).** The fiscal
   warning branch contained a misplaced `ALTER TABLE` against a closed/read-only
   analytical connection. This could crash the outlook exactly when a critical
   collection failure occurred. The statement was removed; the schema column is
   created only during database initialization.

7. **Configured coverage could exceed 100% (high).** Historical rows from
   removed subscriptions were counted in the numerator but not the configured
   denominator. Coverage is now counted from configured subscription details.

8. **Realized savings compared unequal MTD windows (critical).** A raw MTD
   snapshot was labeled monthly and compared with a later raw MTD snapshot. The
   apparent difference could be entirely due to observation day. Baseline and
   current values are now normalized with the same monthly-equivalent method,
   currency must match, and legacy baselines with unknown methodology remain
   unmeasured rather than fabricating realization.

9. **Cross-month run-rate depended on the first month (high).** A rolling
   30-day observation beginning in February used 28/29 days while one beginning
   in March used 31. True MTD uses the actual calendar month; cross-month windows
   use the Gregorian average month (365.2425 / 12). Monetary rounding is Decimal
   half-up at the valuation boundary.

10. **Retail proxy could price the wrong VM size (critical).** A missing D8
    price could select a D2 from the same family and use its absolute commitment
    cost. Proxy matching now requires the same instance-flexibility ratio/size,
    preferring another version in-region and then a disclosed peer region.

11. **Rightsizing retail cache ignored currency (critical).** The cache key was
    `(region, SKU, profile)` even though retail snapshots can contain several
    currencies; whichever row was iterated last won. The key now includes
    currency and the FOCUS baseline is restricted to one dominant currency.

12. **Ingestion holes could become anomaly zeroes (high).** Missing resource
    days may be genuine zero usage, but missing subscription days are collection
    gaps. The anomaly engine now verifies the containing subscription's current
    and matching-weekday ledger dates before treating absent child rows as zero.

13. **Opportunity average used incompatible populations (high).** Total
    findings was implicitly compared with savings from only valued/deduplicated
    findings. The API now returns `valuedCount`, `unvaluedCount`,
    `valueCoveragePercent`, and `averageRiskAdjustedPerValuedOpportunity`.
    Rightsizing refresh similarly returns modeled baseline, modeled savings, and
    modeled savings rate.

### Open defects / required foundation work

1. **Binary floating-point ledger (P1).** Cost, price, budget, and savings
   columns are `DOUBLE`, and ingestion converts source values to Python `float`.
   Proposed fix: store source money as `DECIMAL(38, 12)`, rates as
   `DECIMAL(38, 18)`, and published money as `DECIMAL(38, 2)` with an explicit
   rounding policy. Preserve source text/scale for reconciliation.

2. **Savings Plan model is monthly, not hourly portfolio simulation (P1).** A
   per-VM monthly SP retail equivalent cannot model use-it-or-lose-it hourly
   commitment, benefit ordering, concurrent usage, existing commitments, or
   negotiated PAYG rates. Proposed fix: build an hourly eligible-usage cube from
   FOCUS, subtract current RI/SP coverage, simulate candidate hourly commitments
   at shared/subscription scope, and optimize total net savings over 7/30/60-day
   lookbacks. Until then, retain “estimate / candidate review” language and do
   not present the result as a purchase quantity.

3. **Allocation and unit-economics legacy views do not publish a currency
   contract (P1).** They aggregate `costs_current` and return unlabeled money.
   Proposed fix: require a currency parameter or return one ledger per currency;
   never use `max(currency)` after summation.

4. **Opportunity source-estimate totals can span currencies (P1).** Governed
   valuation is currency-labeled per row, but the legacy
   `estimatedMonthlySavings` summary sums source estimates without a selected
   currency. Proposed fix: publish source-estimate totals by currency and make
   the governed, one-currency valuation total the executive default.

5. **Forecasts clamp negative daily/monthly totals to zero (P2).** Credits,
   refunds, and corrections are omitted from the forecast model. Proposed fix:
   forecast gross usage and adjustments separately, then publish both gross and
   net outlooks with adjustment volatility.

6. **Current virtual tags restate history (P2).** Historical charge allocation
   uses today's inventory and rule set. A rule change rewrites prior showback.
   Proposed fix: effective-dated tag/rule snapshots and immutable monthly
   allocation journals, plus explicit restatement entries.

7. **Missing past fiscal months are projected (P2).** The fiscal engine fills a
   missing historical month with a projection instead of marking an incomplete
   actual period. Proposed fix: require a closed-month coverage control before
   treating it as actual or projected and expose an “estimated historical gap”
   status separately.

8. **ActualCost is unsuitable for commitment chargeback by resource (P2).** An
   upfront reservation purchase can appear in one period while a covered VM can
   show zero ActualCost. AmortizedCost/FOCUS EffectiveCost should be the default
   for unit economics, commitment performance, and resource savings; ActualCost
   remains the invoice/reconciliation view.

## 2. Conservative logic and misinterpretation risks

1. A reservation bucket claims zero savings unless every member has a FOCUS
   list-cost baseline, reconciles to retail, and has an unambiguous target price.
   One unpriced VM can zero the whole bucket. This prevents overstatement but can
   materially understate estate potential. Publish modeled/unmodeled baseline
   dollars, not just modeled bucket count, and model members independently before
   consolidating purchase quantity.

2. The 10% FOCUS-to-retail reconciliation gate excludes negotiated-price or
   non-730-hour workloads even when the FOCUS list baseline is valid. Use exact
   observed hourly quantity where available; treat retail reconciliation as a
   quality flag, not a zero-value gate.

3. Generic technical-review VMs remain on demand; only explicit lifecycle-risk
   workloads enter Savings Plan review. This is a governance choice, not an
   economic conclusion. Separate “economic candidate” from “approval lane.”

4. Existing Advisor RI/SP recommendations are corroboration only and never added
   to Flux savings. This prevents overlap but means missing Flux prices suppress
   valid Azure-modeled savings. Use Advisor as a governed fallback with source,
   scope, term, lookback, and overlap key retained.

5. Opportunity valuation deduplicates overlapping sources by resource/family and
   executive totals take the largest value per resource. This prevents double
   counting RI, SP, rightsize, and shutdown alternatives, but can suppress truly
   additive actions such as VM resize plus unrelated disk cleanup. Add an action
   compatibility matrix and portfolio solver.

6. Risk-adjusted savings multiplies gross value by confidence. This is a
   prioritization measure, not accounting savings. UI and exports must keep gross,
   confidence, risk-adjusted, approved, and realized values in separate columns.

7. Retail peer-region and same-size/version fallbacks are estimates. They should
   never be mixed into “exact retail” totals without an estimated-value share and
   source region/SKU disclosure.

8. The daily forecast requires 28 calendar days but can still have sparse
   observations. Add observed-day coverage and block publishing a point estimate
   below the governed threshold.

## 3. High-variance justifications

### Finding counts versus valued savings

A total finding count and a total monthly saving do not form a valid per-action average. The count includes detected,
governance, evidence-needed, subscription-scoped, unvalued, and deduplicated
findings. The governed savings numerator includes only valued rows and then takes
one non-overlapping value per resource. The corrected contract exposes the valued
denominator and value-coverage percentage. If the rerun still shows a low value
per **valued** item, inspect micro-opportunity thresholds and source currency; if
it is low only per **detected** item, the variance is presentation/coverage, not
arithmetic.

### VM spend versus modeled savings

A blanket 20% savings benchmark applies to total VM spend only if all of it is eligible,
on-demand, continuously used, economically coverable, and non-overlapping after
rightsizing. Flux currently excludes or zeros:

- VMs without charge-attributed FOCUS list cost;
- buckets where any member fails retail reconciliation or target pricing;
- workloads held for technical review or removed as waste;
- already reserved units;
- Windows license cost outside the compute commitment discount;
- Advisor scenarios, because they may overlap Flux RI/SP/resize scenarios; and
- unmodeled Savings Plan concurrency because the current engine is monthly.

Therefore a modeled saving well below that benchmark may be mathematically consistent with the modeled subset while
still being an incomplete estimate of estate potential. The new modeled baseline
and savings-rate fields make the distinction testable. The target control is:

`modeled savings + explicitly unmodeled potential = total eligible economic baseline`

with every exclusion assigned a dollar amount and reason. A production rerun is
required to attribute the spend across those buckets; no conclusion about the
specific estate variance should be made from count data alone.

### Billing latency and 401 exclusions

Normal T-2/T-3 latency changes recency, not historical truth, and belongs in data
lineage. An HTTP 401 is scope-specific: retained last-good history may continue to
support historical analysis with a stale qualifier; without retained history the
scope is excluded and is an administrator action. It must not zero the failed
subscription or invalidate unaffected subscription balances.

## 4. Financial verification unit tests

Implemented regression cases:

- calendar MTD normalization and leap-February (29 days);
- stable cross-month monthly equivalent;
- Decimal half-up micro-dollar boundary (`0.005 -> 0.01`);
- mixed-currency budget isolation;
- virtual-tag month-key/date contract and T-2 MTD projection;
- configured coverage excludes stale removed subscriptions;
- HTTP 401 isolation and administrator-action classification;
- realized savings requires comparable monthly method and currency;
- same-family retail proxy cannot use a different VM size; and
- FOCUS/retail commitment math does not add Advisor overlap.

Required next control suite:

1. **Ledger conservation:** source charge sum equals normalized ledger sum by
   source, subscription, day/month, cost type, and currency; tolerance zero at
   stored decimal scale.
2. **FOCUS conservation:** BilledCost, EffectiveCost, ContractedCost, and ListCost
   reconcile independently; never sum unlike measures.
3. **Commitment simulation:** hourly eligible PAYG fixture with uneven daytime
   usage proves unused commitment does not roll to another hour and overlapping
   RI/SP coverage is subtracted once.
4. **Partial ingestion:** one missing subscription/day blocks anomaly publication
   but retains unaffected scope totals.
5. **Credits/refunds:** gross usage, adjustment, and net forecast remain
   separately reconcilable.
6. **Allocation journal:** direct + shared allocation + unallocated equals the
   selected-currency source ledger after rounding; residual cents are assigned by
   deterministic largest-remainder logic.
7. **Tag restatement:** an effective-dated rule change does not rewrite a closed
   month's allocation without an explicit restatement journal.
8. **Price fallback:** exact, same-region/version proxy, peer-region proxy, and
   unavailable cases carry mutually exclusive status and value shares.

## Operational notes (added during Aug 9 docs refresh)

- **Build at refresh:** `2.0.0` + `ca66a85` (`version.json` stamped in `azure-pipelines.yml` → `settings.build_commit` → `GET /api/health` `commit`, `GET /api/session` `build`). Audit findings remain valid against this build; no ledger, pricing, or FOCUS ingestion changes were made in this docs-only pass.
- **Pipeline safety-net** (commit `72bfcdd`, 2026-08-08): `azure-pipelines.yml` now reapplies `FLUX_HOST=0.0.0.0`, `FLUX_PORT=8000`, `WEBSITE_SKIP_RUNNING_KUDUAGENT=false`, `FLUX_AUTH_MODE=entra` on every deploy. A 2026-08-08 destructive `GET /config/appsettings` read-modify-write wiped every WebJob; the safety-net prevents recurrence. Subsequent doc reviews should confirm runtime parity via `GET /api/health` `commit`.
- **Ports & planes:** dev Flux `8765` / Rill `8786` / prod `0.0.0.0:8000`; two-plane model is PostgreSQL operational + DuckDB analytical snapshots (`FLUX_ANALYTICS_SNAPSHOT_MODE=snapshot` on web, `analytics_publications`). Audit P1 items (binary DOUBLE ledger, allocation currency contract) span both planes.
- **windowDays history-awareness** (commit `c1a9c3f`): `GET /api/inventory/changes` and `GET /api/cost/anomalies` now accept `windowDays` and read full history tables filtered by `computed_at`/`evaluated_at` when scoped — relevant to anomaly-warming (defect 12) and drift volume tests that previously read only latest state.
- **Commitment optimizer:** since this audit, defect F (Savings Plan monthly-estimate engine) has been replaced by `flux-commitment-optimizer-v2` hourly portfolio simulation with purchase-ready gates (see `docs/COMMITMENT-OPTIMIZER.md`); the audit's P1 on the monthly model is therefore superseded for the optimizer surface but still describes the legacy `__savingsplan__` lane retained for planning.

## External accounting basis

Microsoft documents that open-period Cost Management data is estimated and can
lag by 8-24 hours for EA/MCA and up to 72 hours for PAYG. ActualCost represents
invoice timing, while AmortizedCost spreads commitment purchases and attributes
benefit usage. Azure Savings Plan recommendations simulate actual hourly eligible
PAYG usage and optimize a dollar-per-hour commitment; unused hourly commitment
does not roll over. The Azure Retail Prices API provides public retail estimates,
with non-USD values described as budget references rather than contracted rates.

