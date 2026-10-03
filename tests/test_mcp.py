"""POST /mcp — read-only governed tools for AI agents.

Stateless MCP (streamable-HTTP, JSON responses). Off unless both
FLUX_MCP_ENABLED and FLUX_MCP_TOKEN are set; every request needs the
bearer token; tools are read-only and governed by the same validators as
the SQL console."""
import dataclasses
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app, settings as app_settings


def enabled_settings():
    return dataclasses.replace(
        app_settings, mcp_enabled=True, mcp_token="test-token"
    )


def rpc(client, method, params=None, request_id=1, token="test-token"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers)


class McpEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_disabled_by_default_is_404(self):
        resp = rpc(self.client, "initialize")
        self.assertEqual(resp.status_code, 404)

    def test_wrong_token_is_401(self):
        with patch("api.main.settings", enabled_settings()):
            resp = rpc(self.client, "initialize", token="wrong")
            self.assertEqual(resp.status_code, 401)

    def test_initialize_and_tools_list(self):
        with patch("api.main.settings", enabled_settings()):
            init = rpc(self.client, "initialize").json()
            self.assertEqual(init["result"]["serverInfo"]["name"], "flux-finops")
            tools = rpc(self.client, "tools/list").json()["result"]["tools"]
            names = {tool["name"] for tool in tools}
            self.assertEqual(
                names,
                {"semantic_catalog", "semantic_query", "semantic_sql",
                 "source_freshness"},
            )

    def test_notification_returns_202_without_body(self):
        with patch("api.main.settings", enabled_settings()):
            resp = self.client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={"Authorization": "Bearer test-token"},
            )
            self.assertEqual(resp.status_code, 202)

    def test_semantic_sql_tool_executes(self):
        with patch("api.main.settings", enabled_settings()), \
             patch("api.main.database") as mock_db:
            db = mock_db.connect.return_value.__enter__.return_value
            cursor = db.execute.return_value
            cursor.fetchall.return_value = [["Virtual Machines", 12.5]]
            cursor.description = [
                ("service_name", "VARCHAR"), ("total", "DOUBLE"),
            ]
            resp = rpc(self.client, "tools/call", {
                "name": "semantic_sql",
                "arguments": {
                    "sql": (
                        "SELECT service_name, SUM(amount) AS total "
                        "FROM semantic_daily_cost GROUP BY 1"
                    )
                },
            })
        result = resp.json()["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["columns"], ["service_name", "total"])
        self.assertEqual(payload["rows"], [["Virtual Machines", 12.5]])

    def test_ddl_surfaces_as_tool_error_not_execution(self):
        with patch("api.main.settings", enabled_settings()):
            resp = rpc(self.client, "tools/call", {
                "name": "semantic_sql",
                "arguments": {"sql": "DROP TABLE semantic_daily_cost"},
            })
        result = resp.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Only SELECT", result["content"][0]["text"])

    def test_unknown_method_is_json_rpc_error(self):
        with patch("api.main.settings", enabled_settings()):
            resp = rpc(self.client, "bogus/method")
            self.assertEqual(resp.json()["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
