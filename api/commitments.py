"""Commitment (reservation) inventory, utilization, and recommendations.

Closes the loop the right-sizing plan opens: the plan decides what to buy,
this feed shows whether what was bought is being used. Two ARM surfaces:

- ``Microsoft.Capacity/reservations`` lists every reservation the caller
  can see, including 1/7/30-day utilization aggregates. The App Service
  identity needs the **Reservations Reader** role at the tenant capacity
  scope (``/providers/Microsoft.Capacity``); until granted, the fetch
  reports exactly that instead of failing the job.
- ``Microsoft.Consumption/reservationRecommendations`` per subscription
  works with the Reader access the identity already has.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RESERVATIONS_URL = (
    "{endpoint}/providers/Microsoft.Capacity/reservations"
    "?api-version=2022-11-01"
)
RECOMMENDATIONS_URL = (
    "{endpoint}/subscriptions/{subscription}/providers"
    "/Microsoft.Consumption/reservationRecommendations"
    "?api-version=2024-08-01"
)

RESERVATIONS_READER_HINT = (
    "Grant the application identity the 'Reservations Reader' role at "
    "scope /providers/Microsoft.Capacity to inventory reservations "
    "(az role assignment create --role 'Reservations Reader' "
    "--scope /providers/Microsoft.Capacity --assignee <principal-id>)."
)


def _get_json(url: str, token: str, timeout_seconds: int) -> dict[str, Any]:
    request = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _paged(url: str, token: str, timeout_seconds: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        payload = _get_json(next_url, token, timeout_seconds)
        rows.extend(payload.get("value") or [])
        next_url = payload.get("nextLink")
    return rows


def _http_error_message(error: HTTPError) -> str:
    detail = error.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(detail)
        message = str(
            (parsed.get("error") or {}).get("message") or detail
        )
    except json.JSONDecodeError:
        message = detail
    return f"HTTP {error.code}: {message[:400]}"


def _utilization(properties: dict[str, Any]) -> dict[int, float | None]:
    aggregates = (properties.get("utilization") or {}).get("aggregates") or []
    values: dict[int, float | None] = {1: None, 7: None, 30: None}
    for aggregate in aggregates:
        try:
            grain = int(float(aggregate.get("grain")))
        except (TypeError, ValueError):
            continue
        if grain in values and aggregate.get("value") is not None:
            values[grain] = float(aggregate["value"])
    return values


def normalize_reservation(item: dict[str, Any]) -> dict[str, Any]:
    properties = item.get("properties") or {}
    utilization = _utilization(properties)
    identifier = str(item.get("id") or "")
    order_id = identifier.split("/reservations/")[0].rsplit("/", 1)[-1] if (
        "/reservationOrders/" in identifier
    ) else ""
    expiry = str(
        properties.get("expiryDate")
        or properties.get("expiryDateTime")
        or ""
    )[:10]
    return {
        "reservationId": identifier.lower(),
        "orderId": order_id,
        "displayName": str(properties.get("displayName") or item.get("name") or ""),
        "sku": str((item.get("sku") or {}).get("name") or ""),
        "resourceType": str(properties.get("reservedResourceType") or ""),
        "region": str(item.get("location") or ""),
        "quantity": int(properties.get("quantity") or 0),
        "term": str(properties.get("term") or ""),
        "scopeType": str(properties.get("appliedScopeType") or ""),
        "state": str(properties.get("provisioningState") or ""),
        "expiryDate": expiry or None,
        "utilization1d": utilization[1],
        "utilization7d": utilization[7],
        "utilization30d": utilization[30],
    }


def normalize_recommendation(
    item: dict[str, Any], subscription_id: str, subscription_name: str
) -> dict[str, Any]:
    properties = item.get("properties") or {}
    return {
        "subscriptionId": subscription_id,
        "subscriptionName": subscription_name,
        "scope": str(properties.get("scope") or ""),
        "resourceType": str(properties.get("resourceType") or ""),
        "sku": str(properties.get("skuName") or item.get("sku") or ""),
        "region": str(item.get("location") or ""),
        "term": str(properties.get("term") or ""),
        "lookBack": str(properties.get("lookBackPeriod") or ""),
        "recommendedQuantity": float(
            properties.get("recommendedQuantity") or 0
        ),
        "costWithoutCommitment": (
            float(properties["costWithNoReservedInstances"])
            if properties.get("costWithNoReservedInstances") is not None
            else None
        ),
        "costWithCommitment": (
            float(properties["totalCostWithReservedInstances"])
            if properties.get("totalCostWithReservedInstances") is not None
            else None
        ),
        "netSavings": (
            float(properties["netSavings"])
            if properties.get("netSavings") is not None
            else None
        ),
    }


def fetch_commitments(
    *,
    credential: Any,
    management_endpoint: str,
    subscriptions: list[dict[str, Any]],
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Fetch reservation inventory and recommendations, tolerating gaps.

    Missing rights degrade to actionable messages instead of failures:
    partial data still lands, and the report says exactly what to grant.
    """
    endpoint = management_endpoint.rstrip("/")
    token = credential.get_token(f"{endpoint}/.default").token
    reservations: list[dict[str, Any]] = []
    reservation_error = ""
    try:
        reservations = [
            normalize_reservation(item)
            for item in _paged(
                RESERVATIONS_URL.format(endpoint=endpoint),
                token,
                timeout_seconds,
            )
        ]
    except HTTPError as error:
        message = _http_error_message(error)
        reservation_error = (
            f"Reservation inventory unavailable ({message}). "
            f"{RESERVATIONS_READER_HINT}"
            if error.code in (401, 403)
            else f"Reservation inventory failed: {message}"
        )
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        reservation_error = f"Reservation inventory failed: {error}"

    recommendations: list[dict[str, Any]] = []
    recommendation_errors: list[str] = []
    for subscription in subscriptions:
        subscription_id = str(subscription.get("subscriptionId") or "")
        if not subscription_id:
            continue
        label = str(subscription.get("label") or subscription_id)
        try:
            recommendations.extend(
                normalize_recommendation(item, subscription_id, label)
                for item in _paged(
                    RECOMMENDATIONS_URL.format(
                        endpoint=endpoint, subscription=subscription_id
                    ),
                    token,
                    timeout_seconds,
                )
            )
        except HTTPError as error:
            recommendation_errors.append(
                f"{label}: {_http_error_message(error)}"
            )
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            recommendation_errors.append(f"{label}: {error}")

    return {
        "reservations": reservations,
        "recommendations": recommendations,
        "reservationError": reservation_error,
        "recommendationErrors": recommendation_errors,
    }
