# Azure Cost Management API throttling and Flux scheduling policy

**Status:** Implemented for Query and Cost Details request paths; Export execution remains pending
**Scope:** Azure Cost Management Query API, Cost Details API, and Exports used by Flux
**Owner:** Flux FinOps platform
**Last reviewed:** 2026-08-04

## Executive summary

Flux now spaces Cost Management requests with a tenant-keyed, database-backed rolling quota ledger plus the existing conservative delay. It retries HTTP 429 and 503 responses and honors Microsoft retry headers when they are present.

The ledger reserves capacity across rolling 10-second/1-minute/1-hour windows, persists server cooldowns, and reconciles reservations with Azure's `consumed` QPU response header. The fixed delay remains as a conservative minimum interval and fallback for older or test-only stores.

The target policy is:

1. Prefer cached FOCUS data and scheduled Exports for historical reporting.
2. Use the Query API for bounded freshness checks and targeted recovery, not repeated full-history polling.
3. Put every Cost Management request through one tenant-wide permit ledger.
4. Treat Microsoft response headers as authoritative and honor server-provided retry time exactly.
5. Operate below the published ceiling, leaving headroom for Azure and other consumers.
6. Make scope-level progress visible so a throttled run resumes fairly rather than repeatedly retrying the same subscription.

## Microsoft-published limits and behavior

Microsoft documents Cost Management Query API quotas in **QPU**, or query processing units. The current published tenant quotas are:

| Window | Published quota | Flux operating ceiling (recommended) |
|---|---:|---:|
| 10 seconds | 12 QPU | 6 QPU |
| 1 minute | 60 QPU | 30 QPU |
| 1 hour | 600 QPU | 300 QPU |

The Flux ceilings are deliberately 50% of the published values. They are an operating policy, not an Azure limit. They leave capacity for other applications and for changes in query cost or Microsoft’s accounting.

Microsoft currently describes the QPU calculation as one QPU per month of data queried, but notes that the logic may change and that date range and other factors can affect cost. The response headers are therefore more trustworthy than a local estimate.

The relevant response headers are:

- `x-ms-ratelimit-microsoft.costmanagement-qpu-consumed`
- `x-ms-ratelimit-microsoft.costmanagement-qpu-remaining`
- `x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after`
- `x-ms-ratelimit-microsoft.costmanagement-entity-retry-after`
- `x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after`
- `x-ms-ratelimit-microsoft.consumption-retry-after`
- `Retry-After`

The `retry-after` value supplied for the applicable limit must take precedence over Flux’s local delay. A 429 is not evidence that the request should be retried immediately; it is a server instruction to wait.

Microsoft also recommends calling Cost Management APIs no more than once per day for the same data because Cost Management data refreshes approximately every four hours. More frequent calls generally do not provide newer data and create unnecessary load.

### Cost Details API

Cost Details report generation is asynchronous: Flux creates a report, polls the operation, and downloads the result. Microsoft documents 429 handling through `x-ms-ratelimit-microsoft.consumption-retry-after` and 503 handling through `Retry-After`. A Cost Details request is limited to a maximum one-month period and data no older than 13 months.

Cost Details is appropriate for small, targeted datasets or recovery of a failed scope. For large or recurring month-over-month history, Microsoft recommends Exports because they scale better and avoid repeatedly regenerating the same report.

### Exports

Exports are the preferred ingestion path for recurring historical data at estate scale. Export execution can also return 429 with `x-ms-ratelimit-microsoft.consumption-retry-after`; that value must be honored before the next execution attempt. A custom export period is limited to one calendar month.

## What Flux does today

The current implementation has several good safeguards:

- `SharedRequestGate` uses the operational database to coordinate a named `cost-management` request slot across workers.
- The default request delay is 20 seconds.
- The delay is multiplied by Flux’s estimated QPU cost for a date range.
- HTTP 429 and 503 responses are retried up to the configured maximum.
- Retry headers are checked before exponential fallback.
- A shared cooldown is registered after a throttle response.
- Cost history is queried in 14-day windows, and Cost Details fallback is capped at four reports per run.
- Background cost-history work and ad-hoc synchronization share the Cost Management gate.

The relevant defaults are currently:

```text
cost_management_request_delay_seconds = 20
cost_management_max_retries = 5
cost_management_throttle_cooldown_seconds = 30
cost_history_chunk_days = 14
cost_details_max_reports_per_run = 4
cost_history_refresh_days = 14
```

