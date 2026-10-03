# FinOps rule trade-off simulator

Last reviewed: 2026-08-09  
Status note: simulator removed 2026-08-04; doc retained as removal record.

## Status

The simulator has been removed from the active Flux site and API surface as of 2026-08-04. The previous implementation remains recoverable in git history at commit `10b4fe1c9ce8ec1ff2308cd027874bbe1e407fb` (`feat: add dynamic financial rule simulator`).

## What was implemented

The planning report contained a review-only card with two local sliders:

- stale evidence limit, from 1 to 90 days;
- disk IOPS p95 review limit, from 0 to 200 IOPS.

The frontend sent those values to `GET /api/reports/rule-simulator`. The backend recalculated three categories from current data: compute sizing, orphaned public IPs, and low-IOPS unattached disks. It returned candidate counts and modeled monthly savings. No remediation or persisted policy change was possible through this feature.

## Failure observed

Moving a lever did not reliably change the displayed price. The request wiring existed, but the result was not guaranteed to be sensitive to the selected value:

1. The stale-days lever only changed items whose observed/computed age crossed the selected boundary.
2. The IOPS lever only changed disks with both required current telemetry metrics and a summed p95 value inside the selected threshold. Missing telemetry was excluded.
3. The chart displayed the returned total without explaining when no candidates crossed a boundary.
4. There was no frontend interaction test or backend contract test asserting that meaningful lever changes produce a changed result when fixture data supports it.
5. There was no explicit data coverage, freshness, or “unchanged because no candidates crossed the threshold” state in the card.

Consequently, the control could look broken even when the API request completed successfully, and it was not at the current Flux standard for governed planning experiences.

## Why it was removed

The surface presented an apparently dynamic savings model without enough evidence and interaction transparency to support a planning or executive conversation. Leaving it visible would create more confusion than value, especially because the total could appear invariant while the underlying candidate set remained unchanged.

## Preserved progress

The original implementation is preserved in git history at the commit listed above. The current removal deletes the active frontend card, client method, type, CSS, backend route, and database method. Existing opportunity, rightsizing, telemetry, and financial-planning capabilities are unaffected.

## Two-plane note

The removed simulator read only analytical snapshots (`opportunity_*`, `telemetry_metric_summaries`); it never wrote to the operational plane and was therefore safe to remove without migration. Any reintroduction should follow the same read-only analytical pattern.

## Requirements before reintroduction

Any replacement should be built as a governed scenario-planning component, not as a thin threshold demo. At minimum it should:

- show the selected inputs, affected candidate counts, and savings delta from the current baseline;
- identify which resources crossed each threshold and why;
- disclose data age, telemetry coverage, currency, and exclusions;
- debounce and cancel in-flight recalculations;
- distinguish “no candidates crossed” from loading, error, and unavailable evidence;
- have API contract tests proving sensitivity for each lever with deterministic fixtures;
- have browser tests moving each control and asserting a changed result where fixture data warrants it;
- keep the review-only posture explicit and never imply authorization to remediate;
- use the same export, audit, and scenario-assumption standards as the rest of Financial planning.

The simulator should return only after those requirements are implemented and reviewed against the current planning UI patterns.

## Operational notes

- **Build:** `2.0.0` + `ca66a85` (`version.json` → `settings.build_commit`).
- **Pipeline safety-net:** `azure-pipelines.yml` reapplies `FLUX_HOST=0.0.0.0`, `FLUX_PORT=8000`, `WEBSITE_SKIP_RUNNING_KUDUAGENT=false`, `FLUX_AUTH_MODE=entra` on every deploy (2026-08-08 wipe-class fix, commit `72bfcdd`).
- **Ports:** dev `8765` / Rill `8786` / prod `8000`; when restored, the simulator must read from analytics snapshots (`FLUX_ANALYTICS_SNAPSHOT_MODE=snapshot`) like every other planning surface.
