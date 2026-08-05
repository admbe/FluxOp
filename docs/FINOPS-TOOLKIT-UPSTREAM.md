# Microsoft FinOps Toolkit upstream governance

Flux pins compatible Microsoft FinOps Toolkit open data to a reviewed release,
commit, and SHA-256 checksum in `api/finops_toolkit.py`. Runtime synchronization
will not accept a changed file.

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
