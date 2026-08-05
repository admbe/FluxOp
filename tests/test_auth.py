import base64
import json
import unittest

from fastapi import HTTPException

from api.auth import AuthService
from api.config import Settings


def principal_header(claims):
    payload = {
        "auth_typ": "aad",
        "name_typ": "name",
        "role_typ": "roles",
        "claims": claims,
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.auth = AuthService(
            Settings(
                auth_mode="entra",
                entra_tenant_id="tenant-1",
                entra_admin_assignments=("flux.admin", "admin-group"),
                entra_reader_assignments=("flux.reader", "reader-group"),
            )
        )

    def test_admin_app_role_maps_to_admin_and_reader(self):
        encoded = principal_header(
            [
                {"typ": "name", "val": "Ada Lovelace"},
                {"typ": "roles", "val": "Flux.Admin"},
                {"typ": "tid", "val": "tenant-1"},
                {"typ": "oid", "val": "user-1"},
            ]
        )
        session = self.auth.resolve({"x-ms-client-principal": encoded})
        self.assertTrue(session["permissions"]["canRead"])
        self.assertTrue(session["permissions"]["canManageIntegrations"])
        self.assertEqual(session["user"]["displayName"], "Ada Lovelace")

    def test_group_can_map_to_reader(self):
        encoded = principal_header(
            [
                {"typ": "groups", "val": "reader-group"},
                {"typ": "tid", "val": "tenant-1"},
            ]
        )
        session = self.auth.resolve({"x-ms-client-principal": encoded})
        self.assertTrue(session["permissions"]["canRead"])
        self.assertFalse(session["permissions"]["canManageIntegrations"])

    def test_wrong_tenant_is_rejected(self):
        encoded = principal_header(
            [
                {"typ": "roles", "val": "Flux.Reader"},
                {"typ": "tid", "val": "tenant-2"},
            ]
        )
        with self.assertRaises(HTTPException) as error:
            self.auth.resolve({"x-ms-client-principal": encoded})
        self.assertEqual(error.exception.status_code, 403)

    def test_missing_principal_is_anonymous(self):
        session = self.auth.resolve({})
        self.assertFalse(session["authenticated"])
        self.assertFalse(session["permissions"]["canRead"])


if __name__ == "__main__":
    unittest.main()
