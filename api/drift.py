from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from statistics import median
from typing import Any


METHOD_VERSION = "inventory-drift-v1"


def resource_fingerprint(resource: dict[str, Any]) -> str:
    governed = {
        "sku": resource.get("sku") or "",
        "kind": resource.get("kind") or "",
        "region": resource.get("region") or "",
        "resourceGroup": resource.get("resourceGroup") or "",
        "managedBy": resource.get("managedBy") or "",
        "tags": resource.get("tags") or {},
    }
    encoded = json.dumps(
        governed,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def classify_changes(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for resource_id in sorted(previous.keys() | current.keys()):
        before = previous.get(resource_id)
        after = current.get(resource_id)
        if before is None:
            changes.append(_change("created", None, after))
            continue
        if after is None:
            changes.append(_change("deleted", before, None))
            continue
        before_fingerprint = resource_fingerprint(before)
        after_fingerprint = resource_fingerprint(after)
        if before_fingerprint == after_fingerprint:
            continue
        if (
            before.get("resourceGroup") != after.get("resourceGroup")
            or before.get("region") != after.get("region")
        ):
            changes.append(_change("moved", before, after))
        if before.get("sku") != after.get("sku"):
            change_type = (
                "resized"
                if str(after.get("resourceType", "")).lower()
                == "microsoft.compute/virtualmachines"
                else "retiered"
            )
            changes.append(_change(change_type, before, after))
        if before.get("tags") != after.get("tags"):
            changes.append(_change("retagged", before, after))
        if (
            before.get("kind") != after.get("kind")
            or before.get("managedBy") != after.get("managedBy")
        ):
            changes.append(_change("reconfigured", before, after))
    return changes


def _change(
    change_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    resource = after or before or {}
    details = {}
    for key in ("sku", "kind", "region", "resourceGroup", "managedBy", "tags"):
        old_value = before.get(key) if before else None
        new_value = after.get(key) if after else None
        if old_value != new_value:
            details[key] = {"from": old_value, "to": new_value}
    return {
        "resourceId": str(resource.get("resourceId") or "").lower(),
        "resourceName": resource.get("name") or "",
        "resourceType": str(resource.get("resourceType") or "").lower(),
        "subscriptionId": str(resource.get("subscriptionId") or "").lower(),
        "subscriptionName": resource.get("subscriptionName") or "",
        "resourceGroup": resource.get("resourceGroup") or "",
        "region": resource.get("region") or "",
        "changeType": change_type,
        "fromFingerprint": resource_fingerprint(before) if before else "",
        "toFingerprint": resource_fingerprint(after) if after else "",
        "details": details,
    }


def anomaly_result(
    count: int,
    history: list[int],
    *,
    minimum_points: int,
    threshold_k: float,
) -> dict[str, Any]:
    if len(history) < minimum_points:
        return {
            "status": "warming_up",
            "baselinePoints": len(history),
            "baselineMedian": None,
            "mad": None,
            "kScore": None,
            "isAnomaly": False,
        }
    center = float(median(history))
    deviations = [abs(value - center) for value in history]
    mad = float(median(deviations))
    if mad == 0:
        k_score = 999.0 if count != center else 0.0
    else:
        k_score = round(abs(count - center) / mad, 3)
    is_anomaly = k_score > threshold_k
    return {
        "status": "anomalous" if is_anomaly else "normal",
        "baselinePoints": len(history),
        "baselineMedian": center,
        "mad": mad,
        "kScore": k_score,
        "isAnomaly": is_anomaly,
    }


def change_counts(
    changes: list[dict[str, Any]],
) -> Counter[tuple[str, str, str, str, str]]:
    counts: Counter[tuple[str, str, str, str, str]] = Counter()
    for change in changes:
        subscription_id = change["subscriptionId"]
        resource_group = change["resourceGroup"]
        change_type = change["changeType"]
        counts[
            ("subscription", subscription_id, subscription_id, "", change_type)
        ] += 1
        counts[
            (
                "resource_group",
                f"{subscription_id}/{resource_group}",
                subscription_id,
                resource_group,
                change_type,
            )
        ] += 1
    return counts
