from __future__ import annotations

import base64
import json
import os
from typing import Any, Mapping

from fastapi import HTTPException, Request, status

from .config import Settings


ROLE_CLAIMS = {
    "roles",
    "role",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
}
GROUP_CLAIMS = {
    "groups",
    "group",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
}
USER_ID_CLAIMS = (
    "http://schemas.microsoft.com/identity/claims/objectidentifier",
    "oid",
    "sub",
    "nameidentifier",
)
TENANT_CLAIMS = (
    "http://schemas.microsoft.com/identity/claims/tenantid",
    "tid",
)
NAME_CLAIMS = (
    "name",
    "preferred_username",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
)
EMAIL_CLAIMS = (
    "preferred_username",
    "email",
    "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)


def _first(claims: dict[str, list[str]], names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        values = claims.get(name.lower(), [])
        if values:
            return values[0]
    return default


def decode_principal(value: str) -> dict[str, Any]:
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.b64decode(padded, validate=True).decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The App Service principal header is invalid.",
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The App Service principal header has an invalid shape.",
        )
    return payload


class AuthService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.admin_assignments = set(settings.entra_admin_assignments)
        self.reader_assignments = set(settings.entra_reader_assignments)

    def resolve(self, headers: Mapping[str, str]) -> dict[str, Any]:
        if self.settings.auth_mode == "mock":
            # Never hand out the local-admin session unless mock auth was
            # explicitly unlocked; require_reader/require_admin also refuse
            # with 503, this keeps /api/session honest about it.
            if not getattr(self.settings, "allow_mock_auth", False):
                return self._session(authenticated=False)
            return self._session(
                authenticated=True,
                user_id="local-admin",
                name=self.settings.mock_user_name,
                email=self.settings.mock_user_email,
                tenant_id="local",
                roles=["admin", "reader"],
                claims_source="mock",
            )
        if self.settings.auth_mode in {"none", "anonymous"}:
            return self._session(authenticated=False)
        return self._entra_session(headers)

    def _entra_session(self, headers: Mapping[str, str]) -> dict[str, Any]:
        if not getattr(self.settings, "trust_easyauth_headers", False):
            # Without a platform front end stripping inbound x-ms-* headers,
            # this header is whatever the caller typed — any client could
            # mint an admin principal (#101). Refuse to read it unless the
            # deployment asserts Easy Auth fronting
            # (FLUX_TRUST_EASYAUTH_HEADERS=1; implied on App Service).
            return self._session(authenticated=False)
        encoded = headers.get("x-ms-client-principal", "")
        if not encoded:
            return self._session(authenticated=False)
        payload = decode_principal(encoded)
        claims: dict[str, list[str]] = {}
        for item in payload.get("claims", []):
            claim_type = str(item.get("typ", "")).strip().lower()
            claim_value = str(item.get("val", "")).strip()
            if claim_type and claim_value:
                claims.setdefault(claim_type, []).append(claim_value)

        tenant_id = _first(claims, TENANT_CLAIMS)
        if (
            self.settings.entra_tenant_id
            and tenant_id.lower() != self.settings.entra_tenant_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The signed-in user belongs to a different Microsoft Entra tenant.",
            )

        assignments = {
            value.lower()
            for claim_type, values in claims.items()
            if claim_type in ROLE_CLAIMS or claim_type in GROUP_CLAIMS
            for value in values
        }
        roles: list[str] = []
        if assignments & self.admin_assignments:
            roles.extend(["admin", "reader"])
        elif assignments & self.reader_assignments:
            roles.append("reader")

        return self._session(
            authenticated=True,
            user_id=_first(
                claims,
                USER_ID_CLAIMS,
                headers.get("x-ms-client-principal-id", ""),
            ),
            name=_first(
                claims,
                NAME_CLAIMS,
                headers.get("x-ms-client-principal-name", "Microsoft Entra user"),
            ),
            email=_first(claims, EMAIL_CLAIMS),
            tenant_id=tenant_id,
            roles=roles,
            claims_source="app_service",
        )

    def _session(
        self,
        *,
        authenticated: bool,
        user_id: str = "",
        name: str = "",
        email: str = "",
        tenant_id: str = "",
        roles: list[str] | None = None,
        claims_source: str = "",
    ) -> dict[str, Any]:
        resolved_roles = roles or []
        return {
            "authenticated": authenticated,
            "authMode": self.settings.auth_mode,
            "user": {
                "id": user_id,
                "displayName": name,
                "email": email,
                "tenantId": tenant_id,
                "roles": resolved_roles,
                "claimsSource": claims_source,
            }
            if authenticated
            else None,
            "permissions": {
                "canRead": "reader" in resolved_roles or "admin" in resolved_roles,
                "canManageIntegrations": "admin" in resolved_roles,
                "canSyncIntegrations": "admin" in resolved_roles,
            },
            "authActions": {
                "loginPath": self.settings.auth_login_path,
                "logoutPath": (
                    f"{self.settings.auth_logout_path}"
                    "?post_logout_redirect_uri=/"
                ),
            },
        }


def _is_prod() -> bool:
    return bool(os.getenv("WEBSITE_SITE_NAME", "").strip())


def _is_auth_misconfigured(settings: Settings) -> bool:
    """True when the deployment must refuse to serve rather than fail open.

    Mock auth resolves every caller to local-admin with both roles, so it is
    treated as a misconfiguration *anywhere* unless the operator has also set
    FLUX_ALLOW_MOCK_AUTH=1. Gating this on WEBSITE_SITE_NAME alone left every
    non-App-Service host -- containers, VMs, staging boxes, a laptop bound to
    0.0.0.0 -- serving unauthenticated admin on all 119 routes.
    """
    if settings.auth_mode == "mock":
        return not getattr(settings, "allow_mock_auth", False)
    if not _is_prod():
        return False
    if settings.auth_mode == "entra" and not settings.entra_tenant_id.strip():
        return True
    return False


def session_from_request(request: Request) -> dict[str, Any]:
    return request.app.state.auth.resolve(request.headers)


def require_reader(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.auth.settings
    if _is_auth_misconfigured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth misconfigured",
        )
    session = session_from_request(request)
    if not session["authenticated"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in with Microsoft Entra ID to use Flux.",
        )
    if not session["permissions"]["canRead"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Flux.Reader or Flux.Admin access is required.",
        )
    return session


def require_admin(request: Request) -> dict[str, Any]:
    session = require_reader(request)
    if not session["permissions"]["canManageIntegrations"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Flux.Admin access is required to manage Azure integrations.",
        )
    return session
