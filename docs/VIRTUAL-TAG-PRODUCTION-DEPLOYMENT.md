# Virtual-tag production deployment

Last reviewed: 2026-08-09
Operational state: `virtual_tag_overrides` / `virtual_tag_rule_audit` on the operational plane

## Current state

`Tagging-Effort/dc2a_virtual_tag_overrides.json` contains 20,848 approved override values across 3,364 resources. The API limits one import request to 20,000 values, so the deployment script uses 5,000-value chunks by default.

The workspace also contains three prior rollback records showing earlier production batches of 6,950, 6,950, and 6,948 applied values. Do not reapply the payload until the production read-back or import history confirms which values are already current. The import is an idempotent upsert, but reapplying it can overwrite newer manual/imported values and creates a more complicated rollback history.

### Two-plane note

Virtual-tag overrides are operational state (PostgreSQL / operational DuckDB), not analytical evidence. Restoring an analytical snapshot does not roll back overrides; only an explicit `overrides/rollback` call or a new import does. Conversely, an analytical snapshot publish does not need to copy override rows — they are already durably stored on the operational plane.

## Safe procedure

1. Confirm the payload is the approved file and inspect its counts:

   ```powershell
   .\scripts\deploy_virtual_tags.ps1 `
     -Payload .\Tagging-Effort\dc2a_virtual_tag_overrides.json
   ```

   This is the default dry run and makes no production request.

2. Use an admin bearer token without putting it in the script or source control:

   ```powershell
   $env:FLUX_ACCESS_TOKEN = '<short-lived Flux admin bearer token>'
   ```

3. Apply with a new, uniquely named rollback file:

   ```powershell
   .\scripts\deploy_virtual_tags.ps1 `
     -Apply `
     -RollbackFile .\Tagging-Effort\virtual-tag-rollback-2026-08-09.jsonl `
     -OutcomeFile .\Tagging-Effort\virtual-tag-outcomes-2026-08-09.csv
   ```

   The script verifies Flux admin permissions, validates duplicate resource/tag keys, applies chunks, and writes the exact previous value/source plus the expected newly applied value after each successful chunk.

4. Validate representative resources through Inventory and the effective-tag endpoint before changing reporting allocation:

   ```
   GET /api/virtual-tags/effective?resourceId=<id>
   GET /api/reports/virtual-tags?dimension=<key>
   ```

5. Roll back only if required:

   ```powershell
   .\scripts\deploy_virtual_tags.ps1 `
     -Rollback `
     -RollbackFile .\Tagging-Effort\virtual-tag-rollback-2026-08-09.jsonl `
     -OutcomeFile .\Tagging-Effort\virtual-tag-rollback-outcomes-2026-08-09.csv
   ```

## Rollback behavior

Rollback restores the prior value and source. If a value did not exist before the import, rollback deletes the override. The API uses an optimistic concurrency guard: if somebody has changed a value since deployment, that item is reported as a conflict and is not overwritten. Resolve conflicts manually after review.

This process changes Flux virtual-tag metadata only (operational plane). It does not write native Azure tags and does not change Azure resources. Analytical read replicas and snapshots need no invalidation — effective tags are re-evaluated at query time.

## Operational notes

- **Build:** `2.0.0` + `ca66a85` (`version.json` → `settings.build_commit`).
- **Pipeline safety-net:** `azure-pipelines.yml` additive settings reapplies `FLUX_HOST=0.0.0.0`, `FLUX_PORT=8000`, `WEBSITE_SKIP_RUNNING_KUDUAGENT=false`, `FLUX_AUTH_MODE=entra` — deploy after a settings wipe self-heals the web/scheduler.
- **Ports:** dev `8765` / Rill `8786` / prod `8000`.

## Verification checklist

- [ ] `GET /api/virtual-tags/dimensions` returns the expected dimensions
- [ ] Spot-check `GET /api/virtual-tags/effective?resourceId=...` for 3–5 resources per dimension
- [ ] `GET /api/reports/virtual-tags?dimension=BusinessRegion` shows expected classified/unclassified totals
- [ ] Ask Flux `get_virtual_tag_showback` returns the same totals (same governing contract)
- [ ] Rollback file contains one JSONL line per applied override with correct `previousValue`/`previousSource`
