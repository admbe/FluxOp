# Microsoft FinOps Toolkit upstream governance

Last reviewed: 2026-08-09
Implementation: `api/finops_toolkit.py` (`TOOLKIT_VERSION = v14`, commit `f3b1b23`)

Flux pins compatible Microsoft FinOps Toolkit open data to a reviewed release,
commit, and SHA-256 checksum in `api/finops_toolkit.py`. Runtime synchronization
will not accept a changed file. Checksum-pinned datasets:

| Dataset | File | SHA-256 (prefix) |
|---|---|---|
| Services | `Services.csv` | `641546dd…` |
| ResourceTypes | `ResourceTypes.csv` | `eaa24add…` |
| Regions | `Regions.csv` | `f16205cf…` |
| PricingUnits | `PricingUnits.csv` | `b8d31126…` |
| CommitmentDiscountEligibility | `CommitmentDiscountEligibility.csv` | `9a62314a…` |

Run the read-only drift report:

```powershell
python .\scripts\check_finops_toolkit_drift.py
```

Use `--json` for automation and `--fail-on-drift` for a non-zero review gate.
The checker reports a new release, a moved tag, or changed pinned dataset. It
does not download into Flux storage, alter checksums, import data, or execute
upstream code.

When drift is detected:

1. Review Microsoft release notes and the MIT-licensed source changes.
2. Re-run the parity review in `docs/REPORTING-PARITY.md`.
3. Validate schema and meaning changes for every dataset.
4. Update the pinned version, commit, and checksums in a reviewed pull request.
5. Run the complete unit, frontend, and application smoke-test suite.

## Two-plane placement

Toolkit reference tables (`finops_toolkit_*`) are analytical (DuckDB); import provenance and version metadata ride alongside them. The operational plane holds only the drift-review outcome (whether a pull request has accepted the new pin) — there is no automatic operational write on drift detection.

## Reports that depend on this

Rate-optimization eligible-cost mix, coverage, and AHB eligibility review use Toolkit data joined to `daily_cost_history` at query time. A stale or failed Toolkit import surfaces as `directional` coverage rather than a silent zero. See `REPORTING-PARITY.md` for the full Native/Partial/Blocked matrix.
