"""Best-effort webhook notifications for autoresearch events (#106).

A single configured webhook URL receives a JSON payload on key events (e.g.
a new best score committing). The payload sets both `text` (Slack incoming
webhooks) and `content` (Discord webhooks) to the message, plus structured
fields, so one URL works for Slack, Discord, or any generic receiver.

Sending is always best-effort: a down or misconfigured webhook must never
break an experiment run, so `send_notification` swallows every error and
returns a bool instead of raising.
"""
from __future__ import annotations

from typing import Any

import requests

DEFAULT_TIMEOUT = 5


def build_payload(event: str, message: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "evo",
        "event": event,
        "text": message,      # Slack incoming-webhook message key
        "content": message,   # Discord webhook message key
    }
    if extra:
        payload["data"] = extra
    return payload


def send_notification(
    url: str,
    event: str,
    message: str,
    extra: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    """POST a notification payload. Best-effort: returns True on a 2xx
    response, False on any non-2xx or transport error. Never raises."""
    try:
        resp = requests.post(url, json=build_payload(event, message, extra), timeout=timeout)
        return 200 <= resp.status_code < 300
    except Exception:  # noqa: BLE001 -- a bad webhook must never break a run
        return False
