"""Watch endpoints — list, add (LLM-compiled), remove."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from ..db import session_scope
from ..models import Watch
from ..pipelines import watches as _watches


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/watches")
def list_watches() -> list[dict]:
    rows = _watches.list_watches()
    return [
        {
            "id": r["id"],
            "raw_text": r["raw_text"],
            "active": r["active"],
            "trigger_count": r["trigger_count"],
            "last_triggered_at": _aware_iso(r["last_triggered_at"]),
            "created_at": _aware_iso(r["created_at"]),
        }
        for r in rows
    ]


@router.get("/watches/{wid}")
def get_watch(wid: int) -> dict:
    """Single watch row including the compiled `spec` so the UI can
    show "this is what your plain-English request was turned into"."""
    with session_scope() as s:
        w = s.get(Watch, wid)
        if w is None:
            raise HTTPException(404, f"watch #{wid} not found")
        try:
            spec = json.loads(w.condition_json) if w.condition_json else {}
        except json.JSONDecodeError:
            spec = {"_raw": w.condition_json}
        return {
            "id": w.id,
            "raw_text": w.raw_text,
            "spec": spec,
            "active": w.active,
            "trigger_count": w.trigger_count,
            "last_triggered_at": _aware_iso(w.last_triggered_at),
            "created_at": _aware_iso(w.created_at),
        }


class AddRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@router.post("/watches")
async def add_watch(body: AddRequest) -> dict:
    """Add a watch from plain English. The LLM compiles it to a
    structured spec. Returns the bot's status message verbatim — the
    UI surfaces it via toast/notification."""
    msg = await _watches.add_watch(body.text)
    ok = msg.startswith("🔔")
    return {"ok": ok, "message": msg}


@router.delete("/watches/{wid}")
def remove_watch(wid: int) -> dict:
    res = _watches.remove_watch(wid)
    if not res["ok"]:
        raise HTTPException(404, res["message"])
    return res
