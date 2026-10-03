"""
Observability wiring — issue #50.

- JSON structured logging to stdout (so App Service / Log Analytics)
  with correlation fields already emitted.
- Optional Application Insights export via opencensus-ext-azure when
  APPLICATIONINSIGHTS_CONNECTION_STRING (additive appsettings) is set.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_trace_id: ContextVar[str | None] = ContextVar("_flux_trace_id", default=None)


def current_trace_id() -> str | None:
    return _trace_id.get()


def new_trace_id() -> str:
    value = uuid.uuid4().hex[:16]
    _trace_id.set(value)
    return value


def set_trace_id(value: str):
    return _trace_id.set(value)


def reset_trace_id(token) -> None:
    _trace_id.reset(token)


class FluxJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "traceId": getattr(record, "traceId", None) or current_trace_id(),
        }
        for key in ("subscriptionId", "costType", "window", "qpuEstimated", "qpuConsumed", "qpuRemaining", "retryAfter", "runId", "scopeId", "statusCode"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        # Drop None keys to keep payload small.
        return json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False)


def configure_logging() -> None:
    level_name = os.getenv("FLUX_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")).strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(FluxJsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Attach Application Insights handler if a connection string is present.
    # Additive appsettings (never GET->PUT wipe) — see docs/POSTGRES-DUCKDB-INTERIM-SCALING-PLAN.md § safety-net.
    conn = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if conn:
        try:
            from opencensus.ext.azure.log_exporter import AzureLogHandler  # type: ignore[import-not-found]

            ai_handler = AzureLogHandler(connection_string=conn)
            ai_handler.setFormatter(FluxJsonFormatter())
            root.addHandler(ai_handler)
            logging.getLogger("flux.observability").info("Application Insights export enabled")
        except Exception as exc:  # pragma: no cover — optional path
            logging.getLogger("flux.observability").warning("Application Insights handler not attached: %s", exc)


def log_with_context(logger: logging.Logger, level: int, msg: str, *args: Any, extra: dict[str, Any] | None = None, exc_info: Any = None) -> None:
    base: dict[str, Any] = {"traceId": current_trace_id()}
    if extra:
        base.update(extra)
    logger.log(level, msg, *args, extra=base, exc_info=exc_info)
