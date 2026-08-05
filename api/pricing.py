from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


HOURS_PER_MONTH = 730.0
SOURCE = "azure_retail_prices_api"


def retail_price_stage_parts(
    prices: list[dict[str, Any]],
    *,
    complete: bool,
    collected_at: datetime,
) -> tuple[str, dict[str, Any]]:
    """Staging key and payload for one retail-price collection.

    The key is unique per collection: source freshness only advances when a
    payload is applied, so every run that fetched prices must stage its own
    snapshot even when Azure returns byte-identical rates. A content-only
    key made an unchanged catalogue collide with an earlier collection's
    ledger row and fail closed (StagedPayloadConflict) on every run until
    prices happened to drift. The snapshot id derives from the same stamp
    and digest, keeping the payload a pure function of the key, so the
    fail-closed check still guards against corrupted redelivery.
    """
    digest = hashlib.sha256(
        json.dumps(prices, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]
    stamp = collected_at.strftime("%Y%m%dT%H%M%SZ")
    return (
        f"retail-prices-{stamp}-{digest}",
        {
            "snapshotId": f"retail-{stamp}-{digest}",
            "prices": prices,
            "complete": complete,
        },
    )


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def price_profile(os_type: str, license_type: str) -> tuple[str, str]:
    operating_system = (os_type or "").strip().lower()
    license_value = (license_type or "").strip().lower()
    if operating_system == "linux":
        return "linux", "linux"
    if operating_system == "windows" and license_value == "windows_server":
        return "linux", "azure_hybrid_benefit"
    if operating_system == "windows" and license_value == "windows_client":
        # Azure Virtual Desktop Windows client rights do not add the
        # Windows Server license-included meter to VM compute.
        return "linux", "windows_client_entitlement"
    if operating_system == "windows":
        return "windows", "license_included"
    return "unknown", "unknown"


class AzureRetailPriceProvider:
    def __init__(
        self,
        endpoint: str = "https://prices.azure.com/api/retail/prices",
        api_version: str = "2023-01-01-preview",
        timeout_seconds: int = 30,
        request_delay_ms: int = 100,
        hours_per_month: float = HOURS_PER_MONTH,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.request_delay_ms = max(0, request_delay_ms)
        self.hours_per_month = hours_per_month

    def _request_url(self, request: dict[str, Any]) -> str:
        region = str(request["region"]).replace("'", "''")
        sku = str(request["targetSku"]).replace("'", "''")
        currency = str(request["currency"] or "USD").upper()
        query = (
            "serviceName eq 'Virtual Machines' and "
            f"armRegionName eq '{region}' and "
            f"armSkuName eq '{sku}'"
        )
        parameters = {
            'api-version': self.api_version,
            'currencyCode': f"'{currency}'",
            'meterRegion': "'primary'",
            '$filter': query,
        }
        return f"{self.endpoint}?{urlencode(parameters)}"

    def _pages(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url = url
        page_count = 0
        while next_url and page_count < 5:
            request = Request(
                next_url,
                headers={"User-Agent": "FluxFinOps/RetailPriceValuation"},
            )
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
            items.extend(payload.get("Items") or [])
            next_url = str(payload.get("NextPageLink") or "")
            page_count += 1
        return items

    @staticmethod
    def _base_candidate(
        item: dict[str, Any],
        request: dict[str, Any],
    ) -> bool:
        product = str(item.get("productName") or "")
        meter = str(item.get("meterName") or "")
        if str(item.get("serviceName") or "") != "Virtual Machines":
            return False
        if str(item.get("armRegionName") or "") != request["region"]:
            return False
        if str(item.get("armSkuName") or "") != request["targetSku"]:
            return False
        if item.get("isPrimaryMeterRegion") is not True:
            return False
        if float(item.get("tierMinimumUnits") or 0) != 0:
            return False
        if not product.startswith("Virtual Machines "):
            return False
        if "spot" in meter.lower() or "low priority" in meter.lower():
            return False
        return True

    @staticmethod
    def _profile_candidate(
        item: dict[str, Any], request: dict[str, Any]
    ) -> bool:
        if not AzureRetailPriceProvider._base_candidate(item, request):
            return False
        if str(item.get("type") or "") != "Consumption":
            return False
        if str(item.get("unitOfMeasure") or "").lower() != "1 hour":
            return False
        profile = request["priceProfile"]
        product = str(item.get("productName") or "")
        is_windows = " windows" in product.lower()
        return is_windows if profile == "windows" else not is_windows

    @staticmethod
    def _select_current(
        candidates: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, bool]:
        """Select one current price, reporting ambiguous distinct rates."""
        if not candidates:
            return None, False
        latest_effective = max(
            (_timestamp(item.get("effectiveStartDate")) for item in candidates),
            default=None,
        )
        current = (
            [
                item for item in candidates
                if _timestamp(item.get("effectiveStartDate")) == latest_effective
            ]
            if latest_effective
            else candidates
        )
        distinct_prices = {
            (
                round(float(item.get("retailPrice") or 0), 10),
                str(item.get("currencyCode") or "").upper(),
            )
            for item in current
        }
        if len(distinct_prices) != 1:
            return None, True
        return (
            sorted(
                current,
                key=lambda item: (
                    str(item.get("meterId") or ""),
                    str(item.get("productName") or ""),
                ),
            )[0],
            False,
        )

    def fetch_one(self, request: dict[str, Any]) -> dict[str, Any]:
        base = {
            **request,
            "source": SOURCE,
            "sourceUrl": self._request_url(request),
            "hoursPerMonth": self.hours_per_month,
        }
        if request["priceProfile"] == "unknown":
            return {
                **base,
                "status": "unsupported_os",
                "message": "The VM operating system could not be determined.",
                "candidateCount": 0,
                "raw": {},
            }

        items = self._pages(base["sourceUrl"])
        payg_candidates = [
            item for item in items if self._profile_candidate(item, request)
        ]
        selected, ambiguous = self._select_current(payg_candidates)
        if ambiguous:
            return {
                **base,
                "status": "ambiguous",
                "message": "Current PAYG meters returned distinct rates.",
                "candidateCount": len(payg_candidates),
                "raw": {"candidates": payg_candidates},
            }
        if not selected:
            return {
                **base,
                "status": "not_found",
                "message": "No governed primary hourly consumption meter matched.",
                "candidateCount": 0,
                "raw": {"returnedItems": len(items)},
            }

        hourly_price = float(selected["retailPrice"])

        linux_candidates = [
            item
            for item in items
            if self._base_candidate(item, request)
            and str(item.get("type") or "") == "Consumption"
            and str(item.get("unitOfMeasure") or "").lower() == "1 hour"
            and " windows" not in str(item.get("productName") or "").lower()
        ]
        linux_selected, linux_ambiguous = self._select_current(linux_candidates)
        compute_hourly = (
            float(linux_selected["retailPrice"])
            if linux_selected and not linux_ambiguous
            else hourly_price if request["priceProfile"] == "linux" else None
        )
        license_hourly = (
            max(hourly_price - compute_hourly, 0.0)
            if compute_hourly is not None
            else None
        )

        reservation_candidates = [
            item
            for item in items
            if self._base_candidate(item, request)
            and str(item.get("type") or "") == "Reservation"
            and str(item.get("reservationTerm") or "").strip().lower()
            == "1 year"
            and " windows" not in str(item.get("productName") or "").lower()
        ]
        reservation, reservation_ambiguous = self._select_current(
            reservation_candidates
        )
        ri_upfront = (
            float(reservation["retailPrice"])
            if reservation and not reservation_ambiguous
            else None
        )
        ri_monthly = (
            ri_upfront / 12 + float(license_hourly or 0) * self.hours_per_month
            if ri_upfront is not None and license_hourly is not None
            else None
        )

        savings_plan_rates = [
            float(item.get("retailPrice") or item.get("unitPrice") or 0)
            for item in (selected.get("savingsPlan") or [])
            if str(item.get("term") or "").strip().lower() == "1 year"
        ]
        sp_hourly = (
            savings_plan_rates[0]
            if len({round(value, 10) for value in savings_plan_rates}) == 1
            else None
        )
        return {
            **base,
            "status": "matched",
            "message": (
                "Matched PAYG retail; commitment rates are populated only "
                "when Azure returned one unambiguous 1-year price."
            ),
            "candidateCount": len(payg_candidates),
            "hourlyPrice": hourly_price,
            "monthlyPrice": round(hourly_price * self.hours_per_month, 2),
            "monthlyComputePrice": (
                round(compute_hourly * self.hours_per_month, 2)
                if compute_hourly is not None else None
            ),
            "monthlyLicensePrice": (
                round(float(license_hourly) * self.hours_per_month, 2)
                if license_hourly is not None else None
            ),
            "ri1YearUpfront": round(ri_upfront, 2) if ri_upfront is not None else None,
            "ri1YearMonthly": round(ri_monthly, 2) if ri_monthly is not None else None,
            "sp1YearMonthly": (
                round(sp_hourly * self.hours_per_month, 2)
                if sp_hourly is not None else None
            ),
            "meterId": str(selected.get("meterId") or ""),
            "meterName": str(selected.get("meterName") or ""),
            "productName": str(selected.get("productName") or ""),
            "skuName": str(selected.get("skuName") or ""),
            "unitOfMeasure": str(selected.get("unitOfMeasure") or ""),
            "effectiveStartDate": selected.get("effectiveStartDate"),
            "raw": {
                "payg": selected,
                "reservation1Year": reservation,
                "savingsPlan": selected.get("savingsPlan") or [],
            },
        }

    def fetch(
        self,
        requests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        results = []
        for index, request in enumerate(requests):
            try:
                results.append(self.fetch_one(request))
            except Exception as error:
                results.append(
                    {
                        **request,
                        "source": SOURCE,
                        "sourceUrl": self._request_url(request),
                        "hoursPerMonth": self.hours_per_month,
                        "status": "error",
                        "message": str(error)[:500],
                        "candidateCount": 0,
                        "raw": {},
                    }
                )
            if index < len(requests) - 1 and self.request_delay_ms:
                time.sleep(self.request_delay_ms / 1000)
        return results
