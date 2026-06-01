"""News endpoints — feed + dossier (cached LLM analysis) + chat about
a single news item."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import dossier as _dossier
from ..analytics import dedupe as _dedupe
from ..db import session_scope
from ..models import ArticleBody, NewsItem
from ..utils import is_routine_payout_headline, parse_tickers_csv


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
    dedupe: bool = Query(
        False,
        description="When true, drop non-canonical members of a cluster "
                    "from the response. Each surviving row carries "
                    "`cluster_size` so the UI can render +N dupes."
    ),
) -> list[dict]:
    """Recent news, newest first. Optional ticker filter. Carries
    cluster info so the UI can show "+N dupes" badges on syndicated
    stories.

    Ticker filter is multi-aware: matches if the requested symbol is
    in `tickers_csv` (LIKE substring) OR equals the legacy single
    `ticker` column. That way a "$NVDA and $AMD" story shows up under
    both AMD and NVDA filters."""
    # Window on EITHER clock. yfinance/RSS routinely hand us articles whose
    # original `published_at` is days old (re-syndication, per-ticker feed
    # backfill), yet we ingest them now — and the Overview live feed fires on
    # INGESTION, so they flash by there. Keying this list on `published_at`
    # alone (the old behaviour) silently dropped the freshly-arrived-but-old
    # items, so news the user just saw in the live feed never appeared under
    # /intel. Union the two clocks: anything recently PUBLISHED *or* recently
    # FETCHED. Still ordered by publish date below, so displayed timestamps
    # stay monotonic and genuinely-fresh stories lead.
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    with session_scope() as s:
        q = select(NewsItem).where(
            (NewsItem.published_at >= cutoff_naive)
            | (NewsItem.fetched_at >= cutoff_naive)
        )
        if ticker:
            sym = ticker.upper()
            q = q.where(
                (NewsItem.ticker == sym)
                | NewsItem.tickers_csv.contains(f",{sym},")
            )
        rows = s.exec(
            q.order_by(NewsItem.published_at.desc()).limit(limit * 2)
        ).all()
    # Drop routine fund/ETF payout boilerplate ("… declares monthly
    # distribution of $0.36") — zero trading signal, ~25% of the raw feed.
    # Over-fetched above so the feed still fills to `limit` after the cut.
    # (The ingester now skips these too; this also clears the backlog.)
    rows = [r for r in rows if not is_routine_payout_headline(r.title)][:limit]

    # Cluster overlay (uses the same `hours` window so we don't miss
    # the canonical when it's older but the dup is fresh).
    clusters = _dedupe.for_news_ids(
        [r.id for r in rows], hours=max(hours, 48)
    )

    out = []
    for r in rows:
        info = clusters.get(r.id)
        cluster_size = info["size"] if info else 1
        is_canonical = info["is_canonical"] if info else True
        if dedupe and not is_canonical:
            continue
        out.append({
            "id": r.id,
            "ticker": r.ticker, "title": r.title, "url": r.url,
            "source": r.source, "summary": r.summary,
            "ts": _aware_iso(r.published_at),
            "impact_1d_pct": r.impact_1d_pct,
            "sentiment": r.sentiment,
            "is_macro": r.is_macro,
            "tickers": parse_tickers_csv(r.tickers_csv) or (
                [r.ticker] if r.ticker else []
            ),
            "cluster_size": cluster_size,
            "is_canonical": is_canonical,
            "sibling_ids": info["sibling_ids"] if info else [],
        })
    return out


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
            "tickers": parse_tickers_csv(n.tickers_csv) or (
                [n.ticker] if n.ticker else []
            ),
        }


@router.get("/news/{news_id}/article")
def article_body(news_id: int) -> dict:
    """Cached extracted article body, if we have one. Doesn't trigger
    a new fetch — that already happened when the dossier was first
    composed. Returns `body: null` if no body is on file (article was
    behind a paywall or extraction failed)."""
    with session_scope() as s:
        n = s.get(NewsItem, news_id)
        if n is None:
            raise HTTPException(404, f"news #{news_id} not found")
        row = s.get(ArticleBody, n.url)
        if row is None or row.source == "stub":
            return {
                "news_id": news_id,
                "url": n.url,
                "body": None,
                "source": row.source if row else None,
                "char_count": 0,
                "fetched_at": _aware_iso(row.fetched_at) if row else None,
            }
        return {
            "news_id": news_id,
            "url": n.url,
            "body": row.body,
            "source": row.source,
            "char_count": row.char_count,
            "fetched_at": _aware_iso(row.fetched_at),
        }
