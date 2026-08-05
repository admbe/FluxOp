from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase
from api.remediation import (
    correlation_key,
    disk_task,
    package_console_script,
    package_csv,
    servicenow_form_url,
)


FINDING = {
    "resourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/"
    "Microsoft.Compute/disks/data-disk-01",
    "resourceName": "data-disk-01",
    "source": "flux_intelligence",
    "kind": "unattached_disk",
    "confidence": "High",
    "confidenceScore": 0.92,
    "firstSeen": "2026-07-01T00:00:00",
    "lastSeen": "2026-08-02T00:00:00",
    "ageDays": 32,
    "consecutiveCount": 30,
    "reason": "The managed disk is unattached.",
    "subscriptionId": "sub-1",
    "subscriptionName": "prod-sub",
    "resourceGroup": "rg",
    "region": "westus3",
    "currentSku": "Premium_LRS",
    "estimatedMonthlySavings": 42.5,
    "actualMonthlyCost": 42.5,
    "savingsCurrency": "USD",
}


class RemediationTaskTests(unittest.TestCase):
    def test_correlation_key_is_stable_and_distinct(self):
        first = correlation_key("unattached_disk", FINDING["resourceId"])
        second = correlation_key(
            "unattached_disk", FINDING["resourceId"].upper()
        )
        self.assertEqual(first, second)
        self.assertNotEqual(
            first, correlation_key("aged_snapshot", FINDING["resourceId"])
        )

    def test_disk_task_carries_required_content(self):
        task = disk_task(
            FINDING,
            {"application-owner": {"value": "Hannes Garber", "source": "imported"}},
            "https://flux.example.com",
        )
        self.assertIn("data-disk-01", task["title"])
        self.assertEqual(task["financials"]["estimatedAnnualSavings"], 510.0)
        self.assertEqual(task["ownership"]["applicationOwner"], "Hannes Garber")
        self.assertTrue(task["validationSteps"])
        self.assertIn("snapshot", task["rollbackPlan"].lower())
        self.assertIn("portal.azure.com", task["links"]["azureResource"])
        csv_text = package_csv([task], assignment_group="AzureCloud_CF")
        self.assertIn("correlation_id", csv_text.splitlines()[0])
        self.assertIn("data-disk-01", csv_text)
        self.assertIn("AzureCloud_CF", csv_text)
        self.assertIn("servicenow_prefilled_form_url", csv_text.splitlines()[0])

    def test_servicenow_form_url_prefills_planned_task(self):
        from urllib.parse import unquote

        task = disk_task(FINDING, {}, "https://flux.example.com")
        task["description"] += " caret^separator"
        url = servicenow_form_url(
            task, "https://instance.service-now.com"
        )
        self.assertTrue(
            url.startswith(
                "https://instance.service-now.com/planned_task.do"
                "?sys_id=-1&sysparm_query="
            )
        )
        decoded = unquote(url.split("sysparm_query=", 1)[1])
        # Exactly the two prefill fields: one '^' separator survives.
        self.assertEqual(decoded.count("^"), 1)
        self.assertIn("short_description=", decoded)
        self.assertIn("^description=", decoded)
        self.assertIn(task["correlationKey"], decoded)
        self.assertIn("caret-separator", decoded)
        self.assertLess(len(url), 8000)

    def test_console_script_is_gated_and_deduplicated(self):
        task = disk_task(FINDING, {}, "https://flux.example.com")
        task["description"] += ' quotes " and \\backslash and </script>'
        script = package_console_script(
            [task, task],
            "AzureCloud_CF",
            priority="3",
            configuration_item="Azure AD (Entra)",
            due_days=30,
            limit=1,
        )
        self.assertIn(task["correlationKey"], script)
        self.assertIn('"AzureCloud_CF"', script)
        self.assertIn('"planned_task"', script)
        # Field defaults from the task owner: priority, CI, due date.
        self.assertIn('const PRIORITY = "3"', script)
        self.assertIn('"Azure AD (Entra)"', script)
        self.assertIn("DUE_DAYS = 30", script)
        self.assertIn("cmdb_ci: ciId", script)
        self.assertIn("due_date: due", script)
        # limit trims the embedded batch: one task despite two passed.
        # The key appears twice per embedded task (field + description).
        self.assertEqual(script.count(task["correlationKey"]), 2)
        # Nothing is created without an explicit confirm().
        self.assertIn("if (!confirm(", script)
        # Re-runs skip records that already carry the correlation ID.
        self.assertIn("descriptionLIKE", script)
        # Session token, never credentials.
        self.assertIn("X-UserToken", script)
        self.assertNotIn("password", script.lower())
        # Prints a paste-ready Flux reconcile command at the end.
        self.assertIn("/api/remediation/reconcile", script)
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if node:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(script)
                path = handle.name
            check = subprocess.run(
                [node, "--check", path], capture_output=True, text=True
            )
            Path(path).unlink()
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_savings_falls_back_to_disk_cost(self):
        finding = dict(FINDING)
        finding.pop("estimatedMonthlySavings")
        task = disk_task(finding, {}, "https://flux.example.com")
        self.assertEqual(
            task["financials"]["estimatedMonthlySavings"], 42.5
        )
        self.assertEqual(
            task["financials"]["estimatedAnnualSavings"], 510.0
        )

    def test_package_filters_reemits_and_suppresses(self):
        def disk_finding(name, savings):
            resource_id = (
                "/subscriptions/sub-1/resourceGroups/rg/providers/"
                f"Microsoft.Compute/disks/{name}"
            )
            return {
                "findingId": f"unattached_disk:{resource_id}",
                "ruleId": "unattached_disk",
                "source": "flux_intelligence",
                "resourceId": resource_id,
                "relatedResourceId": "",
                "subscriptionId": "sub-1",
                "subscriptionName": "prod-sub",
                "resourceType": "microsoft.compute/disks",
                "resourceGroup": "rg",
                "region": "westus3",
                "category": "Cost",
                "impact": "High",
                "confidence": "High",
                "title": f"{name}: Unattached managed disk",
                "reason": "The managed disk is unattached.",
                "evidence": {},
                "estimatedMonthlySavings": savings,
                "savingsCurrency": "USD",
                "ruleVersion": "test",
            }

        def disk_resource(name):
            return {
                "resourceId": (
                    "/subscriptions/sub-1/resourceGroups/rg/providers/"
                    f"Microsoft.Compute/disks/{name}"
                ),
                "name": name,
                "resourceType": "microsoft.compute/disks",
                "subscriptionId": "sub-1",
                "subscriptionName": "prod-sub",
                "resourceGroup": "rg",
                "region": "westus3",
                "tags": {},
            }

        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "package.duckdb")
            database.init()
            database.store_snapshot(
                "package-snap",
                [disk_resource("disk-big"), disk_resource("disk-small")],
                intelligence=[
                    disk_finding("disk-big", 12.0),
                    disk_finding("disk-small", 0.30),
                ],
                intelligence_collected=True,
            )
            package = database.remediation_package(minimum_monthly_cost=5.0)
            names = [t["resource"]["diskName"] for t in package["tasks"]]
            self.assertEqual(names, ["disk-big"])
            self.assertEqual(package["skippedActiveRemediations"], [])
            # Re-downloading while merely 'exported' re-emits the task.
            again = database.remediation_package(minimum_monthly_cost=5.0)
            self.assertEqual(
                [t["resource"]["diskName"] for t in again["tasks"]],
                ["disk-big"],
            )
            # The sub-threshold disk was never lifecycle-registered.
            registered = {
                row["resourceId"].rsplit("/", 1)[-1]
                for row in database.remediation_status()
            }
            self.assertEqual(registered, {"disk-big"})
            # Once filed in ServiceNow, the task stops regenerating.
            key = package["tasks"][0]["correlationKey"]
            database.remediation_reconcile(
                [
                    {
                        "correlationKey": key,
                        "taskNumber": "TASK0164893",
                        "status": "submitted",
                    }
                ]
            )
            suppressed = database.remediation_package(
                minimum_monthly_cost=5.0
            )
            self.assertEqual(suppressed["tasks"], [])
            self.assertEqual(suppressed["skippedActiveRemediations"], [key])

    def test_lifecycle_prevents_duplicates_until_terminal(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "remediation.duckdb")
            database.init()
            key = correlation_key("unattached_disk", FINDING["resourceId"])
            with database.operational_connect() as db:
                db.execute(
                    "INSERT INTO remediation_tasks VALUES "
                    "(?, 'unattached_disk', ?, 'exported', '', '', '{}',"
                    " now(), now())",
                    [key, FINDING["resourceId"].lower()],
                )
            reconciled = database.remediation_reconcile(
                [
                    {
                        "correlationKey": key,
                        "taskNumber": "TASK0012345",
                        "status": "approved",
                    },
                    {"correlationKey": key, "status": "not-a-status"},
                ]
            )
            self.assertEqual(reconciled["applied"], 1)
            self.assertEqual(reconciled["rejected"], 1)
            status = database.remediation_status()
            self.assertEqual(status[0]["taskNumber"], "TASK0012345")
            self.assertEqual(status[0]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