### What the current delay means

If a request really costs one QPU, a 20-second minimum spacing allows at most about three requests per minute, or 180 QPU per hour. That is below Microsoft’s 60 QPU/minute and 600 QPU/hour published quotas.

However, that calculation is only safe if all of the following are true:

- every request costs one QPU;
- no other Flux process or external client consumes the same tenant quota;
- all request paths use the same limiter;
- the request does not encounter an entity or client-type limit;
- Microsoft’s QPU accounting remains unchanged.

Those assumptions are not guaranteed. A 12-month query is locally treated as materially more expensive than a short query, and successful Query responses now reconcile that estimate with Azure's QPU headers. Cost Details uses the same tenant gate for its management-plane create and poll calls; Export execution remains an external scheduled operation and still needs to be run through the same policy when automated by Flux.

## Diagnosis of the repeated-throttling problem

The current design is now **quota-aware** for Flux's Query and Cost Details request paths. Export execution remains the next integration point if Flux begins creating or executing exports itself.

The main gaps are:

1. **Export execution is outside the application gate.** The PowerShell provisioning script is not a recurring Flux request worker and does not yet reserve from the operational ledger.
2. **Header persistence is scoped to request-attempt and sync telemetry.** A dedicated quota dashboard still needs to expose the tenant ledger and rolling remaining capacity.
3. **Fixed-delay conservatism remains.** The 20-second delay is still applied as a minimum interval, so the ledger is safe but not yet optimized for maximum throughput.
4. **Multiple work classes.** Background history, ad-hoc synchronization, Cost Details recovery, and Exports can compete for the same tenant’s service capacity. A request gate alone is not a complete priority scheduler.
5. **Repeated historical polling.** Historical data is refreshed more often than Microsoft’s once-per-day recommendation unless cache freshness prevents the request.
6. **Backfill bursts.** Initial 90-day backfills, 14-day windows, 13-month monthly queries, and multiple cost types can produce a large queue after a deployment or failed run.
7. **Unfair retry risk.** A repeatedly failing head-of-queue scope can delay every other missing subscription unless the work queue records next-eligible time and rotates fairly.

The five direct subscription checks that returned HTTP 429 are consistent with the original diagnosis. They should not be interpreted as proof that Flux exceeded the published numbers by itself; they prove why header-driven cooldowns and a shared tenant ledger are necessary.

## Required scheduling policy

### 1. One tenant-wide permit ledger

All Cost Management Query, Cost Details, and Export work should pass through a durable scheduler keyed by Azure tenant. The scheduler should track:

- request type and endpoint;
- subscription/scope;
- estimated QPU before dispatch;
- actual QPU consumed and remaining after response;
- server retry-after and next eligible time;
- 10-second, 1-minute, and 1-hour reservations;
- owner/worker lease and attempt count.

The database-backed gate now stores rolling reservations and server cooldown state in `cost_management_quota_state`. It is keyed by tenant so background history, ad-hoc synchronization, and Cost Details recovery contend for the same capacity.

### 2. Conservative operating ceiling

Flux currently uses the following conservative operating ceiling:

- no more than 6 estimated QPU per 10 seconds;
- no more than 30 estimated QPU per minute;
- no more than 300 estimated QPU per hour;
- one active Cost Management request per tenant at a time;
- optional 10–30% randomized jitter on normal scheduling delays (not yet enabled; the existing 20-second minimum remains the conservative default).

This is intentionally conservative. The scheduler may dispatch sooner only when the persisted headers and rolling ledger show capacity. The ceiling must never be confused with Microsoft’s published quota.

### 3. Header-driven backoff

When Azure returns 429:

1. Stop dispatching new work for the affected tenant or limit, depending on the header returned.
2. Persist the exact server-provided retry duration and next eligible timestamp.
3. Honor the longer of the applicable server delay and the local rolling-window delay.
4. Requeue the scope with exponential attempt metadata and fair rotation.
5. Do not consume another retry until the persisted timestamp has passed.

When Azure returns 503, honor `Retry-After`. For 401/403, fail fast and open an access/configuration issue; retries will not repair authorization. For a validation 400, record the request and stop retrying it.

The local maximum retry delay should not truncate an explicit server instruction. If an operational watchdog is needed, it should mark the run as waiting and resume later rather than retrying early.

### 4. Work priorities

The recommended order is:

