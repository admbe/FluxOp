from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class TelemetryError(RuntimeError):
    pass


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _request_json(
    request: Request, *, timeout: int = 60, retries: int = 4, delay_ms: int = 0
) -> dict[str, Any]:
    for attempt in range(retries):
        if delay_ms:
            time.sleep(delay_ms / 1000)
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == retries - 1:
                detail = error.read().decode("utf-8", errors="replace")[:500]
                raise TelemetryError(f"HTTP {error.code}: {detail}") from error
            retry_after = int(error.headers.get("Retry-After", "0") or 0)
            time.sleep(max(retry_after, 2**attempt))
        except (TimeoutError, URLError) as error:
            if attempt == retries - 1:
                raise TelemetryError(str(error)) from error
            time.sleep(2**attempt)
    raise TelemetryError("Telemetry request failed.")


class AzureMonitorProvider:
    API_VERSION = "2023-10-01"
    METRIC_NAMESPACE = "microsoft.compute/virtualMachines"
    MAX_BATCH_SIZE = 50
    METRICS = (
        "Percentage CPU",
        "Network In Total",
        "Network Out Total",
        "Disk Read Operations/Sec",
        "Disk Write Operations/Sec",
    )

    def __init__(self, credential: Any, endpoint: str, days: int = 14):
        self.credential = credential
        self.endpoint = endpoint.rstrip("/")
        self.days = days

    @staticmethod
    def _region(value: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    @classmethod
    def _batches(
        cls, resources: list[dict[str, Any]]
    ) -> list[tuple[str, str, list[dict[str, Any]]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for resource in resources:
            grouped[
                (
                    str(resource.get("subscriptionId") or "").strip(),
                    cls._region(resource.get("region")),
                )
            ].append(resource)
        return [
            (subscription_id, region, items[offset : offset + cls.MAX_BATCH_SIZE])
            for (subscription_id, region), items in grouped.items()
            for offset in range(0, len(items), cls.MAX_BATCH_SIZE)
        ]

    def _summaries(
        self,
        resource_id: str,
        metrics: list[dict[str, Any]],
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for metric in metrics:
            values: list[float] = []
            timestamps: list[str] = []
            for series in metric.get("timeseries", []):
                for point in series.get("data", []):
                    value = next(
                        (
                            point.get(key)
                            for key in ("average", "maximum", "total")
                            if point.get(key) is not None
                        ),
                        None,
                    )
                    if value is not None:
                        values.append(float(value))
                        timestamps.append(str(point.get("timeStamp") or ""))
            if not values:
                continue
            expected = max(1, self.days * 24)
            summaries.append(
                {
                    "resourceId": resource_id.lower(),
                    "source": "azure_monitor",
                    "metric": metric.get("name", {}).get("value", ""),
                    "unit": metric.get("unit", ""),
                    "windowStart": start,
                    "windowEnd": end,
                    "sampleCount": len(values),
                    "coveragePercent": min(100.0, len(values) / expected * 100),
                    "average": sum(values) / len(values),
                    "p95": _percentile(values, 0.95),
                    "maximum": max(values),
                    "lastValue": values[-1],
                    "lastObservedAt": timestamps[-1] if timestamps else end.isoformat(),
                }
            )
        return summaries

    def fetch(
        self, resources: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        token = self.credential.get_token(
            "https://metrics.monitor.azure.com/.default"
        ).token
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=self.days)
        summaries: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        for subscription_id, region, batch in self._batches(resources):
            if not subscription_id or not region:
                message = "A subscription ID and Azure region are required for batch metrics."
                attempts.extend(
                    {
                        "resourceId": resource["resourceId"].lower(),
                        "source": "azure_monitor",
                        "status": "error",
                        "metricCount": 0,
                        "message": message,
                    }
                    for resource in batch
                )
                continue
            query = urlencode(
                {
                    "api-version": self.API_VERSION,
                    "metricnames": ",".join(self.METRICS),
                    "metricnamespace": self.METRIC_NAMESPACE,
                    "starttime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "endtime": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "interval": "PT1H",
                }
            )
            url = (
                f"https://{region}.metrics.monitor.azure.com/subscriptions/"
                f"{quote(subscription_id, safe='')}/metrics:getBatch?{query}"
            )
            request = Request(
                url,
                data=json.dumps(
                    {"resourceids": [resource["resourceId"] for resource in batch]}
                ).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                payload = _request_json(request)
            except Exception as error:
                attempts.extend(
                    {
                        "resourceId": resource["resourceId"].lower(),
                        "source": "azure_monitor",
                        "status": "error",
                        "metricCount": 0,
                        "message": str(error)[:500],
                    }
                    for resource in batch
                )
                continue
            results = {
                str(result.get("resourceid") or "").lower(): result
                for result in payload.get("values", [])
            }
            for resource in batch:
                resource_id = resource["resourceId"].lower()
                result = results.get(resource_id)
                if result is None:
                    attempts.append(
                        {
                            "resourceId": resource_id,
                            "source": "azure_monitor",
                            "status": "error",
                            "metricCount": 0,
                            "message": "Azure Monitor returned no result for this resource.",
                        }
                    )
                    continue
                metrics = result.get("value", [])
                resource_summaries = self._summaries(
                    resource_id, metrics, start, end
                )
                summaries.extend(resource_summaries)
                metric_count = len(resource_summaries)
                metric_errors = [
                    metric
                    for metric in metrics
                    if str(metric.get("errorCode") or "Success").lower() != "success"
                ]
                status = (
                    "covered"
                    if metric_count
                    else "error"
                    if metric_errors
                    else "no_data"
                )
                if metric_count:
                    message = f"{metric_count} metric summaries collected."
                    if metric_errors:
                        message += f" {len(metric_errors)} metrics returned errors."
                elif metric_errors:
                    first_error = metric_errors[0]
                    message = str(
                        first_error.get("errorMessage")
                        or first_error.get("errorCode")
                        or "Azure Monitor metric query failed."
                    )[:500]
                else:
                    message = (
                        "Azure Monitor returned no platform metric data for the "
                        "requested window."
                    )
                attempts.append(
                    {
                        "resourceId": resource_id,
                        "source": "azure_monitor",
                        "status": status,
                        "metricCount": metric_count,
                        "message": message,
                    }
                )
        return summaries, attempts


class AmaLogAnalyticsProvider:
    """Guest-level VM metrics from the AMA/DCR Log Analytics workspace.

    Platform metrics cannot see inside the guest, so memory utilization --
    the evidence right-sizing needs to say a CPU-idle VM is or is not
    memory-constrained -- is absent without an agent. The tenant's AMA baseline
    DCRs land Windows "% Committed Bytes In Use" and Linux "% Used Memory"
    in the central Log Analytics workspace's Perf table; this reads them
    and emits one "Memory Used Percentage" summary per VM in the shape
    store_telemetry_summaries expects. CPU is deliberately not read here:
    the platform metric already covers it, and a second CPU series would
    only create source disagreement.
    """

    SOURCE = "ama_log_analytics"
    ENDPOINT = "https://api.loganalytics.io"
    METRIC = "Memory Used Percentage"

    def __init__(self, credential: Any, workspace_id: str, days: int = 14):
        self.credential = credential
        self.workspace_id = workspace_id.strip()
        self.days = days

    def fetch(
        self, resources: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        token = self.credential.get_token(f"{self.ENDPOINT}/.default").token
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=self.days)
        # Counter names are localized to each VM's OS display language, so
        # matching English names alone silently drops part of the fleet:
        # verified live 2026-08-02, 13 German-locale VMs emit
        # "Arbeitsspeicher / Zugesicherte verwendete Bytes (%)". Selecting
        # the percentage counter under the memory object is robust to
        # counter-name localization; only the object-name list needs
        # extending for a new OS language. The DCRs collect exactly one
        # percentage memory counter per platform (Windows "% Committed
        # Bytes In Use", Linux "% Used Memory"), so this cannot average
        # unlike series together.
        query = (
            "Perf"
            f"| where TimeGenerated between (datetime({start.isoformat()}) "
            f".. datetime({end.isoformat()}))"
            '| where ObjectName in ("Memory", "Arbeitsspeicher")'
            '| where CounterName contains "%"'
            "| summarize value = avg(CounterValue) "
            "by bin(TimeGenerated, 1h), ResourceId = tolower(_ResourceId)"
            "| order by TimeGenerated asc"
        )
        request = Request(
            f"{self.ENDPOINT}/v1/workspaces/"
            f"{quote(self.workspace_id, safe='')}/query",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        payload = _request_json(request, timeout=120)
        tables = payload.get("tables") or []
        columns = [
            str(column.get("name") or "")
            for column in (tables[0].get("columns") if tables else [])
        ]
        try:
            time_index = columns.index("TimeGenerated")
            resource_index = columns.index("ResourceId")
            value_index = columns.index("value")
        except ValueError as error:
            raise TelemetryError(
                f"Unexpected Log Analytics response columns: {columns}"
            ) from error
        series: dict[str, tuple[list[float], list[str]]] = {}
        for row in tables[0].get("rows", []):
            resource_id = str(row[resource_index] or "").lower()
            value = row[value_index]
            if not resource_id or value is None:
                continue
            values, timestamps = series.setdefault(resource_id, ([], []))
            values.append(float(value))
            timestamps.append(str(row[time_index] or ""))

        expected = max(1, self.days * 24)
        summaries: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        for resource in resources:
            resource_id = str(resource.get("resourceId") or "").lower()
            values, timestamps = series.get(resource_id, ([], []))
            if values:
                summaries.append(
                    {
                        "resourceId": resource_id,
                        "source": self.SOURCE,
                        "metric": self.METRIC,
                        "unit": "Percent",
                        "windowStart": start,
                        "windowEnd": end,
                        "sampleCount": len(values),
                        "coveragePercent": min(
                            100.0, len(values) / expected * 100
                        ),
                        "average": sum(values) / len(values),
                        "p95": _percentile(values, 0.95),
                        "maximum": max(values),
                        "lastValue": values[-1],
                        "lastObservedAt": timestamps[-1]
                        or end.isoformat(),
                        "aggregationMethod": "hourly average of guest counters",
                        "lineage": {
                            "workspaceId": self.workspace_id,
                            "table": "Perf",
                            "counters": [
                                "% Committed Bytes In Use",
                                "% Used Memory",
                            ],
                        },
                    }
                )
                attempts.append(
                    {
                        "resourceId": resource_id,
                        "source": self.SOURCE,
                        "status": "covered",
                        "metricCount": 1,
                        "message": (
                            f"{len(values)} hourly guest memory samples."
                        ),
                    }
                )
            else:
                attempts.append(
                    {
                        "resourceId": resource_id,
                        "source": self.SOURCE,
                        "status": "no_data",
                        "metricCount": 0,
                        "message": (
                            "No AMA guest memory samples in the workspace "
                            "for this VM (agent not reporting yet, VM "
                            "excluded, or policy remediation pending)."
                        ),
                    }
                )
        return summaries, attempts


def _property_map(device: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in (
        "systemProperties",
        "autoProperties",
        "customProperties",
        "inheritedProperties",
    ):
        for item in device.get(key) or []:
            name = str(item.get("name") or "").strip().lower()
            if name:
                result[name] = str(item.get("value") or "").strip()
    return result


def _host_key(value: str) -> str:
    return value.strip().lower().split(".", 1)[0].replace(" ", "")


class LogicMonitorProvider:
    FIELDS = (
        "id,name,displayName,description,hostStatus,deviceType,hostGroupIds,"
        "systemProperties,autoProperties,customProperties,inheritedProperties"
    )

    def __init__(self, account: str, token: str, group_ids: tuple[str, ...], delay_ms: int = 250):
        if not account or not token:
            raise TelemetryError("LogicMonitor account or bearer token is not configured.")
        self.base_url = f"https://{account}.logicmonitor.com/santaba/rest"
        self.group_ids = group_ids
        self.delay_ms = delay_ms
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Version": "3",
            "Accept": "application/json",
        }

    METRIC_SPECS = (
        {
            "platform": "windows",
            "datasource": "Microsoft_Windows_CPU",
            "instances": "first",
            "points": {
                "CPUBusyPercent": ("Percentage CPU", "Percent", 1.0, False),
            },
        },
        {
            "platform": "windows",
            "datasource": "WinMemory64",
            "instances": "first",
            "points": {
                "AvailableBytes": ("Available Memory", "Bytes", 1.0, False),
            },
        },
        {
            "platform": "windows",
            "datasource": "WinLogicalDrivePerformance-",
            "instances": "windows_disk",
            "points": {
                "DiskReadsPerSec": (
                    "Disk Read Operations/Sec",
                    "CountPerSecond",
                    1.0,
                    False,
                ),
                "DiskWritesPerSec": (
                    "Disk Write Operations/Sec",
                    "CountPerSecond",
                    1.0,
                    False,
                ),
                "DiskReadBytesPerSec": (
                    "Disk Read Bytes/Sec",
                    "BytesPerSecond",
                    1.0,
                    False,
                ),
                "DiskWriteBytesPerSec": (
                    "Disk Write Bytes/Sec",
                    "BytesPerSecond",
                    1.0,
                    False,
                ),
            },
        },
        {
            "platform": "windows",
            "datasource": "WinIf-",
            "instances": "windows_network",
            "points": {
                "BytesReceivedPerSec": (
                    "Network In Total",
                    "BytesPerSecond",
                    1.0,
                    False,
                ),
                "BytesSentPerSec": (
                    "Network Out Total",
                    "BytesPerSecond",
                    1.0,
                    False,
                ),
            },
        },
        {
            "platform": "linux",
            "datasource": "NetSNMPCPUwithCores",
            "instances": "first",
            "points": {
                "CPUBusyPercent": ("Percentage CPU", "Percent", 1.0, False),
            },
        },
        {
            "platform": "linux",
            "datasource": "NetSNMP_Memory_Usage",
            "instances": "first",
            "points": {
                "ActiveMemoryPercent": (
                    "Memory Used Percentage",
                    "Percent",
                    1.0,
                    False,
                ),
            },
        },
        {
            "platform": "linux",
            "datasource": "NetSNMPdiskIO-",
            "instances": "linux_disk",
            "points": {
                "ReadOperations": (
                    "Disk Read Operations/Sec",
                    "CountPerSecond",
                    1.0,
                    True,
                ),
                "WriteOperations": (
                    "Disk Write Operations/Sec",
                    "CountPerSecond",
                    1.0,
                    True,
                ),
                "BytesRead64bit": (
                    "Disk Read Bytes/Sec",
                    "BytesPerSecond",
                    1.0,
                    True,
                ),
                "BytesWritten64bit": (
                    "Disk Write Bytes/Sec",
                    "BytesPerSecond",
                    1.0,
                    True,
                ),
            },
        },
        {
            "platform": "linux",
            "datasource": "SNMP_Network_Interfaces",
            "instances": "linux_network",
            "points": {
                "InMbps": (
                    "Network In Total",
                    "BytesPerSecond",
                    1_000_000 / 8,
                    False,
                ),
                "OutMbps": (
                    "Network Out Total",
                    "BytesPerSecond",
                    1_000_000 / 8,
                    False,
                ),
            },
        },
    )

    def _paged(
        self,
        path: str,
        *,
        fields: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            separator = "&" if "?" in path else "?"
            url = (
                f"{self.base_url}{path}{separator}size=1000&offset={offset}"
                f"&fields={quote(fields or self.FIELDS)}"
            )
            page = _request_json(
                Request(url, headers=self.headers), delay_ms=self.delay_ms
            ).get("items", [])
            items.extend(page)
            if len(page) < 1000:
                return items
            offset += len(page)

    def discover(self) -> list[dict[str, Any]]:
        devices: dict[str, dict[str, Any]] = {}
        for group_id in self.group_ids:
            for device in self._paged(f"/device/groups/{quote(group_id)}/devices"):
                item = dict(device)
                item["_fluxPlatform"] = (
                    "Windows"
                    if str(group_id) == "5"
                    else "Linux"
                    if str(group_id) == "4"
                    else "Unknown"
                )
                item["_fluxGroupId"] = str(group_id)
                existing = devices.get(str(device.get("id")))
                if (
                    existing
                    and existing.get("_fluxPlatform") in {"Windows", "Linux"}
                ):
                    continue
                devices[str(device.get("id"))] = item
        return list(devices.values())

    @staticmethod
    def match(
        devices: list[dict[str, Any]], resources: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_id = {item["resourceId"].lower(): item for item in resources}
        by_name: dict[str, list[dict[str, Any]]] = {}
        for resource in resources:
            by_name.setdefault(_host_key(resource["name"]), []).append(resource)
        matches: list[dict[str, Any]] = []
        for device in devices:
            props = _property_map(device)
            resource_ids = {
                value.lower()
                for key, value in props.items()
                if "azureresourceid" in key and value
            }
            candidates = [by_id[value] for value in resource_ids if value in by_id]
            method = "azure_resource_id"
            if not candidates:
                names = {
                    _host_key(str(value))
                    for value in (
                        device.get("name"),
                        device.get("displayName"),
                        props.get("system.hostname"),
                    )
                    if value
                }
                candidates = [
                    resource
                    for name in names
                    for resource in by_name.get(name, [])
                ]
                method = "hostname"
            unique = {item["resourceId"].lower(): item for item in candidates}
            status = "matched" if len(unique) == 1 else "ambiguous" if unique else "unmatched"
            resource_id = next(iter(unique)) if status == "matched" else ""
            matches.append(
                {
                    "source": "logicmonitor",
                    "sourceResourceId": str(device.get("id") or ""),
                    "sourceName": str(device.get("displayName") or device.get("name") or ""),
                    "resourceId": resource_id,
                    "status": status,
                    "method": method if unique else "",
                    "confidence": "high" if status == "matched" else "review",
                    "details": {
                        "hostStatus": device.get("hostStatus"),
                        "candidateCount": len(unique),
                        "platform": device.get("_fluxPlatform") or "Unknown",
                        "groupId": device.get("_fluxGroupId") or "",
                    },
                }
            )
        return matches

    @staticmethod
    def _instance_label(instance: dict[str, Any]) -> str:
        for name in ("displayName", "wildAlias", "wildValue", "name", "id"):
            value = str(instance.get(name) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _select_instances(
        cls,
        instances: list[dict[str, Any]],
        mode: str,
        maximum: int,
    ) -> list[dict[str, Any]]:
        active = [
            item for item in instances if not bool(item.get("stopMonitoring"))
        ]
        if mode == "first":
            return active[:1]
        if mode == "windows_disk":
            total = [
                item
                for item in active
                if re.search(r"^_?total$", cls._instance_label(item), re.I)
            ]
            if total:
                return total[:1]
            active = [
                item
                for item in active
                if not re.search(
                    r"^harddiskvolume|_total|total$",
                    cls._instance_label(item),
                    re.I,
                )
            ]
        elif mode == "linux_disk":
            preferred = [
                item
                for item in active
                if re.search(
                    r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|dm-\d+)$",
                    cls._instance_label(item),
                    re.I,
                )
            ]
            active = preferred or [
                item
                for item in active
                if not re.search(
                    r"^(loop|ram|fd|sr\d+$)",
                    cls._instance_label(item),
                    re.I,
                )
            ]
        elif mode == "windows_network":
            active = [
                item
                for item in active
                if not re.search(
                    r"loopback|teredo|isatap|tunnel|pseudo",
                    cls._instance_label(item),
                    re.I,
                )
            ]
        elif mode == "linux_network":
            active = [
                item
                for item in active
                if not re.search(
                    r"loopback|lo \[|id:1\]",
                    cls._instance_label(item),
                    re.I,
                )
            ]
        return active[:maximum]

    def _data_sources(self, device_id: str) -> list[dict[str, Any]]:
        return self._paged(
            f"/device/devices/{quote(device_id)}/devicedatasources"
            "?filter=instanceNumber%3E0",
            fields="id,dataSourceName,instanceNumber",
        )

    def _instances(
        self,
        device_id: str,
        datasource_id: str,
    ) -> list[dict[str, Any]]:
        return self._paged(
            f"/device/devices/{quote(device_id)}/devicedatasources/"
            f"{quote(datasource_id)}/instances",
            fields="id,name,displayName,wildValue,wildAlias,stopMonitoring",
        )

    def _series(
        self,
        device_id: str,
        datasource_id: str,
        instance_id: str,
        datapoints: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[tuple[datetime, float]]]:
        stores: dict[str, dict[datetime, float]] = {
            name: {} for name in datapoints
        }
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(hours=4))
            query = urlencode(
                {
                    "start": int(cursor.timestamp()),
                    "end": int(chunk_end.timestamp()),
                    "format": "data",
                    "datapoints": ",".join(datapoints),
                }
            )
            request = Request(
                f"{self.base_url}/device/devices/{quote(device_id)}/"
                f"devicedatasources/{quote(datasource_id)}/instances/"
                f"{quote(instance_id)}/data?{query}",
                headers=self.headers,
            )
            response = _request_json(
                request,
                delay_ms=self.delay_ms,
            )
            payload = response.get("data") or response
            returned = list(payload.get("dataPoints") or [])
            timestamps = list(payload.get("time") or [])
            rows = list(payload.get("values") or [])
            for raw_time, row in zip(timestamps, rows):
                numeric_time = float(raw_time)
                observed = datetime.fromtimestamp(
                    numeric_time / 1000 if numeric_time > 10_000_000_000 else numeric_time,
                    timezone.utc,
                )
                for index, point in enumerate(returned):
                    if point not in stores or index >= len(row):
                        continue
                    try:
                        stores[point][observed] = float(row[index])
                    except (TypeError, ValueError):
                        continue
            cursor = chunk_end + timedelta(seconds=1)
        return {
            name: sorted(values.items())
            for name, values in stores.items()
        }

    @staticmethod
    def _rates(
        values: list[tuple[datetime, float]],
    ) -> list[tuple[datetime, float]]:
        rates: list[tuple[datetime, float]] = []
        for previous, current in zip(values, values[1:]):
            seconds = (current[0] - previous[0]).total_seconds()
            delta = current[1] - previous[1]
            if seconds > 0 and delta >= 0:
                rates.append((current[0], delta / seconds))
        return rates

    def fetch_metrics(
        self,
        target: dict[str, Any],
        start: datetime,
        end: datetime,
        *,
        maximum_instances: int = 8,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        device_id = str(target["sourceResourceId"])
        resource_id = str(target["resourceId"]).lower()
        platform = str(target.get("platform") or "").lower()
        datasources = self._data_sources(device_id)
        by_name = {
            str(item.get("dataSourceName") or ""): item
            for item in datasources
        }
        if platform not in {"windows", "linux"}:
            platform = (
                "windows"
                if any(name.startswith(("Microsoft_Windows_", "Win")) for name in by_name)
                else "linux"
                if any(name.startswith(("NetSNMP", "SNMP_")) for name in by_name)
                else ""
            )

        aggregate: dict[
            tuple[str, str], dict[datetime, float]
        ] = defaultdict(dict)
        warnings: list[str] = []
        used_datasources: list[str] = []
        for spec in self.METRIC_SPECS:
            if spec["platform"] != platform:
                continue
            datasource = by_name.get(str(spec["datasource"]))
            if not datasource:
                continue
            datasource_id = str(datasource.get("id") or "")
            used_datasources.append(str(spec["datasource"]))
            try:
                instances = self._select_instances(
                    self._instances(device_id, datasource_id),
                    str(spec["instances"]),
                    maximum_instances,
                )
                for instance in instances:
                    series = self._series(
                        device_id,
                        datasource_id,
                        str(instance.get("id") or ""),
                        list(spec["points"]),
                        start,
                        end,
                    )
                    for datapoint, mapping in spec["points"].items():
                        metric, unit, multiplier, counter = mapping
                        values = series.get(datapoint, [])
                        if counter:
                            values = self._rates(values)
                        bucket = aggregate[(metric, unit)]
                        for observed, value in values:
                            bucket[observed] = (
                                bucket.get(observed, 0.0)
                                + value * float(multiplier)
                            )
            except Exception as error:
                warnings.append(
                    f"{spec['datasource']}: {str(error)[:300]}"
                )

        samples = [
            {
                "resourceId": resource_id,
                "source": "logicmonitor",
                "sourceResourceId": device_id,
                "metric": metric,
                "unit": unit,
                "observedAt": observed,
                "value": value,
                "lineage": {
                    "sourceSystem": "LogicMonitor",
                    "deviceId": device_id,
                    "platform": platform or "unknown",
                    "windowStart": start.isoformat(),
                    "windowEnd": end.isoformat(),
                    "datasources": sorted(set(used_datasources)),
                    "method": "checkpointed_incremental_v1",
                },
            }
            for (metric, unit), values in aggregate.items()
            for observed, value in sorted(values.items())
        ]
        return samples, warnings
