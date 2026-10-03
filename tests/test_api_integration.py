"""API integration tests — auth, rate-limit Body(), CORS, 503, SPA, validation.

Gitea issue #30: no HTTP-level tests existed; slowapi+Body() forward-ref
regression shipped three times because only function-level pipeline tests
ran. This suite exercises the real FastAPI app through TestClient so
signature/middleware/exception-handler breaks fail loudly.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.auth import AuthService
from api.config import Settings
from api.database import DatabaseBusyError
from api.main import app, limiter, settings as app_settings


def _reset_limiter() -> None:
    # slowapi keeps counters in limiter._storage (MemoryStorage).
    # Reset between isolated test cases to avoid 429 poison.
    try:
        limiter.reset()
    except Exception:
        pass
    for attr in ("_storage", "_fallback_storage"):
        store = getattr(limiter, attr, None)
        if store is not None:
            try:
                store.reset()
            except Exception:
                # MemoryStorage.reset() exists; ignore if not.
                pass


class _BaseApiTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_limiter()
        self.client = TestClient(app, raise_server_exceptions=False)
        # Save original auth so entra-mode tests can restore mock.
        self._orig_auth = app.state.auth
        # FLUX_AUTH_MODE now defaults to "entra" (fail closed), and CI sets no
        # auth environment, so the app-level AuthService would 401 every
        # route before body/query validation even runs. Install an explicitly
        # unlocked mock service so these tests exercise the handlers.
        app.state.auth = AuthService(
            Settings(auth_mode="mock", allow_mock_auth=True)
        )
        # Ensure limiter is enabled even in test env.
        limiter.enabled = True

    def tearDown(self) -> None:
        _reset_limiter()
        app.state.auth = self._orig_auth
        limiter.enabled = True


# ---------------------------------------------------------------------------
# Auth — require_reader / require_admin
# ---------------------------------------------------------------------------

class AuthIntegrationTests(_BaseApiTest):
    """require_reader (401/403) and require_admin (403) via real dependency."""

    def test_mock_mode_reader_endpoint_passes(self) -> None:
        # Unlocked mock auth (installed by setUp) is an admin+reader.
        with patch("api.main.database") as mock_db:
            mock_db.overview.return_value = {"total": 1}
            resp = self.client.get("/api/overview")
            self.assertEqual(resp.status_code, 200, resp.text[:500])
            self.assertEqual(resp.json(), {"total": 1})

    def test_auth_mode_defaults_to_entra_not_mock(self) -> None:
        """A missing FLUX_AUTH_MODE must not resolve callers to local-admin."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLUX_AUTH_MODE", None)
            self.assertEqual(Settings().auth_mode, "entra")

    def test_mock_without_explicit_unlock_fails_closed(self) -> None:
        """FLUX_AUTH_MODE=mock alone must serve 503, on any host.

        Regression: the fail-closed guard was gated on WEBSITE_SITE_NAME, so
        every non-App-Service host (container, VM, staging box, laptop bound
        to 0.0.0.0) served unauthenticated admin on all routes.
        """
        service = AuthService(Settings(auth_mode="mock", allow_mock_auth=False))
        app.state.auth = service
        resp = self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "auth misconfigured")
        # /api/session must not claim an authenticated admin either.
        self.assertFalse(service.resolve({})["authenticated"])

    def test_entra_anonymous_returns_401(self) -> None:
        entra = AuthService(Settings(auth_mode="entra", trust_easyauth_headers=True, entra_tenant_id=""))
        app.state.auth = entra
        resp = self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Sign in", resp.json()["detail"])

    def test_forged_principal_refused_without_easyauth_trust(self) -> None:
        # Off App Service nothing strips inbound x-ms-* headers, so a
        # hand-minted principal is just text a caller typed (#101). Unless
        # the deployment asserts Easy Auth fronting, it must not
        # authenticate anyone — regardless of how plausible its claims are.
        import base64

        entra = AuthService(Settings(
            auth_mode="entra",
            entra_tenant_id="tenant-1",
            entra_admin_assignments=("flux.admin",),
            trust_easyauth_headers=False,
        ))
        app.state.auth = entra
        forged = base64.b64encode(json.dumps({
            "claims": [
                {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": "tenant-1"},
                {"typ": "roles", "val": "flux.admin"},
            ]
        }).encode()).decode()
        resp = self.client.get(
            "/api/overview", headers={"x-ms-client-principal": forged}
        )
        self.assertEqual(resp.status_code, 401, resp.text[:300])

    def test_entra_reader_can_read_but_not_admin(self) -> None:
        import base64

        def _principal(claims):
            payload = {"claims": claims}
            return base64.b64encode(json.dumps(payload).encode()).decode()

        entra = AuthService(
            Settings(
                auth_mode="entra", trust_easyauth_headers=True,
                entra_tenant_id="tenant-1",
                entra_admin_assignments=("flux.admin",),
                entra_reader_assignments=("flux.reader",),
            )
        )
        app.state.auth = entra
        reader_header = _principal(
            [{"typ": "roles", "val": "flux.reader"}, {"typ": "tid", "val": "tenant-1"}]
        )
        headers = {"x-ms-client-principal": reader_header}
        with patch("api.main.database") as mock_db:
            mock_db.overview.return_value = {"total": 1}
            resp = self.client.get("/api/overview", headers=headers)
            self.assertEqual(resp.status_code, 200)

        # Reader must NOT access admin-only endpoint.
        resp = self.client.get("/api/integrations/azure", headers=headers)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Flux.Admin", resp.json()["detail"])

    def test_entra_admin_can_access_admin_endpoint(self) -> None:
        import base64

        def _principal(claims):
            return base64.b64encode(json.dumps({"claims": claims}).encode()).decode()

        entra = AuthService(
            Settings(
                auth_mode="entra", trust_easyauth_headers=True,
                entra_tenant_id="tenant-1",
                entra_admin_assignments=("flux.admin",),
                entra_reader_assignments=("flux.reader",),
            )
        )
        app.state.auth = entra
        admin_header = _principal(
            [{"typ": "roles", "val": "flux.admin"}, {"typ": "tid", "val": "tenant-1"}]
        )
        headers = {"x-ms-client-principal": admin_header}
        with patch("api.main.database") as mock_db:
            mock_db.integration.return_value = {
                "enabled": False,
                "name": "Azure",
                "tenantId": "",
                "authMode": "local_powershell",
                "subscriptions": [],
            }
            resp = self.client.get("/api/integrations/azure", headers=headers)
            self.assertEqual(resp.status_code, 200)

    def test_entra_unknown_role_returns_403(self) -> None:
        import base64

        def _principal(claims):
            return base64.b64encode(json.dumps({"claims": claims}).encode()).decode()

        entra = AuthService(
            Settings(
                auth_mode="entra", trust_easyauth_headers=True,
                entra_tenant_id="tenant-1",
                entra_admin_assignments=("flux.admin",),
                entra_reader_assignments=("flux.reader",),
            )
        )
        app.state.auth = entra
        other = _principal([{"typ": "roles", "val": "SomeOtherRole"}, {"typ": "tid", "val": "tenant-1"}])
        resp = self.client.get("/api/overview", headers={"x-ms-client-principal": other})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Flux.Reader", resp.json()["detail"])


# ---------------------------------------------------------------------------
# Rate-limited endpoints + Body() regression (f72dd05)
# ---------------------------------------------------------------------------

class RateLimitBodyTests(_BaseApiTest):
    """
    5 rate-limited routes exist (@limiter.limit). The Body() forward-ref bug
    made any POST with a Pydantic body model 500 before the handler ran.
    The fix reads raw body + model_validate manually; TestClient POSTs must
    never 500 for any valid/invalid JSON shape.
    """

    def test_optimizer_empty_body_never_500(self) -> None:
        resp = self.client.post("/api/commitments/optimizer/runs", content=b"")
        self.assertNotEqual(resp.status_code, 500, resp.text[:500])

    def test_optimizer_empty_json_object_never_500(self) -> None:
        resp = self.client.post("/api/commitments/optimizer/runs", json={})
        self.assertNotEqual(resp.status_code, 500, resp.text[:500])

    def test_optimizer_invalid_json_is_422(self) -> None:
        resp = self.client.post(
            "/api/commitments/optimizer/runs",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_optimizer_out_of_range_422(self) -> None:
        resp = self.client.post("/api/commitments/optimizer/runs", json={"lookbackDays": 3})
        self.assertEqual(resp.status_code, 422)
        # Validation shape has `detail` list with loc/type/msg.
        body = resp.json()
        self.assertIn("detail", body)
        self.assertIsInstance(body["detail"], list)
        self.assertTrue(any("lookback" in str(d).lower() or "ge" in str(d).lower() for d in body["detail"]))

    def test_optimizer_unknown_field_tolerated(self) -> None:
        resp = self.client.post(
            "/api/commitments/optimizer/runs", json={"lookbackDays": 30, "bogus": True}
        )
        self.assertNotEqual(resp.status_code, 500)
        self.assertNotEqual(resp.status_code, 422, resp.text[:500])

    def test_optimizer_valid_payload_reaches_handler(self) -> None:
        # With flag disabled (default) the handler returns 409, not 422/500.
        resp = self.client.post("/api/commitments/optimizer/runs", json={"lookbackDays": 30})
        self.assertNotEqual(resp.status_code, 500)
        self.assertNotEqual(resp.status_code, 422)
        # When disabled we expect 409; when enabled it would be 200/409-lease.
        self.assertIn(resp.status_code, (200, 409, 429), resp.text[:400])
        if resp.status_code == 409:
            self.assertIn("disabled", resp.json()["detail"].lower())

    def test_rate_limited_get_inventory_passes_with_mocked_db(self) -> None:
        with patch("api.main.database") as mock_db:
            mock_db.inventory.return_value = {"items": [], "total": 0}
            resp = self.client.get("/api/inventory?limit=2")
            self.assertNotEqual(resp.status_code, 500, resp.text[:500])
            self.assertEqual(resp.status_code, 200)

    def test_rate_limited_get_cost_report_passes(self) -> None:
        with patch("api.main.database") as mock_db:
            mock_db.cost_report.return_value = {
                "summary": {"currency": "USD"},
                "period": {"start": "2026-08-01", "end": "2026-08-12"},
                "resources": [],
            }
            resp = self.client.get("/api/reports/cost?costType=AmortizedCost")
            self.assertEqual(resp.status_code, 200, resp.text[:500])

    def test_rate_limited_get_opportunities_passes(self) -> None:
        with patch("api.main.database") as mock_db:
            mock_db.opportunities.return_value = {"items": [], "total": 0}
            resp = self.client.get("/api/opportunities?limit=2")
            self.assertEqual(resp.status_code, 200, resp.text[:500])

    def test_rate_limited_get_commitments_report_passes(self) -> None:
        with patch("api.main.database") as mock_db:
            mock_db.commitment_inventory.return_value = {"items": []}
            resp = self.client.get("/api/reports/commitments")
            self.assertEqual(resp.status_code, 200, resp.text[:500])


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

class CorsIntegrationTests(_BaseApiTest):
    # config default is CLOSED: with FLUX_CORS_ORIGINS unset no CORS
    # middleware is mounted at all, so reflection tests only mean something
    # when the app was imported with origins configured. (They previously
    # failed everywhere and nobody saw it — CI piped unittest into `tail`,
    # which swallowed the exit code.)
    @unittest.skipUnless(
        "http://localhost:5173" in app_settings.cors_origins,
        "FLUX_CORS_ORIGINS does not include the dev origin",
    )
    def test_cors_allows_configured_origin(self) -> None:
        resp = self.client.get("/api/health", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "http://localhost:5173")
        self.assertIn(resp.headers.get("access-control-allow-credentials"), ("true", "True"))

    @unittest.skipUnless(
        "http://localhost:5173" in app_settings.cors_origins,
        "FLUX_CORS_ORIGINS does not include the dev origin",
    )
    def test_cors_preflight(self) -> None:
        resp = self.client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Preflight should succeed and advertise allowed origin.
        self.assertIn(resp.status_code, (200, 204))
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "http://localhost:5173")

    def test_cors_disallows_unknown_origin(self) -> None:
        # Unknown origin should not be reflected.
        resp = self.client.get("/api/health", headers={"Origin": "http://evil.example.com"})
        # Starlette CORSMiddleware simply omits the header for disallowed origins.
        self.assertIsNone(resp.headers.get("access-control-allow-origin"))


# ---------------------------------------------------------------------------
# 503 mapping for DatabaseBusyError
# ---------------------------------------------------------------------------

class DatabaseBusy503Tests(_BaseApiTest):
    def test_inventory_busy_maps_to_503_with_retry_after(self) -> None:
        with patch("api.main.database") as mock_db:
            mock_db.inventory.side_effect = DatabaseBusyError(2.3)
            resp = self.client.get("/api/inventory?limit=2")
            self.assertEqual(resp.status_code, 503, resp.text[:500])
            body = resp.json()
            self.assertIn("temporarily busy", body["detail"].lower())
            self.assertAlmostEqual(body["waitedSeconds"], 2.3, places=1)
            self.assertEqual(resp.headers.get("retry-after"), "15")

    def test_cost_report_busy_maps_to_503(self) -> None:
        with patch("api.main.database") as mock_db:
            mock_db.cost_report.side_effect = DatabaseBusyError(1.0)
            resp = self.client.get("/api/reports/cost")
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.headers.get("retry-after"), "15")

    def test_overview_busy_maps_to_503(self) -> None:
        with patch("api.main.database") as mock_db:
            mock_db.overview.side_effect = DatabaseBusyError(0.7)
            resp = self.client.get("/api/overview")
            self.assertEqual(resp.status_code, 503)
            self.assertIn("waitedSeconds", resp.json())

    def test_raise_as_exception_propagates_via_handler(self) -> None:
        # Direct raise through a mocked route dependency (inventory) ensures the
        # exception_handler, not a try/except in the route, produces the 503.
        with patch("api.main.database") as mock_db:
            mock_db.opportunities.side_effect = DatabaseBusyError(15.0)
            resp = self.client.get("/api/opportunities?limit=2")
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.json()["waitedSeconds"], 15.0)


