# Financial planning forecast method

## Primary method

Financial Planning now uses `post-migration-run-rate-v1` as the executive fiscal-year forecast.

The forecast:

- carries completed months through as actuals;
- estimates the in-progress month from the current-month estimate when available;
- uses the average of the three most recent complete months as the post-migration run-rate for future months;
- applies the user-recorded monthly growth assumption, if any;
- subtracts planned right-sizing savings only when explicitly enabled in assumptions;
- calculates uncertainty bands from backtesting the same trailing-mean rule and widens them with forecast distance.

This is intentionally not a “before cloud migration versus now” comparison. It estimates the estate’s current operating trajectory from the period after the migration, which is the decision-relevant baseline for future rightsizing, reservations, savings plans, and other approved actions.

## Seasonal comparison

The former method is retained as `seasonal-yoy-comparison-v1`. It uses the same month from the prior year, scaled by a trailing year-over-year factor, with a trailing-mean fallback. It is returned as `seasonalComparison` for context only and is not used for the executive total, chart, budget variance, or planning pulse.

The UI labels this comparison as “not used for executive forecast,” and the exported assumptions sheet includes its FY total as a reference value.

## Why the change was made

The prior model could compare current cloud months with periods before the data-center migration. That created large artificial declines in months whose prior-year values represented a different operating model, making the forecast difficult to explain in an IT Leadership meeting. A trailing post-migration run-rate is more honest while the estate is still establishing a stable cloud baseline.

## Limitations and review points

The run-rate is not a causal savings forecast. It does not claim that spending will fall without an approved action. It also remains sensitive to an incomplete cost-history backfill, migration activity in the trailing window, one-time projects, and the selected monthly growth or planned-savings assumptions. Coverage warnings and recorded assumptions remain part of the forecast response and export.
