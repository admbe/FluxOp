"""DC2A metadata-enrichment worksheet builder and override packager.

Stage 1 (build): merge a Flux inventory CSV export with the DC2A/Copilot
ownership dataset batches into one reviewable worksheet -- one row per
Azure resource, native state preserved, proposals populated only where a
batch row matches, every proposed value carrying its source and
confidence, conflicts and unmatched rows flagged for review.

Stage 2 (overrides): turn APPROVED worksheet rows into a virtual-tag
override payload for Flux (POST /api/virtual-tags/overrides/import).
Virtual tags first, by design: nothing touches native Azure tags here.
Native tagging, if later approved, runs through
scripts/apply_native_tags.ps1 which snapshots exact prior state for
rollback.

Usage:
  python scripts/dc2a_enrich.py build --inventory flux-inventory.csv
  python scripts/dc2a_enrich.py overrides
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAGGING = ROOT / "Tagging-Effort"
COPILOT = TAGGING / "#Copilot"
WORKSHEET = TAGGING / "dc2a_enrichment_worksheet.csv"
OVERRIDES_OUT = TAGGING / "dc2a_virtual_tag_overrides.json"

WORKSHEET_COLUMNS = [
    "SubscriptionId", "SubscriptionName", "ResourceGroup", "ResourceName",
    "ResourceId", "ResourceType", "Region", "CurrentTags",
    "ProposedApplication", "ProposedApplicationOwner", "ProposedITOwner",
    "ProposedDepartment", "ProposedRegionClassification",
    "ProposedEnvironment", "ProposedMigrationWave",
    "SourceOfValues", "Confidence", "ValidationStatus", "ValidationNotes",
    "ApprovalStatus", "ChangeStatus", "RollbackValue",
]

# Worksheet proposal column -> (batch column, virtual tag key)
PROPOSAL_MAP = {
    "ProposedApplication": ("ApplicationName", "application"),
    "ProposedApplicationOwner": ("ApplicationOwnerName", "application-owner"),
    "ProposedITOwner": ("ITOwnerName", "it-owner"),
    "ProposedDepartment": ("BusinessDepartment", "department"),
    "ProposedRegionClassification": ("GeographicRegion", "region-classification"),
    "ProposedEnvironment": ("Environment", "environment"),
    "ProposedMigrationWave": ("MigrationWave", "migration-wave"),
}


def _norm_server(value: str) -> str:
    """HCECIS004.limafood.com and hcecis004 must meet in the middle."""
    return (value or "").strip().lower().split(".")[0]


def _pick(row: dict[str, str], *names: str) -> str:
    lowered = {key.strip().lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return str(lowered[name.lower()] or "").strip()
    return ""


def load_batches() -> dict[str, list[dict[str, str]]]:
    by_server: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in sorted(COPILOT.glob("*dataset*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                server = _norm_server(row.get("ServerName", ""))
                azure_name = _norm_server(row.get("AzureResourceName", ""))
                row["_batch"] = path.name
                for key in filter(None, {server, azure_name}):
                    by_server[key].append(row)
    return by_server


def build(inventory_path: Path) -> None:
    batches = load_batches()
    if not batches:
        sys.exit(f"No *dataset*.csv batches found under {COPILOT}")
    rows_out: list[dict[str, str]] = []
    matched = conflict = 0
    with inventory_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = _pick(row, "name", "resource name", "resourceName")
            record = {
                "SubscriptionId": _pick(row, "subscriptionId", "subscription id"),
                "SubscriptionName": _pick(
                    row, "subscriptionName", "subscription", "subscription name"
                ),
                "ResourceGroup": _pick(row, "resourceGroup", "resource group"),
                "ResourceName": name,
                "ResourceId": _pick(row, "resourceId", "resource id"),
                "ResourceType": _pick(row, "resourceType", "type", "resource type"),
                "Region": _pick(row, "region", "location"),
                "CurrentTags": _pick(row, "tags", "tags json", "tagsJson"),
                "ApprovalStatus": "",
                "ChangeStatus": "",
                "RollbackValue": "",
            }
            candidates = batches.get(_norm_server(name), [])
            if not candidates:
                record.update(
                    {
                        "ValidationStatus": "unmatched",
                        "ValidationNotes": "No DC2A batch row for this name.",
                        "SourceOfValues": "",
                        "Confidence": "",
                    }
                )
                for column in PROPOSAL_MAP:
                    record[column] = ""
                rows_out.append(record)
                continue
            notes: list[str] = []
            sources: set[str] = set()
            confidences: set[str] = set()
            has_conflict = False
            for column, (batch_column, _tag) in PROPOSAL_MAP.items():
                values = {
                    value
                    for value in (
                        _pick(item, batch_column) for item in candidates
                    )
                    if value and value.lower() != "unknown"
                }
                if len(values) > 1:
                    has_conflict = True
                    notes.append(
                        f"{column}: conflicting values {sorted(values)}"
                    )
                    record[column] = ""
                else:
                    record[column] = next(iter(values), "")
            for item in candidates:
                source = _pick(item, "SourceDocumentOrConversation")
                if source:
                    sources.add(f"{source} [{item['_batch']}]")
                level = _pick(item, "Confidence")
                if level:
                    confidences.add(level)
                if _pick(item, "ReviewRequired").lower() == "yes":
                    review_note = _pick(item, "ReviewNotes")
                    if review_note:
                        notes.append(f"Review: {review_note}")
            order = {"low": 0, "medium": 1, "high": 2}
            confidence = min(
                confidences, key=lambda item: order.get(item.lower(), 0),
                default="",
            )
            record.update(
                {
                    "ValidationStatus": "conflict" if has_conflict else "matched",
                    "ValidationNotes": "; ".join(notes)[:500],
                    "SourceOfValues": "; ".join(sorted(sources))[:500],
                    "Confidence": confidence,
                }
            )
            matched += 1
            conflict += 1 if has_conflict else 0
            rows_out.append(record)
    with WORKSHEET.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKSHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_out)
    print(
        f"Worksheet: {WORKSHEET}\n"
        f"resources={len(rows_out)} matched={matched} conflicts={conflict} "
        f"unmatched={len(rows_out) - matched}\n"
        "Review, set ApprovalStatus=Approved on rows to apply, then run "
        "the overrides stage."
    )


def overrides() -> None:
    if not WORKSHEET.exists():
        sys.exit(f"Worksheet not found: {WORKSHEET}; run build first.")
    payload: list[dict[str, str]] = []
    skipped_low = 0
    with WORKSHEET.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("ApprovalStatus", "").strip().lower() != "approved":
                continue
            if row.get("Confidence", "").strip().lower() == "low":
                skipped_low += 1
                continue
            resource_id = row.get("ResourceId", "").strip()
            if not resource_id:
                continue
            for column, (_batch_column, tag_key) in PROPOSAL_MAP.items():
                value = (row.get(column) or "").strip()
                if not value:
                    continue
                payload.append(
                    {
                        "resourceId": resource_id,
                        "tagKey": tag_key,
                        "tagValue": value,
                        "source": "imported",
                        "note": (
                            f"DC2A enrichment; {row.get('SourceOfValues', '')}"
                        )[:300],
                    }
                )
    OVERRIDES_OUT.write_text(
        json.dumps({"overrides": payload}, indent=2), encoding="utf-8"
    )
    print(
        f"Override payload: {OVERRIDES_OUT} ({len(payload)} values; "
        f"{skipped_low} approved-but-low-confidence rows skipped).\n"
        "Apply with an admin session: POST /api/virtual-tags/overrides/"
        "import with this file's JSON body. The response's 'previous' "
        "array is the rollback record -- save it."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--inventory", required=True, type=Path)
    sub.add_parser("overrides")
    args = parser.parse_args()
    if args.command == "build":
        build(args.inventory)
    else:
        overrides()


if __name__ == "__main__":
    main()
