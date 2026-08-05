from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from urllib.request import Request, urlopen

from .database import FluxDatabase


TOOLKIT_VERSION = "v14"
TOOLKIT_COMMIT = "f3b1b23f3ea6044bcd8cb767620cdd43704ce90a"
TOOLKIT_LICENSE = "MIT"
TOOLKIT_PROJECT_URL = "https://github.com/microsoft/finops-toolkit"
RAW_ROOT = (
    "https://raw.githubusercontent.com/microsoft/finops-toolkit/"
    f"{TOOLKIT_VERSION}/src/open-data"
)


@dataclass(frozen=True)
class ToolkitDataset:
    name: str
    filename: str
    sha256: str

    @property
    def source_url(self) -> str:
        return f"{RAW_ROOT}/{self.filename}"


DATASETS = (
    ToolkitDataset(
        "Services",
        "Services.csv",
        "80235d624655498839e181e17dc939390dbc2265daac1c5529cfbc1fa9fe3ad5",
    ),
    ToolkitDataset(
        "ResourceTypes",
        "ResourceTypes.csv",
        "eac9195325f9b6f5eb1a16a26af9674c1259e3e8a872e2184f7e2d4fe499ae2f",
    ),
    ToolkitDataset(
        "Regions",
        "Regions.csv",
        "46cab7300079df85edc287fb81b06b2ccc643bead0fa9daf554dd11f370a3688",
    ),
    ToolkitDataset(
        "PricingUnits",
        "PricingUnits.csv",
        "772fd9b18054b4478656328ff69543124622f2cea6bb935a8b74d887133f984f",
    ),
    ToolkitDataset(
        "CommitmentDiscountEligibility",
        "CommitmentDiscountEligibility.csv",
        "f54a05bfddd79e233072c37a7aca97407fada1d2ce5defe27c53c55a8833e14d",
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(dataset: ToolkitDataset, cache_root: Path) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_root / dataset.filename
    if destination.exists() and _sha256(destination) == dataset.sha256:
        return destination
    temporary = destination.with_suffix(destination.suffix + ".download")
    request = Request(
        dataset.source_url,
        headers={"User-Agent": "FluxFinOps/FinOpsToolkitCompatibility"},
    )
    with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)
    actual = _sha256(temporary)
    if actual != dataset.sha256:
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"{dataset.name} checksum mismatch: expected "
            f"{dataset.sha256}, received {actual}."
        )
    temporary.replace(destination)
    return destination


def synchronize_open_data(
    database: FluxDatabase,
    cache_root: Path,
) -> dict[str, int]:
    files = {
        dataset.name: _download(dataset, cache_root)
        for dataset in DATASETS
    }
    metadata = {
        dataset.name: {
            "toolkitVersion": TOOLKIT_VERSION,
            "upstreamCommit": TOOLKIT_COMMIT,
            "sourceUrl": dataset.source_url,
            "sha256": dataset.sha256,
            "license": TOOLKIT_LICENSE,
        }
        for dataset in DATASETS
    }
    return database.replace_finops_toolkit_open_data(files, metadata)
