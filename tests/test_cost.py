import json
from contextlib import redirect_stdout
from io import BytesIO, StringIO
import unittest
from datetime import date
from unittest.mock import patch
from urllib.error import HTTPError

from api.cost import (
    CostManagementError,
    CostManagementProvider,
    SharedRequestGate,
    query_month_count,
    sleep_with_output,
)


class FakeToken:
    token = "cost-token"


class FakeCredential:
    def __init__(self):
        self.scope = ""

    def get_token(self, scope):
        self.scope = scope
        return FakeToken()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def cost_payload(amount):
    return {
        "properties": {
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "ResourceId", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": [
                [
                    amount,
                    "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
                    "USD",
                ]
            ],
        }
    }


def commitment_payload(amount=25.0):
    return {
        "properties": {
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "ResourceGuid", "type": "String"},
                {"name": "PricingModel", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": [
                [
                    amount,
                    "00000000-0000-0000-0000-000000000001",
                    "Reservation",
                    "USD",
                ]
            ],
        }
    }


def daily_payload(amount=25.0):
    return {
        "properties": {
            "columns": [
                {"name": "Cost", "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "ResourceId", "type": "String"},
                {"name": "ServiceName", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": [
                [
                    amount,
                    20260720,
                    "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
                    "Virtual Machines",
                    "USD",
                ]
            ],
        }
    }


class CostManagementProviderTests(unittest.TestCase):
    @patch("api.cost.urlopen")
    def test_fetches_actual_and_amortized_cost_with_lineage(self, mock_urlopen):
        mock_urlopen.side_effect = [
            FakeResponse(cost_payload(12.5)),
            FakeResponse(cost_payload(10.0)),
        ]
        credential = FakeCredential()
        provider = CostManagementProvider(
            credential=credential,
            sleep=lambda _: None,
        )

        result = provider.fetch(
            {
                "subscriptions": [
                    {"subscriptionId": "SUB-1", "label": "Production"}
                ]
            }
        )

        self.assertEqual(
            credential.scope,
            "https://management.azure.com/.default",
        )
        self.assertEqual(
            result.completed_scopes,
            [("sub-1", "ActualCost"), ("sub-1", "AmortizedCost")],
        )
        self.assertEqual(
            [record["costType"] for record in result.records],
            ["ActualCost", "AmortizedCost"],
        )
        self.assertEqual(result.records[0]["amount"], 12.5)
        self.assertEqual(result.records[0]["currency"], "USD")
        self.assertEqual(
            result.records[0]["source"],
            "azure_cost_management_query",
        )
        self.assertTrue(result.records[0]["periodStart"])
        self.assertTrue(result.records[0]["periodEnd"])

    def test_continues_remaining_scopes_after_persistent_throttling(self):
        credential = FakeCredential()
        provider = CostManagementProvider(
            credential=credential,
            sleep=lambda _: None,
        )
        with patch.object(
            provider,
            "_query_subscription",
            side_effect=CostManagementError("throttled", status_code=429),
        ) as query:
            result = provider.fetch(
                {
                    "subscriptions": [
                        {"subscriptionId": "sub-1", "label": "One"},
                        {"subscriptionId": "sub-2", "label": "Two"},
                    ]
                }
            )

        self.assertEqual(query.call_count, 4)
        self.assertEqual(result.completed_scopes, [])
        self.assertEqual(len(result.warnings), 4)

    @patch("api.cost.urlopen")
    def test_daily_scope_reports_each_throttle_retry(self, mock_urlopen):
        throttled = HTTPError(
            "https://management.azure.com/test",
            429,
            "Too many requests",
            {
                "Retry-After": "1",
                "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "7",
            },
            BytesIO(
                json.dumps(
                    {"error": {"message": "Please retry."}}
                ).encode("utf-8")
            ),
        )
        mock_urlopen.side_effect = [
            throttled,
            FakeResponse(daily_payload()),
        ]
        events = []
        provider = CostManagementProvider(
            credential=FakeCredential(),
            max_retries=1,
            sleep=lambda _: None,
        )

        records = provider.fetch_daily_scope(
            "sub-1",
            "AmortizedCost",
            date(2026, 7, 1),
            date(2026, 7, 25),
            attempt_callback=events.append,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            [event["status"] for event in events],
            ["retrying", "succeeded"],
        )
        self.assertEqual(events[0]["statusCode"], 429)
        self.assertEqual(events[0]["retryAfterSeconds"], 7)

    @patch("api.cost.urlopen")
    def test_current_scope_reports_throttle_retry(self, mock_urlopen):
        throttled = HTTPError(
            "https://management.azure.com/test",
            429,
            "Too many requests",
            {"Retry-After": "9"},
            BytesIO(
                json.dumps(
                    {"error": {"message": "Please retry."}}
                ).encode("utf-8")
            ),
        )
        mock_urlopen.side_effect = [
            throttled,
            FakeResponse(cost_payload(25.0)),
        ]
        events = []
        provider = CostManagementProvider(
            credential=FakeCredential(),
            max_retries=1,
            sleep=lambda _: None,
        )

        records = provider.fetch_scope(
            "sub-1",
            "ActualCost",
            attempt_callback=events.append,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            [event["status"] for event in events],
            ["retrying", "succeeded"],
        )
        self.assertEqual(events[0]["retryAfterSeconds"], 9)

    @patch("api.cost.urlopen")
    def test_fetches_commitment_cost_by_meter_and_pricing_model(
        self,
        mock_urlopen,
    ):
        mock_urlopen.return_value = FakeResponse(commitment_payload())
        provider = CostManagementProvider(
            credential=FakeCredential(),
            sleep=lambda _: None,
        )

        records = provider.fetch_commitment_scope("SUB-1")

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["meterId"],
            "00000000-0000-0000-0000-000000000001",
        )
        self.assertEqual(records[0]["pricingModel"], "Reservation")
        request_body = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(request_body["type"], "ActualCost")
        self.assertEqual(
            [item["name"] for item in request_body["dataset"]["grouping"]],
            ["ResourceGuid", "PricingModel"],
        )
        self.assertEqual(
            request_body["dataset"]["filter"]["dimensions"]["values"],
            ["Usage"],
        )
        self.assertEqual(
            mock_urlopen.call_args.args[0].get_header("Clienttype"),
            "FluxFinOps",
        )

    @patch("api.cost.urlopen")
    def test_fetches_custom_daily_cost_for_resource_and_service(
        self,
        mock_urlopen,
    ):
        mock_urlopen.return_value = FakeResponse(daily_payload())
        provider = CostManagementProvider(
            credential=FakeCredential(),
            sleep=lambda _: None,
        )

        records = provider.fetch_daily_scope(
            "SUB-1",
            "AmortizedCost",
            date(2026, 7, 1),
            date(2026, 7, 22),
        )

        self.assertEqual(records[0]["usageDate"], "2026-07-20")
        self.assertEqual(records[0]["serviceName"], "Virtual Machines")
        request_body = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(request_body["timeframe"], "Custom")
        self.assertEqual(request_body["dataset"]["granularity"], "Daily")
        self.assertEqual(
            [item["name"] for item in request_body["dataset"]["grouping"]],
            ["ResourceId", "ServiceName"],
        )

    def test_query_month_count_matches_cost_management_qpu_window(self):
        self.assertEqual(
            query_month_count(date(2026, 7, 1), date(2026, 7, 25)),
            1,
        )
        self.assertEqual(
            query_month_count(date(2026, 5, 1), date(2026, 7, 25)),
            3,
        )


class WatchdogSafeSleepTests(unittest.TestCase):
    """Long pacing and throttle waits must never present the WebJob
    watchdog with 120 quiet seconds."""

    def test_short_sleep_stays_silent_and_unchunked(self):
        naps: list[float] = []
        output = StringIO()
        with redirect_stdout(output):
            sleep_with_output(naps.append, 45, "[pace] waiting")
        self.assertEqual(naps, [45.0])
        self.assertEqual(output.getvalue(), "")

    def test_long_sleep_chunks_and_emits_output(self):
        naps: list[float] = []
        output = StringIO()
        with redirect_stdout(output):
            sleep_with_output(naps.append, 240, "[pace] waiting")
        self.assertEqual(naps, [30.0] * 8)
        lines = output.getvalue().strip().splitlines()
        # A line after every chunk except the last: the longest silent
        # stretch is one 30-second chunk.
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[0], "[pace] waiting: waited 30s of 240s.")

    def test_shared_gate_pace_emits_output_on_long_waits(self):
        class FakeGateDatabase:
            def __init__(self):
                self.remaining = 240.0

            def claim_throttle_slot(self, name, interval_seconds):
                return self.remaining

        state = FakeGateDatabase()
        naps: list[float] = []

        def nap(seconds: float) -> None:
            naps.append(seconds)
            state.remaining -= seconds

        gate = SharedRequestGate(state, sleep=nap)
        output = StringIO()
        with redirect_stdout(output):
            gate.pace(240)
        self.assertEqual(naps, [30.0] * 8)
        # Output begins once 60 cumulative seconds have passed and then
        # follows every chunk, so silence stays far below the threshold.
        lines = output.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 7)
        self.assertIn("[pace] cost-management: waited 60s", lines[0])


if __name__ == "__main__":
    unittest.main()
