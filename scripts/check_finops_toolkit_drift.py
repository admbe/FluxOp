"""Read-only Microsoft FinOps Toolkit upstream drift check.

This intentionally reports changes without importing or executing upstream
content. Updates remain a reviewed code change with new checksums and tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.finops_toolkit import DATASETS, TOOLKIT_COMMIT, TOOLKIT_VERSION  # noqa: E402


def _get_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "FluxFinOps/ToolkitDrift"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _sha256_url(url: str) -> str:
    digest = hashlib.sha256()
    request = Request(url, headers={"User-Agent": "FluxFinOps/ToolkitDrift"})
    with urlopen(request, timeout=120) as response:
        while block := response.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()

def classify_artifact(path: str) -> str:
    value = path.lower().replace("\\", "/")
    if "open-data" in value or value.endswith((".csv", ".parquet")):
        return "dataset"
    if any(token in value for token in ("measure", ".dax", "semantic-model")):
        return "measure"
    if any(token in value for token in ("recommendation", "/rules/", "rule.")):
        return "rule"
    if any(token in value for token in ("power-bi", "powerbi", "/reports/", "workbook")):
        return "report-feature"
    return "supporting"


def check() -> dict[str, Any]:
    release = _get_json(
        "https://api.github.com/repos/microsoft/finops-toolkit/releases/latest"
    )
    latest_version = str(release.get("tag_name") or "")
    commit = _get_json(
        "https://api.github.com/repos/microsoft/finops-toolkit/commits/"
        f"{TOOLKIT_VERSION}"
    )
    pinned_tag_commit = str(commit.get("sha") or "")
    comparison = (
        _get_json(
            "https://api.github.com/repos/microsoft/finops-toolkit/compare/"
            f"{TOOLKIT_COMMIT}...{latest_version}"
        )
        if latest_version
        else {}
    )
    artifact_changes = [
        {
            "path": str(item.get("filename") or ""),
            "status": str(item.get("status") or "changed"),
            "category": classify_artifact(str(item.get("filename") or "")),
            "changes": int(item.get("changes") or 0),
        }
        for item in comparison.get("files") or []
    ]
    datasets = []
    for dataset in DATASETS:
        actual = _sha256_url(dataset.source_url)
        datasets.append(
            {
                "name": dataset.name,
                "sourceUrl": dataset.source_url,
                "expectedSha256": dataset.sha256,
                "actualSha256": actual,
                "changed": actual != dataset.sha256,
            }
        )
    return {
        "project": "microsoft/finops-toolkit",
        "pinnedVersion": TOOLKIT_VERSION,
        "pinnedCommit": TOOLKIT_COMMIT,
        "pinnedTagCommit": pinned_tag_commit,
        "latestVersion": latest_version,
        "newReleaseAvailable": bool(
            latest_version and latest_version != TOOLKIT_VERSION
        ),
        "pinnedTagMoved": bool(
            pinned_tag_commit and pinned_tag_commit != TOOLKIT_COMMIT
        ),
        "datasets": datasets,
        "artifactChanges": artifact_changes,
        "artifactChangeCounts": {
            category: sum(
                1 for item in artifact_changes
                if item["category"] == category
            )
            for category in (
                "dataset", "measure", "rule", "report-feature", "supporting"
            )
        },
        "comparisonTruncated": len(artifact_changes) >= 300,
        "reviewRequired": (
            latest_version != TOOLKIT_VERSION
            or pinned_tag_commit != TOOLKIT_COMMIT
            or any(item["changed"] for item in datasets)
            or bool(artifact_changes)
        ),
        "policy": (
            "Report only. Never auto-import a new release or changed dataset."
        ),
    }


def markdown(result: dict[str, Any]) -> str:
    lines = [
        "# FinOps Toolkit upstream drift",
        "",
        f"- Pinned release: `{result['pinnedVersion']}`",
        f"- Latest release: `{result['latestVersion'] or 'unavailable'}`",
        f"- Pinned commit: `{result['pinnedCommit']}`",
        f"- Current tag commit: `{result['pinnedTagCommit'] or 'unavailable'}`",
        f"- Review required: **{'yes' if result['reviewRequired'] else 'no'}**",
        "",
        "| Dataset | Checksum |",
        "|---|---|",
    ]
    lines.extend(
        f"| {item['name']} | {'changed' if item['changed'] else 'verified'} |"
        for item in result["datasets"]
    )
    lines.extend(
        [
            "",
            "## Review checklist",
            "",
            "| Category | Changed files |",
            "|---|---:|",
            *[
                f"| {category} | {count} |"
                for category, count in result["artifactChangeCounts"].items()
            ],
            "",
            *[
                f"- [ ] `{item['category']}` · {item['status']} · "
                f"`{item['path']}`"
                for item in result["artifactChanges"]
                if item["category"] != "supporting"
            ],
            "",
            (
                "> GitHub returned its maximum comparison page; review the "
                "upstream diff directly for additional files."
                if result["comparisonTruncated"]
                else ""
            ),
            "",
            result["policy"],
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Return exit code 2 when review is required.",
    )
    args = parser.parse_args()
    result = check()
    print(json.dumps(result, indent=2) if args.json else markdown(result))
    return 2 if args.fail_on_drift and result["reviewRequired"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
