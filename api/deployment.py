from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
from typing import Iterator

from filelock import FileLock


class DeploymentQuiesced(RuntimeError):
    pass


def quiesce_path(database_path: Path) -> Path:
    return Path(str(database_path) + ".deployment-quiesce")


def deployment_lock_path(database_path: Path) -> Path:
    return Path(str(database_path) + ".deployment.lock")


def is_deployment_quiesced(database_path: Path) -> bool:
    return quiesce_path(database_path).exists()


@contextmanager
def deployment_lease(database_path: Path) -> Iterator[None]:
    """Hold a shared job lease that a deployment can drain exclusively."""
    if is_deployment_quiesced(database_path):
        raise DeploymentQuiesced("Flux deployment quiesce is active.")

    lock_path = deployment_lock_path(database_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        import fcntl

        with lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                if is_deployment_quiesced(database_path):
                    raise DeploymentQuiesced("Flux deployment quiesce is active.")
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return

    # Local Windows development has no portable shared flock equivalent.
    # An exclusive filelock preserves the same safety invariant, with lower
    # concurrency only in that environment.
    with FileLock(str(lock_path) + ".windows", timeout=-1):
        if is_deployment_quiesced(database_path):
            raise DeploymentQuiesced("Flux deployment quiesce is active.")
        yield
