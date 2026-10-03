"""Commitment inventory, utilization, and recommendations.

Closes the loop the right-sizing plan opens: the plan decides what to buy,
this feed shows whether what was bought is being used. ARM surfaces:

- ``Microsoft.Capacity/reservations`` lists every reservation the caller
  can see, including 1/7/30-day utilization aggregates. The App Service
  identity needs the **Reservations Reader** role at the tenant capacity
  scope (``/providers/Microsoft.Capacity``); until granted, the fetch
  reports exactly that instead of failing the job.
- ``Microsoft.Consumption/reservationRecommendations`` per subscription
  works with the Reader access the identity already has.
- ``Microsoft.BillingBenefits/savingsPlanOrders`` inventories Savings Plan
  commitments (hourly commitment, term, scope, utilization). Also requires
  Reservations Reader at the tenant capacity scope.
- ``Microsoft.CostManagement/recommendations`` per subscription returns
  Savings Plan recommendation candidates alongside Reservation ones.
- ``generateBenefitUtilizationSummariesReport`` (Cost Management) reports
  realized benefit utilization for reconciliation. It is asynchronous: the
  POST returns a Location header polled until the report is ready.
"""

from __future__ import annotations

import json
import time
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
SAVINGS_PLAN_ORDERS_URL = (
    "{endpoint}/providers/Microsoft.BillingBenefits/savingsPlanOrders"
    "?api-version=2024-11-01"
)
COST_MANAGEMENT_RECOMMENDATIONS_URL = (
    "{endpoint}/subscriptions/{subscription}/providers"
    "/Microsoft.CostManagement/recommendations"
    "?api-version=2024-08-01"
)
BENEFIT_UTILIZATION_REPORT_URL = (
    "{endpoint}/subscriptions/{subscription}/providers"
    "/Microsoft.CostManagement/generateBenefitUtilizationSummariesReport"
    "?api-version=2024-08-01"
)
BENEFIT_UTILIZATION_POLL_SECONDS = 20
BENEFIT_UTILIZATION_MAX_POLLS = 15

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


def _post_json(
    url: str, token: str, body: dict[str, Any], timeout_seconds: int
) -> tuple[int, dict[str, Any], dict[str, str]]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read().decode("utf-8")
        headers = {k.lower(): v for k, v in response.headers.items()}
        return response.status, (json.loads(payload) if payload else {}), headers


def normalize_savings_plan_order(item: dict[str, Any]) -> dict[str, Any]:
    properties = item.get("properties") or {}
    commitment = properties.get("commitment") or {}
    plan = properties.get("plan") or {}
    applied_scopes = properties.get("appliedScopes") or []
    identifier = str(item.get("id") or "")
    utilization = _utilization(properties)
    hourly = commitment.get("amount")
    grain = str(commitment.get("grain") or "").lower()
    if hourly is not None and grain and grain != "hourly":
        hourly = None
    return {
        "savingsPlanId": identifier.lower(),
        "orderId": str(item.get("name") or ""),
        "displayName": str(properties.get("displayName") or ""),
        "hourlyCommitment": float(hourly) if hourly is not None else None,
        "currency": str(commitment.get("currencyCode") or plan.get("currencyCode") or ""),
        "term": str(plan.get("term") or properties.get("term") or ""),
        "scopeType": str(properties.get("appliedScopeType") or ""),
        "appliedScopes": [str(scope) for scope in applied_scopes],
        "purchaseDate": str(properties.get("purchaseDateTime") or "")[:10] or None,
        "expiryDate": str(properties.get("expiryDateTime") or "")[:10] or None,
        "billingPlan": str(properties.get("billingPlan") or ""),
        "state": str(properties.get("provisioningState") or ""),
        "utilization1d": utilization[1],
        "utilization7d": utilization[7],
        "utilization30d": utilization[30],
    }


def normalize_sp_recommendation(
    item: dict[str, Any], subscription_id: str, subscription_name: str
) -> dict[str, Any]:
    properties = item.get("properties") or {}
    insights = properties.get("insights") or {}
    recommendation = properties.get("recommendation") or {}
    return {
        "subscriptionId": subscription_id,
        "subscriptionName": subscription_name,
        "kind": str(properties.get("kind") or item.get("kind") or ""),
        "scope": str(properties.get("scope") or ""),
        "lookBack": str(properties.get("lookBackPeriod") or ""),
        "term": str(recommendation.get("term") or ""),
        "recommendedCommitment": (
            float(recommendation["recommendedHourlyCommitment"])
            if recommendation.get("recommendedHourlyCommitment") is not None
            else None
        ),
        "candidateCommitments": [
            float(value)
            for value in (insights.get("candidateCommitments") or [])
            if value is not None
        ],
        "savingsAmount": (
            float(recommendation["savingsAmount"])
            if recommendation.get("savingsAmount") is not None
            else None
        ),
        "savingsPercentage": (
            float(recommendation["savingsPercentage"])
            if recommendation.get("savingsPercentage") is not None
            else None
        ),
        "costWithoutCommitment": (
            float(insights["costWithoutCommitment"])
            if insights.get("costWithoutCommitment") is not None
            else None
        ),
        "benefitCost": (
            float(insights["benefitCost"])
            if insights.get("benefitCost") is not None
            else None
        ),
        "overageCost": (
            float(insights["overageCost"])
            if insights.get("overageCost") is not None
            else None
        ),
        "wasteCost": (
            float(insights["wasteCost"])
            if insights.get("wasteCost") is not None
            else None
        ),
        "coverage": (
            float(insights["coverage"])
            if insights.get("coverage") is not None
            else None
        ),
        "utilization": (
            float(insights["utilization"])
            if insights.get("utilization") is not None
            else None
        ),
        "currency": str(insights.get("currency") or ""),
        "firstConsumptionDate": str(insights.get("firstConsumptionDate") or ""),
        "lastConsumptionDate": str(insights.get("lastConsumptionDate") or ""),
        "hourlyChargeSeries": insights.get("hourlyCommitmentCharges")
        or insights.get("hourlyCharges")
        or [],
    }


