"""Call endpoints — recent calls list + dossier (cached) + chat."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import dossier as _dossier
from .. import scorecard
from ..db import session_scope
from ..models import TradingCall


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/calls")
def list_recent(days: int = 7, limit: int = 60) -> list[dict]:
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).replace(tzinfo=None)
    with session_scope() as s:
        rows = s.exec(
            select(TradingCall)
            .where(TradingCall.created_at >= cutoff_naive)
            .order_by(TradingCall.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id, "ticker": r.ticker, "direction": r.direction,
                "conviction": r.conviction, "source": r.source,
                "thesis": r.thesis,
                "ts": _aware_iso(r.created_at),
                "ret_1d_pct": r.ret_1d_pct,
                "ret_5d_pct": r.ret_5d_pct,
                "ret_20d_pct": r.ret_20d_pct,
                "price_at_call": r.price_at_call,
                "settled": r.settled,
            } for r in rows
        ]


@router.get("/calls/{call_id}/dossier")
def call_dossier(call_id: int, refresh: bool = False) -> dict:
    body = _dossier.call_dossier(call_id, refresh=refresh)
    meta = _dossier.call_summary_meta(call_id)
    return {"call_id": call_id, "body": body, "meta": meta}


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=600)


@router.post("/calls/{call_id}/ask")
def ask_about_call(call_id: int, body: AskRequest) -> dict:
    answer = _dossier.ask_about_call(call_id, body.question)
    return {"call_id": call_id, "answer": answer}


@router.get("/calls/{call_id}")
def get_one(call_id: int) -> dict:
    with session_scope() as s:
        c = s.get(TradingCall, call_id)
        if c is None:
            raise HTTPException(404, f"call #{call_id} not found")
        return {
            "id": c.id, "ticker": c.ticker, "direction": c.direction,
            "conviction": c.conviction, "source": c.source,
            "thesis": c.thesis,
            "ts": _aware_iso(c.created_at),
            "ret_1d_pct": c.ret_1d_pct,
            "ret_5d_pct": c.ret_5d_pct,
            "ret_20d_pct": c.ret_20d_pct,
            "price_at_call": c.price_at_call,
            "settled": c.settled,
        }


@router.get("/scorecard")
def scorecard_overall() -> dict:
    return scorecard.track_record_summary()
