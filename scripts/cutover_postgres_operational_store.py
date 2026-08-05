from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

from azure.identity import DeviceCodeCredential
from azure.mgmt.web import WebSiteManagementClient
from azure.mgmt.web.models import StringDictionary


TENANT_ID = os.getenv("FLUX_AZURE_TENANT_ID", "")
SUBSCRIPTION_ID = os.getenv("FLUX_AZURE_SUBSCRIPTION_ID", "")
RESOURCE_GROUP = os.getenv("FLUX_APP_RESOURCE_GROUP", "")
WEB_APP_NAME = os.getenv("FLUX_APP_NAME", "FluxFinOps")
POSTGRES_SERVER = os.getenv("FLUX_POSTGRES_SERVER_NAME", "")
POSTGRES_DATABASE = os.getenv("FLUX_POSTGRES_DATABASE", "postgres")
POSTGRES_USER = os.getenv("FLUX_POSTGRES_ADMIN_LOGIN", "fluxadmin")
PASSWORD_PATH = Path(
    os.getenv("FLUX_POSTGRES_PASSWORD_PATH", ".tmp/postgres-admin-password.txt")
)
DUCKDB_PATH = os.getenv("FLUX_DUCKDB_PATH", "/home/data/flux.duckdb")
WORKER_NAME = os.getenv("FLUX_SYNC_WEBJOB_NAME", "flux-sync-worker")


def _prompt_callback(*args: object, **_: object) -> None:
    url = args[0] if len(args) > 0 else "https://login.microsoft.com/device"
    code = args[1] if len(args) > 1 else ""
    expiry = args[2] if len(args) > 2 else None
    print(
        {"url": str(url), "code": str(code), "expires": str(expiry)},
        flush=True,
    )