# ---------------------------------------------------------------------------
# ValidationError shape
# ---------------------------------------------------------------------------

class ValidationErrorShapeTests(_BaseApiTest):
    def test_query_validation_returns_422_with_detail_list(self) -> None:
        resp = self.client.get("/api/inventory?limit=999999")  # le=2000
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertIn("detail", body)
        self.assertIsInstance(body["detail"], list)
        first = body["detail"][0]
        self.assertIn("loc", first)
        self.assertIn("msg", first)
        self.assertIn("type", first)

    def test_cost_report_start_after_end_returns_422(self) -> None:
        # This used to tolerate any non-200 because the slowapi + postponed-
        # annotations bug left startDate/endDate as unresolved ForwardRef
        # query params. api.main no longer uses `from __future__ import
        # annotations`, so the real signature is back and the endpoint's own
        # start-before-end guard is reachable again.
        with patch("api.main.database"):
            resp = self.client.get("/api/reports/cost?startDate=2026-08-12&endDate=2026-08-01")
            self.assertEqual(resp.status_code, 422, resp.text[:400])
            self.assertIn("startDate must be on or before endDate", resp.json()["detail"])

    def test_put_budget_groups_empty_is_422_or_handled(self) -> None:
        # BudgetGroupsUpdate with invalid annual_amount should 422.
        resp = self.client.put(
            "/api/integrations/budget-groups",
            json={"groups": [{"name": "", "annualAmount": -1}]},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIsInstance(resp.json()["detail"], list)

    def test_semantic_query_missing_required_field_422(self) -> None:
        resp = self.client.post("/api/semantic/query", json={})
        self.assertEqual(resp.status_code, 422)
        detail = resp.json()["detail"]
        self.assertIsInstance(detail, list)
        # The failure must be about the BODY fields, not a phantom query
        # parameter: loc ("query", "payload") was the forward-ref bug's
        # signature and made this test pass while the endpoint was broken.
        for issue in detail:
            self.assertEqual(issue["loc"][0], "body", issue)

    def test_invalid_cost_type_enum_422(self) -> None:
        resp = self.client.get("/api/reports/cost?costType=BogusCost")
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------------------------
# Route signature integrity — the slowapi + postponed-annotations regression
# ---------------------------------------------------------------------------

class RouteSignatureIntegrityTests(_BaseApiTest):
    """Gitea #30 shipped again as `query.payload: Field required`.

    With ``from __future__ import annotations`` in api.main, FastAPI resolves
    a rate-limited route's string annotations in slowapi's wrapper globals;
    any name slowapi does not itself import silently degrades to a required
    query parameter typed ForwardRef, so every POST body is rejected with a
    422 before the handler runs. The future-import is gone from api.main;
    these tests fail loudly if any route parameter ever degrades again.
    """

    def test_no_route_parameter_is_an_unresolved_forwardref(self) -> None:
        from typing import ForwardRef

        offenders: list[str] = []
        for route in app.routes:
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            for params in (
                dependant.query_params,
                dependant.path_params,
                dependant.body_params,
                dependant.header_params,
                dependant.cookie_params,
            ):
                for param in params:
                    if isinstance(param.type_, ForwardRef):
                        offenders.append(f"{route.path}:{param.name}")
        self.assertEqual(offenders, [], "annotations degraded to query params")

    def test_semantic_query_valid_body_returns_200(self) -> None:
        # The missing half of test_semantic_query_missing_required_field_422:
        # a VALID body must reach the database layer. Under the forward-ref
        # bug this returned 422 while the invalid-body test kept passing.
        sentinel = {"columns": [], "rows": [], "sql": "SELECT 1", "rowCount": 0}
        with patch("api.main.database") as mock_db:
            mock_db.run_semantic_query.return_value = sentinel
            resp = self.client.post(
                "/api/semantic/query",
                json={
                    "model": "daily_cost",
                    "measures": ["total_cost"],
                    "dimensions": ["service_name"],
                    "grain": "day",
                    "start": "2026-07-01",
                    "end": None,
                    "limit": 100,
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text[:500])
            self.assertEqual(resp.json(), sentinel)
            mock_db.run_semantic_query.assert_called_once()


# ---------------------------------------------------------------------------
# SQL console — POST /api/semantic/sql
# ---------------------------------------------------------------------------

class SemanticSqlConsoleTests(_BaseApiTest):
    def test_valid_select_executes_and_returns_typed_columns(self) -> None:
        with patch("api.main.database") as mock_db:
            db = mock_db.connect.return_value.__enter__.return_value
            cursor = db.execute.return_value
            cursor.fetchall.return_value = [["Virtual Machines", 12.5]]
            cursor.description = [
                ("service_name", "VARCHAR"),
                ("total", "DOUBLE"),
            ]
            resp = self.client.post(
                "/api/semantic/sql",
                json={
                    "sql": (
                        "SELECT service_name, SUM(amount) AS total "
                        "FROM semantic_daily_cost GROUP BY 1"
                    )
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text[:500])
        body = resp.json()
        self.assertEqual(body["columns"], ["service_name", "total"])
        self.assertEqual(body["types"], ["VARCHAR", "DOUBLE"])
        self.assertEqual(body["rows"], [["Virtual Machines", 12.5]])
        self.assertFalse(body["truncated"])

    def test_ddl_is_rejected_with_the_validator_message(self) -> None:
        resp = self.client.post(
            "/api/semantic/sql", json={"sql": "DROP TABLE semantic_daily_cost"}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Only SELECT", resp.json()["detail"])

    def test_ungoverned_relation_is_rejected_and_names_the_views(self) -> None:
        resp = self.client.post(
            "/api/semantic/sql", json={"sql": "SELECT * FROM daily_cost_history"}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("not a governed semantic view", resp.json()["detail"])


# ---------------------------------------------------------------------------
# SPA fallback + security headers
# ---------------------------------------------------------------------------

class SpaFallbackTests(_BaseApiTest):
    def test_unknown_api_path_returns_json_404(self) -> None:
        resp = self.client.get("/api/does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("Unknown API path", resp.json()["detail"])

    def test_security_headers_present(self) -> None:
        resp = self.client.get("/api/health")
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(resp.headers.get("x-frame-options"), "DENY")
        csp = resp.headers.get("content-security-policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src", csp)

    def test_spa_fallback_serves_index_when_present(self) -> None:
        # The frontend catch-all serves index.html at request time if it exists.
        # Create a temporary index.html in the configured dist directory.
        dist = Path(app_settings.frontend_dist)
        dist.mkdir(parents=True, exist_ok=True)
        index = dist / "index.html"
        # Preserve existing file if any.
        had_index = index.exists()
        prev = index.read_bytes() if had_index else None
        try:
            index.write_text("<html>flux-fallback-test</html>", encoding="utf-8")
            resp = self.client.get("/some/frontend/route/that/does/not/exist")
            self.assertEqual(resp.status_code, 200, resp.text[:500])
            self.assertIn("flux-fallback-test", resp.text)
            self.assertIn("text/html", resp.headers.get("content-type", ""))
            # Real asset path should also fall back when the specific file is absent.
            resp2 = self.client.get("/another/unknown/path")
            self.assertEqual(resp2.status_code, 200)
        finally:
            if prev is not None:
                index.write_bytes(prev)
            else:
                try:
                    index.unlink()
                except FileNotFoundError:
                    pass

    def test_health_always_200_without_auth(self) -> None:
        # /api/health is unauthenticated by design; even entra anonymous passes.
        entra = AuthService(Settings(auth_mode="entra", trust_easyauth_headers=True, entra_tenant_id=""))
        app.state.auth = entra
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
