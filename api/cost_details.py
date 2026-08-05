from __future__ import annotations

import csv
from datetime import date, datetime
import gzip
import io
import json
import shutil
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from azure.core.credentials import TokenCredential

from .cost import CostManagementError, SharedRequestGate, sleep_with_output


AttemptCallback = Callable[[dict[str, Any]], None]


def _error_detail(error: HTTPError) -> str:
    raw = error.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
        detail = payload.get("error") or {}
        return str(detail.get("message") or detail.get("code") or raw)
    except json.JSONDecodeError:
        return raw


def _retry_delay(headers: Any, attempt: int) -> float:
    value = (
        headers.get("x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after")
        or headers.get(
            "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after"
        )
        or headers.get(
            "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after"
        )
        or headers.get("x-ms-ratelimit-microsoft.consumption-retry-after")
        or headers.get("Retry-After")
        or str(min(2**attempt, 30))
    )
    try:
        return max(float(value), 1)
    except (TypeError, ValueError):
        return float(min(2**attempt, 30))


def _usage_date(value: Any) -> str:
    text = str(value or "").strip()
    for pattern in ("%m/%d/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError as error:
        raise CostManagementError(
            f"Cost Details returned an invalid date: {text or '<empty>'}"
        ) from error


class CostDetailsReportProvider:
    """Generate and normalize Azure Cost Details reports.

    The API is asynchronous and returns short-lived signed blob links. Links are
    consumed in memory by this provider and are never returned, persisted, or
    logged by Flux.
    """

    def __init__(
        self,
        *,
        credential: TokenCredential,
        management_endpoint: str = "https://management.azure.com",
        api_version: str = "2025-03-01",
        timeout_seconds: int = 120,
        max_retries: int = 3,
        poll_interval_seconds: float = 20,
        max_poll_attempts: int = 30,
        client_type: str = "FluxFinOps",
        request_gate: SharedRequestGate | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.credential = credential
        self.management_endpoint = management_endpoint.rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.poll_interval_seconds = max(1, poll_interval_seconds)
        self.max_poll_attempts = max(1, max_poll_attempts)
        self.client_type = client_type.strip() or "FluxFinOps"
        self.request_gate = request_gate
        self.sleep = sleep

    def _pace_request(self) -> str | None:
        if self.request_gate is None:
            return None
        return self.request_gate.pace(0, 1)

    def access_token(self) -> str:
        try:
            return self.credential.get_token(
                f"{self.management_endpoint}/.default"
            ).token
        except Exception as error:
            raise CostManagementError(
                "The Azure identity could not obtain a Cost Details token."
            ) from error

    def _request_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "ClientType": self.client_type,
        }

    def _start_report(
        self,
        *,
        subscription_id: str,
        cost_type: str,
        start_date: date,
        end_date: date,
        access_token: str,
        attempt_callback: AttemptCallback | None,
    ) -> tuple[dict[str, Any] | None, str | None, float]:
        url = (
            f"{self.management_endpoint}/subscriptions/{subscription_id}"
            "/providers/Microsoft.CostManagement/generateCostDetailsReport"
            f"?api-version={self.api_version}"
        )
        body = json.dumps(
            {
                "metric": cost_type,
                "timePeriod": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        attempt = 0
        while True:
            reservation_id = self._pace_request()
            request = Request(
                url,
                data=body,
                method="POST",
                headers={
                    **self._request_headers(access_token),
                    "Content-Type": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    status = int(getattr(response, "status", 200))
                    raw = response.read()
                    payload = json.loads(raw.decode("utf-8")) if raw else None
                    location = response.headers.get("Location")
                    retry_after = _retry_delay(response.headers, 1)
                    if attempt_callback:
                        attempt_callback(
                            {
                                "attemptNumber": attempt + 1,
                                "status": (
                                    "succeeded" if status == 200 else "accepted"
                                ),
                                "statusCode": status,
                                "retryAfterSeconds": (
                                    retry_after if status == 202 else None
                                ),
                                "message": (
                                    "Cost Details report is ready."
                                    if status == 200
                                    else "Cost Details report generation accepted."
                                ),
                            }
                        )
                    if status == 200:
                        if self.request_gate is not None:
                            self.request_gate.reconcile(reservation_id, 1)
                        return payload, None, 0
                    if status != 202 or not location:
                        raise CostManagementError(
                            "Cost Details report generation did not return "
                            "a completion payload or operation location.",
                            status_code=status,
                        )
                    return (
                        None,
                        urljoin(self.management_endpoint + "/", location),
                        retry_after,
                    )
            except HTTPError as error:
                detail = _error_detail(error)
                if error.code in {429, 503} and attempt < self.max_retries:
                    attempt += 1
                    delay = _retry_delay(error.headers, attempt)
                    if attempt_callback:
                        attempt_callback(
                            {
                                "attemptNumber": attempt,
                                "status": "retrying",
                                "statusCode": error.code,
                                "retryAfterSeconds": delay,
                                "message": detail,
                            }
                        )
                    if self.request_gate is not None:
                        self.request_gate.register_cooldown(delay)
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
                    f"Cost Details returned HTTP {error.code}: {detail}",
                    status_code=error.code,
                ) from error
            except (URLError, TimeoutError) as error:
                raise CostManagementError(
                    f"Cost Details report generation failed: {error}"
                ) from error

    def _poll_report(
        self,
        operation_url: str,
        *,
        access_token: str,
        initial_delay: float,
        attempt_callback: AttemptCallback | None,
    ) -> dict[str, Any]:
        delay = initial_delay or self.poll_interval_seconds
        for poll_number in range(1, self.max_poll_attempts + 1):
            # Azure's Retry-After for report generation can exceed the
            # 120-second WebJob watchdog window; sleep with output.
            sleep_with_output(
                self.sleep,
                delay,
                f"[cost-details] poll {poll_number} waiting {int(delay)}s "
                "for report generation",
            )
            reservation_id = self._pace_request()
            request = Request(
                operation_url,
                method="GET",
                headers=self._request_headers(access_token),
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    status = int(getattr(response, "status", 200))
                    raw = response.read()
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                    if status == 200:
                        if self.request_gate is not None:
                            self.request_gate.reconcile(reservation_id, 1)
                        if str(payload.get("status") or "").casefold() == "failed":
                            raise CostManagementError(
                                "Azure reported that Cost Details generation failed.",
                                status_code=200,
                            )
                        if attempt_callback:
                            attempt_callback(
                                {
                                    "attemptNumber": poll_number,
                                    "status": "succeeded",
                                    "statusCode": 200,
                                    "retryAfterSeconds": None,
                                    "message": "Cost Details report completed.",
                                }
                            )
                        return payload
                    if status != 202:
                        raise CostManagementError(
                            f"Cost Details operation returned HTTP {status}.",
                            status_code=status,
                        )
                    delay = _retry_delay(response.headers, poll_number)
            except HTTPError as error:
                detail = _error_detail(error)
                if error.code in {429, 503}:
                    retry_delay = _retry_delay(error.headers, poll_number)
                    if self.request_gate is not None:
                        self.request_gate.register_cooldown(retry_delay)
                    delay = retry_delay
                    if attempt_callback:
                        attempt_callback(
                            {
                                "attemptNumber": poll_number,
                                "status": "retrying",
                                "statusCode": error.code,
                                "retryAfterSeconds": retry_delay,
                                "message": detail,
                            }
                        )
                    continue
                raise CostManagementError(
                    f"Cost Details operation returned HTTP {error.code}: {detail}",
                    status_code=error.code,
                ) from error
            except (URLError, TimeoutError) as error:
                raise CostManagementError(
                    f"Cost Details operation polling failed: {error}"
                ) from error
        raise CostManagementError(
            "Cost Details report did not complete within the polling window.",
            status_code=202,
        )

    @staticmethod
    def _blob_links(payload: dict[str, Any]) -> list[str]:
        manifest = payload.get("manifest") or (
            (payload.get("properties") or {}).get("manifest")
        )
        links = [
            str(item.get("blobLink") or "")
            for item in (manifest or {}).get("blobs") or []
            if item.get("blobLink")
        ]
        if not links:
            raise CostManagementError(
                "Cost Details completed without downloadable report blobs."
            )
        return links

    def _download_rows(self, blob_link: str) -> Iterator[dict[str, str]]:
        # A signed blob URL is intentionally used only inside this method. It is
        # never included in a persisted status message or exception.
        request = Request(blob_link, method="GET", headers={"Accept": "text/csv"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                with tempfile.TemporaryFile(mode="w+b") as temporary:
                    shutil.copyfileobj(response, temporary)
                    temporary.seek(0)
                    compressed = temporary.read(2) == b"\x1f\x8b"
                    temporary.seek(0)
                    binary: Any = (
                        gzip.GzipFile(fileobj=temporary)
                        if compressed
                        else temporary
                    )
                    with io.TextIOWrapper(
                        binary,
                        encoding="utf-8-sig",
                        newline="",
                    ) as text:
                        yield from csv.DictReader(text)
        except HTTPError as error:
            raise CostManagementError(
                f"Cost Details blob download returned HTTP {error.code}.",
                status_code=error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CostManagementError(
                f"Cost Details blob download failed: {error}"
            ) from error

    @staticmethod
    def _normalize_rows(
        rows: Iterable[dict[str, str]],
        *,
        subscription_id: str,
        cost_type: str,
    ) -> list[dict[str, Any]]:
        aggregate: dict[tuple[str, str, str, str], float] = {}
        subscription_resource = f"/subscriptions/{subscription_id}"
        for row in rows:
            item = {
                str(key or "").casefold(): value
                for key, value in row.items()
            }
            usage_date = _usage_date(item.get("date"))
            resource_id = str(
                item.get("resourceid") or subscription_resource
            ).strip().lower()
            service_name = str(
                item.get("metercategory")
                or item.get("servicefamily")
                or item.get("consumedservice")
                or "Unclassified"
            ).strip()
            currency = str(
                item.get("billingcurrency")
                or item.get("billingcurrencycode")
                or ""
            ).strip()
            raw_amount = str(
                item.get("costinbillingcurrency") or "0"
            ).strip()
            try:
                amount = float(raw_amount)
            except ValueError as error:
                raise CostManagementError(
                    "Cost Details returned a non-numeric billing cost."
                ) from error
            key = (usage_date, resource_id, service_name, currency)
            aggregate[key] = aggregate.get(key, 0.0) + amount
        return [
            {
                "usageDate": key[0],
                "costType": cost_type,
                "subscriptionId": subscription_id,
                "resourceId": key[1],
                "serviceName": key[2],
                "amount": amount,
                "currency": key[3],
                "source": "azure_cost_details_report",
            }
            for key, amount in sorted(aggregate.items())
        ]

    def fetch_scope(
        self,
        subscription_id: str,
        cost_type: str,
        start_date: date,
        end_date: date,
        *,
        access_token: str | None = None,
        attempt_callback: AttemptCallback | None = None,
    ) -> list[dict[str, Any]]:
        normalized_subscription = subscription_id.lower()
        if cost_type not in {"ActualCost", "AmortizedCost"}:
            raise ValueError("Cost Details supports ActualCost or AmortizedCost.")
        if end_date < start_date:
            raise ValueError("Cost Details end date must not precede start date.")
        if (
            start_date.year,
            start_date.month,
        ) != (
            end_date.year,
            end_date.month,
        ):
            raise ValueError("A Cost Details request must stay within one month.")
        token = access_token or self.access_token()
        payload, operation_url, retry_after = self._start_report(
            subscription_id=normalized_subscription,
            cost_type=cost_type,
            start_date=start_date,
            end_date=end_date,
            access_token=token,
            attempt_callback=attempt_callback,
        )
        if payload is None:
            payload = self._poll_report(
                operation_url or "",
                access_token=token,
                initial_delay=retry_after,
                attempt_callback=attempt_callback,
            )
        raw_rows = (
            row
            for blob_link in self._blob_links(payload)
            for row in self._download_rows(blob_link)
        )
        return self._normalize_rows(
            raw_rows,
            subscription_id=normalized_subscription,
            cost_type=cost_type,
        )
