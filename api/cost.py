from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from azure.core.credentials import TokenCredential
from filelock import FileLock


class CostManagementError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class CostFetchResult:
    records: list[dict[str, Any]] = field(default_factory=list)
    completed_scopes: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _azure_error(error: HTTPError) -> str:
    raw = error.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
        detail = payload.get("error") or {}
        return detail.get("message") or detail.get("code") or raw
    except json.JSONDecodeError:
        return raw


def _header_float(headers: Any, name: str) -> float | None:
    value = headers.get(name) if headers is not None else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _column_index(columns: list[dict[str, Any]], name: str) -> int | None:
    expected = name.casefold()
    for index, column in enumerate(columns):
        if str(column.get("name", "")).casefold() == expected:
            return index
    return None


def _usage_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise CostManagementError(
            "Cost Management daily response contained an empty UsageDate."
        )
    if text.isdigit() and len(text) == 8:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError as error:
        raise CostManagementError(
            f"Cost Management returned an invalid UsageDate: {text}"
        ) from error


def query_month_count(start_date: date, end_date: date) -> int:
    """Estimate Cost Management Query API QPUs for a date range."""
    if end_date < start_date:
        return 1
    return (
        (end_date.year - start_date.year) * 12
        + end_date.month
        - start_date.month
        + 1
    )


def sleep_with_output(
    sleep: Callable[[float], None],
    seconds: float,
    describe: str,
    *,
    quiet_below: float = 60.0,
    chunk_seconds: float = 30.0,
) -> None:
    """Sleep in chunks, emitting a progress line as each chunk completes.

    The WebJob watchdog kills a triggered job after 120 seconds without
    output or CPU activity, and pacing or throttle waits routinely exceed
    that (a twelve-month monthly query paces request-delay x 12 = 240s by
    default). Sleeps shorter than ``quiet_below`` stay silent.
    """
    remaining = max(float(seconds), 0.0)
    if remaining <= 0:
        return
    if remaining < quiet_below:
        sleep(remaining)
        return
    total = remaining
    waited = 0.0
    while remaining > 0:
        chunk = min(remaining, chunk_seconds)
        sleep(chunk)
        waited += chunk
        remaining -= chunk
        if remaining > 0:
            print(
                f"{describe}: waited {int(waited)}s of {int(total)}s.",
                flush=True,
            )


class SharedRequestGate:
    """Cross-process request pacing through the operational control plane.

    Replaces the historical file-based gate: every process observes the same
    spacing and 429 cooldowns because the state lives in one shared row.
    """

    def __init__(
        self,
        database: Any,
        name: str = "cost-management",
        sleep: Callable[[float], None] = time.sleep,
        tenant_key: str = "default",
        qpu_windows: list[tuple[int, float]] | None = None,
        minimum_interval_seconds: float = 0.0,
    ):
        self.database = database
        self.name = name
        self.sleep = sleep
        self.tenant_key = tenant_key.strip() or "default"
        self.qpu_windows = qpu_windows or [(10, 6), (60, 30), (3600, 300)]
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)

    @property
    def quota_name(self) -> str:
        return f"{self.name}:{self.tenant_key}"

    def pace(self, interval_seconds: float, estimated_qpu: float = 1) -> str | None:
        # Chunked sleeps with periodic output: a long pace (240s for a
        # twelve-month query) or a shared 429 cooldown must not present the
        # WebJob watchdog with 120 quiet seconds.
        waited = 0.0
        while True:
            quota_claim = getattr(
                self.database, "claim_cost_management_quota", None
            )
            if quota_claim is not None:
                wait, reservation_id = quota_claim(
                    self.quota_name,
                    estimated_qpu,
                    self.qpu_windows,
                    minimum_interval_seconds=max(
                        self.minimum_interval_seconds,
                        interval_seconds,
                    ),
                )
            else:
                wait = self.database.claim_throttle_slot(self.name, interval_seconds)
                reservation_id = None
            if wait <= 0:
                return reservation_id
            chunk = min(wait, 30.0)
            self.sleep(chunk)
            waited += chunk
            if waited >= 60:
                print(
                    f"[pace] {self.name if self.tenant_key == 'default' else self.quota_name}: waited {int(waited)}s for a "
                    f"request slot ({max(int(wait - chunk), 0)}s estimated "
                    "remaining).",
                    flush=True,
                )

    def register_cooldown(self, cooldown_seconds: float) -> None:
        quota_cooldown = getattr(
            self.database, "register_cost_management_quota_cooldown", None
        )
        if quota_cooldown is not None:
            quota_cooldown(self.quota_name, cooldown_seconds)
        else:
            self.database.register_throttle_cooldown(
                self.name, cooldown_seconds
            )

    def reconcile(
        self,
        reservation_id: str | None,
        consumed_qpu: float | None,
    ) -> None:
        reconcile = getattr(
            self.database, "reconcile_cost_management_quota", None
        )
        if reconcile is not None:
            reconcile(
                self.quota_name,
                reservation_id,
                consumed_qpu=consumed_qpu,
            )


