"""News endpoints — feed + dossier (cached LLM analysis) + chat about
a single news item."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import dossier as _dossier
from ..db import session_scope
from ..models import NewsItem


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/news")
def list_recent(
    hours: int = Query(24, ge=1, le=168),
    ticker: str | None = Query(None),
    limit: int = Query(60, ge=1, le=200),
) -> list[dict]:
    """Recent news, newest first. Optional ticker filter."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    with session_scope() as s:
        q = select(NewsItem).where(NewsItem.published_at >= cutoff_naive)
        if ticker:
            q = q.where(NewsItem.ticker == ticker.upper())
        rows = s.exec(
            q.order_by(NewsItem.published_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "ticker": r.ticker, "title": r.title, "url": r.url,
                "source": r.source, "summary": r.summary,
                "ts": _aware_iso(r.published_at),
                "impact_1d_pct": r.impact_1d_pct,
                "sentiment": r.sentiment,
                "is_macro": r.is_macro,
            } for r in rows
        ]


@router.get("/news/{news_id}/dossier")
def news_dossier(news_id: int, refresh: bool = False) -> dict:
    """Cached LLM dossier. `refresh=true` forces regen."""
    body = _dossier.news_dossier(news_id, refresh=refresh)
    meta = _dossier.news_analysis_meta(news_id)
    return {
        "news_id": news_id,
        "body": body,
        "meta": meta,
    }


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=600)


@router.post("/news/{news_id}/ask")
def ask_about_news(news_id: int, body: AskRequest) -> dict:
    """Follow-up chat about a news item — NOT cached."""
    answer = _dossier.ask_about_news(news_id, body.question)
    return {"news_id": news_id, "answer": answer}


@router.get("/news/{news_id}")
def get_one(news_id: int) -> dict:
    """Full row for the modal header."""
    with session_scope() as s:
        n = s.get(NewsItem, news_id)
        if n is None:
            raise HTTPException(404, f"news #{news_id} not found")
        return {
            "id": n.id, "ticker": n.ticker, "title": n.title,
            "url": n.url, "source": n.source, "summary": n.summary,
            "ts": _aware_iso(n.published_at),
            "impact_1h_pct": n.impact_1h_pct,
            "impact_1d_pct": n.impact_1d_pct,
            "sentiment": n.sentiment,
            "is_macro": n.is_macro,
        }
