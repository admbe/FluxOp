import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from api.config import Settings
from api.database import FluxDatabase
from api.intelligence_assistant import (
    DeepSeekProvider,
    DocumentationIndex,
    FoundryProvider,
    GovernedToolExecutor,
    IntelligenceAssistant,
    IntelligenceBudgetExceeded,
    IntelligenceProviderError,
    ProviderResult,
    _actions_for_tools,
    _anthropic_translate_messages,
    _anthropic_translate_tools,
    _extract_json,
    assess_response_quality,
    validate_response,
)


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.messages = []

    def complete(self, *, model, messages, tools, user_id):
        self.calls += 1
        self.messages.append(messages)
        if self.calls == 1:
            return ProviderResult(
                message={
                    "role": "assistant",
                    "reasoning_content": "private scratchpad",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "get_report_catalog",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                usage={
                    "prompt_tokens": 100,
                    "cached_prompt_tokens": 20,
                    "completion_tokens": 20,
                },
            )
        return ProviderResult(
            message={
                "role": "assistant",
                "content": json.dumps(
                    {
                        "summary": "Flux has six governed report contracts.",
                        "blocks": [
                            {
                                "type": "markdown",
                                "content": "This is a **retrieved fact**.",
                            }
                        ],
                        "facts": ["Five report contracts were returned."],
                        "interpretations": [],
                        "limitations": ["No live cost scope was requested."],
                        "sources": [
                            {
                                "tool": "get_report_catalog",
                                "description": "Governed catalog",
                            }
                        ],
                        "followUps": ["Show amortized cost."],
                    }
                ),
            },
            usage={
                "prompt_tokens": 200,
                "cached_prompt_tokens": 40,
                "completion_tokens": 80,
            },
        )


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RepairingProvider:
    def __init__(self, repair_succeeds=True):
        self.calls = 0
        self.repair_succeeds = repair_succeeds

    def complete(self, *, model, messages, tools, user_id):
        self.calls += 1
        if self.calls == 1 or not self.repair_succeeds:
            return ProviderResult(
                message={
                    "role": "assistant",
                    "content": "Amortized cost decreased based on governed data.",
                },
                usage={
                    "prompt_tokens": 20,
                    "cached_prompt_tokens": 0,
                    "completion_tokens": 10,
                },
            )
        return ProviderResult(
            message={
                "role": "assistant",
                "content": json.dumps({
                    "summary": "Amortized cost decreased.",
                    "blocks": [{
                        "type": "markdown",
                        "content": "Amortized cost decreased.",
                    }],
                    "facts": ["The governed result decreased."],
                    "interpretations": [],
                    "limitations": [],
                    "sources": [],
                    "followUps": [],
                }),
            },
            usage={
                "prompt_tokens": 30,
                "cached_prompt_tokens": 0,
                "completion_tokens": 20,
            },
        )


class IntelligenceAssistantTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.folder.name) / "assistant.duckdb")
        self.database.init()
        self.settings = Settings(
            intelligence_ai_enabled=True,
            intelligence_ai_provider="deepseek",
            deepseek_api_key="test-only",
            intelligence_ai_stop_at_usd=8,
            intelligence_ai_budget_usd=10,
        )

    def tearDown(self):
        self.folder.cleanup()

    def test_governed_tools_reject_sql_shaped_arguments(self):
        tools = GovernedToolExecutor(self.database, self.settings)
        with self.assertRaisesRegex(ValueError, "Arbitrary SQL"):
            tools.execute("search_inventory", {"sql": "select * from secrets"})
        with self.assertRaisesRegex(ValueError, "not approved"):
            tools.execute("execute_sql", {})

    def test_virtual_tag_showback_is_available_to_ask_flux(self):
        self.database.seed_demo()
        self.database.save_virtual_tag_dimension(
            {"key": "Environment", "name": "Environment"}, actor="tester"
        )
        tools = GovernedToolExecutor(self.database, self.settings)
        result = tools.execute(
            "get_virtual_tag_showback",
            {"dimension": "Environment", "costType": "ActualCost"},
        )
        self.assertEqual(result["dimension"], "Environment")
        self.assertIn("classifiedCost", result["summary"])
        self.assertEqual(
            result["lineage"]["precedence"],
            "manual > imported > rule > native",
        )

    def test_fleet_tools_are_governed_and_return_batches(self):
        self.database.seed_demo()
        tools = GovernedToolExecutor(self.database, self.settings)

        fleet = tools.execute("get_fleet_telemetry", {"resourceType": ""})
        self.assertGreater(fleet["returned"], 1)
        self.assertIn("cpuEvidenceCount", fleet)

        recommendations = tools.execute(
            "get_rightsizing_recommendations", {"limit": 10}
        )
        self.assertIn("items", recommendations)

        # Batch tools remain governed: no SQL passthrough.
        with self.assertRaisesRegex(ValueError, "Arbitrary SQL"):
            tools.execute("get_fleet_telemetry", {"sql": "select 1"})

    def test_rightsizing_plan_tool_reports_human_intent(self):
        self.database.seed_demo()
        created = self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5",
             "strategy": "1-year reservation", "refQuantity": 2,
             "refMonthlySavings": 120.0},
            updated_by="alice",
        )
        checkout_key = (
            "/subscriptions/00000000-0000-0000-0000-000000000001/"
            "resourcegroups/flux-demo/providers/microsoft.compute/"
            "virtualmachines/checkout-api"
        )
        self.database.assign_rightsizing_vms(
            [{"vmKey": checkout_key, "vmName": "checkout-api",
              "bucketKey": created["bucketKey"],
              "decision": "Confirmed", "note": "sized against p95"}],
            actor="alice",
        )
        tools = GovernedToolExecutor(self.database, self.settings)

        plan = tools.execute("get_rightsizing_plan", {})
        self.assertEqual(plan["planSummary"]["totalVms"], 2)
        bucket = next(
            b for b in plan["buckets"]
            if b["bucketKey"] == created["bucketKey"]
        )
        self.assertEqual(bucket["memberCount"], 1)
        self.assertEqual(bucket["plannedQuantity"], 2)
        self.assertEqual(bucket["plannerReferenceMonthlySavings"], 120.0)
        self.assertEqual(plan["decisionCounts"], {"Confirmed": 1})
        self.assertIn("human intent", plan["lineage"])

        detail = tools.execute(
            "get_rightsizing_plan",
            {"bucketKey": created["bucketKey"]},
        )
        member = detail["bucketMembers"]["members"][0]
        self.assertEqual(member["name"], "checkout-api")
        self.assertEqual(member["decision"], "Confirmed")
        self.assertEqual(member["note"], "sized against p95")

        wrong = tools.execute(
            "get_rightsizing_plan", {"bucketKey": "westus9|Nope"}
        )
        self.assertIn("Valid keys", wrong["bucketKeyError"])

    def test_create_rightsizing_board_tool_writes_a_real_non_primary_board(self):
        self.database.seed_demo()
        tools = GovernedToolExecutor(self.database, self.settings)

        result = tools.execute(
            "create_rightsizing_board",
            {"name": "Aggressive downsize option", "description": "scenario"},
        )
        self.assertTrue(result["created"])
        self.assertFalse(result["isPrimary"])
        boards = self.database.rightsizing_boards()
        created = next(b for b in boards if b["id"] == result["boardId"])
        self.assertEqual(created["name"], "Aggressive downsize option")
        self.assertFalse(created["isPrimary"])
        self.assertEqual(created["createdBy"], "Flux Intelligence")

        # This is a write tool: it must never be served from the response
        # cache, or a second teammate asking for a board with the same name
        # would silently get back the first teammate's board id instead of
        # a new board actually being created.
        again = tools.execute(
            "create_rightsizing_board",
            {"name": "Aggressive downsize option", "description": "scenario"},
        )
        self.assertNotEqual(again["boardId"], result["boardId"])
        self.assertEqual(
            len(self.database.rightsizing_boards()),
            len(boards) + 1,
            "the second identical call must create a second board, not "
            "return a cached result",
        )

        with self.assertRaisesRegex(ValueError, "board name is required"):
            tools.execute("create_rightsizing_board", {"name": "  "})

    def test_search_documentation_without_wiki_token_only_searches_repository(self):
        docs = DocumentationIndex(settings=Settings())
        result = docs.search("FluxFinOps")
        self.assertIn("not configured as a source", result["limitation"])
        self.assertTrue(all(r.get("source") == "repository" for r in result["results"]))

    @patch("api.intelligence_assistant.urlopen")
    def test_search_documentation_merges_managed_wiki_results_when_configured(
        self, mocked_urlopen,
    ):
        mocked_urlopen.side_effect = [
            FakeHttpResponse({
                "data": {"pages": {"search": {"results": [
                    {"title": "FluxFinOps", "description": "Platform overview",
                     "path": "platforms/fluxfinops", "locale": "en"},
                ]}}},
            }),
            FakeHttpResponse({
                "data": {"pages": {"singleByPath": {
                    "title": "FluxFinOps",
                    "description": "Platform overview",
                    "content": "FluxFinOps is a focused internal alpha covering Azure inventory and FinOps.",
                    "path": "platforms/fluxfinops",
                }}},
            }),
        ]
        docs = DocumentationIndex(
            settings=Settings(
                wiki_base_url="https://wiki.example.com",
                wiki_api_token="test-only",
            ),
        )
        result = docs.search("focused internal alpha")
        self.assertIn("both searched", result["limitation"])
        wiki_hits = [r for r in result["results"] if r.get("source") == "managed wiki"]
        self.assertEqual(len(wiki_hits), 1)
        self.assertEqual(wiki_hits[0]["document"], "wiki:/platforms/fluxfinops")
        self.assertIn("focused internal alpha", wiki_hits[0]["excerpt"])
        first_request = mocked_urlopen.call_args_list[0].args[0]
        self.assertEqual(first_request.get_header("Authorization"), "Bearer test-only")

    @patch("api.intelligence_assistant.urlopen")
    def test_search_documentation_degrades_gracefully_when_wiki_is_unreachable(
        self, mocked_urlopen,
    ):
        mocked_urlopen.side_effect = URLError("no route to host")
        docs = DocumentationIndex(
            settings=Settings(
                wiki_base_url="https://wiki.example.com",
                wiki_api_token="test-only",
            ),
        )
        result = docs.search("FluxFinOps")
        self.assertTrue(all(r.get("source") == "repository" for r in result["results"]))
        self.assertIn("unreachable", result["limitation"])

    def test_fleet_analysis_fits_in_few_tool_calls(self):
        """A fleet question must not need one call per resource."""
        self.database.seed_demo()
        tools = GovernedToolExecutor(self.database, self.settings)
        fleet = tools.execute("get_fleet_telemetry", {"resourceType": ""})
        # One call covers every matching resource up to the disclosed limit.
        self.assertEqual(fleet["returned"], min(fleet["matching"], 200))
        self.assertFalse(fleet["truncated"] and not fleet["limitations"])

    def test_chat_uses_tools_and_persists_review_transcript(self):
        assistant = IntelligenceAssistant(self.database, self.settings)
        provider = FakeProvider()
        assistant.provider = provider
        response = assistant.chat(
            messages=[{"role": "user", "content": "What reports are available?"}],
            context={"page": "reports"},
            model_profile="fast",
            session={
                "user": {
                    "id": "user-1",
                    "email": "someone@example.com",
                    "tenantId": "tenant-1",
                }
            },
        )
        self.assertEqual(response["summary"], "Flux has six governed report contracts.")
        self.assertIn("durationMs", response["performance"])
        self.assertEqual(response["performance"]["toolCallCount"], 1)
        self.assertEqual(response["performance"]["modelCallCount"], 2)
        self.assertEqual(response["performance"]["toolCacheHits"], 0)
        self.assertFalse(response["performance"]["rillInPath"])
        self.assertEqual(response["quality"]["status"], "pass")
        self.assertNotIn("provider", response)
        self.assertNotIn("model", response)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(
            provider.messages[1][-1]["role"],
            "tool",
        )
        with self.database.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT user_hash, tool_names_json, prompt_tokens,
                       completion_tokens, feedback_reason
                FROM intelligence_usage_events
                """
            ).fetchone()
            columns = {
                item[1]
                for item in connection.execute(
                    "PRAGMA table_info('intelligence_usage_events')"
                ).fetchall()
            }
            transcript = connection.execute(
                """
                SELECT messages_json, response_json, raw_response_text
                FROM intelligence_transcript_events
                """
            ).fetchone()
        self.assertNotIn("someone@example.com", row[0])
        self.assertIn("get_report_catalog", row[1])
        self.assertEqual(row[2], 300)
        self.assertEqual(row[3], 100)
        self.assertFalse({"prompt", "response", "reasoning_content"} & columns)
        self.assertIn("What reports are available?", transcript[0])
        self.assertIn("Flux has six governed report contracts.", transcript[1])
        self.assertNotIn("private scratchpad", transcript[2])
        usage = self.database.intelligence_usage_status()
        self.assertEqual(usage["requestCount"], 1)
        self.assertEqual(usage["successfulRequestCount"], 1)
        self.assertEqual(usage["failedRequestCount"], 0)
        self.assertGreaterEqual(usage["averageLatencyMs"], 0)
        self.assertGreaterEqual(usage["p95LatencyMs"], 0)
        self.assertTrue(
            assistant.record_client_performance(
                request_id=response["requestId"],
                client_round_trip_ms=response["performance"]["durationMs"] + 15,
                client_render_ms=4,
                client_end_to_end_ms=response["performance"]["durationMs"] + 19,
                session={
                    "user": {
                        "id": "user-1",
                        "email": "someone@example.com",
                        "tenantId": "tenant-1",
                    }
                },
            )
        )
        review = self.database.intelligence_transcript_review()
        self.assertEqual(len(review["items"]), 1)
        self.assertEqual(review["items"][0]["performance"]["renderMs"], 4)
        self.assertFalse(review["items"][0]["performance"]["rillInPath"])
        self.assertTrue(
            self.database.record_intelligence_feedback(
                response["requestId"], "helpful", ""
            )
        )
        quality = self.database.intelligence_quality_status(
            slow_request_ms=0
        )
        self.assertEqual(quality["requestCount"], 1)
        self.assertEqual(quality["helpfulCount"], 1)
        self.assertEqual(quality["slowRequestCount"], 1)
        self.assertEqual(quality["structuredContractFailureCount"], 0)
        self.assertTrue(quality["bottlenecks"])
        self.assertEqual(quality["assessedCount"], 1)
        self.assertEqual(quality["averageScore"], 100)
        self.assertEqual(quality["regressionFailureCount"], 0)

    def test_budget_ceiling_stops_before_provider_call(self):
        self.database.record_intelligence_usage(
            request_id="existing",
            user_hash="hash",
            provider="deepseek",
            model="deepseek-v4-pro",
            status="succeeded",
            latency_ms=1,
            prompt_tokens=1,
            cached_prompt_tokens=0,
            completion_tokens=1,
            estimated_cost_usd=8.01,
            tool_names=[],
        )
        assistant = IntelligenceAssistant(self.database, self.settings)
        assistant.provider = FakeProvider()
        with self.assertRaises(IntelligenceBudgetExceeded):
            assistant.chat(
                messages=[{"role": "user", "content": "Analyze cost."}],
                context={"page": "reports"},
                model_profile="fast",
                session={"user": {"id": "user-1", "tenantId": "tenant-1"}},
            )
        self.assertEqual(assistant.provider.calls, 0)

    @patch("api.intelligence_assistant.urlopen")
    def test_provider_requests_enforced_json_output(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeHttpResponse({
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"summary":"ok","blocks":[]}',
                },
            }],
            "usage": {},
        })
        provider = DeepSeekProvider(self.settings)
        provider.complete(
            model="deepseek-v4-flash",
            messages=[{"role": "system", "content": "Return JSON."}],
            tools=[],
            user_id="flux_user",
        )
        request = mocked_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(
            payload["response_format"],
            {"type": "json_object"},
        )
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    @patch("api.intelligence_assistant.urlopen")
    def test_empty_content_filter_response_is_flagged_for_review(
        self,
        mocked_urlopen,
    ):
        mocked_urlopen.return_value = FakeHttpResponse({
            "choices": [{
                "finish_reason": "content_filter",
                "message": {"role": "assistant", "content": ""},
            }],
            "usage": {},
        })
        provider = DeepSeekProvider(self.settings)
        with self.assertRaises(IntelligenceProviderError) as raised:
            provider.complete(
                model="deepseek-v4-flash",
                messages=[{"role": "system", "content": "Return JSON."}],
                tools=[],
                user_id="flux_user",
            )
        self.assertEqual(raised.exception.code, "response_policy_review")

    def test_anthropic_translate_messages_handles_system_and_tool_round_trip(self):
        system_text, translated = _anthropic_translate_messages([
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "What reports exist?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "get_report_catalog",
                        "arguments": '{"limit": 5}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"ok": true, "data": []}',
            },
        ])
        self.assertEqual(system_text, "Be concise.")
        self.assertEqual(translated[0], {"role": "user", "content": "What reports exist?"})
        self.assertEqual(translated[1]["role"], "assistant")
        self.assertEqual(
            translated[1]["content"],
            [{
                "type": "tool_use",
                "id": "call-1",
                "name": "get_report_catalog",
                "input": {"limit": 5},
            }],
        )
        self.assertEqual(
            translated[2],
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": '{"ok": true, "data": []}',
                }],
            },
        )

    def test_anthropic_translate_tools_maps_to_input_schema(self):
        translated = _anthropic_translate_tools([{
            "type": "function",
            "function": {
                "name": "get_cost_summary",
                "description": "Retrieve governed cost summary.",
                "parameters": {"type": "object", "properties": {"costType": {"type": "string"}}},
            },
        }])
        self.assertEqual(translated, [{
            "name": "get_cost_summary",
            "description": "Retrieve governed cost summary.",
            "input_schema": {"type": "object", "properties": {"costType": {"type": "string"}}},
        }])

    @patch("api.intelligence_assistant.urlopen")
    def test_foundry_provider_routes_claude_models_to_anthropic_endpoint(
        self, mocked_urlopen,
    ):
        mocked_urlopen.return_value = FakeHttpResponse({
            "content": [
                {"type": "text", "text": "Here is what I found."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_report_catalog",
                    "input": {},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 50,
                "output_tokens": 12,
                "cache_read_input_tokens": 5,
            },
        })
        settings = Settings(
            foundry_endpoint="https://example-resource.services.ai.azure.com/models",
            foundry_api_key="test-only",
        )
        provider = FoundryProvider(settings)
        result = provider.complete(
            model="claude-opus-5",
            messages=[
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "What reports exist?"},
            ],
            tools=[{
                "type": "function",
                "function": {"name": "get_report_catalog", "parameters": {}},
            }],
            user_id="flux_user",
        )
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://example-resource.services.ai.azure.com/anthropic/v1/messages",
        )
        self.assertEqual(request.get_header("X-api-key"), "test-only")
        payload = json.loads(request.data)
        self.assertEqual(payload["system"], "Be concise.")
        self.assertNotIn("response_format", payload)
        self.assertEqual(result.message["content"], "Here is what I found.")
        self.assertEqual(
            result.message["tool_calls"],
            [{
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "get_report_catalog", "arguments": "{}"},
            }],
        )
        self.assertEqual(result.usage["prompt_tokens"], 50)
        self.assertEqual(result.usage["cached_prompt_tokens"], 5)
        self.assertEqual(result.usage["completion_tokens"], 12)

    @patch("api.intelligence_assistant.urlopen")
    def test_foundry_provider_non_claude_models_use_openai_shape(
        self, mocked_urlopen,
    ):
        mocked_urlopen.return_value = FakeHttpResponse({
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"summary":"ok","blocks":[]}'},
            }],
            "usage": {},
        })
        settings = Settings(
            foundry_endpoint="https://example-resource.services.ai.azure.com/models",
            foundry_api_key="test-only",
            foundry_api_version="2024-05-01-preview",
        )
        provider = FoundryProvider(settings)
        provider.complete(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            user_id="flux_user",
        )
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://example-resource.services.ai.azure.com/models/chat/completions"
            "?api-version=2024-05-01-preview",
        )
        self.assertEqual(request.get_header("Api-key"), "test-only")

    def test_plain_text_response_is_repaired_once(self):
        assistant = IntelligenceAssistant(self.database, self.settings)
        assistant.provider = RepairingProvider()
        response = assistant.chat(
            messages=[{"role": "user", "content": "What changed?"}],
            context={"page": "reports"},
            model_profile="fast",
            session={"user": {"id": "user-1", "tenantId": "tenant-1"}},
        )
        self.assertEqual(response["summary"], "Amortized cost decreased.")
        self.assertEqual(
            response["performance"]["responseMode"],
            "structured_repair",
        )
        self.assertEqual(response["performance"]["modelCallCount"], 2)
        self.assertNotIn(
            "structured response contract",
            " ".join(response["limitations"]).lower(),
        )

    def test_plain_text_fallback_is_rendered_without_contract_error(self):
        assistant = IntelligenceAssistant(self.database, self.settings)
        assistant.provider = RepairingProvider(repair_succeeds=False)
        response = assistant.chat(
            messages=[{"role": "user", "content": "What changed?"}],
            context={"page": "reports"},
            model_profile="fast",
            session={"user": {"id": "user-1", "tenantId": "tenant-1"}},
        )
        self.assertEqual(
            response["summary"],
            "Amortized cost decreased based on governed data.",
        )
        self.assertEqual(
            response["performance"]["responseMode"],
            "plain_text",
        )
        self.assertNotIn(
            "structured response contract",
            " ".join(response["limitations"]).lower(),
        )

    def test_response_contract_removes_unsafe_content(self):
        response = validate_response(
            {
                "summary": "test",
                "blocks": [
                    {
                        "type": "mermaid",
                        "title": "Unsafe",
                        "content": "flowchart LR\n A --> B\n click A javascript:alert(1)",
                    },
                    {
                        "type": "markdown",
                        "content": "[bad](javascript:alert(1))",
                    },
                ],
            }
        )
        self.assertEqual(len(response["blocks"]), 1)
        self.assertNotIn("javascript:", response["blocks"][0]["content"].lower())

    def test_structured_response_is_recovered_after_provider_preface(self):
        response = _extract_json(
            """
            Based on the governed data, here is what changed.

            {
              "summary": "Amortized cost decreased.",
              "blocks": [
                {
                  "type": "markdown",
                  "content": "A value with braces: `{example}`."
                }
              ],
              "facts": [],
              "interpretations": [],
              "limitations": [],
              "sources": [],
              "followUps": []
            }

            This trailing sentence should not be rendered.
            """
        )
        self.assertEqual(response["summary"], "Amortized cost decreased.")
        self.assertEqual(response["blocks"][0]["type"], "markdown")

    def test_compact_tables_and_assistant_permission_followups_are_repaired(self):
        response = validate_response(
            {
                "summary": "Telemetry coverage",
                "blocks": [
                    {
                        "type": "markdown",
                        "content": (
                            "Coverage: | Status | Count | Percentage | "
                            "|---|---|---| | Covered | 1,488 | 21.1% | "
                            "| not_attempted | 5,456 | 77.5% |"
                        ),
                    }
                ],
                "followUps": [
                    "Would you like me to examine the specific VMs in prod-sub?"
                ],
            }
        )
        markdown = response["blocks"][0]["content"]
        self.assertIn("Coverage:\n\n| Status | Count | Percentage |", markdown)
        self.assertIn("\n|---|---|---|", markdown)
        self.assertIn("\n| Covered | 1,488 | 21.1% |", markdown)
        self.assertEqual(
            response["followUps"],
            ["Can you examine the specific VMs in prod-sub?"],
        )

    def test_deterministic_quality_flags_undisclosed_partial_coverage(self):
        answer = validate_response({
            "summary": "Cost was 100 USD.",
            "blocks": [{"type": "markdown", "content": "Cost was 100 USD."}],
            "facts": ["The governed result returned 100 USD."],
            "sources": [{
                "tool": "get_focus_cost",
                "description": "FOCUS charge evidence",
            }],
            "followUps": ["Can you break this down by service?"],
        })
        quality = assess_response_quality(
            answer,
            ["get_focus_cost"],
            [{
                "ok": True,
                "data": {
                    "coverage": {
                        "configuredScopes": 11,
                        "availableScopes": 4,
                    }
                },
            }],
            "structured",
        )
        self.assertEqual(quality["status"], "review")
        self.assertIn("partial_coverage_not_disclosed", quality["flags"])
        answer["summary"] = "Partial coverage: 4/11 subscriptions total 100 USD."
        answer["blocks"][0]["content"] = (
            "Partial coverage: 4/11 subscriptions total 100 USD."
        )
        repaired = assess_response_quality(
            answer,
            ["get_focus_cost"],
            [{
                "coverage": {
                    "configuredScopes": 11,
                    "availableScopes": 4,
                }
            }],
            "structured",
        )
        self.assertEqual(repaired["status"], "pass")

    def test_governed_tool_cache_and_composite_cost_contract(self):
        tools = GovernedToolExecutor(self.database, self.settings)
        first, first_hit = tools.execute_with_metadata(
            "get_report_catalog", {}
        )
        second, second_hit = tools.execute_with_metadata(
            "get_report_catalog", {}
        )
        self.assertFalse(first_hit)
        self.assertTrue(second_hit)
        self.assertEqual(first["version"], second["version"])
        investigation = tools.execute("investigate_cost_change", {})
        self.assertIn("dailyCost", investigation)
        self.assertIn("focusCharges", investigation)
        self.assertIn("coverage", investigation["focusCharges"])

    def test_navigation_actions_are_server_owned(self):
        actions = _actions_for_tools([
            "get_focus_cost",
            "search_inventory",
            "search_inventory",
        ])
        self.assertEqual(
            [item["href"] for item in actions],
            ["#/reports", "#/inventory"],
        )


if __name__ == "__main__":
    unittest.main()

    def test_midconversation_system_becomes_user_turn(self):
        """The structured-output repair prompt arrives as a system message
        after an assistant turn. Hoisting it into Anthropic's top-level
        system field left the transcript ending on an assistant message,
        which Foundry rejected with 'does not support assistant message
        prefill' -- observed in production 2026-07-31."""
        system_text, translated = _anthropic_translate_messages([
            {"role": "system", "content": "Base contract."},
            {"role": "user", "content": "What changed?"},
            {"role": "assistant", "content": "Plain text, not JSON."},
            {"role": "system", "content": "Formatting repair only."},
        ])
        self.assertEqual(system_text, "Base contract.")
        self.assertEqual(translated[-1]["role"], "user")
        self.assertIn("Formatting repair only.", translated[-1]["content"])

    def test_translation_never_ends_on_an_assistant_turn(self):
        _, translated = _anthropic_translate_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "trailing"},
        ])
        self.assertEqual(translated[-1]["role"], "user")