1. Current-period coverage repair for scopes missing from the report.
2. Scheduled Export ingestion and its download/poll operations.
3. Freshness checks for already-covered scopes.
4. Historical backfill.
5. Cost Details fallback for scopes whose Query/Export path failed.
6. Ad-hoc or manually requested full synchronization.

Ad-hoc synchronization should be rate-limited or queued behind the same tenant scheduler; it must not bypass the background worker’s budget.

### 5. Cache and idempotency rules

- Do not query the same scope, cost type, and date window again while a successful result is inside its freshness period.
- Cache completed historical invoice periods permanently or until an explicit correction is requested.
- Query the current period at most once per day unless the user explicitly requests a refresh.
- Keep 14-day chunks for recovery if needed, but do not use them as a reason to repeatedly re-read settled history.
- Prefer one monthly Export/FOCUS ingestion path over many per-subscription Query calls where the source data supports it.

### 6. Fair scope rotation

The queue should use round-robin or weighted fair scheduling across subscriptions and cost types. A scope that returns 429 should move to its next eligible time; it should not remain at the head of the queue. Each run should record:

- scopes attempted and completed;
- scopes deferred because of throttling;
- scopes that failed authorization;
- next scheduled retry;
- coverage gained by the run.

## Observability and alerting

The following should be visible in logs, operational tables, and eventually the admin UI:

- request count by endpoint, tenant, subscription, and client type;
- estimated QPU and actual `qpu-consumed`;
- latest `qpu-remaining` for each quota window when supplied;
- 429 count and the header that caused the pause;
- 503 count and `Retry-After`;
- current tenant cooldown and next eligible timestamp;
- queue depth and oldest queued scope;
- successful cost-history coverage by subscription and month;
- time since last successful refresh;
- Cost Details and Export operation age/poll count.

Alert when:

- any tenant has three consecutive 429s;
- a cooldown exceeds 15 minutes;
- a scope has been deferred for more than 24 hours;
- coverage falls below the configured estate target;
- actual QPU consumption is materially higher than the local estimate;
- the same scope repeatedly fails while other scopes are healthy.

## Operational runbook

When throttling is observed:

1. Confirm the tenant, endpoint, response status, and retry header.
2. Check the persisted request attempts and shared throttle state.
3. Stop or pause nonessential backfill and ad-hoc synchronization.
4. Allow the server-provided cooldown to expire.
5. Resume with current-period coverage first and rotate scopes fairly.
6. Inspect whether another client or pipeline is consuming the tenant budget.
7. If 429s continue after the conservative ceiling is applied, reduce concurrency further and open a Microsoft support case with timestamps, request IDs, endpoint, and headers.

Do not solve throttling by blindly increasing retries. That increases load and can delay recovery for every subscription sharing the quota.

## Implementation acceptance criteria

The throttling work should be considered complete when:

- every Cost Management request path uses the same tenant-aware scheduler;
- response QPU and retry headers are persisted;
- rolling 10-second, 1-minute, and 1-hour budgets are enforced;
- explicit server retry times are never shortened;
- historical reads are cache-aware and normally no more frequent than daily;
- throttled scopes are requeued fairly;
- the UI/report can explain queue state, last successful refresh, and next retry;
- automated tests cover concurrent workers, 429 with each retry header, 503, missing headers, and restart during cooldown.

## Microsoft references

- [Manage Azure costs with automation](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/manage-automation) — refresh cadence, Query API QPU quotas, QPU headers, and retry guidance.
- [Query - Usage REST API](https://learn.microsoft.com/en-us/rest/api/cost-management/query/usage?view=rest-cost-management-2025-03-01) — Query API response and 429/503 retry behavior.
- [Review subscription billing data with REST API](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/review-subscription-billing) — asynchronous Cost Details workflow and refresh behavior.
- [Cost details best practices](https://learn.microsoft.com/en-us/azure/cost-management-billing/automate/usage-details-best-practices) — caching, daily freshness guidance, chunking, and when to use Exports.
- [Exports - Execute REST API](https://learn.microsoft.com/en-us/rest/api/cost-management/exports/execute?view=rest-cost-management-2025-03-01) — Export execution and throttling response.
- [Generate Cost Details operation results](https://learn.microsoft.com/en-us/rest/api/cost-management/generate-cost-details-report/get-operation-results?view=rest-cost-management-2025-03-01) — Cost Details limits and retry behavior.

Microsoft can change quotas and accounting behavior. This document should be reviewed whenever the API version, ingestion strategy, or Azure Cost Management guidance changes.
