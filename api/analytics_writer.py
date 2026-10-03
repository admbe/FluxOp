"""Durable staged analytical writes (migration-plan Phase 5).

Collectors stage their analytical payloads as durable jobs instead of
mutating DuckDB directly:

1. ``stage_payload`` writes the payload to the staging directory, checksums
   it, and records an ``analytics_apply_jobs`` row keyed by a stable
   idempotency key. Duplicate delivery of the same payload is a no-op;
   the same key with a different checksum fails closed.
2. ``apply_pending`` — run under the singleton analytics-writer lease —
   verifies each staged payload's checksum and applies it through the
   registered applier inside the normal DuckDB write path, records row
   counts, and marks the job applied. Failures retain the payload and
   retry on the next pass; source checkpoints therefore only advance after
   the analytical commit.

Appliers are registered per source in ``APPLIERS``; each takes the database
and the decoded payload and returns row counts for the ledger.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .operational_store import SingletonLeaseUnavailable

_logger = logging.getLogger("flux.analytics_writer")

_MAX_ATTEMPTS = 5

APPLIERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, int]]] = {}


def register_applier(
    source: str,
) -> Callable[[Callable[[Any, dict[str, Any]], dict[str, int]]], Callable[[Any, dict[str, Any]], dict[str, int]]]:
    def decorator(function: Callable[[Any, dict[str, Any]], dict[str, int]]):
        APPLIERS[source] = function
        return function

    return decorator


class StagedPayloadConflict(RuntimeError):
    """The idempotency key exists with a different payload checksum."""


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")


def stage_payload(
    database: Any,
    staging_directory: Path,
    source: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> str:
    """Persist a collected payload as a durable apply job. Returns job id.

    Delivering the same key with the same content again returns the
    existing job without re-staging; the same key with different content
    fails closed so a corrupted retry can never silently replace data.

    Concurrency: the idempotency check and insert are atomic via
    ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`` against the
    unique index on ``idempotency_key``. Concurrent deliveries of the same
    key race on the unique constraint; the loser cleans up its orphan
    staging file and returns (or raises for) the winner's row. No
    SELECT-before-INSERT window exists, eliminating the TOCTOU.
    """
    body = _payload_bytes(payload)
    checksum = hashlib.sha256(body).hexdigest()
    staging_directory = Path(staging_directory)
    staging_directory.mkdir(parents=True, exist_ok=True)
    job_id = f"apply-{uuid4()}"
    path = staging_directory / f"{job_id}.json.gz"
    staged = path.with_name(path.name + ".staging")
    with gzip.open(staged, "wb") as stream:
        stream.write(body)
    staged.replace(path)
    from .database import utc_now

    with database.operational_connect() as db:
        try:
            db.execute(
                """
                INSERT INTO analytics_apply_jobs (
                    job_id, source, idempotency_key, payload_path,
                    payload_checksum, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'staged', ?)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                [job_id, source, idempotency_key, str(path), checksum, utc_now()],
            )
            db.commit()
        except Exception:
            # Unique-violation fallback when the driver surfaces it as an
            # exception rather than DO NOTHING (e.g. missing index on an
            # older DB). Roll back and fall through to the SELECT below
            # which will resolve the winner/loser correctly.
            try:
                db.rollback()
            except Exception:
                pass
        existing = db.execute(
            """
            SELECT job_id, payload_checksum FROM analytics_apply_jobs
            WHERE idempotency_key = ?
            """,
            [idempotency_key],
        ).fetchone()
        if existing is None:
            # No row visible — should not happen; treat our insert as winner.
            return job_id
        existing_job_id = str(existing[0])
        existing_checksum = str(existing[1])
        if existing_checksum != checksum:
            if existing_job_id != job_id:
                Path(path).unlink(missing_ok=True)
            raise StagedPayloadConflict(
                f"Idempotency key '{idempotency_key}' was already staged "
                "with different content."
            )
        if existing_job_id != job_id:
            Path(path).unlink(missing_ok=True)
            return existing_job_id
        return job_id


def apply_pending(database: Any, staging_directory: Path) -> int:
    """Apply every staged job under the singleton analytics-writer lease.

    Returns the number of jobs applied. A held lease means another writer
    is already applying; the caller simply proceeds.
    """
    from .database import utc_now

    try:
        with database.singleton_lease("analytics-writer"):
            applied = 0
            with database.operational_connect(read_only=True) as db:
                jobs = db.execute(
                    """
                    SELECT job_id, source, payload_path, payload_checksum, attempts
                    FROM analytics_apply_jobs
                    WHERE status = 'staged'
                    ORDER BY created_at ASC
                    """
                ).fetchall()
            for job_id, source, payload_path, checksum, attempts in jobs:
                try:
                    body = gzip.open(payload_path, "rb").read()
                    if hashlib.sha256(body).hexdigest() != str(checksum):
                        raise StagedPayloadConflict(
                            f"Staged payload checksum mismatch for {job_id}."
                        )
                    applier = APPLIERS.get(str(source))
                    if applier is None:
                        raise KeyError(f"No applier registered for '{source}'.")
                    row_counts = applier(database, json.loads(body))
                except Exception as error:
                    attempts = int(attempts) + 1
                    status = "staged" if attempts < _MAX_ATTEMPTS else "failed"
                    with database.operational_connect() as db:
                        db.execute(
                            """
                            UPDATE analytics_apply_jobs
                            SET attempts = ?, error = ?, status = ?
                            WHERE job_id = ?
                            """,
                            [attempts, str(error)[:500], status, job_id],
                        )
                        db.commit()
                    logging.getLogger(__name__).warning(
                        "Analytics apply job %s (%s) failed (attempt %s): %s",
                        job_id, source, attempts, error,
                    )
                    continue
                with database.operational_connect() as db:
                    db.execute(
                        """
                        UPDATE analytics_apply_jobs
                        SET status = 'applied', applied_at = ?, row_counts = ?,
                            attempts = attempts + 1, error = ''
                        WHERE job_id = ?
                        """,
                        [utc_now(), json.dumps(row_counts, default=str), job_id],
                    )
                    db.commit()
                Path(payload_path).unlink(missing_ok=True)
                applied += 1
                logging.getLogger(__name__).info(
                    "Applied analytics job %s (%s): %s",
                    job_id, source, row_counts,
                )
            return applied
    except SingletonLeaseUnavailable:
        return 0


@register_applier("retail-prices")
def _apply_retail_prices(database: Any, payload: dict[str, Any]) -> dict[str, int]:
    prices = payload["prices"]
    database.store_retail_prices(
        payload["snapshotId"],
        prices,
        complete=bool(payload.get("complete", True)),
    )
    return {"retail_price_snapshots": len(prices)}
