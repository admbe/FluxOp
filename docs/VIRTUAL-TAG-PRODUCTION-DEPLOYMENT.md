# Virtual-tag production deployment

## Current state

`Tagging-Effort/dc2a_virtual_tag_overrides.json` contains 20,848 approved override values across 3,364 resources. The API limits one import request to 20,000 values, so the deployment script uses 5,000-value chunks by default.

The workspace also contains three prior rollback records showing earlier production batches of 6,950, 6,950, and 6,948 applied values. Do not reapply the payload until the production read-back or import history confirms which values are already current. The import is an idempotent upsert, but reapplying it can overwrite newer manual/imported values and creates a more complicated rollback history.

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
     -RollbackFile .\Tagging-Effort\virtual-tag-rollback-2026-08-04.jsonl `
     -OutcomeFile .\Tagging-Effort\virtual-tag-outcomes-2026-08-04.csv
   ```

   The script verifies Flux admin permissions, validates duplicate resource/tag keys, applies chunks, and writes the exact previous value/source plus the expected newly applied value after each successful chunk.

4. Validate representative resources through Inventory and the effective-tag endpoint before changing reporting allocation.

5. Roll back only if required:

   ```powershell
   .\scripts\deploy_virtual_tags.ps1 `
     -Rollback `
     -RollbackFile .\Tagging-Effort\virtual-tag-rollback-2026-08-04.jsonl `
     -OutcomeFile .\Tagging-Effort\virtual-tag-rollback-outcomes-2026-08-04.csv
   ```

## Rollback behavior

Rollback restores the prior value and source. If a value did not exist before the import, rollback deletes the override. The API uses an optimistic concurrency guard: if somebody has changed a value since deployment, that item is reported as a conflict and is not overwritten. Resolve conflicts manually after review.

This process changes Flux virtual-tag metadata only. It does not write native Azure tags and does not change Azure resources.
