"""Symbol detail endpoint — everything the bot knows about ONE ticker.

The frontend's `/symbol/[ticker]` page consumes this. It's a join /
gather across all the bot's surfaces — calls, news, filings, theses,
reddit, price stats — so the SPA renders one tab cleanly instead of
firing six separate queries that all need to be merged client-side."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import portfolio as _portfolio
from .. import thesis as _thesis
from ..db import session_scope
from ..models import (
    CryptoMicro,
    Filing,
    NewsItem,
    PriceContext,
    RedditMention,
    SymbolNote,
    Thesis,
    TradingCall,
    Watchlist,
)


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/symbol/{ticker}")
def profile(
    ticker: str,
    days: int = Query(90, ge=7, le=720),
) -> dict:
    """Bundle: header (last price + chg) + recent calls + recent news +
    filings + active theses + reddit mentions, all scoped to one
    ticker. `days` controls the news/filings/reddit window."""
    sym = ticker.upper().lstrip("$")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_naive = cutoff.replace(tzinfo=None)

    stats = _portfolio.ticker_stats(sym)

    with session_scope() as s:
        wl = s.exec(select(Watchlist).where(Watchlist.ticker == sym)).first()
        pc = s.get(PriceContext, sym)
        # Perp microstructure — only crypto carries it. Drives the
        # funding-squeeze detector + crypto regime; surfacing it here gives
        # the per-coin "why" (funding extreme, OI surge, book skew).
        cm = s.get(CryptoMicro, sym)

        calls = s.exec(
            select(TradingCall)
            .where(TradingCall.ticker == sym)
            .order_by(TradingCall.created_at.desc())
            .limit(40)
        ).all()

        # Multi-ticker aware: a story that mentions both NVDA + AMD
        # shows up under both symbol pages. Matches either the legacy
        # primary `ticker` column OR a substring hit in `tickers_csv`.
        news = s.exec(
            select(NewsItem)
            .where(
                (NewsItem.ticker == sym)
                | NewsItem.tickers_csv.contains(f",{sym},")
            )
            .where(NewsItem.published_at >= cutoff_naive)
            .order_by(NewsItem.published_at.desc())
            .limit(40)
        ).all()

        filings = s.exec(
            select(Filing)
            .where(Filing.ticker == sym)
            .where(Filing.filed_at >= cutoff_naive)
            .order_by(Filing.filed_at.desc())
            .limit(20)
        ).all()

        theses = s.exec(
            select(Thesis)
            .where(Thesis.ticker == sym)
            .where(Thesis.state == "active")
            .order_by(Thesis.created_at.desc())
        ).all()

        reddit = s.exec(
            select(RedditMention)
            .where(RedditMention.ticker == sym)
            .where(RedditMention.created_at >= cutoff_naive)
            .order_by(RedditMention.created_at.desc())
            .limit(20)
        ).all()

        # Quick aggregates
        n_total = len(news)
        sent_avg = (
            sum(n.sentiment or 0 for n in news) / max(1, n_total)
        ) if n_total else None
        bullish = sum(1 for n in news if (n.sentiment or 0) > 0.15)
        bearish = sum(1 for n in news if (n.sentiment or 0) < -0.15)

        reddit_score = sum(r.score for r in reddit)
        reddit_comments = sum(r.num_comments for r in reddit)
        reddit_sent = (
            sum(r.sentiment or 0 for r in reddit) / max(1, len(reddit))
        ) if reddit else None

        return {
            "ticker": sym,
            "asset_class": wl.asset_class if wl else None,
            "in_watchlist": wl is not None,
            "stats": stats,
            "context": {
                "last_price": pc.last_price if pc else None,
                "change_1d_pct": (
                    round((pc.change_1d_pct or 0) * 100, 2) if pc else None
                ),
                "change_5d_pct": (
                    round((pc.change_5d_pct or 0) * 100, 2) if pc else None
                ),
                "volume_vs_20d_avg": (
                    round(pc.volume_vs_20d_avg, 2) if pc else None
                ),
            } if pc else None,
            "micro": {
                "venue": cm.venue,
                "funding_rate_pct": (
                    round(cm.funding_rate * 100, 4)
                    if cm.funding_rate is not None else None
                ),
                "oi_change_24h_pct": (
                    round(cm.oi_change_24h_pct * 100, 2)
                    if cm.oi_change_24h_pct is not None else None
                ),
                "orderbook_imbalance": cm.orderbook_imbalance,
                "open_interest": cm.open_interest,
                "updated_at": _aware_iso(cm.updated_at),
            } if cm is not None else None,
            "calls": [
                {
                    "id": c.id,
                    "direction": c.direction,
                    "conviction": c.conviction,
                    "source": c.source,
                    "thesis": c.thesis,
                    "ts": _aware_iso(c.created_at),
                    "ret_1d_pct": c.ret_1d_pct,
                    "ret_5d_pct": c.ret_5d_pct,
                    "ret_20d_pct": c.ret_20d_pct,
                    "price_at_call": c.price_at_call,
                    "settled": c.settled,
                }
                for c in calls
            ],
            "news": [
                {
                    "id": n.id,
                    "title": n.title,
                    "url": n.url,
                    "source": n.source,
                    "summary": n.summary,
                    "ts": _aware_iso(n.published_at),
                    "impact_1d_pct": n.impact_1d_pct,
                    "sentiment": n.sentiment,
                }
                for n in news
            ],
            "news_stats": {
                "count": n_total,
                "sentiment_avg": sent_avg,
                "bullish": bullish,
                "bearish": bearish,
            },
            "filings": [
                {
                    "id": f.id,
                    "form_type": f.form_type,
                    "accession_number": f.accession_number,
                    "filed_at": _aware_iso(f.filed_at),
                    "primary_doc_url": f.primary_doc_url,
                    "summary": f.summary,
                    "materiality_score": f.materiality_score,
                    "materiality_reason": f.materiality_reason,
                }
                for f in filings
            ],
            "theses": [
                {
                    "id": t.id,
                    "direction": t.direction,
                    "title": t.title,
                    "body": t.body,
                    "invalidation_criteria": t.invalidation_criteria,
                    "conviction": t.conviction,
                    "target_price": t.target_price,
                    "horizon_days": t.horizon_days,
                    "state": t.state,
                    "created_at": _aware_iso(t.created_at),
                    "supporting_events": 0,  # quick view; full counts on /api/theses
                    "challenging_events": 0,
                }
                for t in theses
            ],
            "reddit": [
                {
                    "id": r.id,
                    "subreddit": r.subreddit,
                    "author": r.author,
                    "title": r.title,
                    "score": r.score,
                    "num_comments": r.num_comments,
                    "ts": _aware_iso(r.created_at),
                    "permalink": r.permalink,
                    "sentiment": r.sentiment,
                }
                for r in reddit
            ],
            "reddit_stats": {
                "count": len(reddit),
                "score_total": reddit_score,
                "comments_total": reddit_comments,
                "sentiment_avg": reddit_sent,
            },
        }


# ── Per-ticker symbol notes ──────────────────────────────────────────────

class SymbolNoteRequest(BaseModel):
    body: str = Field(default="", max_length=4000)


def _note_row(t: str, n: SymbolNote | None) -> dict:
    return {
        "ticker": t,
        "body": n.body if n else "",
        "updated_at": _aware_iso(n.updated_at) if n else None,
    }


@router.get("/symbol/{ticker}/note")
def get_note(ticker: str) -> dict:
    """Persistent per-ticker journal note. Empty body if no note yet."""
    sym = ticker.upper().replace("$", "")
    with session_scope() as s:
        n = s.get(SymbolNote, sym)
    return _note_row(sym, n)


@router.put("/symbol/{ticker}/note")
def put_note(ticker: str, body: SymbolNoteRequest) -> dict:
    """Upsert the note. Empty/whitespace body deletes the row so the
    "has-note" tag goes away naturally."""
    sym = ticker.upper().replace("$", "")
    if not sym:
        raise HTTPException(400, "ticker required")
    text = (body.body or "").strip()
    with session_scope() as s:
        n = s.get(SymbolNote, sym)
        if not text:
            if n is not None:
                s.delete(n)
            return {"ticker": sym, "body": "", "updated_at": None}
        if n is None:
            n = SymbolNote(ticker=sym, body=text, updated_at=datetime.now(timezone.utc))
        else:
            n.body = text
            n.updated_at = datetime.now(timezone.utc)
        s.add(n)
        # Re-read for serialisation safety.
        updated_at_iso = _aware_iso(n.updated_at)
    return {"ticker": sym, "body": text, "updated_at": updated_at_iso}