def _request(
    token: str,
    method: str,
    url: str,
    *,
    payload: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: int = 180,
) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(extra_headers or {})
    request = Request(
        url,
        data=payload,
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _command(
    token: str,
    scm_url: str,
    command: str,
    *,
    check: bool = True,
) -> dict[str, object]:
    payload = json.dumps(
        {"command": command, "dir": "/home/site/wwwroot"}
    ).encode("utf-8")
    result = json.loads(
        _request(
            token,
            "POST",
            f"{scm_url}/api/command",
            payload=payload,
            content_type="application/json",
            timeout=600,
        )
    )
    if check and int(result.get("ExitCode", -1)) != 0:
        raise RuntimeError(
            str(result.get("Error") or result.get("Output") or result)
        )
    return result


def _worker_action(token: str, scm_url: str, action: str) -> None:
    del scm_url
    _request(
        token,
        "POST",
        (
            "https://management.azure.com/subscriptions/"
            f"{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/"
            f"Microsoft.Web/sites/{WEB_APP_NAME}/continuouswebjobs/"
            f"{WORKER_NAME}/{action}?api-version=2022-03-01"
        ),
        payload=b"",
        timeout=60,
    )


def main() -> None:
    password = PASSWORD_PATH.read_text(encoding="utf-8").strip()
    database_url = (
        f"postgresql://{quote(POSTGRES_USER, safe='')}:"
        f"{quote(password, safe='')}"
        f"@{POSTGRES_SERVER}.postgres.database.azure.com:5432/"
        f"{POSTGRES_DATABASE}?sslmode=require"
    )
    credential = DeviceCodeCredential(
        tenant_id=TENANT_ID,
        prompt_callback=_prompt_callback,
    )
    token = credential.get_token(
        "https://management.azure.com/.default"
    ).token
    client = WebSiteManagementClient(credential, SUBSCRIPTION_ID)
    site = client.web_apps.get(RESOURCE_GROUP, WEB_APP_NAME)
    scm_host = next(
        (
            host
            for host in (site.enabled_host_names or [])
            if ".scm." in host
        ),
        "",
    )
    if not scm_host:
        raise RuntimeError("FluxFinOps SCM host could not be resolved.")
    scm_url = f"https://{scm_host}"
    marker_url = f"{scm_url}/api/vfs/data/flux.duckdb.deployment-quiesce"

    migration_job_url = (
        f"{scm_url}/api/triggeredwebjobs/flux-operational-migration"
    )
    try:
        existing_job = json.loads(
            _request(token, "GET", migration_job_url, timeout=30)
        )
    except Exception as error:
        raise RuntimeError(
            "The PostgreSQL migration WebJob is not deployed."
        ) from error

    settings = client.web_apps.list_application_settings(
        RESOURCE_GROUP,
        WEB_APP_NAME,
    )
    properties = dict(settings.properties or {})
    properties["FLUX_OPERATIONAL_DATABASE_URL"] = database_url
    properties["FLUX_OPERATIONAL_DATABASE_ENABLED"] = "false"
    client.web_apps.update_application_settings(
        RESOURCE_GROUP,
        WEB_APP_NAME,
        StringDictionary(properties=properties),
    )

    marker_created = False
    worker_stopped = False
    cutover_succeeded = False
    try:
        marker = (
            f"postgres-cutover="
            f"{datetime.now(timezone.utc).isoformat()}\n"
        ).encode("utf-8")
        for attempt in range(24):
            try:
                _request(
                    token,
                    "PUT",
                    marker_url,
                    payload=marker,
                    content_type="application/octet-stream",
                    extra_headers={"If-Match": "*"},
                )
                break
            except Exception:
                if attempt == 23:
                    raise
                time.sleep(5)
        marker_created = True
        try:
            _worker_action(token, scm_url, "stop")
            worker_stopped = True
        except Exception:
            # A stopped or temporarily unavailable worker is acceptable after
            # the quiesce marker has been published.
            worker_stopped = False

        for _ in range(120):
            lock = _command(
                token,
                scm_url,
                "flock -xn /home/data/flux.duckdb.deployment.lock -c true",
                check=False,
            )
            if int(lock.get("ExitCode", -1)) == 0:
                break
            time.sleep(5)
        else:
            raise RuntimeError("DuckDB writers did not quiesce in ten minutes.")

        previous_run_id = str(
            (existing_job.get("latest_run") or {}).get("id") or ""
        )
        _request(
            token,
            "POST",
            (
                "https://management.azure.com/subscriptions/"
                f"{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/"
                f"Microsoft.Web/sites/{WEB_APP_NAME}/triggeredwebjobs/"
                "flux-operational-migration/run?api-version=2022-03-01"
            ),
            payload=b"",
            timeout=60,
        )
        migration_payload: dict[str, object] | None = None
        for _ in range(120):
            current_job = json.loads(
                _request(token, "GET", migration_job_url, timeout=30)
            )
            latest_run = current_job.get("latest_run") or {}
            run_id = str(latest_run.get("id") or "")
            run_status = str(latest_run.get("status") or "")
            if run_id and run_id != previous_run_id:
                if run_status == "Success":
                    output = _request(
                        token,
                        "GET",
                        str(latest_run.get("output_url") or ""),
                        timeout=30,
                    ).decode("utf-8", errors="replace")
                    match = re.search(
                        r'(\{"status":"succeeded".*\})',
                        output,
                    )
                    if not match:
                        raise RuntimeError(
                            "Migration WebJob did not publish validation output."
                        )
                    migration_payload = json.loads(match.group(1))
                    break
                if run_status in {"Failed", "Aborted"}:
                    raise RuntimeError(
                        f"Migration WebJob finished with {run_status}."
                    )
            time.sleep(2)
        if migration_payload is None:
            raise RuntimeError("Migration WebJob did not finish in four minutes.")
        if migration_payload.get("status") != "succeeded":
            raise RuntimeError("Operational-state validation did not succeed.")

        properties["FLUX_OPERATIONAL_DATABASE_ENABLED"] = "true"
        client.web_apps.update_application_settings(
            RESOURCE_GROUP,
            WEB_APP_NAME,
            StringDictionary(properties=properties),
        )
        client.web_apps.restart(RESOURCE_GROUP, WEB_APP_NAME)
        cutover_succeeded = True
    finally:
        if marker_created:
            try:
                _request(
                    token,
                    "DELETE",
                    marker_url,
                    extra_headers={"If-Match": "*"},
                    timeout=60,
                )
            except Exception:
                pass
        if cutover_succeeded or worker_stopped:
            try:
                _worker_action(token, scm_url, "start")
            except Exception:
                pass

    deadline = time.monotonic() + 180
    worker_status = ""
    while time.monotonic() < deadline:
        current = client.web_apps.get(RESOURCE_GROUP, WEB_APP_NAME)
        try:
            worker = json.loads(
                _request(
                    token,
                    "GET",
                    f"{scm_url}/api/continuouswebjobs/{WORKER_NAME}",
                    timeout=30,
                )
            )
            worker_status = str(worker.get("status") or "")
        except Exception:
            worker_status = ""
        if (
            str(current.state).lower() == "running"
            and worker_status.lower() in {"running", "initializing"}
        ):
            break
        time.sleep(5)
    else:
        raise RuntimeError(
            "FluxFinOps or its sync worker did not become ready after cutover."
        )

    print(
        {
            "webApp": WEB_APP_NAME,
            "operationalDatabase": "postgres",
            "migrationTables": len(migration_payload.get("tables") or []),
            "workerStatus": worker_status,
            "state": str(current.state),
        },
        flush=True,
    )


if __name__ == "__main__":
    main()
