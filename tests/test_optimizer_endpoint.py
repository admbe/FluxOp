"""Request-level coverage for POST /api/commitments/optimizer/runs.

This endpoint shipped twice without a single HTTP-level test and broke both
times inside FastAPI's dependency machinery (f72dd05's Body() attempt left
every request dying 500 in body validation, before the handler ran). The
existing optimizer tests exercise the pipeline functions only, which is why
41 of them passed while the button returned "Request failed (500)".
"""
import unittest

from fastapi.testclient import TestClient

from api.auth import AuthService
from api.config import Settings
from api.main import app


class OptimizerRunEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # Auth fails closed since the 2026-08-14 audit landed: the module
        # app defaults to entra, so these request-level tests must install
        # an explicitly unlocked mock service like test_api_integration does.
        self._orig_auth = app.state.auth
        app.state.auth = AuthService(
            Settings(auth_mode="mock", allow_mock_auth=True)
        )

    def tearDown(self):
        app.state.auth = self._orig_auth

    def test_empty_body_never_500s(self):
        response = self.client.post(
            "/api/commitments/optimizer/runs", content=b""
        )
        self.assertNotEqual(response.status_code, 500)
        # Locally the optimizer flag is off, so the friendly 409 is the
        # expected terminal state; anything else that isn't a 500 means
        # the flag is on and the run path was reached.
        if response.status_code == 409:
            self.assertIn("disabled", response.json()["detail"])

    def test_empty_json_object_never_500s(self):
        response = self.client.post(
            "/api/commitments/optimizer/runs", json={}
        )
        self.assertNotEqual(response.status_code, 500)

    def test_invalid_json_is_422(self):
        response = self.client.post(
            "/api/commitments/optimizer/runs",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)

    def test_out_of_range_lookback_is_422(self):
        response = self.client.post(
            "/api/commitments/optimizer/runs",
            json={"lookbackDays": 3},
        )
        self.assertEqual(response.status_code, 422)

    def test_unknown_field_is_tolerated_like_every_other_endpoint(self):
        # ApiModel ignores extras (only the S-005 query model forbids them),
        # so manual validation must not become stricter than the signature
        # path it replaced.
        response = self.client.post(
            "/api/commitments/optimizer/runs",
            json={"lookbackDays": 30, "bogus": True},
        )
        self.assertNotEqual(response.status_code, 500)
        self.assertNotEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
