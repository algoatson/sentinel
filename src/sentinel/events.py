"""In-process pub/sub for live dashboard events.

The bot's ingesters (news, filings, calls, trades, watches) call
``publish(kind, payload)`` after they insert a new row. Anyone with
an active SSE connection to ``/api/events`` receives the event in real
time — no polling delay.

Design notes:
- Single asyncio loop, so a list of ``asyncio.Queue`` works. No locks
  needed.
- Slow consumers get dropped silently rather than backpressuring the
  bot. A dashboard tab that's been idle behind a paused browser tab
  will simply miss events — when it focuses it re-queries via TanStack
  and catches up.
- A small history ring (last 200) lets a new subscriber catch up on
  what they "just missed" — useful when the dashboard reconnects.

This module never raises out of the ingest path. Failures are logged
at debug.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from loguru import logger


# Newest-last bounded history. Capped so a busy day doesn't balloon memory.
_HISTORY: deque[dict] = deque(maxlen=200)
_SUBSCRIBERS: list[asyncio.Queue[dict]] = []

# Monotonic event id for client-side `Last-Event-ID` reconnection.
_NEXT_ID = 1


def _now_iso() -> str:
    """ISO-8601 UTC timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def publish(kind: str, payload: dict[str, Any]) -> None:
    """Broadcast an event to every SSE subscriber.

    Safe to call from any async context. Failures are swallowed —
    events are best-effort, never load-bearing for ingest.
    """
    global _NEXT_ID
    try:
        ev = {
            "id": _NEXT_ID,
            "kind": kind,
            "payload": payload,
            "ts": _now_iso(),
        }
        _NEXT_ID += 1
        _HISTORY.append(ev)
        for q in list(_SUBSCRIBERS):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # Slow consumer; the reconnect path picks up via history.
                pass
    except Exception as e:
        logger.debug("events.publish({}) failed: {}", kind, e)


def history(since_id: int | None = None) -> list[dict]:
    """Recent events, oldest-first. Optionally filter to those after
    `since_id` for resuming a disconnected stream."""
    items = list(_HISTORY)
    if since_id is not None:
        items = [e for e in items if e["id"] > since_id]
    return items


def subscribe() -> asyncio.Queue[dict]:
    """Register a new SSE consumer and return its queue."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
    _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q: asyncio.Queue[dict]) -> None:
    try:
        _SUBSCRIBERS.remove(q)
    except ValueError:
        pass


def subscriber_count() -> int:
    return len(_SUBSCRIBERS)
