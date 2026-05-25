"""Server-Sent-Events endpoint — live stream of bot events."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .. import events as _events


router = APIRouter()


@router.get("/events/recent")
def recent(since_id: int | None = None) -> dict:
    """Catch-up snapshot of recent events. The SPA calls this on
    initial load to populate the notifications inbox without waiting
    for the next live tick."""
    return {"events": _events.history(since_id)}


async def _stream(request: Request, last_event_id: int | None):
    """Yield SSE-formatted events forever, with keep-alives every 25s
    so proxies don't drop idle connections. Cleans up on disconnect."""
    queue = _events.subscribe()
    try:
        # Replay missed events first (since the user's Last-Event-ID).
        if last_event_id is not None:
            for ev in _events.history(last_event_id):
                yield _sse_event(ev)
        # Then live.
        while True:
            if await request.is_disconnected():
                break
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                # Comment line as keep-alive (SSE clients ignore lines
                # starting with ':' but they still nudge the proxy).
                yield ": keepalive\n\n"
                continue
            yield _sse_event(ev)
    finally:
        _events.unsubscribe(queue)


def _sse_event(ev: dict) -> str:
    """Format one event in SSE wire format."""
    return (
        f"id: {ev['id']}\n"
        f"event: {ev['kind']}\n"
        f"data: {json.dumps(ev)}\n\n"
    )


@router.get("/events")
async def stream(request: Request) -> StreamingResponse:
    """Long-lived text/event-stream. Each line is a JSON-encoded
    event. The browser's EventSource handles reconnection + the
    Last-Event-ID header automatically."""
    raw = request.headers.get("last-event-id")
    last_id: int | None = None
    if raw:
        try:
            last_id = int(raw)
        except ValueError:
            pass
    return StreamingResponse(
        _stream(request, last_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "keep-alive",
        },
    )
