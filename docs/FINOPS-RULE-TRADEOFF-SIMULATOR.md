# FinOps rule trade-off simulator

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