class CostManagementProvider:
    def __init__(
        self,
        *,
        credential: TokenCredential,
        management_endpoint: str = "https://management.azure.com",
        api_version: str = "2025-03-01",
        timeout_seconds: int = 120,
        max_retries: int = 3,
        request_delay_seconds: float = 0,
        request_gate_path: Path | None = None,
        request_gate: SharedRequestGate | None = None,
        client_type: str = "FluxFinOps",
        tenant_key: str = "default",
        qpu_windows: list[tuple[int, float]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.credential = credential
        self.management_endpoint = management_endpoint.rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.request_delay_seconds = max(0, request_delay_seconds)
        self.request_gate_path = request_gate_path
        self.request_gate = request_gate
        self.client_type = client_type.strip() or "FluxFinOps"
        self.tenant_key = tenant_key.strip() or "default"
        self.qpu_windows = qpu_windows or [(10, 6), (60, 30), (3600, 300)]
        self.sleep = sleep
        self.clock = clock
        self._last_request_at = 0.0

    @staticmethod
    def _gate_state(raw: str) -> tuple[float, float]:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return (
                    float(value.get("lastRequestAt") or 0),
                    float(value.get("blockedUntil") or 0),
                )
            return float(value), 0.0
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0.0, 0.0

    @staticmethod
    def _write_gate_state(
        path: Path,
        *,
        last_request_at: float,
        blocked_until: float,
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "lastRequestAt": last_request_at,
                    "blockedUntil": blocked_until,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def _pace_request(self, estimated_qpu: int) -> str | None:
        interval = self.request_delay_seconds * max(1, estimated_qpu)
        if self.request_gate is not None:
            return self.request_gate.pace(interval, estimated_qpu)
        if interval <= 0:
            return None
        if self.request_gate_path is None:
            delay = interval - (self.clock() - self._last_request_at)
            if self._last_request_at and delay > 0:
                sleep_with_output(
                    self.sleep, delay, "[pace] waiting for a request slot"
                )
            self._last_request_at = self.clock()
            return None

        state_path = self.request_gate_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(state_path) + ".lock", timeout=300):
            try:
                last_request_at, blocked_until = self._gate_state(
                    state_path.read_text(encoding="utf-8")
                )
            except FileNotFoundError:
                last_request_at, blocked_until = 0.0, 0.0
            now = self.clock()
            delay = max(
                interval - (now - last_request_at)
                if last_request_at
                else 0,
                blocked_until - now,
            )
            if delay > 0:
                sleep_with_output(
                    self.sleep, delay, "[pace] waiting for a request slot"
                )
            self._write_gate_state(
                state_path,
                last_request_at=self.clock(),
                blocked_until=blocked_until,
            )
            return None

    def _register_throttle(self, delay: float) -> None:
        if self.request_gate is not None:
            self.request_gate.register_cooldown(delay)
            return
        if self.request_gate_path is None:
            return
        state_path = self.request_gate_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(state_path) + ".lock", timeout=300):
            try:
                last_request_at, blocked_until = self._gate_state(
                    state_path.read_text(encoding="utf-8")
                )
            except FileNotFoundError:
                last_request_at, blocked_until = self._last_request_at, 0.0
            self._write_gate_state(
                state_path,
                last_request_at=last_request_at,
                blocked_until=max(blocked_until, self.clock() + delay),
            )

    def _request(
        self,
        url: str,
        body: bytes,
        access_token: str,
        *,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
        estimated_qpu: int = 1,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            reservation_id = self._pace_request(estimated_qpu)
            request = Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "ClientType": self.client_type,
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    response_headers = getattr(response, "headers", {})
                    consumed_qpu = _header_float(
                        response_headers,
                        "x-ms-ratelimit-microsoft.costmanagement-qpu-consumed",
                    )
                    remaining_qpu = _header_float(
                        response_headers,
                        "x-ms-ratelimit-microsoft.costmanagement-qpu-remaining",
                    )
                    if self.request_gate is not None:
                        self.request_gate.reconcile(reservation_id, consumed_qpu)
                    if attempt_callback:
                        attempt_callback(
                            {
                                "attemptNumber": attempt + 1,
                                "status": "succeeded",
                                "statusCode": getattr(response, "status", 200),
                                "retryAfterSeconds": None,
                                "qpuConsumed": consumed_qpu,
                                "qpuRemaining": remaining_qpu,
                                "message": "Cost Management request succeeded.",
                            }
                        )
                    return payload
            except HTTPError as error:
                detail = _azure_error(error)
                if error.code in {429, 503} and attempt < self.max_retries:
                    attempt += 1
                    retry_after = (
                        error.headers.get(
                            "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after"
                        )
                        or error.headers.get(
                            "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after"
                        )
                        or error.headers.get(
                            "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after"
                        )
                        or error.headers.get(
                            "x-ms-ratelimit-microsoft.consumption-retry-after"
                        )
                        or error.headers.get("Retry-After")
                        or str(min(2**attempt, 30))
                    )
                    try:
                        delay = max(float(retry_after), 1)
                    except ValueError:
                        delay = min(2**attempt, 30)
                    if attempt_callback:
                        attempt_callback(
                            {
                                "attemptNumber": attempt,
                                "status": "retrying",
                                "statusCode": error.code,
                                "retryAfterSeconds": delay,
                                "qpuConsumed": _header_float(
                                    error.headers,
                                    "x-ms-ratelimit-microsoft.costmanagement-qpu-consumed",
                                ),
                                "qpuRemaining": _header_float(
                                    error.headers,
                                    "x-ms-ratelimit-microsoft.costmanagement-qpu-remaining",
                                ),
                                "message": detail,
                            }
                        )
                    self._register_throttle(delay)
                    sleep_with_output(
                        self.sleep,
                        delay,
                        f"[throttle] HTTP {error.code}, retrying after "
                        f"{int(delay)}s",
                    )
                    continue
                if attempt_callback:
                    attempt_callback(
                        {
                            "attemptNumber": attempt + 1,
                            "status": "failed",
                            "statusCode": error.code,
                            "retryAfterSeconds": None,
                            "message": detail,
                        }
                    )
                raise CostManagementError(
                    f"Cost Management returned HTTP {error.code}: {detail}",
                    status_code=error.code,
                ) from error
            except (URLError, TimeoutError) as error:
                if attempt_callback:
                    attempt_callback(
                        {
                            "attemptNumber": attempt + 1,
                            "status": "failed",
                            "statusCode": None,
                            "retryAfterSeconds": None,
                            "message": str(error),
                        }
                    )
                raise CostManagementError(
                    f"Cost Management request failed: {error}"
                ) from error
            except json.JSONDecodeError as error:
                if attempt_callback:
                    attempt_callback(
                        {
                            "attemptNumber": attempt + 1,
                            "status": "failed",
                            "statusCode": 200,
                            "retryAfterSeconds": None,
                            "message": "Cost Management returned invalid JSON.",
                        }
                    )
                raise CostManagementError(
                    "Cost Management returned an invalid JSON response."
                ) from error

    def _query_subscription(
        self,
        *,
        subscription_id: str,
        cost_type: str,
        access_token: str,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        url = (
            f"{self.management_endpoint}/subscriptions/{subscription_id}"
            "/providers/Microsoft.CostManagement/query"
            f"?api-version={self.api_version}"
        )
        body = json.dumps(
            {
                "type": cost_type,
                "timeframe": "MonthToDate",
                "dataset": {
                    "granularity": "None",
                    "aggregation": {
                        "totalCost": {
                            "name": "Cost",
                            "function": "Sum",
                        }
                    },
                    "grouping": [
                        {
                            "type": "Dimension",
                            "name": "ResourceId",
                        }
                    ],
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            payload = self._request(
                next_url,
                body,
                access_token,
                attempt_callback=attempt_callback,
                estimated_qpu=1,
            )
            properties = payload.get("properties") or {}
            columns = properties.get("columns") or []
            cost_index = _column_index(columns, "Cost")
            resource_index = _column_index(columns, "ResourceId")
            currency_index = _column_index(columns, "Currency")
            if cost_index is None:
                raise CostManagementError(
                    "Cost Management response did not contain a Cost column."
                )
            for row in properties.get("rows") or []:
                resource_id = (
                    str(row[resource_index] or "").lower()
                    if resource_index is not None
                    else ""
                )
                currency = (
                    str(row[currency_index] or "")
                    if currency_index is not None
                    else ""
                )
                rows.append(
                    {
                        "subscriptionId": subscription_id.lower(),
                        "resourceId": resource_id,
                        "costType": cost_type,
                        "amount": float(row[cost_index] or 0),
                        "currency": currency,
                        "source": "azure_cost_management_query",
                    }
                )
            next_url = properties.get("nextLink") or None
        return rows

    def _query_commitment_subscription(
        self,
        *,
        subscription_id: str,
        access_token: str,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Return usage cost grouped at the grain needed for commitment analysis.

        Cost Management Query supports at most two groupings. Resource cost keeps
        its existing ResourceId grain; this independent query uses ResourceGuid
        (the Query API's supported meter identifier) and PricingModel so Toolkit
        eligibility can be joined without overstating the result as charge-level
        utilization.
        """
        url = (
            f"{self.management_endpoint}/subscriptions/{subscription_id}"
            "/providers/Microsoft.CostManagement/query"
            f"?api-version={self.api_version}"
        )
        body = json.dumps(
            {
                "type": "ActualCost",
                "timeframe": "MonthToDate",
                "dataset": {
                    "granularity": "None",
                    "aggregation": {
                        "totalCost": {
                            "name": "Cost",
                            "function": "Sum",
                        }
                    },
                    "filter": {
                        "dimensions": {
                            "name": "ChargeType",
                            "operator": "In",
                            "values": ["Usage"],
                        }
                    },
                    "grouping": [
                        {"type": "Dimension", "name": "ResourceGuid"},
                        {"type": "Dimension", "name": "PricingModel"},
                    ],
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            payload = self._request(
                next_url,
                body,
                access_token,
                attempt_callback=attempt_callback,
                estimated_qpu=1,
            )
            properties = payload.get("properties") or {}
            columns = properties.get("columns") or []
            cost_index = _column_index(columns, "Cost")
            meter_index = _column_index(columns, "ResourceGuid")
            pricing_index = _column_index(columns, "PricingModel")
            currency_index = _column_index(columns, "Currency")
            if cost_index is None or meter_index is None or pricing_index is None:
                raise CostManagementError(
                    "Cost Management commitment response did not contain "
                    "Cost, ResourceGuid, and PricingModel columns."
                )
            for row in properties.get("rows") or []:
                rows.append(
                    {
                        "subscriptionId": subscription_id.lower(),
                        "meterId": str(row[meter_index] or "").lower(),
                        "pricingModel": str(row[pricing_index] or "Unknown"),
                        "amount": float(row[cost_index] or 0),
                        "currency": (
                            str(row[currency_index] or "")
                            if currency_index is not None
                            else ""
                        ),
                        "source": "azure_cost_management_query",
                    }
                )
            next_url = properties.get("nextLink") or None
        return rows

    def _query_daily_subscription(
        self,
        *,
        subscription_id: str,
        cost_type: str,
        start_date: date,
        end_date: date,
        access_token: str,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        url = (
            f"{self.management_endpoint}/subscriptions/{subscription_id}"
            "/providers/Microsoft.CostManagement/query"
            f"?api-version={self.api_version}"
        )
        start = datetime.combine(
            start_date, datetime_time.min, tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        end = datetime.combine(
            end_date, datetime_time.max, tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        body = json.dumps(
            {
                "type": cost_type,
                "timeframe": "Custom",
                "timePeriod": {"from": start, "to": end},
                "dataset": {
                    "granularity": "Daily",
                    "aggregation": {
                        "totalCost": {
                            "name": "Cost",
                            "function": "Sum",
                        }
                    },
                    "grouping": [
                        {"type": "Dimension", "name": "ResourceId"},
                        {"type": "Dimension", "name": "ServiceName"},
                    ],
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        estimated_qpu = query_month_count(start_date, end_date)
        while next_url:
            payload = self._request(
                next_url,
                body,
                access_token,
                attempt_callback=attempt_callback,
                estimated_qpu=estimated_qpu,
            )
            properties = payload.get("properties") or {}
            columns = properties.get("columns") or []
            cost_index = _column_index(columns, "Cost")
            date_index = _column_index(columns, "UsageDate")
            resource_index = _column_index(columns, "ResourceId")
            service_index = _column_index(columns, "ServiceName")
            currency_index = _column_index(columns, "Currency")
            if cost_index is None or date_index is None:
                raise CostManagementError(
                    "Cost Management daily response did not contain "
                    "Cost and UsageDate columns."
                )
            for row in properties.get("rows") or []:
                rows.append(
                    {
                        "usageDate": _usage_date(row[date_index]),
                        "costType": cost_type,
                        "subscriptionId": subscription_id.lower(),
                        "resourceId": (
                            str(row[resource_index] or "").lower()
                            if resource_index is not None
                            else ""
                        ),
                        "serviceName": (
                            str(row[service_index] or "")
                            if service_index is not None
                            else ""
                        ),
                        "amount": float(row[cost_index] or 0),
                        "currency": (
                            str(row[currency_index] or "")
                            if currency_index is not None
                            else ""
                        ),
                        "source": "azure_cost_management_query",
                    }
                )
            next_url = properties.get("nextLink") or None
        return rows

    def _query_monthly_subscription(
        self,
        *,
        subscription_id: str,
        cost_type: str,
        start_date: date,
        end_date: date,
        access_token: str,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Whole-subscription monthly totals for fiscal-year forecasting.

        No grouping: one row per month and currency keeps the response tiny,
        and the fiscal outlook only needs estate- and subscription-level
        month totals, never per-resource months.
        """
        url = (
            f"{self.management_endpoint}/subscriptions/{subscription_id}"
            "/providers/Microsoft.CostManagement/query"
            f"?api-version={self.api_version}"
        )
        start = datetime.combine(
            start_date, datetime_time.min, tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        end = datetime.combine(
            end_date, datetime_time.max, tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        body = json.dumps(
            {
                "type": cost_type,
                "timeframe": "Custom",
                "timePeriod": {"from": start, "to": end},
                "dataset": {
                    "granularity": "Monthly",
                    "aggregation": {
                        "totalCost": {
                            "name": "Cost",
                            "function": "Sum",
                        }
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        rows: list[dict[str, Any]] = []
        next_url: str | None = url
        estimated_qpu = query_month_count(start_date, end_date)
        while next_url:
            payload = self._request(
                next_url,
                body,
                access_token,
                attempt_callback=attempt_callback,
                estimated_qpu=estimated_qpu,
            )
            properties = payload.get("properties") or {}
            columns = properties.get("columns") or []
            cost_index = _column_index(columns, "Cost")
            # Monthly granularity labels the period column BillingMonth;
            # some api-versions return UsageDate instead.
            month_index = _column_index(columns, "BillingMonth")
            if month_index is None:
                month_index = _column_index(columns, "UsageDate")
            currency_index = _column_index(columns, "Currency")
            if cost_index is None or month_index is None:
                raise CostManagementError(
                    "Cost Management monthly response did not contain "
                    "Cost and BillingMonth columns."
                )
            for row in properties.get("rows") or []:
                month_value = _usage_date(row[month_index])
                rows.append(
                    {
                        "month": f"{month_value[:7]}-01",
                        "costType": cost_type,
                        "subscriptionId": subscription_id.lower(),
                        "amount": float(row[cost_index] or 0),
                        "currency": (
                            str(row[currency_index] or "")
                            if currency_index is not None
                            else ""
                        ),
                        "source": "azure_cost_management_query",
                    }
                )
            next_url = properties.get("nextLink") or None
        return rows

    def fetch_monthly_scope(
        self,
        subscription_id: str,
        cost_type: str,
        start_date: date,
        end_date: date,
        *,
        access_token: str | None = None,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        return self._query_monthly_subscription(
            subscription_id=subscription_id.lower(),
            cost_type=cost_type,
            start_date=start_date,
            end_date=end_date,
            access_token=access_token or self.access_token(),
            attempt_callback=attempt_callback,
        )

    def access_token(self) -> str:
        try:
            return self.credential.get_token(
                f"{self.management_endpoint}/.default"
            ).token
        except Exception as error:
            raise CostManagementError(
                "The Azure identity could not obtain a Cost Management token."
            ) from error

    def fetch_scope(
        self,
        subscription_id: str,
        cost_type: str,
        *,
        access_token: str | None = None,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_subscription = subscription_id.lower()
        records = self._query_subscription(
            subscription_id=normalized_subscription,
            cost_type=cost_type,
            access_token=access_token or self.access_token(),
            attempt_callback=attempt_callback,
        )
        now = datetime.now(timezone.utc)
        period_start = now.date().replace(day=1).isoformat()
        period_end = now.date().isoformat()
        for record in records:
            record["periodStart"] = period_start
            record["periodEnd"] = period_end
        return records

    def fetch_commitment_scope(
        self,
        subscription_id: str,
        *,
        access_token: str | None = None,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        records = self._query_commitment_subscription(
            subscription_id=subscription_id.lower(),
            access_token=access_token or self.access_token(),
            attempt_callback=attempt_callback,
        )
        now = datetime.now(timezone.utc)
        period_start = now.date().replace(day=1).isoformat()
        period_end = now.date().isoformat()
        for record in records:
            record["periodStart"] = period_start
            record["periodEnd"] = period_end
        return records

    def fetch_daily_scope(
        self,
        subscription_id: str,
        cost_type: str,
        start_date: date,
        end_date: date,
        *,
        access_token: str | None = None,
        attempt_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        return self._query_daily_subscription(
            subscription_id=subscription_id.lower(),
            cost_type=cost_type,
            start_date=start_date,
            end_date=end_date,
            access_token=access_token or self.access_token(),
            attempt_callback=attempt_callback,
        )

    def fetch(self, integration: dict[str, Any]) -> CostFetchResult:
        subscriptions = [
            item
            for item in integration.get("subscriptions", [])
            if item.get("subscriptionId")
        ]
        if not subscriptions:
            raise CostManagementError(
                "Add at least one Azure subscription before collecting cost."
            )
        access_token = self.access_token()

        result = CostFetchResult()
        for subscription in subscriptions:
            subscription_id = subscription["subscriptionId"].lower()
            label = subscription.get("label") or subscription_id
            for cost_type in ("ActualCost", "AmortizedCost"):
                try:
                    records = self.fetch_scope(
                        subscription_id,
                        cost_type,
                        access_token=access_token,
                    )
                    result.records.extend(records)
                    result.completed_scopes.append((subscription_id, cost_type))
                except CostManagementError as error:
                    result.warnings.append(f"{label} {cost_type}: {error}")
        return result
