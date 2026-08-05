from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable

from azure.storage.blob import BlobServiceClient


def focus_error_message(error: Exception) -> str:
    """Translate Azure Blob data-plane authorization failures into an action."""
    message = str(error)
    status_code = getattr(error, "status_code", None)
    normalized = message.lower()
    if (
        status_code in {401, 403}
        or "authorizationpermissionmismatch" in normalized
        or "authorization permission mismatch" in normalized
    ):
        return (
            "FOCUS export storage access was denied. Assign the Flux App "
            "Service managed identity the Storage Blob Data Reader role on "
            "the configured export container or storage account, then retry. "
            f"Azure response: {message}"
        )
    return message


@dataclass(frozen=True)
class FocusManifest:
    path: str
    payload: dict[str, Any]
    open_blob: Callable[[str, Path], None]

    @property
    def submitted_at(self) -> str:
        return str((self.payload.get("runInfo") or {}).get("submittedTime") or "")


def _validate_manifest(
    path: str, payload: dict[str, Any], expected_type: str = "FocusCost"
) -> None:
    export = payload.get("exportConfig") or {}
    run = payload.get("runInfo") or {}
    if export.get("type") != expected_type:
        raise ValueError(f"{path} is not a {expected_type} export.")
    if not run.get("runId") or not run.get("startDate") or not run.get("endDate"):
        raise ValueError(f"{path} does not contain a complete export run.")
    if not payload.get("blobs"):
        raise ValueError(f"{path} does not reference any charge files.")


def local_manifests(
    root: Path, expected_type: str = "FocusCost"
) -> list[FocusManifest]:
    manifests: list[FocusManifest] = []
    for manifest_path in root.rglob("manifest.json"):
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        _validate_manifest(str(manifest_path), payload, expected_type)

        def copy_local(blob_name: str, destination: Path, *, base=root) -> None:
            parts = blob_name.split("/")
            source = base.joinpath(*parts)
            if not source.exists() and parts[0].lower() == "focus":
                source = base.joinpath(*parts[1:])
            if not source.exists():
                candidates = list(base.rglob(Path(blob_name).name))
                if len(candidates) != 1:
                    raise FileNotFoundError(blob_name)
                source = candidates[0]
            destination.write_bytes(source.read_bytes())

        manifests.append(
            FocusManifest(str(manifest_path.resolve()), payload, copy_local)
        )
    return sorted(manifests, key=lambda item: item.submitted_at)


def azure_blob_manifests(
    account_url: str,
    container: str,
    prefix: str,
    credential: Any,
    expected_type: str = "FocusCost",
) -> list[FocusManifest]:
    service = BlobServiceClient(account_url=account_url, credential=credential)
    client = service.get_container_client(container)
    manifests: list[FocusManifest] = []
    for blob in client.list_blobs(name_starts_with=prefix):
        if not blob.name.lower().endswith("manifest.json"):
            continue
        payload = json.loads(
            client.download_blob(blob.name).readall().decode("utf-8-sig")
        )
        _validate_manifest(blob.name, payload, expected_type)

        def download(
            blob_name: str,
            destination: Path,
            *,
            container_client=client,
        ) -> None:
            with destination.open("wb") as stream:
                container_client.download_blob(blob_name).readinto(stream)

        manifests.append(FocusManifest(blob.name, payload, download))
    return sorted(manifests, key=lambda item: item.submitted_at)


def import_manifests(
    database: Any,
    import_run_id: str,
    manifests: Iterable[FocusManifest],
    *,
    maximum_manifests: int,
) -> dict[str, int]:
    available = list(manifests)
    imported = 0
    skipped = 0
    charges = 0
    candidates = [
        item
        for item in available
        if not database.focus_manifest_imported(item.path)
    ][: max(maximum_manifests, 1)]
    for item in candidates:
        with tempfile.TemporaryDirectory(prefix="flux-focus-") as temporary:
            root = Path(temporary)
            files: list[tuple[str, Path]] = []
            for index, blob in enumerate(item.payload["blobs"]):
                blob_name = str(blob["blobName"])
                target = root / f"{index:04d}.csv"
                item.open_blob(blob_name, target)
                files.append((blob_name, target))
            charges += database.store_focus_manifest(
                import_run_id,
                item.path,
                item.payload,
                files,
            )
            imported += 1
    skipped = len(available) - len(candidates)
    return {"imported": imported, "skipped": max(skipped, 0), "charges": charges}
