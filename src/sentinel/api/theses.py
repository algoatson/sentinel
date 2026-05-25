"""Thesis-engine endpoints — list active + recently-closed, detail
with event timeline, manual close action."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import thesis as _thesis


router = APIRouter()


class CloseRequest(BaseModel):
    state: str = Field(..., pattern="^(validated|invalidated|matured|closed)$")
    reason: str = Field(..., min_length=1, max_length=400)


@router.get("/theses/active")
def list_active() -> list[dict]:
    return _thesis.list_active()


@router.get("/theses/closed")
def list_recent_closed(days: int = 30) -> list[dict]:
    return _thesis.list_recent_closed(days)


@router.get("/theses/{thesis_id}")
def get_thesis(thesis_id: int) -> dict:
    t = _thesis.get_thesis(thesis_id)
    if t is None:
        raise HTTPException(404, f"thesis #{thesis_id} not found")
    return t


@router.post("/theses/{thesis_id}/close")
def close_thesis(thesis_id: int, body: CloseRequest) -> dict:
    ok = _thesis.close_thesis(
        thesis_id, state=body.state, reason=body.reason,
    )
    if not ok:
        raise HTTPException(
            400, f"thesis #{thesis_id} could not be closed "
            f"(missing or already non-active)"
        )
    return {"ok": True, "thesis_id": thesis_id, "state": body.state}


@router.post("/theses/run-generate")
async def run_generate_now() -> dict:
    """Manual trigger for the daily generator — useful when the user
    wants to see it work without waiting for the cron."""
    await _thesis.run_generate_cycle()
    return {"ok": True}


@router.post("/theses/run-review")
async def run_review_now() -> dict:
    """Manual trigger for the review cycle."""
    await _thesis.run_review_cycle()
    return {"ok": True}
