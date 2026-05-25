"""In-process pub/sub for live dashboard events.

The bot's ingesters (news, filings, calls, trades, watches) call
``publish(kind, payload)`` after they insert a new row. Anyone with
an active SSE connection to ``/api/events`` receives the event in real
time — no polling delay.

Thread safety:
- Publishers run in APScheduler worker threads (sync code path).
- SSE subscribers consume in the asyncio event loop thread.
- ``asyncio.Queue`` is coroutine-safe, NOT thread-safe — so we
  capture the event loop at subscribe time and dispatch the
  ``put_nowait`` via ``loop.call_soon_threadsafe``. That's the
  documented safe path for cross-thread queue puts.

- The history deque is GIL-protected for ``append()``, so the
  ``_HISTORY`` ring (deque with maxlen) is fine to touch from any
  thread.
- ``_NEXT_ID`` is bumped under a lock to avoid duplicate ids on
  simultaneous publishes from different threads.

This module never raises out of the ingest path. Failures are logged
at debug.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any

from loguru import logger


# Newest-last bounded history. Capped so a busy day doesn't balloon memory.
_HISTORY: deque[dict] = deque(maxlen=200)
# (queue, loop) tuples — loop is captured at subscribe time so we can
# dispatch puts cross-thread via call_soon_threadsafe.
_SUBSCRIBERS: list[tuple[asyncio.Queue[dict], asyncio.AbstractEventLoop]] = []
_LOCK = threading.Lock()

# Monotonic event id for client-side `Last-Event-ID` reconnection.
_NEXT_ID = 1


def _now_iso() -> str:
    """ISO-8601 UTC timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def publish(kind: str, payload: dict[str, Any]) -> None:
    """Broadcast an event to every SSE subscriber.

    Thread-safe: scheduler workers + sync ingesters can call this
    without touching the asyncio loop directly. Failures are
    swallowed — events are best-effort, never load-bearing for
    ingest.
    """
    global _NEXT_ID
    try:
        with _LOCK:
            ev_id = _NEXT_ID
            _NEXT_ID += 1
        ev = {
            "id": ev_id,
            "kind": kind,
            "payload": payload,
            "ts": _now_iso(),
        }
        _HISTORY.append(ev)
        for q, loop in list(_SUBSCRIBERS):
            try:
                # call_soon_threadsafe is documented as safe to invoke
                # from any thread; it schedules the put_nowait on the
                # loop's own thread, dodging asyncio.Queue's lack of
                # cross-thread safety.
                loop.call_soon_threadsafe(_dispatch, q, ev)
            except RuntimeError:
                # Loop closed (server shutting down) — skip silently.
                pass
    except Exception as e:
        logger.debug("events.publish({}) failed: {}", kind, e)


def _dispatch(q: asyncio.Queue[dict], ev: dict) -> None:
    """Loop-side helper: drops events when a slow consumer's queue is
    full instead of blocking the publisher."""
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:
        pass


def history(since_id: int | None = None) -> list[dict]:
    """Recent events, oldest-first. Optionally filter to those after
    `since_id` for resuming a disconnected stream."""
    items = list(_HISTORY)
    if since_id is not None:
        items = [e for e in items if e["id"] > since_id]
    return items


def subscribe() -> asyncio.Queue[dict]:
    """Register a new SSE consumer and return its queue. Must be
    called from inside the asyncio event loop (which `/api/events`
    is)."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    _SUBSCRIBERS.append((q, loop))
    return q


def unsubscribe(q: asyncio.Queue[dict]) -> None:
    for i, (qq, _loop) in enumerate(_SUBSCRIBERS):
        if qq is q:
            _SUBSCRIBERS.pop(i)
            return


def subscriber_count() -> int:
    return len(_SUBSCRIBERS)
