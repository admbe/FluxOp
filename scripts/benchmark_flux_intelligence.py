from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import time
from typing import Any

from api.config import ROOT, Settings
from api.database import FluxDatabase
from api.intelligence_assistant import IntelligenceAssistant


def load_cases(path: Path, limit: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload["cases"])[:limit]


def run_profile(
    assistant: IntelligenceAssistant,
    profile: str,
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    outcomes = []
    for case in cases:
        started = time.monotonic()
        try:
            response = assistant.chat(
                messages=[{"role": "user", "content": case["prompt"]}],
                context={"page": "intelligence", "evaluationCase": case["id"]},
                model_profile=profile,
                session={
                    "user": {
                        "id": "intelligence-benchmark",
                        "tenantId": "offline-evaluation",
                    }
                },
            )
            source_tools = {
                item.get("tool") for item in response.get("sources", [])
            }
            expected = set(case.get("expectedTools") or [])
            outcomes.append(
                {
                    "id": case["id"],
                    "ok": True,
                    "latencySeconds": round(time.monotonic() - started, 3),
                    "expectedToolCoverage": (
                        1.0 if not expected else len(expected & source_tools) / len(expected)
                    ),
                    "hasLimitations": bool(response.get("limitations")),
                }
            )
        except Exception as error:
            outcomes.append(
                {
                    "id": case["id"],
                    "ok": False,
                    "latencySeconds": round(time.monotonic() - started, 3),
                    "error": type(error).__name__,
                }
            )
    successful = [item for item in outcomes if item["ok"]]
    return {
        "profile": profile,
        "cases": len(outcomes),
        "successful": len(successful),
        "successRate": round(len(successful) / max(1, len(outcomes)), 3),
        "medianLatencySeconds": round(
            statistics.median(item["latencySeconds"] for item in outcomes), 3
        ),
        "meanExpectedToolCoverage": round(
            statistics.mean(
                item["expectedToolCoverage"] for item in successful
            ) if successful else 0,
            3,
        ),
        "outcomes": outcomes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Flux Intelligence benchmark without retaining prompts or responses."
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "evaluations" / "flux-intelligence.json",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--profiles",
        default="fast,benchmark",
        help="Comma-separated model profiles: fast,benchmark",
    )
    arguments = parser.parse_args()
    if not os.getenv("FLUX_DEEPSEEK_API_KEY"):
        raise SystemExit("FLUX_DEEPSEEK_API_KEY must be supplied through the environment.")
    settings = Settings(
        intelligence_ai_enabled=True,
        database_path=Path(
            os.getenv(
                "FLUX_BENCHMARK_DUCKDB_PATH",
                str(ROOT / "data" / "flux-benchmark.duckdb"),
            )
        ),
    )
    database = FluxDatabase(settings.database_path)
    database.init()
    assistant = IntelligenceAssistant(database, settings)
    cases = load_cases(arguments.cases, max(1, min(arguments.limit, 50)))
    result = {
        "evaluationVersion": json.loads(
            arguments.cases.read_text(encoding="utf-8")
        )["version"],
        "note": (
            "Only aggregate outcomes are emitted. Prompt, response, and "
            "reasoning content are not written."
        ),
        "profiles": [
            run_profile(assistant, profile.strip(), cases)
            for profile in arguments.profiles.split(",")
            if profile.strip() in {"fast", "benchmark"}
        ],
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
