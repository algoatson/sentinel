"""Social-pulse endpoints — Reddit mentions, ticker-scoped or recent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import dossier as _dossier
from ..db import session_scope
from ..models import RedditMention


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


def _author(raw: str | None) -> str:
    """Bare reddit handle — RSS hands us `/u/name`, the ingester stores
    `u/name`; strip the prefix so the UI can render a single clean `u/`."""
    a = (raw or "").strip().lstrip("/")
    return a[2:] if a.lower().startswith("u/") else a


def _row(r: RedditMention) -> dict:
    return {
        "id": r.id,
        "subreddit": r.subreddit,
        "ticker": r.ticker,
        "author": _author(r.author),
        "title": r.title,
        "body_excerpt": r.body_excerpt or "",
        "score": r.score,
        "num_comments": r.num_comments,
        "ts": _aware_iso(r.created_at),
        "permalink": r.permalink,
        "sentiment": r.sentiment,
        "is_thesis": r.is_thesis,
    }


@router.get("/social")
def recent(
    hours: int = Query(48, ge=1, le=720),
    ticker: str | None = Query(None),
    limit: int = Query(40, ge=1, le=200),
) -> list[dict]:
    """Recent reddit mentions, newest first. Filter by ticker
    optionally; default is global pulse."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    with session_scope() as s:
        q = select(RedditMention).where(RedditMention.created_at >= cutoff_naive)
        if ticker:
            q = q.where(RedditMention.ticker == ticker.upper())
        rows = s.exec(
            q.order_by(RedditMention.created_at.desc()).limit(limit)
        ).all()
        return [_row(r) for r in rows]


@router.get("/social/top-tickers")
def top_tickers(hours: int = Query(48, ge=1, le=720), n: int = 10) -> list[dict]:
    """Tickers ranked by mention volume in the window. Quick "what's
    Reddit talking about" snapshot."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    from collections import defaultdict
    counts: dict[str, dict] = defaultdict(
        lambda: {"mentions": 0, "score": 0, "comments": 0, "sentiment_sum": 0.0}
    )
    with session_scope() as s:
        rows = s.exec(
            select(RedditMention).where(RedditMention.created_at >= cutoff_naive)
        ).all()
        for r in rows:
            c = counts[r.ticker]
            c["mentions"] += 1
            c["score"] += r.score
            c["comments"] += r.num_comments
            c["sentiment_sum"] += r.sentiment or 0
    out = [
        {
            "ticker": t,
            "mentions": d["mentions"],
            "score": d["score"],
            "comments": d["comments"],
            "sentiment_avg": d["sentiment_sum"] / max(1, d["mentions"]),
        }
        for t, d in counts.items()
    ]
    out.sort(key=lambda x: x["mentions"], reverse=True)
    return out[:n]


@router.get("/social/{mention_id}")
def get_one(mention_id: int) -> dict:
    """Full row for the Reddit detail drawer header."""
    with session_scope() as s:
        r = s.get(RedditMention, mention_id)
        if r is None:
            raise HTTPException(404, f"reddit mention #{mention_id} not found")
        return _row(r)


@router.get("/social/{mention_id}/dossier")
def social_dossier(mention_id: int, refresh: bool = False) -> dict:
    """Cached LLM read on the thread (signal-vs-noise). `refresh=true`
    forces regen."""
    body = _dossier.reddit_dossier(mention_id, refresh=refresh)
    meta = _dossier.reddit_analysis_meta(mention_id)
    return {"mention_id": mention_id, "body": body, "meta": meta}


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=600)


@router.post("/social/{mention_id}/ask")
def ask_about_social(mention_id: int, body: AskRequest) -> dict:
    """Follow-up chat about a Reddit thread — NOT cached."""
    answer = _dossier.ask_about_reddit(mention_id, body.question)
    return {"mention_id": mention_id, "answer": answer}
