"""Verify and parse webhook deliveries (X-Promptli-Id / -Timestamp / -Signature: v1=...)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

from promptli._generated.models import EventEnvelope

DEFAULT_TOLERANCE = 300


def compute_signature(secret: str, event_id: str, timestamp: str, body: bytes) -> str:
    key = secret.removeprefix("whsec_").encode()
    return hmac.new(key, f"{event_id}.{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify_webhook(
    payload: bytes | str,
    headers: Mapping[str, str],
    secret: str | list[str],
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> bool:
    """True if the request was signed by Promptli with `secret` (or any of several during rotation).

    Pass the raw request body exactly as received (do not re-serialize JSON).
    """
    body = payload.encode() if isinstance(payload, str) else payload
    h = {k.lower(): v for k, v in headers.items()}
    event_id = h.get("x-promptli-id", "")
    ts = h.get("x-promptli-timestamp", "")
    header = h.get("x-promptli-signature", "")
    if not (event_id and ts.isdigit() and header):
        return False
    if abs((now if now is not None else time.time()) - int(ts)) > tolerance:
        return False
    provided = [p.removeprefix("v1=") for p in header.split() if p.startswith("v1=")]
    for s in [secret] if isinstance(secret, str) else secret:
        expected = compute_signature(s, event_id, ts, body)
        if any(hmac.compare_digest(expected, p) for p in provided):
            return True
    return False


def parse_event(payload: bytes | str | Mapping[str, Any]) -> EventEnvelope:
    """Parse a webhook body / Agent Link event into a typed envelope (unknown fields are kept loose)."""
    data = json.loads(payload) if isinstance(payload, (bytes, str)) else dict(payload)
    return EventEnvelope.model_validate(data)
