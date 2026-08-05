import json
from datetime import date
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from api.cost import CostManagementError
from api.cost_details import CostDetailsReportProvider


class FakeToken:
    token = "cost-details-token"


class FakeCredential:
    def get_token(self, _scope):
        return FakeToken()


class FakeResponse:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.buffer = BytesIO(
            body
            if isinstance(body, bytes)
            else json.dumps(body).encode("utf-8")
        )
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, size=-1):
        return self.buffer.read(size)


CSV_REPORT = """date,meterCategory,serviceFamily,consumedService,SubscriptionId,ResourceId,billingCurrency,costInBillingCurrency
07/01/2026,Virtual Machines,Compute,Microsoft.Compute,SUB-1,/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1,USD,10.25
07/01/2026,Virtual Machines,Compute,Microsoft.Compute,SUB-1,/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1,USD,2.75
07/02/2026,Support,Other,Microsoft.Support,SUB-1,,USD,5.00
"""


class CostDetailsReportProviderTests(unittest.TestCase):
    @patch("api.cost_details.urlopen")
    def test_generates_polls_downloads_and_aggregates_report(
        self,
        mock_urlopen,
    ):
        mock_urlopen.side_effect = [
            FakeResponse(
                status=202,
                headers={
                    "Location": "https://management.azure.com/operation/123",
                    "Retry-After": "20",
                },
            ),
            FakeResponse(
                {
                    "status": "Completed",
                    "manifest": {
                        "blobs": [
                            {"blobLink": "https://storage.invalid/report.csv?sas"}
                        ]
                    },
                }
            ),
            FakeResponse(CSV_REPORT.encode("utf-8")),
        ]
        events = []
        sleeps = []
        provider = CostDetailsReportProvider(
            credential=FakeCredential(),
            sleep=sleeps.append,
        )

        records = provider.fetch_scope(
            "SUB-1",
            "ActualCost",
            date(2026, 7, 1),
            date(2026, 7, 2),
            attempt_callback=events.append,
        )

        self.assertEqual(sleeps, [20])
        self.assertEqual(
            [event["status"] for event in events],
            ["accepted", "succeeded"],
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["amount"], 13.0)
        self.assertEqual(
            records[1]["resourceId"],
            "/subscriptions/sub-1",
        )
        self.assertEqual(
            records[0]["source"],
            "azure_cost_details_report",
        )
        start_request = mock_urlopen.call_args_list[0].args[0]
        self.assertEqual(start_request.method, "POST")
        self.assertEqual(
            json.loads(start_request.data),
            {
                "metric": "ActualCost",
                "timePeriod": {
                    "start": "2026-07-01",
                    "end": "2026-07-02",
                },
            },
        )
        blob_request = mock_urlopen.call_args_list[2].args[0]
        self.assertIsNone(blob_request.get_header("Authorization"))

    @patch("api.cost_details.urlopen")
    def test_surfaces_generate_report_api_error_without_signed_url(
        self,
        mock_urlopen,
    ):
        mock_urlopen.side_effect = HTTPError(
            "https://management.azure.com/test",
            403,
            "Forbidden",
            {},
            BytesIO(
                json.dumps(
                    {"error": {"message": "Managed identity lacks access."}}
                ).encode("utf-8")
            ),
        )
        provider = CostDetailsReportProvider(
            credential=FakeCredential(),
            sleep=lambda _delay: None,
        )

        with self.assertRaisesRegex(
            CostManagementError,
            "Managed identity lacks access",
        ) as raised:
            provider.fetch_scope(
                "sub-1",
                "AmortizedCost",
                date(2026, 7, 1),
                date(2026, 7, 2),
            )

        self.assertEqual(raised.exception.status_code, 403)

    def test_rejects_cross_month_report(self):
        provider = CostDetailsReportProvider(
            credential=FakeCredential(),
            sleep=lambda _delay: None,
        )

        with self.assertRaisesRegex(ValueError, "within one month"):
            provider.fetch_scope(
                "sub-1",
                "ActualCost",
                date(2026, 6, 1),
                date(2026, 7, 1),
            )


if __name__ == "__main__":
    unittest.main()
