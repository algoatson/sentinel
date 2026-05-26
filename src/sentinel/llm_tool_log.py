"""In-memory telemetry ring for LLM tool calls.

The autonomous pipelines occasionally drive the LLM through
``llm_tools.tool_loop`` which lets the model call read-only Python
functions to fetch extra context. We want a lightweight way to *see*
which calls the model has been making — both to debug a confused
dossier ("why did it think ZEST was a sector move?") and to spot a
runaway loop early.

Keeping this purely in-memory and bounded:
  * No DB writes — tool calls fire several times per cycle on a busy
    day; a write per call would balloon the SQLite WAL.
  * Bounded deque (default 500 entries) so memory is capped on a busy
    day.
  * Thread-safe — the publishers are async tasks in a worker thread.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable


_MAX = 500
_RING: deque[dict] = deque(maxlen=_MAX)
_NEXT_ID = 1
_LOCK = threading.Lock()


def _summarise(value: Any, max_chars: int = 240) -> str:
    """Compact representation of a tool result. We want the panel to
    show enough to know what came back without dragging a 4-kB chart
    payload through the inbox UI."""
    import json
    try:
        s = json.dumps(value, default=str)
    except (TypeError, ValueError):
        s = str(value)
    if len(s) > max_chars:
        return s[: max_chars - 1] + "…"
    return s


def record(
    *,
    pipeline: str,
    tool: str,
    arguments: dict | str | None,
    result: Any,
    ticker: str | None = None,
    iteration: int | None = None,
    took_ms: float | None = None,
) -> None:
    """Append one entry. Never raises — telemetry is best-effort and
    must never be load-bearing for ingest."""
    global _NEXT_ID
    try:
        with _LOCK:
            ev_id = _NEXT_ID
            _NEXT_ID += 1
        # Detect errors so the UI can colour them. The dispatcher
        # returns {"error": "..."} on failure.
        is_err = (
            isinstance(result, dict) and "error" in result
            and "ok" not in result
        )
        entry = {
            "id": ev_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "pipeline": pipeline,
            "tool": tool,
            "ticker": ticker,
            "iteration": iteration,
            "arguments": arguments if isinstance(arguments, dict) else (
                {} if not arguments else {"_raw": str(arguments)[:200]}
            ),
            "result_summary": _summarise(result),
            "ok": not is_err,
            "took_ms": (
                round(took_ms, 1) if took_ms is not None else None
            ),
        }
        _RING.append(entry)
    except Exception:
        # Telemetry path must never raise out to the caller.
        pass


def recent(limit: int = 60, since_id: int | None = None) -> list[dict]:
    """Newest-first slice of the ring. ``since_id`` lets the UI poll
    incrementally without re-fetching the whole window."""
    out: list[dict] = []
    # Snapshot the deque under the lock; iteration over a deque is
    # not thread-safe while another thread is appending.
    with _LOCK:
        snap: Iterable[dict] = list(_RING)
    for ev in reversed(snap):
        if since_id is not None and ev["id"] <= since_id:
            break
        out.append(ev)
        if len(out) >= limit:
            break
    return out


def stats() -> dict:
    """Quick aggregate over the buffered window."""
    with _LOCK:
        snap = list(_RING)
    if not snap:
        return {"count": 0, "errors": 0, "by_tool": {}, "by_pipeline": {}}
    errors = sum(1 for e in snap if not e["ok"])
    by_tool: dict[str, int] = {}
    by_pipeline: dict[str, int] = {}
    for e in snap:
        by_tool[e["tool"]] = by_tool.get(e["tool"], 0) + 1
        by_pipeline[e["pipeline"]] = by_pipeline.get(e["pipeline"], 0) + 1
    return {
        "count": len(snap),
        "errors": errors,
        "by_tool": by_tool,
        "by_pipeline": by_pipeline,
    }
