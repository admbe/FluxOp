"""Flux's proposed right-sizing plan.

Builds a complete commitment-planning board from governed data alone and
publishes it as a system-owned, read-only board ("Flux proposal"). People
copy it into an editable board to make it theirs; Flux never edits human
boards and humans never edit Flux's.

The proposal is deterministic arithmetic over governed inputs -- no model
involvement -- so every placement and quantity can be explained line by
line. Factors the engine cannot compute from data (capacity guarantees,
department chargeback constraints, license entitlements) are surfaced as
explicit review notes instead of silent assumptions.

Factor map (from the planning checklist this engine implements):
- Waste elimination ......... idle/stopped-allocated VMs go to Excluded
                              before any baseline is counted.
- Lookback windows .......... Flux acts on available telemetry, marking
                              <30-day or <60%-coverage evidence provisional.
- Performance bottlenecks ... downsizes only ever use the governed target
                              SKU, and carry an IOPS/network check note.
- Decommissioning risk ...... only explicit lifecycle-risk workloads enter
                              Savings Plan review; generic technical reviews
                              remain on demand.
- ISF + operational overhead  same-family buckets consolidate into one
                              purchase SKU using instance-size-flexibility
                              ratios (vCPU-proportional within a family).
- Economics ................. FOCUS VM list cost must reconcile within 10%
                              of regional PAYG retail; proposed SKUs use
                              Azure 1-year RI/SP retail, while Advisor is
                              non-additive corroboration only.
- Existing coverage ......... active reservations net out of proposed
                              quantities, with expiry-driven renewal notes.
- Term commitment ........... proposals default to a 1-year term and state
                              the 3-year trade-off in the note.
- License stacking .......... Windows license-included pricing adds an
                              Azure Hybrid Benefit note before committing.
- Scope configuration ....... the board description recommends shared
                              scope and names the chargeback trade-off.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

FLUX_BOARD_NAME = "Flux proposal"
FLUX_ACTOR = "flux"
REFRESH_INTERVAL = timedelta(days=3)

BOARD_DESCRIPTION = (
    "Flux applies decisions in this order: remove governed waste; keep "
    "technical-review workloads on demand; route only explicit lifecycle-"
    "risk workloads to Savings Plan review; apply governed resize targets; "
    "then consolidate durable workloads by region and instance-flexibility "
    "family. Economics never sum Azure Advisor scenarios. Flux uses the "
    "latest 30-day FOCUS list-cost run rate for each VM, accepts it only "
    "when it reconciles within 10% of that VM's current regional PAYG retail "
    "price, and compares the proposed SKU with Azure's current 1-year retail "
    "reservation or Savings Plan rate. Windows license cost is preserved; "
    "only compute receives the reservation discount. Unreconciled workloads "
    "remain visible but contribute no claimed savings. Proposed scope is "
    "Shared, trading lower stranded-discount risk for harder chargeback. "
    "Reservations provide a discount, not capacity: pair them with an "
    "on-demand capacity reservation where hardware assurance is required."
)

RETAIL_RECONCILIATION_TOLERANCE = 0.10

_SKU_PATTERN = re.compile(
    r"^standard_([a-z]+)(\d+)(-\d+)?([a-z]*)(?:_(v\d+))?$", re.IGNORECASE
)

_WASTE_KINDS = {
    "stopped_allocated_vm",
    "deallocated_vm_residual_cost",
}


def parse_sku(sku: str) -> dict[str, Any] | None:
    """Family and size ratio for instance-size-flexibility grouping.

    Within an Azure VM family, ISF ratios are proportional to vCPU count,
    which the SKU name carries (Standard_D4as_v5 -> 4). Constrained-core
    SKUs (E4-2as_v5) keep the parent size for ratio purposes because that
    is what the reservation normalizes against.
    """
    match = _SKU_PATTERN.match((sku or "").strip())
    if not match:
        return None
    letter, size, _constrained, suffix, version = match.groups()
    return {
        "family": f"{letter.upper()}{suffix.lower()}_{(version or 'v1').lower()}",
        "ratio": int(size),
    }


def _lookback_ok(vm: dict[str, Any]) -> bool:
    window = vm.get("windowDays")
    coverage = vm.get("coveragePercent")
    return (
        window is not None
        and window >= 30
        and coverage is not None
        and coverage >= 60
    )


def _needs_review(vm: dict[str, Any]) -> bool:
    reason = str(vm.get("reason") or "").lower()
    action = str(vm.get("action") or "").lower()
    return (
        "review" in action
        or "special" in reason
        or "review" in reason
        or bool(vm.get("decommissionRisk"))
    )


def _money(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "unavailable"


def _evidence_rationale(vm: dict[str, Any]) -> str:
    p95 = vm.get("cpuP95")
    window = vm.get("windowDays")
    coverage = vm.get("coveragePercent")
    source = str(vm.get("telemetrySource") or "governed telemetry")
    cpu_evidence = "unavailable" if p95 is None else f"{float(p95):.1f}%"
    telemetry = (
        f"CPU p95 {cpu_evidence} "
        f"over {window if window is not None else 'unknown'} days at "
        f"{coverage if coverage is not None else 'unknown'}% coverage "
        f"from {source}."
    )
    action = str(vm.get("action") or "none")
    target = str(vm.get("targetSku") or "")
    recommendation = (
        f"Governed recommendation: {action}"
        + (f" to {target}" if target else "")
        + (f" ({vm.get('reason')})." if vm.get("reason") else ".")
    )
    return f"Evidence: {telemetry} {recommendation}"


def _retail_price(
    retail_prices: dict[tuple[str, str, str], dict[str, Any]],
    vm: dict[str, Any],
    sku: str,
) -> dict[str, Any] | None:
    return retail_prices.get(
        (
            str(vm.get("region") or "").strip().lower(),
            str(sku or "").strip().lower(),
            str(vm.get("priceProfile") or "unknown"),
        )
    )


def _reconcile_current_spend(
    vm: dict[str, Any],
    retail_prices: dict[tuple[str, str, str], dict[str, Any]],
) -> tuple[bool, str, dict[str, Any] | None]:
    observed = vm.get("observedMonthlyListCost")
    price = _retail_price(retail_prices, vm, str(vm.get("sku") or ""))
    payg = price.get("monthly_price") if price else None
    if observed is None:
        return False, "FOCUS has no VM-attributed list-cost baseline.", price
    if payg is None or float(payg) <= 0:
        return False, "The current regional PAYG retail rate is unavailable.", price
    variance = abs(float(observed) - float(payg)) / float(payg)
    if variance > RETAIL_RECONCILIATION_TOLERANCE:
        return (
            False,
            f"Observed FOCUS list cost {_money(float(observed))} differs from "
            f"PAYG retail {_money(float(payg))} by {variance:.1%}, above the "
            "10% reconciliation tolerance.",
            price,
        )
    return (
        True,
        f"Observed FOCUS list cost {_money(float(observed))} reconciles to "
        f"PAYG retail {_money(float(payg))} ({variance:.1%} variance).",
        price,
    )


def _decommission_risk(tags: Any) -> str:
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (TypeError, ValueError):
            return ""
    if not isinstance(tags, dict):
        return ""
    normalized = {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in tags.items()
    }
    environment = next(
        (
            normalized[key]
            for key in ("environment", "env", "lifecycle")
            if key in normalized
        ),
        "",
    )
    if environment in {"dev", "development", "test", "qa", "sandbox", "temporary"}:
        return f"environment={environment}"
    for key, value in normalized.items():
        if any(token in key for token in ("expire", "enddate", "end-date", "decommission")):
            if value and value not in {"none", "false", "no", "n/a"}:
                return f"{key}={value}"
    return ""


def _term_months(term: str) -> int:
    value = (term or "").strip().upper()
    return {"P1Y": 12, "1 YEAR": 12, "P3Y": 36, "3 YEARS": 36}.get(
        value, 12
    )


def _lookback_days(value: str) -> int:
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else 0


def build_proposal(
    vms: list[dict[str, Any]],
    *,
    waste_kinds_by_vm: dict[str, str],
    active_reservations: list[dict[str, Any]],
    reservation_recommendations: list[dict[str, Any]],
    retail_prices: dict[tuple[str, str, str], dict[str, Any]],
    as_of: date,
) -> dict[str, Any]:
    """Pure placement math: governed inputs in, buckets and moves out."""
    assignments: list[dict[str, Any]] = []
    members: dict[tuple[str, str], list[dict[str, Any]]] = {}
    savings_plan_rows: list[dict[str, Any]] = []
    counts = {
        "waste": 0,
        "noData": 0,
        "provisional": 0,
        "review": 0,
        "savingsPlan": 0,
        "placed": 0,
    }

    for vm in vms:
        vm_key = vm["vmKey"]
        waste_kind = waste_kinds_by_vm.get(vm_key)
        if (
            waste_kind in _WASTE_KINDS
            or str(vm.get("action") or "").lower() == "shutdown"
        ):
            counts["waste"] += 1
            assignments.append(
                {
                    "vmKey": vm_key,
                    "vmName": vm["name"],
                    "subscriptionName": vm["subscriptionName"],
                    "bucketKey": "__excluded__",
                    "decision": "Deferred",
                    "note": (
                        "Decision: exclude from commitment baseline. Waste "
                        "first: resolve "
                        f"{(waste_kind or 'governed shutdown recommendation').replace('_', ' ')} before this VM "
                        "counts toward baseline commitment capacity. "
                        + _evidence_rationale(vm)
                    ),
                    "economicsStatus": "excluded-waste",
                }
            )
            continue
        if vm.get("noData"):
            counts["noData"] += 1
            assignments.append(
                {
                    "vmKey": vm_key,
                    "vmName": vm["name"],
                    "subscriptionName": vm["subscriptionName"],
                    "bucketKey": "__nodata__",
                    "decision": "Needs discussion",
                    "note": (
                        "Decision: remain on demand. No governed CPU telemetry "
                        "is available, so Flux will not recommend a term "
                        "commitment."
                    ),
                    "economicsStatus": "unmodeled-no-telemetry",
                }
            )
            continue
        provisional_note = ""
        if not _lookback_ok(vm):
            counts["provisional"] += 1
            window = vm.get("windowDays")
            coverage = vm.get("coveragePercent")
            provisional_note = (
                "Provisional evidence: Flux acted on the available "
                f"{window if window is not None else 'unknown'}-day window "
                f"at {coverage if coverage is not None else 'unknown'}% "
                "coverage. Revalidate known cyclical peaks before purchase; "
                "confidence improves after at least 30 days and 60% coverage."
            )
        if vm.get("decommissionRisk"):
            counts["savingsPlan"] += 1
            savings_plan_rows.append(
                {"vm": vm, "provisionalNote": provisional_note}
            )
            continue
        if _needs_review(vm):
            counts["review"] += 1
            assignments.append(
                {
                    "vmKey": vm_key,
                    "vmName": vm["name"],
                    "subscriptionName": vm["subscriptionName"],
                    "bucketKey": "__review__",
                    "decision": "Needs discussion",
                    "note": (
                        "Decision: keep on demand pending technical review. "
                        "A generic performance or evidence review is not a "
                        "valid Savings Plan trigger; resolve the governed "
                        "review before making any term commitment. "
                        + _evidence_rationale(vm)
                        + (f" {provisional_note}" if provisional_note else "")
                    ),
                    "economicsStatus": "unmodeled-technical-review",
                }
            )
            continue
        action = str(vm.get("action") or "").lower()
        target = str(vm.get("targetSku") or "").strip()
        if action in {"resize", "rightsize"} and target:
            sku, note = target, (
                f"Downsize {vm.get('sku')} -> {target} per governed "
                "recommendation. Verify storage IOPS and network ceilings "
                "on the target size before committing."
            )
        else:
            sku, note = str(vm.get("sku") or "").strip(), ""
        if provisional_note:
            note = (note + " " if note else "") + provisional_note
        if 30 <= int(vm.get("windowDays") or 0) < 90:
            note = (note + " " if note else "") + (
                "Evidence meets the 30-day floor but not a full 90-day "
                "business-cycle window; confirm known seasonal peaks."
            )
        if not sku:
            counts["noData"] += 1
            assignments.append(
                {
                    "vmKey": vm_key,
                    "vmName": vm["name"],
                    "subscriptionName": vm["subscriptionName"],
                    "bucketKey": "__nodata__",
                    "note": "No SKU recorded for this VM.",
                }
            )
            continue
        counts["placed"] += 1
        region = str(vm.get("region") or "").strip().lower()
        members.setdefault((region, sku), []).append(
            {"vm": vm, "note": note}
        )

    for row in savings_plan_rows:
        vm = row["vm"]
        action = str(vm.get("action") or "").lower()
        target = (
            str(vm.get("targetSku") or "").strip()
            if action in {"resize", "rightsize"}
            and str(vm.get("targetSku") or "").strip()
            else str(vm.get("sku") or "").strip()
        )
        reconciled, reconciliation, _ = _reconcile_current_spend(
            vm, retail_prices
        )
        target_price = _retail_price(retail_prices, vm, target)
        baseline = (
            float(vm["observedMonthlyListCost"])
            if reconciled and vm.get("observedMonthlyListCost") is not None
            else None
        )
        commitment = (
            float(target_price["monthly_sp_1y"])
            if reconciled and target_price
            and target_price.get("monthly_sp_1y") is not None
            else None
        )
        savings = (
            max(baseline - commitment, 0.0)
            if baseline is not None and commitment is not None
            else None
        )
        economics_status = (
            "modeled-retail-reconciled"
            if savings is not None
            else "unmodeled-retail-reconciliation"
        )
        economics = (
            f"Economics: {reconciliation} The proposed {target} 1-year "
            f"Savings Plan retail equivalent is {_money(commitment)}/month, "
            f"for modeled savings of {_money(savings)}/month."
            if savings is not None
            else f"Economics: {reconciliation} No savings are claimed."
        )
        assignments.append(
            {
                "vmKey": vm["vmKey"],
                "vmName": vm["name"],
                "subscriptionName": vm["subscriptionName"],
                "bucketKey": "__savingsplan__",
                "decision": "Needs discussion",
                "refMonthlyPayg": baseline,
                "refMonthlyCommitment": commitment,
                "refMonthlySavings": savings,
                "economicsStatus": economics_status,
                "note": (
                    "Decision: Savings Plan candidate review, not an "
                    "approved purchase. The explicit lifecycle signal "
                    f"({vm['decommissionRisk']}) makes a flexible dollar "
                    "commitment safer than a hardware-specific reservation. "
                    + _evidence_rationale(vm)
                    + " " + economics
                    + " Validate the shared hourly commitment against "
                    "concurrent eligible spend before purchase; project "
                    "cancellation can still strand an oversized plan."
                    + (
                        f" {row['provisionalNote']}"
                        if row["provisionalNote"] else ""
                    )
                ),
            }
        )

    # ISF consolidation: same region + family folds into one purchase SKU.
    consolidated: dict[tuple[str, str], dict[str, Any]] = {}
    for (region, sku), rows in members.items():
        parsed = parse_sku(sku)
        family_key = (
            (region, f"family:{parsed['family']}") if parsed else (region, sku)
        )
        group = consolidated.setdefault(
            family_key,
            {"region": region, "skus": {}, "rows": []},
        )
        group["skus"][sku] = {
            "count": len(rows),
            "ratio": parsed["ratio"] if parsed else None,
        }
        group["rows"].extend(rows)

    reservation_units: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for reservation in active_reservations:
        parsed = parse_sku(reservation.get("sku") or "")
        if not parsed:
            continue
        key = (
            str(reservation.get("region") or "").strip().lower(),
            f"family:{parsed['family']}",
        )
        reservation_units.setdefault(key, []).append(
            {
                "units": parsed["ratio"] * int(reservation.get("quantity") or 0),
                "expiry": reservation.get("expiryDate"),
                "name": reservation.get("name"),
            }
        )

    buckets: list[dict[str, Any]] = []
    for (region, family_key), group in sorted(consolidated.items()):
        skus = group["skus"]
        ratios_known = all(entry["ratio"] for entry in skus.values())
        # Purchase at the top of the family so the discount splits downward.
        purchase_sku = max(
            skus,
            key=lambda name: (skus[name]["ratio"] or 0, skus[name]["count"]),
        )
        note_parts: list[str] = []
        total_units = (
            sum(entry["ratio"] * entry["count"] for entry in skus.values())
            if ratios_known
            else None
        )
        if ratios_known and len(skus) > 1:
            top_ratio = skus[purchase_sku]["ratio"]
            quantity = -(-total_units // top_ratio)  # ceil
            mix = ", ".join(
                f"{entry['count']}x {name}" for name, entry in sorted(skus.items())
            )
            note_parts.append(
                f"Instance-size-flexible purchase covering {mix} "
                f"({total_units} normalized units at ratio {top_ratio} "
                "per purchased instance). One consolidated reservation "
                "keeps management overhead down."
            )
        elif ratios_known:
            quantity = skus[purchase_sku]["count"]
        else:
            quantity = sum(entry["count"] for entry in skus.values())
            note_parts.append(
                "SKU family not recognized for ISF math; quantity is a "
                "straight instance count."
            )

        covered_units = 0
        for coverage in reservation_units.get((region, family_key), []):
            covered_units += coverage["units"]
            note_parts.append(
                f"Existing reservation {coverage['name']} covers "
                f"{coverage['units']} units until {coverage['expiry']} -- "
                "plan the renewal decision alongside this bucket."
            )
        if ratios_known:
            top_ratio = skus[purchase_sku]["ratio"]
            remaining_units = max(int(total_units or 0) - covered_units, 0)
            quantity = -(-remaining_units // top_ratio)
            if quantity == 0:
                note_parts.append(
                    "Fully covered by existing reservations today; buy "
                    "nothing new unless the expiring coverage lapses."
                )

        monthly_payg = None
        monthly_ri = None
        monthly_savings = None
        ri_upfront = None
        reconciliation_notes: list[str] = []
        modeled_rows: list[dict[str, Any]] = []
        for member in group["rows"]:
            vm = member["vm"]
            reconciled, reconciliation, _ = _reconcile_current_spend(
                vm, retail_prices
            )
            action = str(vm.get("action") or "").lower()
            target_sku = (
                str(vm.get("targetSku") or "").strip()
                if action in {"resize", "rightsize"}
                and str(vm.get("targetSku") or "").strip()
                else str(vm.get("sku") or "").strip()
            )
            target_price = _retail_price(retail_prices, vm, target_sku)
            if (
                reconciled
                and target_price
                and target_price.get("monthly_license_price") is not None
                and vm.get("observedMonthlyListCost") is not None
            ):
                modeled_rows.append(
                    {
                        "vm": vm,
                        "baseline": float(vm["observedMonthlyListCost"]),
                        "targetLicense": float(
                            target_price["monthly_license_price"]
                        ),
                        "reconciliation": reconciliation,
                    }
                )
            else:
                reconciliation_notes.append(
                    f"{vm['name']}: {reconciliation}"
                )

        purchase_prices = [
            price for (price_region, price_sku, _profile), price
            in retail_prices.items()
            if price_region == region
            and price_sku == purchase_sku.lower()
            and price.get("monthly_ri_1y") is not None
            and price.get("monthly_license_price") is not None
        ]
        purchase_price = purchase_prices[0] if purchase_prices else None
        remaining_fraction = (
            max(int(total_units or 0) - covered_units, 0)
            / float(total_units or 1)
            if ratios_known else quantity / float(len(group["rows"]) or 1)
        )
        if quantity == 0:
            monthly_payg = monthly_ri = monthly_savings = ri_upfront = 0.0
        elif len(modeled_rows) == len(group["rows"]) and purchase_price:
            compute_ri = float(purchase_price["monthly_ri_1y"]) - float(
                purchase_price["monthly_license_price"]
            )
            monthly_payg = round(
                sum(row["baseline"] for row in modeled_rows)
                * remaining_fraction,
                2,
            )
            monthly_ri = round(
                compute_ri * quantity
                + sum(row["targetLicense"] for row in modeled_rows)
                * remaining_fraction,
                2,
            )
            monthly_savings = round(max(monthly_payg - monthly_ri, 0.0), 2)
            if purchase_price.get("ri_1y_upfront") is not None:
                ri_upfront = round(
                    float(purchase_price["ri_1y_upfront"]) * quantity, 2
                )
            note_parts.append(
                "Economics: every member's latest 30-day FOCUS list-cost "
                "run rate reconciled within 10% of its current regional "
                "PAYG retail price. The remaining uncovered baseline is "
                f"{_money(monthly_payg)}/month; the proposed resized/licensed "
                f"1-year reservation retail cost is {_money(monthly_ri)}/month, "
                f"for {_money(monthly_savings)}/month modeled savings."
            )
        else:
            note_parts.append(
                "Economics: unmodeled. Flux claims no savings unless every "
                "member reconciles to current PAYG retail and Azure returns "
                "an unambiguous 1-year reservation retail price."
            )
            if reconciliation_notes:
                note_parts.append(
                    "First reconciliation gaps: "
                    + "; ".join(reconciliation_notes[:3])
                    + ("." if len(reconciliation_notes) <= 3 else "; …")
                )

        advisor_matches = [
            item for item in reservation_recommendations
            if str(item.get("region") or "").strip().lower() == region
            and str(item.get("sku") or "").strip().lower() in {
                str(sku).lower() for sku in skus
            }
            and _term_months(str(item.get("term") or "")) == 12
        ]
        if advisor_matches:
            note_parts.append(
                f"Advisor corroboration: {len(advisor_matches)} overlapping "
                "1-year recommendation scenarios exist for member SKUs. "
                "They support review but are deliberately excluded from "
                "the savings total to prevent double counting."
            )
        if any(
            row["vm"].get("licenseModel") == "license_included"
            for row in group["rows"]
        ):
            note_parts.append(
                "Windows license cost is retained outside the reservation "
                "discount. Validate Azure Hybrid Benefit eligibility; AHUB "
                "would reduce the modeled post-commitment cost further."
            )
        note_parts.append(
            "Term: proposed at 1 year to bound decommissioning risk; a "
            "3-year term deepens the discount if the workload is durable."
        )

        buckets.append(
            {
                "region": region,
                "sku": purchase_sku,
                "strategy": "1-year reservation",
                "refQuantity": quantity,
                "refMonthlyPayg": monthly_payg,
                "refMonthlyRi1y": monthly_ri,
                "refRi1yUpfront": ri_upfront,
                "refMonthlySavings": monthly_savings,
                "refReservationCheck": (
                    f"FOCUS + retail reconciled {len(modeled_rows)}/"
                    f"{len(group['rows'])} members"
                ),
                "note": " ".join(note_parts),
                "members": group["rows"],
                "modeledRows": modeled_rows,
            }
        )

    for bucket in buckets:
        bucket_key_suffix = f"{bucket['region']}|{bucket['sku']}"
        modeled_by_vm = {
            row["vm"]["vmKey"]: row for row in bucket.pop("modeledRows")
        }
        modeled_baseline = sum(
            row["baseline"] for row in modeled_by_vm.values()
        )
        for row in bucket.pop("members"):
            vm = row["vm"]
            modeled = modeled_by_vm.get(vm["vmKey"])
            savings_share = (
                float(bucket["refMonthlySavings"]) * modeled["baseline"]
                / modeled_baseline
                if modeled and modeled_baseline
                and bucket.get("refMonthlySavings") is not None
                else None
            )
            payg_share = (
                float(bucket["refMonthlyPayg"]) * modeled["baseline"]
                / modeled_baseline
                if modeled and modeled_baseline
                and bucket.get("refMonthlyPayg") is not None
                else None
            )
            commitment_share = (
                payg_share - savings_share
                if payg_share is not None and savings_share is not None
                else None
            )
            economics = (
                f"Economics: {modeled['reconciliation']} This VM's "
                f"allocation of the bucket model is {_money(payg_share)} "
                f"baseline, {_money(commitment_share)} committed cost, and "
                f"{_money(savings_share)}/month savings."
                if modeled and savings_share is not None
                else "Economics: unmodeled; this VM contributes no claimed savings."
            )
            assignments.append(
                {
                    "vmKey": vm["vmKey"],
                    "vmName": vm["name"],
                    "subscriptionName": vm["subscriptionName"],
                    "bucketKey": bucket_key_suffix,
                    "decision": "Needs discussion",
                    "refMonthlyPayg": payg_share,
                    "refMonthlyCommitment": commitment_share,
                    "refMonthlySavings": savings_share,
                    "economicsStatus": (
                        "modeled-retail-reconciled"
                        if savings_share is not None
                        else "unmodeled-retail-reconciliation"
                    ),
                    "note": (
                        "Decision: propose a 1-year reservation because no "
                        "lifecycle or unresolved technical-review signal was "
                        "found. "
                        + _evidence_rationale(vm)
                        + (f" {row['note']}" if row["note"] else "")
                        + f" Method: place the post-right-size SKU in the "
                        f"{bucket['region']} {bucket['sku']} instance-"
                        "flexibility bucket, net existing reservation units, "
                        "and use a one-year term to limit durability risk. "
                        + economics
                        + " Before purchase, verify target storage IOPS, "
                        "network throughput, capacity requirements, scope/"
                        "chargeback, and Windows/AHUB entitlement."
                    ),
                }
            )

    return {
        "asOf": as_of.isoformat(),
        "buckets": buckets,
        "assignments": assignments,
        "counts": counts,
    }


def gather_inputs(database: Any) -> dict[str, Any]:
    """Governed reads only; every input is data another surface already shows."""
    board = database.rightsizing_plan_board()
    with database.connect(read_only=True) as db:
        waste_rows = db.execute(
            """
            SELECT lower(resource_id), opportunity_kind
            FROM resources_current
            WHERE opportunity_kind IS NOT NULL AND opportunity_kind <> ''
            """
        ).fetchall()
        azure_rows = db.execute(
            """
            SELECT lower(region), sku, term, look_back,
                   recommended_quantity, net_savings,
                   cost_without_commitment, cost_with_commitment
            FROM reservation_recommendations_current
            WHERE lower(resource_type) LIKE '%virtualmachines%'
            """
        ).fetchall()
        price_rows = db.execute(
            """
            SELECT lower(arm_region_name), lower(arm_sku_name),
                   price_profile, monthly_price, monthly_compute_price,
                   monthly_license_price, monthly_ri_1y, ri_1y_upfront,
                   monthly_sp_1y, license_model
            FROM retail_prices_current
            WHERE status = 'matched' AND monthly_price IS NOT NULL
            """
        ).fetchall()
        tag_rows = db.execute(
            "SELECT lower(resource_id), tags_json FROM resources_current "
            "WHERE lower(resource_type) = 'microsoft.compute/virtualmachines'"
        ).fetchall()
    risks = {row[0]: _decommission_risk(row[1]) for row in tag_rows}
    for vm in board["vms"]:
        vm["decommissionRisk"] = risks.get(vm["vmKey"], "")
    return {
        "vms": board["vms"],
        "waste_kinds_by_vm": {row[0]: str(row[1]) for row in waste_rows},
        "active_reservations": database.commitment_inventory()["reservations"],
        "reservation_recommendations": [
            {
                "region": row[0],
                "sku": row[1],
                "term": row[2],
                "lookBack": row[3],
                "recommendedQuantity": row[4],
                "netSavings": row[5],
                "costWithoutCommitment": row[6],
                "costWithCommitment": row[7],
            }
            for row in azure_rows
        ],
        "retail_prices": {
            (row[0], row[1], row[2]): {
                "monthly_price": row[3],
                "monthly_compute_price": row[4],
                "monthly_license_price": row[5],
                "monthly_ri_1y": row[6],
                "ri_1y_upfront": row[7],
                "monthly_sp_1y": row[8],
                "license_model": row[9],
            }
            for row in price_rows
        },
    }


def proposal_status(
    database: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    """Return refresh timing without mutating the board."""
    current = now or datetime.now(timezone.utc)
    board = next(
        (
            item
            for item in database.rightsizing_boards()
            if item.get("createdBy") == FLUX_ACTOR
            and item.get("name") == FLUX_BOARD_NAME
        ),
        None,
    )
    updated = None
    if board and board.get("updatedAt"):
        updated = datetime.fromisoformat(
            str(board["updatedAt"]).replace("Z", "+00:00")
        )
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    next_refresh = updated + REFRESH_INTERVAL if updated else current
    return {
        "board": board,
        "lastRefreshedAt": updated.isoformat() if updated else None,
        "nextRefreshAt": next_refresh.isoformat(),
        "due": updated is None or current >= next_refresh,
        "cadenceDays": REFRESH_INTERVAL.days,
    }


def refresh_flux_proposal(
    database: Any,
    *,
    as_of: date | None = None,
    force: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Regenerate the Flux proposal board in place and return a report."""
    status = proposal_status(database, now=now)
    if not force and not status["due"]:
        return {"status": "current", **status}
    inputs = gather_inputs(database)
    proposal = build_proposal(
        inputs["vms"],
        waste_kinds_by_vm=inputs["waste_kinds_by_vm"],
        active_reservations=inputs["active_reservations"],
        reservation_recommendations=inputs["reservation_recommendations"],
        retail_prices=inputs["retail_prices"],
        as_of=as_of or date.today(),
    )
    modeled_reservation_buckets = sum(
        bucket.get("refMonthlySavings") is not None
        for bucket in proposal["buckets"]
    )
    modeled_savings_plan = sum(
        assignment.get("refMonthlySavings") is not None
        for assignment in proposal["assignments"]
        if assignment["bucketKey"] == "__savingsplan__"
    )
    modeled_monthly_savings = round(
        sum(
            float(bucket.get("refMonthlySavings") or 0)
            for bucket in proposal["buckets"]
        )
        + sum(
            float(assignment.get("refMonthlySavings") or 0)
            for assignment in proposal["assignments"]
            if assignment["bucketKey"] == "__savingsplan__"
        ),
        2,
    )
    description = (
        BOARD_DESCRIPTION
        + " Current model coverage: "
        + f"{modeled_reservation_buckets}/{len(proposal['buckets'])} "
        + "reservation buckets and "
        + f"{modeled_savings_plan}/{proposal['counts']['savingsPlan']} "
        + "Savings Plan candidates; unmodeled decisions are excluded from "
        + f"the {_money(modeled_monthly_savings)}/month total."
    )
    board_id = database.replace_flux_proposal_board(
        board_name=FLUX_BOARD_NAME,
        description=description,
        actor=FLUX_ACTOR,
        buckets=proposal["buckets"],
        assignments=proposal["assignments"],
        summary_note=(
            f"Proposal refreshed: {proposal['counts']['placed']} VMs placed "
            f"into {len(proposal['buckets'])} buckets, "
            f"{proposal['counts']['review']} kept on demand for technical "
            f"review, {proposal['counts']['savingsPlan']} lifecycle-risk "
            f"Savings Plan candidates, "
            f"{proposal['counts']['provisional']} provisional placements, "
            f"{proposal['counts']['noData']} without monitoring, "
            f"{proposal['counts']['waste']} excluded as waste. Retail-"
            f"reconciled modeled savings: {_money(modeled_monthly_savings)}"
            f"/month with {modeled_reservation_buckets}/"
            f"{len(proposal['buckets'])} reservation buckets and "
            f"{modeled_savings_plan}/{proposal['counts']['savingsPlan']} "
            "Savings Plan candidates priced. Azure Advisor scenarios were "
            "corroboration only and were not added to savings."
        ),
    )
    refreshed_at = now or datetime.now(timezone.utc)
    return {
        "status": "refreshed",
        "boardId": board_id,
        "lastRefreshedAt": refreshed_at.isoformat(),
        "nextRefreshAt": (refreshed_at + REFRESH_INTERVAL).isoformat(),
        "cadenceDays": REFRESH_INTERVAL.days,
        **proposal["counts"],
        "bucketCount": len(proposal["buckets"]),
        "modeledReservationBuckets": modeled_reservation_buckets,
        "modeledSavingsPlanCandidates": modeled_savings_plan,
        "modeledMonthlySavings": modeled_monthly_savings,
    }
