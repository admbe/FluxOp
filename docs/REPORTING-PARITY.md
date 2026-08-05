# Native Flux reporting parity

Reference: Microsoft FinOps Toolkit Power BI documentation, April 2026 v14.

Flux uses the Toolkit reports as an acceptance checklist. It does not embed,
publish, or require Power BI. A native result is equivalent when it answers the
same business question from compatible source data; visual layout and DAX
implementation do not need to be identical.

Status meanings:

- **Native** — available in Flux from governed measures.
- **Partial** — the useful subset supported by current source data is native.
- **Blocked by source data** — requires FOCUS charge-level, price-sheet,
  reservation, invoice, or another feed Flux does not currently receive.
- **Not applicable** — specific to infrastructure Flux does not deploy.

## Cost summary

| Toolkit page | Flux status | Native Flux output or dependency |
|---|---|---|
| Summary | Native | Effective-cost KPIs, period change, daily trend, and top contributors |
| Running total | Native | Daily cost with accumulated period total |
| Charge breakdown | Blocked by source data | Requires FOCUS charge category, pricing category, SKU, and meter grain |
| Services | Native | Daily cost and ranked service breakdown |
| Usage analysis | Blocked by source data | Requires comparable usage quantity and unit |
| Subscriptions | Native | Ranked subscriptions with date and service filters |
| Resource groups | Native | Ranked resource groups joined to live ARG identity |
| Resources | Native | Ranked resource table with type, group, region, and cost |
| Regions | Native | Ranked Azure-region cost breakdown |
| Inventory | Native | Resource count, cost, and cost/resource by resource type |
| Prices | Partial | Governed retail target prices exist; used-product price sheet is not connected |
| Purchases | Blocked by source data | Requires FOCUS purchase charges and reservation transactions |
| Data quality | Partial | Source freshness, scope coverage, currency safety, and history range are native |
| Tags example | Partial | Tag coverage and untagged cost are native; promoted allocation tags require policy |

## Rate optimization

| Toolkit page | Flux status | Native Flux output or dependency |
|---|---|---|
| Summary | Partial | Directional eligible-cost mix and AHB opportunities |
| Total savings | Partial | Advisor, governed valuation, and retail target savings; contracted-price savings unavailable |
| Commitment discount savings | Blocked by source data | Requires FOCUS list/contracted/effective prices |
| Commitment discounts | Partial | Directional cost mix by pricing model |
| Commitment discount utilization | Blocked by source data | Requires reservation detail and unused commitment charges |
| Commitment discount resources | Blocked by source data | Requires charge-level benefit attribution |
| Chargeback | Blocked by source data | Requires charge-level allocation and benefit attribution |
| Reservation recommendations | Native | Azure Advisor reservation recommendations in Opportunities |
| Purchases | Blocked by source data | Requires reservation transactions |
| Hybrid Benefit | Native | Windows and SQL VM review rules with entitlement guardrails |
| Prices | Partial | Explicit VM target retail prices with OS/AHB lineage |
| Data quality | Partial | Native source and valuation lineage; price-sheet completeness unavailable |

## Invoicing and chargeback

| Toolkit page | Flux status | Native Flux output or dependency |
|---|---|---|
| Summary | Partial | Billed-query cost summary, not invoice reconciliation |
| Services | Native | Service cost breakdown |
| Chargeback | Partial | Subscription/resource-group breakdown; organizational allocation policy unavailable |
| Invoice recon (MCA) | Blocked by source data | Requires MCA invoice and FOCUS billing identifiers |
| Invoice recon (EA) | Blocked by source data | Requires EA invoice and FOCUS billing identifiers |
| Purchases | Blocked by source data | Requires purchase charges |
| Prices | Partial | Retail target prices only |
| Tags example | Partial | Native tag coverage; allocation mappings require organizational input |

## Policy and governance

| Toolkit page | Flux status | Native Flux output or dependency |
|---|---|---|
| Summary | Native | Estate inventory, subscriptions, regions, resource types, and tag coverage |
| Policy compliance | Native | Read-only ARG PolicyResources assignment posture by subscription |
| Virtual machines | Native | VM inventory, telemetry coverage, cost, and right-sizing evidence |
| Managed disks | Native | Disk inventory and unattached-disk findings |
| SQL databases | Native | Filterable SQL resource inventory and cost where resource IDs are present |
| Network security groups | Native | Filterable NSG inventory; rule-level posture remains a dedicated enhancement |