def fetch_savings_plans(
    *,
    credential: Any,
    management_endpoint: str,
    subscriptions: list[dict[str, Any]],
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Fetch Savings Plan inventory and SP recommendations, tolerating gaps.

    The Savings Plan order inventory needs the same Reservations Reader
    role as the reservation inventory; per-subscription recommendations use
    the Reader access already granted.
    """
    endpoint = management_endpoint.rstrip("/")
    token = credential.get_token(f"{endpoint}/.default").token

    plans: list[dict[str, Any]] = []
    plan_error = ""
    try:
        plans = [
            normalize_savings_plan_order(item)
            for item in _paged(
                SAVINGS_PLAN_ORDERS_URL.format(endpoint=endpoint),
                token,
                timeout_seconds,
            )
        ]
    except HTTPError as error:
        message = _http_error_message(error)
        plan_error = (
            f"Savings Plan inventory unavailable ({message}). "
            f"{RESERVATIONS_READER_HINT}"
            if error.code in (401, 403)
            else f"Savings Plan inventory failed: {message}"
        )
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        plan_error = f"Savings Plan inventory failed: {error}"

    recommendations: list[dict[str, Any]] = []
    recommendation_errors: list[str] = []
    for subscription in subscriptions:
        subscription_id = str(subscription.get("subscriptionId") or "")
        if not subscription_id:
            continue
        label = str(subscription.get("label") or subscription_id)
        try:
            for item in _paged(
                COST_MANAGEMENT_RECOMMENDATIONS_URL.format(
                    endpoint=endpoint, subscription=subscription_id
                ),
                token,
                timeout_seconds,
            ):
                kind = str(
                    (item.get("properties") or {}).get("kind")
                    or item.get("kind")
                    or ""
                )
                if kind.lower() == "savingsplan":
                    recommendations.append(
                        normalize_sp_recommendation(item, subscription_id, label)
                    )
        except HTTPError as error:
            recommendation_errors.append(
                f"{label}: {_http_error_message(error)}"
            )
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            recommendation_errors.append(f"{label}: {error}")

    return {
        "savingsPlans": plans,
        "recommendations": recommendations,
        "savingsPlanError": plan_error,
        "recommendationErrors": recommendation_errors,
    }


def fetch_benefit_utilization(
    *,
    credential: Any,
    management_endpoint: str,
    subscriptions: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    grain: str = "Hourly",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Collect realized benefit utilization summaries per subscription.

    Asynchronous Cost Management report: POST, then poll the Location
    header until the report is ready. Failures degrade to messages so one
    blocked subscription never hides the others.
    """
    endpoint = management_endpoint.rstrip("/")
    token = credential.get_token(f"{endpoint}/.default").token
    summaries: list[dict[str, Any]] = []
    errors: list[str] = []
    for subscription in subscriptions:
        subscription_id = str(subscription.get("subscriptionId") or "")
        if not subscription_id:
            continue
        label = str(subscription.get("label") or subscription_id)
        try:
            status, _body, headers = _post_json(
                BENEFIT_UTILIZATION_REPORT_URL.format(
                    endpoint=endpoint, subscription=subscription_id
                ),
                token,
                {
                    "metric": "UtilizedPercentage",
                    "grain": grain,
                    "grouping": {"type": "Dimension", "name": "BenefitId"},
                    "filter": {
                        "and": [
                            {
                                "name": "UsageDate",
                                "operator": "GreaterThanOrEqualTo",
                                "value": start_date,
                            },
                            {
                                "name": "UsageDate",
                                "operator": "LessThanOrEqualTo",
                                "value": end_date,
                            },
                        ]
                    },
                },
                timeout_seconds,
            )
            location = headers.get("location", "")
            if status not in (200, 202) or not location:
                errors.append(f"{label}: report not accepted (HTTP {status}).")
                continue
            report: dict[str, Any] = {}
            for _attempt in range(BENEFIT_UTILIZATION_MAX_POLLS):
                time.sleep(BENEFIT_UTILIZATION_POLL_SECONDS)
                report = _get_json(location, token, timeout_seconds)
                if report.get("properties", {}).get("provisioningState") in (
                    "Succeeded",
                    "Failed",
                ) or report.get("value") is not None:
                    break
            rows = report.get("value")
            if rows is None:
                errors.append(f"{label}: benefit utilization report did not complete.")
                continue
            for row in rows:
                summaries.append(
                    {
                        "subscriptionId": subscription_id,
                        "subscriptionName": label,
                        "benefitId": str(
                            (row.get("grouping") or {}).get("value")
                            or row.get("benefitId")
                            or ""
                        ),
                        "usageDate": str(row.get("usageDate") or "")[:10],
                        "grain": grain,
                        "utilizedPercentage": (
                            float(row["utilizedPercentage"])
                            if row.get("utilizedPercentage") is not None
                            else None
                        ),
                        "requiredCommitment": (
                            float(row["requiredCommitment"])
                            if row.get("requiredCommitment") is not None
                            else None
                        ),
                        "utilizedAmount": (
                            float(row["utilizedAmount"])
                            if row.get("utilizedAmount") is not None
                            else None
                        ),
                    }
                )
        except HTTPError as error:
            errors.append(f"{label}: {_http_error_message(error)}")
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            errors.append(f"{label}: {error}")
    return {"summaries": summaries, "errors": errors}
