"""Service-level objectives over the signals the platform already emits.

Pipeline warnings say "something broke"; SLOs say "the service the platform
owes its users is (or is not) being met, and how badly". The definitions
live in code so they version with the platform; thresholds can be tuned per
environment through one JSON setting rather than a spray of variables. The
evaluation and transition logic is pure (values in, decisions out), mirroring
``alerts.py``, so the whole state machine is testable without a store or a
network.

States: ``ok`` -> ``warn`` -> ``breach``, plus ``unknown`` when a measurement
cannot be produced. Transition rules:

- entering ``warn`` or ``breach`` (or worsening) notifies immediately;
- staying in a bad state re-notifies on a slow cadence, so a long breach
  neither spams nor silently ages out;
- returning to ``ok`` notifies recovery exactly once and clears the state;
- ``unknown`` never notifies and never claims recovery -- a broken probe is
  not evidence that the objective is met.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class SloDefinition:
    key: str
    label: str
    description: str
    unit: str
    # "below" means lower values are bad (coverage); "above" means higher
    # values are bad (ages, counts).
    direction: str
    warn: float
    breach: float
    runbook: str


DEFAULT_SLOS: tuple[SloDefinition, ...] = (
    SloDefinition(
        key="cost_coverage_percent",
        label="Cost history coverage",
        description=(
            "Share of expected subscription-days ingested across the "
            "collection window. Every report and anomaly baseline consumes "
            "this data; holes silently understate totals."
        ),
        unit="%",
        direction="below",
        warn=98.0,
        breach=90.0,
        runbook="cost-history-reliability",
    ),
    SloDefinition(
        key="cost_scope_success_percent",
        label="Cost scope success rate",
        description=(
            "Share of configured cost scopes whose latest collection "
            "attempt in the last 7 days succeeded."
        ),
        unit="%",
        direction="below",
        warn=95.0,
        breach=80.0,
        runbook="cost-history-reliability",
    ),
    SloDefinition(
        key="stale_source_count",
        label="Stale data sources",
        description=(
            "Data sources currently degraded or stale. Singles already "
            "raise per-source warnings; this objective catches systemic "
            "staleness."
        ),
        unit="sources",
        direction="above",
        warn=2.0,
        breach=4.0,
        runbook="deployment-and-recovery",
    ),
    SloDefinition(
        key="snapshot_age_hours",
        label="Analytical snapshot currency",
        description=(
            "Age of the newest approved analytical snapshot. Web instances "
            "serve reads from this snapshot; an old one means every user "
            "sees old numbers."
        ),
        unit="h",
        direction="above",
        warn=30.0,
        breach=54.0,
        runbook="analytical-snapshot-operations",
    ),
)


def apply_threshold_overrides(
    definitions: tuple[SloDefinition, ...],
    overrides: dict[str, Any] | None,
) -> tuple[SloDefinition, ...]:
    """Overlay ``{key: {"warn": x, "breach": y}}`` onto the defaults."""
    if not overrides:
        return definitions
    adjusted: list[SloDefinition] = []
    for definition in definitions:
        override = overrides.get(definition.key)
        if not isinstance(override, dict):
            adjusted.append(definition)
            continue
        adjusted.append(
            SloDefinition(
                key=definition.key,
                label=definition.label,
                description=definition.description,
                unit=definition.unit,
                direction=definition.direction,
                warn=float(override.get("warn", definition.warn)),
                breach=float(override.get("breach", definition.breach)),
                runbook=definition.runbook,
            )
        )
    return tuple(adjusted)


def evaluate_slo(
    definition: SloDefinition, value: float | None
) -> dict[str, Any]:
    if value is None:
        state = "unknown"
    elif definition.direction == "below":
        state = (
            "breach"
            if value < definition.breach
            else "warn"
            if value < definition.warn
            else "ok"
        )
    else:
        state = (
            "breach"
            if value > definition.breach
            else "warn"
            if value > definition.warn
            else "ok"
        )
    return {
        "key": definition.key,
        "label": definition.label,
        "description": definition.description,
        "unit": definition.unit,
        "direction": definition.direction,
        "warn": definition.warn,
        "breach": definition.breach,
        "runbook": definition.runbook,
        "value": value,
        "state": state,
    }


@dataclass(frozen=True)
class SloState:
    key: str
    state: str
    since: datetime
    last_notified: datetime


_SEVERITY = {"ok": 0, "warn": 1, "breach": 2}


def _format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "unknown"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text}{unit}" if unit == "%" else f"{text} {unit}".strip()


def select_slo_transitions(
    evaluations: list[dict[str, Any]],
    known: dict[str, SloState],
    now: datetime,
    renotify_hours: float = 24,
) -> tuple[list[str], list[tuple[str, str, datetime, datetime]], list[str]]:
    """Decide notifications and state changes.

    Returns ``(messages, upserts, clears)`` where upserts are
    ``(key, state, since, last_notified)`` rows for currently-bad
    objectives and clears are keys whose state rows should be removed
    (recovered). Pure: no I/O.
    """
    messages: list[str] = []
    upserts: list[tuple[str, str, datetime, datetime]] = []
    clears: list[str] = []
    threshold = now - timedelta(hours=renotify_hours)
    for evaluation in evaluations:
        key = evaluation["key"]
        state = evaluation["state"]
        previous = known.get(key)
        value_text = _format_value(evaluation["value"], evaluation["unit"])
        limit = (
            evaluation["breach"] if state == "breach" else evaluation["warn"]
        )
        comparator = "<" if evaluation["direction"] == "below" else ">"
        if state == "unknown":
            # A dead probe is not a recovery and not a new incident; hold
            # the previous state so recovery only ever reports real data.
            if previous is not None:
                upserts.append(
                    (
                        key,
                        previous.state,
                        previous.since,
                        previous.last_notified,
                    )
                )
            continue
        if state == "ok":
            if previous is not None and _SEVERITY.get(previous.state, 0) > 0:
                messages.append(
                    f"✅ Recovered: {evaluation['label']} is back at "
                    f"{value_text} (was {previous.state} since "
                    f"{previous.since:%Y-%m-%d %H:%M} UTC)."
                )
                clears.append(key)
            continue
        icon = "🔴" if state == "breach" else "🟡"
        if previous is None or _SEVERITY[state] > _SEVERITY.get(
            previous.state, 0
        ):
            messages.append(
                f"{icon} SLO {state}: {evaluation['label']} is "
                f"{value_text} ({comparator} {limit}{evaluation['unit']}). "
                f"Runbook: {evaluation['runbook']}."
            )
            upserts.append((key, state, now, now))
        elif _SEVERITY[state] < _SEVERITY.get(previous.state, 0):
            # Improved but still bad (breach -> warn): say so once.
            messages.append(
                f"{icon} Improved to {state}: {evaluation['label']} is "
                f"{value_text} (was {previous.state})."
            )
            upserts.append((key, state, now, now))
        elif previous.last_notified < threshold:
            messages.append(
                f"{icon} Still {state} since "
                f"{previous.since:%Y-%m-%d %H:%M} UTC: "
                f"{evaluation['label']} is {value_text}."
            )
            upserts.append((key, state, previous.since, now))
        else:
            upserts.append(
                (key, state, previous.since, previous.last_notified)
            )
    return messages, upserts, clears
