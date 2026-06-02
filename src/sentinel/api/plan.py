"""Daily-plan scratchpad.

One free-form text body per UTC date — the trader's morning intent.
A new day silently starts a fresh row; saving a past date is still
allowed (handy if you backfill while reviewing). Empty body deletes
the row so a missed day shows as "no plan" instead of an empty
ghost row.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import session_scope
from ..models import Briefing, DailyPlan, GamePlan
from ..pipelines.game_plan import et_date_str


router = APIRouter()


class PlanRequest(BaseModel):
    body: str = Field(default="", max_length=4000)


def _serialize(p: DailyPlan | None, d: date) -> dict:
    return {
        "plan_date": d.isoformat(),
        "body": p.body if p else "",
        "updated_at": (
            (p.updated_at if p.updated_at.tzinfo
             else p.updated_at.replace(tzinfo=timezone.utc)).isoformat()
            if p else None
        ),
    }


@router.get("/plan/today")
def get_today() -> dict:
    """Today's plan (UTC). Empty body if untouched."""
    today = datetime.now(timezone.utc).date()
    with session_scope() as s:
        p = s.get(DailyPlan, today)
    return _serialize(p, today)


# ── Morning Game Plan (pipelines/game_plan.py) ───────────────────────────────
# Registered before the /plan/{ymd} catch-all is irrelevant (two path segments
# vs one), but kept here for locality. Returns the structured, ranked plan.


def _serialize_gameplan(g: GamePlan | None, plan_date: str) -> dict:
    if g is None:
        return {
            "plan_date": plan_date, "generated_at": None,
            "the_read": "", "sections": [], "model": "", "exists": False,
        }
    try:
        sections = json.loads(g.sections_json or "[]")
    except Exception:
        sections = []
    gen = g.generated_at
    return {
        "plan_date": g.plan_date,
        "generated_at": (
            (gen if gen.tzinfo else gen.replace(tzinfo=timezone.utc)).isoformat()
            if gen else None
        ),
        "the_read": g.the_read or "",
        "sections": sections,
        "model": g.model or "",
        "exists": True,
    }


@router.get("/plan/gameplan/today")
def get_gameplan_today() -> dict:
    """Today's Morning Game Plan (ET date). `exists=false` until it's generated."""
    pd = et_date_str()
    with session_scope() as s:
        g = s.get(GamePlan, pd)
    return _serialize_gameplan(g, pd)


@router.get("/plan/gameplan/{ymd}")
def get_gameplan_for(ymd: str) -> dict:
    """Game Plan for an arbitrary YYYY-MM-DD (ET) date."""
    try:
        date.fromisoformat(ymd)
    except ValueError:
        raise HTTPException(400, f"invalid date {ymd!r} — use YYYY-MM-DD")
    with session_scope() as s:
        g = s.get(GamePlan, ymd)
    return _serialize_gameplan(g, ymd)


@router.get("/plan/{ymd}")
def get_for(ymd: str) -> dict:
    """Plan for an arbitrary YYYY-MM-DD date."""
    try:
        d = date.fromisoformat(ymd)
    except ValueError:
        raise HTTPException(400, f"invalid date {ymd!r} — use YYYY-MM-DD")
    with session_scope() as s:
        p = s.get(DailyPlan, d)
    return _serialize(p, d)


@router.put("/plan/today")
def put_today(body: PlanRequest) -> dict:
    """Upsert today's plan. Empty body deletes the row."""
    today = datetime.now(timezone.utc).date()
    return _upsert(today, body.body)


@router.put("/plan/{ymd}")
def put_for(ymd: str, body: PlanRequest) -> dict:
    try:
        d = date.fromisoformat(ymd)
    except ValueError:
        raise HTTPException(400, f"invalid date {ymd!r} — use YYYY-MM-DD")
    return _upsert(d, body.body)


def _upsert(d: date, body_text: str) -> dict:
    text = (body_text or "").strip()
    with session_scope() as s:
        p = s.get(DailyPlan, d)
        if not text:
            if p is not None:
                s.delete(p)
            return {"plan_date": d.isoformat(), "body": "", "updated_at": None}
        now = datetime.now(timezone.utc)
        if p is None:
            p = DailyPlan(plan_date=d, body=text, updated_at=now)
        else:
            p.body = text
            p.updated_at = now
        s.add(p)
        # Capture serialised form before commit drops naive attrs.
        updated_iso = (
            (p.updated_at if p.updated_at.tzinfo
             else p.updated_at.replace(tzinfo=timezone.utc)).isoformat()
        )
    return {"plan_date": d.isoformat(), "body": text, "updated_at": updated_iso}


@router.get("/briefing/today")
def briefing_today() -> dict:
    """Most-recent pre-market briefing body (today's, or yesterday's
    if today's run hasn't happened yet — useful when the dashboard
    loads pre-08:30 ET or on a weekend). Empty body means the
    pipeline hasn't produced one yet."""
    from sqlmodel import select as _select
    with session_scope() as s:
        # Pick the latest row regardless of date — the dashboard
        # surfacing should show the most recent briefing the user
        # cares about (yesterday's on a weekend, today's on a
        # market morning).
        row = s.exec(
            _select(Briefing).order_by(Briefing.brief_date.desc()).limit(1)
        ).first()
        if row is None:
            return {
                "brief_date": None,
                "body": "",
                "importance": None,
                "importance_reason": None,
                "generated_at": None,
            }
        return {
            "brief_date": row.brief_date.isoformat(),
            "body": row.body,
            "importance": row.importance,
            "importance_reason": row.importance_reason,
            "generated_at": (
                row.generated_at if row.generated_at.tzinfo
                else row.generated_at.replace(tzinfo=timezone.utc)
            ).isoformat(),
        }
