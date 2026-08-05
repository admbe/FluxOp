"""Outbound notification of pipeline warnings.

Detection without notification proved insufficient four times in one week:
pipeline_status().warnings caught real failures (orphaned locks, unclaimed
queues, stale snapshots) that still sat unseen for hours-to-days because
they only render on the Administration page. This module pushes NEW
warnings to a webhook (Microsoft Teams / Slack compatible: a JSON POST
with a "text" field) and re-notifies persistent ones on a slow cadence so
a long-lived warning neither spams nor silently ages out.

The selection logic is pure and stateless (state in, decisions out) so it
is testable without an operational store or network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import urllib.request


@dataclass(frozen=True)
class WarningState:
    key: str
    first_seen: datetime
    last_notified: datetime


def warning_key(text: str) -> str:
    """Stable identity for a warning across runs.

    Volatile figures (ages, counts) change between polls while the
    underlying condition persists; hashing digits away keeps one identity
    per condition so re-notification cadence works.
    """
    stable = "".join("#" if ch.isdigit() else ch for ch in text)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


def select_notifications(
    warnings: list[str],
    known: dict[str, WarningState],
    now: datetime,
    renotify_hours: float = 24,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Decide what to send and which states remain active.

    Returns (to_send, active_keys) where to_send is (key, message) pairs. A
    warning is sent when it is new or when it was last notified more than
    renotify_hours ago; every active warning's key is returned so state
    rows can track currently-active conditions.
    """
    to_send: list[tuple[str, str]] = []
    active: list[str] = []
    threshold = now - timedelta(hours=renotify_hours)
    for text in warnings:
        key = warning_key(text)
        active.append(key)
        state = known.get(key)
        if state is None:
            to_send.append((key, text))
        elif state.last_notified < threshold:
            to_send.append(
                (
                    key,
                    f"Still active since {state.first_seen:%Y-%m-%d %H:%M} "
                    f"UTC: {text}",
                )
            )
    return to_send, active


def post_webhook(url: str, texts: list[str], *, timeout: float = 15) -> None:
    """POST the warnings as one message to a Teams/Slack-style webhook."""
    if not texts:
        return
    lines = "\n\n".join(f"⚠️ {text}" for text in texts)
    body = json.dumps(
        {"text": f"**FluxFinOps pipeline warnings**\n\n{lines}"}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()
