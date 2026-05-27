"""Tickers with multi-source signal stacking right now.

The convergence pipeline acts on this internally (filing + price +
social + news in the same window). Surfacing the same view on the
dashboard lets the user see what the bot is about to act on, not
just what it has already acted on.

Pure DB — no LLM. Counts distinct *sources* per ticker over a recent
window, returns the densest stacks first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import Filing, NewsItem, RedditMention, TradingCall


def converging_now(hours: int = 6, limit: int = 8) -> dict:
    """Top stacking tickers in the last `hours`. Each row carries the
    set of source types we saw the ticker in, plus a representative
    timestamp.

    Source taxonomy (light, intentionally simple — same buckets the
    `convergence` pipeline uses):
      * filing   — any Filing in window
      * news     — any NewsItem in window (per-ticker, not macro)
      * social   — any RedditMention in window
      * call     — any unsettled TradingCall in window (the bot has
                   already emitted a directional read)

    The 1-source case is filtered out — that's not convergence.
    """
    hours = max(1, min(int(hours), 48))
    limit = max(1, min(int(limit), 30))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_naive = cutoff.replace(tzinfo=None)

    by_ticker: dict[str, dict] = {}

    def _ensure(ticker: str) -> dict:
        return by_ticker.setdefault(
            ticker,
            {
                "ticker": ticker,
                "sources": set(),
                "last_ts": None,
                "filings": 0,
                "news": 0,
                "social": 0,
                "calls": 0,
            },
        )

    def _bump(ticker: str, source: str, ts) -> None:
        if not ticker:
            return
        r = _ensure(ticker.upper())
        r["sources"].add(source)
        r[source + "s" if not source.endswith("s") else source] = (
            r.get(source + "s" if not source.endswith("s") else source, 0) + 1
        )
        if ts is not None and (r["last_ts"] is None or ts > r["last_ts"]):
            r["last_ts"] = ts

    with session_scope() as s:
        for f in s.exec(
            select(Filing).where(Filing.filed_at >= cutoff_naive)
        ).all():
            _bump(f.ticker, "filing", f.filed_at)
        for n in s.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff_naive)
            .where(NewsItem.is_macro == False)  # noqa: E712
        ).all():
            _bump(n.ticker, "news", n.published_at)
        for r in s.exec(
            select(RedditMention).where(RedditMention.created_at >= cutoff_naive)
        ).all():
            _bump(r.ticker, "social", r.created_at)
        for c in s.exec(
            select(TradingCall).where(TradingCall.created_at >= cutoff_naive)
        ).all():
            _bump(c.ticker, "call", c.created_at)

    rows = []
    for r in by_ticker.values():
        if len(r["sources"]) < 2:
            continue  # 1-source isn't convergence
        last = r["last_ts"]
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        rows.append({
            "ticker": r["ticker"],
            "sources": sorted(r["sources"]),
            "source_count": len(r["sources"]),
            "filings": r.get("filings", 0),
            "news": r.get("news", 0),
            "social": r.get("social", 0),
            "calls": r.get("calls", 0),
            "last_ts": last.isoformat() if last else None,
        })
    rows.sort(
        key=lambda r: (-r["source_count"], -r["filings"] - r["news"] - r["calls"]),
    )
    return {
        "window_hours": hours,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "rows": rows[:limit],
    }
