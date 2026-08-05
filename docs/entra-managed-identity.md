# Entra authorization and managed-identity ARG access

Flux separates user authorization from Azure service authorization.

| Flow | Identity | Mechanism |
|---|---|---|
| User → Flux | Microsoft Entra user or group | App Service Authentication and `X-MS-CLIENT-PRINCIPAL` |
| Flux → Azure Resource Graph and Advisor | App Service managed identity | `ManagedIdentityCredential` and Azure RBAC |
| Flux → Cost Management | App Service managed identity | `ManagedIdentityCredential` and `Microsoft.CostManagement/*/read` |

## 1. Configure Microsoft Entra app roles

On the app registration used by App Service Authentication, define:

| Display name | Value | Allowed member types |
|---|---|---|
| Flux Reader | `Flux.Reader` | Users/Groups |
| Flux Administrator | `Flux.Admin` | Users/Groups |

Assign users or groups through the enterprise application.

Flux maps:

- `Flux.Reader` to read-only application access;
- `Flux.Admin` to read access plus Azure integration configuration and synchronization.

Role values can be replaced or extended with comma-separated app-role values or group object IDs:

```text
FLUX_ENTRA_ADMIN_ASSIGNMENTS=Flux.Admin,<admin-group-object-id>
FLUX_ENTRA_READER_ASSIGNMENTS=Flux.Reader,<reader-group-object-id>
```

## 2. Enable App Service Authentication

In the Web App:

1. Open **Authentication**.
2. Add Microsoft as the identity provider using the Flux app registration.
3. Require authentication.
4. Redirect unauthenticated browser requests to Microsoft.
5. Restrict the issuer to the expected tenant.
6. Configure the app so untrusted traffic cannot bypass the App Service authentication layer.

Application settings:

```text
FLUX_AUTH_MODE=entra
FLUX_ENTRA_TENANT_ID=<tenant-guid>
FLUX_ENTRA_ADMIN_ASSIGNMENTS=Flux.Admin
FLUX_ENTRA_READER_ASSIGNMENTS=Flux.Reader
FLUX_AUTH_LOGIN_PATH=/.auth/login/aad
FLUX_AUTH_LOGOUT_PATH=/.auth/logout
```

App Service validates the user token and injects a Base64-encoded claims document in `X-MS-CLIENT-PRINCIPAL`. Flux:

1. decodes the document;
2. validates the tenant claim when `FLUX_ENTRA_TENANT_ID` is configured;
3. maps role and group claims;
4. returns the resolved session from `/api/session`;
5. enforces reader/admin dependencies on API routes.

The frontend hides Integrations from readers, but the API authorization checks are the security boundary.

## 3. Enable managed identity

### System-assigned

```powershell
$identity = az webapp identity assign `
  --resource-group <app-resource-group> `
  --name <web-app-name> | ConvertFrom-Json

$principalId = $identity.principalId
```

No client ID application setting is required for a system-assigned identity.

### User-assigned

Assign the identity to the Web App, then set:

```text
FLUX_MANAGED_IDENTITY_CLIENT_ID=<managed-identity-client-id>
```

This explicitly selects the user-assigned identity when multiple identities are available.

## 4. Grant Azure Resource Graph access

Azure Resource Graph returns only resources the calling principal can read. Grant the managed identity `Reader` on each configured subscription:

```powershell
az role assignment create `
  --assignee-object-id $principalId `
  --assignee-principal-type ServicePrincipal `
  --role Reader `
  --scope /subscriptions/<subscription-guid>
```

Repeat for every subscription, or assign at a shared management-group scope when that matches the organization's governance model.

The built-in `Reader` role is the straightforward starting point for inventory and Advisor. A custom role must include the appropriate resource read permissions and `Microsoft.ResourceGraph/resources/read`.

Cost synchronization also requires Cost Management query access at each configured subscription or an inherited management-group scope. The deployed `FinOps Platform Reader` custom role includes:

```text
Microsoft.CostManagement/*/read
```

Keep this a read-only data-plane path; Flux does not create budgets, exports, reservations, or Azure resources.

RBAC assignments can take several minutes to propagate.

## 5. Select the provider

In **Integrations**:

1. add the tenant and subscription scopes;
2. select **App Service managed identity**;
3. save;
4. synchronize.

Flux obtains a token for:

```text
https://management.azure.com/.default
```

It then submits paginated Resource Graph requests for resources and active Advisor recommendations, followed by subscription-scoped Cost Management Query requests for actual and amortized month-to-date cost. No client secret or user access token is stored.

## 6. Local development

Keep:

```text
FLUX_AUTH_MODE=mock
```

Authenticate Azure PowerShell:

```powershell
Connect-AzAccount
```

Select **Local Azure PowerShell context** in Integrations.

For controlled authorization tests, set `FLUX_AUTH_MODE=entra` and send a locally generated `X-MS-CLIENT-PRINCIPAL` header. Never accept such client-supplied headers on a production route that bypasses Easy Auth.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401` from Flux | Entra mode is enabled but App Service did not inject a principal. |
| `403` with role message | User is authenticated but has no mapped Flux role. |
| Tenant mismatch | `FLUX_ENTRA_TENANT_ID` does not match the principal tenant claim. |
| Managed identity token failure | Identity is not enabled, or the configured user-assigned client ID is wrong. |
| ARG `403` | Managed identity lacks Reader/custom read access at the requested scope. |
| Empty ARG result | Identity can authenticate but cannot read resources in the configured subscriptions. |
| Cost `403` | Managed identity lacks `Microsoft.CostManagement/*/read` at the subscription or an inherited scope. |
| Cost `429` | Cost Management throttled the query; Flux retries, preserves completed scopes, and retains previous successful scopes. |