## Workload optimization

| Toolkit page | Flux status | Native Flux output or dependency |
|---|---|---|
| Recommendations | Native | Native workload report plus Advisor and Flux Intelligence findings with confidence and valuation |
| Unattached disks | Native | Retirement-candidate report including disks, snapshots, network assets, gateways, availability sets, NSGs, and paid empty infrastructure |

## Data ingestion

The Toolkit report is specific to FinOps Hubs and Azure Data Explorer, which Flux
does not deploy. It is therefore **not applicable**. Flux provides its own
equivalent operational questions through source freshness, per-scope cost-history
runs, telemetry runs, checkpoints, failure retention, and database backups.

## Acceptance rules

1. Currency is always selected explicitly before costs are aggregated. The
   majority currency (highest total sum) is selected via `GROUP BY currency
   ORDER BY sum(amount) DESC LIMIT 1`, not `arg_max(currency, amount)` which
   could pick a minority currency from a single large charge.
2. Actual, amortized/effective, retail, contracted, list, and forecast values
   remain separate measures with source lineage.
3. Missing FOCUS or price-sheet fields are shown as unavailable, never inferred.
4. Every forecast and recommendation exposes its method version and evidence age.
5. Native reports are validated with deterministic database and API tests.
6. New Toolkit releases are reviewed against this matrix before parity status
   changes.
7. Reports exclude the most recent `FLUX_COST_ANOMALY_LATENCY_DAYS` (default 2)
   billing days when the caller has not set an explicit `endDate`, because
   Azure Cost Management finalizes billing data 24–48 hours late. The
   overview daily-cost trend also excludes the past 2 days.

## Native capability contracts

This table is the canonical implementation checklist behind every **Native** or
**Partial** classification above. A page cannot be promoted to Native unless its
measures, filters, drilldowns, export, lineage, and deterministic validation are
all represented here.

| Contract | Toolkit questions covered | Approved measures | Filters | Drilldown and export | Validation |
|---|---|---|---|---|---|
| `cost-summary` | Summary, running total, services, subscriptions, resource groups, resources, regions, inventory, tags | Total, previous-period total, delta, delta %, average daily, running total, actual, amortized, top movers, 30-day and calendar-month forecast bounds | Cost type, currency, period, subscription, service, resource | Contributor charts → resource filter; complete resource CSV | Currency-safe report, comparison, mover, forecast, and catalog tests |
| `cost-anomalies` | Data quality and abnormal-spend investigation | Current, previous week, seasonal median, MAD score, absolute and percent change | Cost type, scope, subscription, service, severity, signal and review status | Subscription → service contributors; service → resource contributors; anomaly CSV and deterministic change-request pack | Mature/warm-up, spike/quiet, contributor, workflow, and export tests |
| `workload-optimization` | Recommendations, total savings, reservation recommendations, Hybrid Benefit, unattached resources | Gross and risk-adjusted monthly value, confidence, age, telemetry readiness, cost exposure | Source, category, kind, subscription, resource type, confidence | Current candidate → change-request evidence; retirement CSV | Confidence, valuation, pricing, telemetry, retirement, and catalog tests |
| `governance-posture` | Policy compliance, VM/disk/SQL/NSG posture | Evaluated, compliant, non-compliant, exempt, unknown, compliance % | Subscription, assignment, compliance state | Assignment → definition/resource state and exemption; read-only API | Latest-complete snapshot, resource drilldown, and catalog tests |
| `rate-optimization` | Directional commitment mix, AHB, reservation recommendations | Eligible, covered, uncovered, directional coverage, governed savings | Subscription, service, pricing model, meter eligibility | Eligible meter and recommendation evidence; no purchase action | Meter eligibility, AHB attribution, and currency-lineage tests |

Rows marked **Blocked by source data** remain acceptance requirements, not
implementation failures. They move to Native only after Flux receives the
required FOCUS charge, invoice, commitment-utilization, or contracted-price
feed; missing fields are never inferred.

## Release review procedure

1. Run `scripts/check_finops_toolkit_drift.py`.
2. Review every changed dataset, measure, rule, and report-feature file.
3. Update the page inventory and native capability contract together.
4. Add or update deterministic comparison fixtures.
5. Change a status only in the reviewed code change; drift tooling never imports
   upstream behavior automatically.
