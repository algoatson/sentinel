"""Social-pulse endpoints — Reddit mentions, ticker-scoped or recent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlmodel import select

from ..db import session_scope
from ..models import RedditMention


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


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
        return [
            {
                "id": r.id,
                "subreddit": r.subreddit,
                "ticker": r.ticker,
                "author": r.author,
                "title": r.title,
                "score": r.score,
                "num_comments": r.num_comments,
                "ts": _aware_iso(r.created_at),
                "permalink": r.permalink,
                "sentiment": r.sentiment,
            }
            for r in rows
        ]


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
