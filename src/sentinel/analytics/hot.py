"""Hot-tickers composite signal.

A ticker is "hot" right now if multiple independent streams are
firing on it at the same time — that's the moment that deserves
human attention, not yet-another single-stream noise event.

Score components (each clamped 0..1, weighted, summed; max raw 100):

  news_count      — how many news items on the ticker in window
  news_sentiment  — abs of average sentiment magnitude (bigger = louder)
  reddit_volume   — reddit mentions × log(score)
  filings_signal  — material filings (mat ≥ 4) × magnitude
  call_strength   — most-recent call's conviction
  price_move      — abs 1d % move
  social_spread   — how many distinct subreddits / news sources

Scoring is deliberately heuristic — no ML — so the ranking is
transparent ("why is NVDA #1? 8 news, 3 high-mat 8-Ks, +6% 1d").
Each surfaced row carries its components so the UI can display the
reasoning.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import select

from ..db import session_scope
from ..models import (
    Filing,
    NewsItem,
    PriceContext,
    RedditMention,
    TradingCall,
    Watchlist,
)


_WEIGHTS = {
    "news_count":     0.18,
    "news_sentiment": 0.10,
    "reddit_volume":  0.18,
    "filings_signal": 0.18,
    "call_strength":  0.12,
    "price_move":     0.14,
    "social_spread":  0.10,
}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def hot_tickers(hours: int = 24, limit: int = 12) -> list[dict[str, Any]]:
    """Top-N hot tickers in the last `hours`. One DB pass per stream;
    O(rows) aggregation in Python."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)

    by_ticker: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "news_count": 0,
        "sentiment_sum": 0.0,
        "sentiment_n": 0,
        "reddit_count": 0,
        "reddit_score_sum": 0,
        "filings_material": 0,
        "filings_max_mat": 0,
        "best_call_conv": 0,
        "best_call_direction": None,
        "news_sources": set(),
        "reddit_subs": set(),
    })

    with session_scope() as s:
        from ..utils import parse_tickers_csv
        # Multi-ticker aware: a single item with [NVDA, AMD] bumps both.
        # Falls back to the legacy primary `ticker` for rows ingested
        # before the tickers_csv migration.
        for n in s.exec(
            select(NewsItem).where(NewsItem.published_at >= cutoff_naive)
        ).all():
            tickers = parse_tickers_csv(n.tickers_csv) or (
                [n.ticker] if n.ticker else []
            )
            if not tickers:
                continue
            for t in tickers:
                d = by_ticker[t]
                d["news_count"] += 1
                if n.sentiment is not None:
                    d["sentiment_sum"] += n.sentiment
                    d["sentiment_n"] += 1
                if n.source:
                    d["news_sources"].add(n.source)

        for r in s.exec(
            select(RedditMention).where(RedditMention.created_at >= cutoff_naive)
        ).all():
            d = by_ticker[r.ticker]
            d["reddit_count"] += 1
            d["reddit_score_sum"] += r.score
            d["reddit_subs"].add(r.subreddit)

        for f in s.exec(
            select(Filing)
            .where(Filing.filed_at >= cutoff_naive)
            .where(Filing.ticker.is_not(None))
        ).all():
            if not f.ticker:
                continue
            # Filings don't have a multi-ticker column; SEC docs are
            # filed by one CIK + one ticker mapping per record. Keep
            # the single-ticker path here.
            d = by_ticker[f.ticker]
            mat = f.materiality_score or 0
            if mat >= 4:
                d["filings_material"] += 1
            if mat > d["filings_max_mat"]:
                d["filings_max_mat"] = mat

        for c in s.exec(
            select(TradingCall)
            .where(TradingCall.created_at >= cutoff_naive)
        ).all():
            d = by_ticker[c.ticker]
            if c.conviction > d["best_call_conv"]:
                d["best_call_conv"] = c.conviction
                d["best_call_direction"] = c.direction

        # Price-context join (one pass, all tickers seen so far).
        tickers = list(by_ticker.keys())
        prices = {}
        if tickers:
            prices = {
                pc.ticker: pc
                for pc in s.exec(
                    select(PriceContext).where(PriceContext.ticker.in_(tickers))
                ).all()
            }
        watchlist_set = {
            w.ticker
            for w in s.exec(select(Watchlist).where(Watchlist.ticker.in_(tickers))).all()
        }

    rows: list[dict[str, Any]] = []
    for ticker, d in by_ticker.items():
        sent_avg = d["sentiment_sum"] / max(1, d["sentiment_n"]) if d["sentiment_n"] else 0.0
        pc = prices.get(ticker)
        price_move_pct = (
            abs((pc.change_1d_pct or 0) * 100) if pc else 0.0
        )

        # Per-component 0..1 scores using soft caps tuned to the bot's
        # typical scale (most tickers see <5 news, <8 reddit in a day).
        s_news     = _clamp01(d["news_count"] / 8.0)
        s_sent     = _clamp01(abs(sent_avg) * 2)
        s_reddit   = _clamp01(
            (d["reddit_count"] * (math.log10(1 + max(0, d["reddit_score_sum"])) / 3))
            / 6.0
        )
        s_filings  = _clamp01(d["filings_material"] / 2.0) * (
            d["filings_max_mat"] / 10.0 if d["filings_max_mat"] else 0.0
        )
        s_call     = _clamp01(d["best_call_conv"] / 5.0)
        s_price    = _clamp01(price_move_pct / 8.0)
        s_spread   = _clamp01(
            (len(d["news_sources"]) + len(d["reddit_subs"])) / 5.0
        )

        score = 100 * (
            _WEIGHTS["news_count"]     * s_news +
            _WEIGHTS["news_sentiment"] * s_sent +
            _WEIGHTS["reddit_volume"]  * s_reddit +
            _WEIGHTS["filings_signal"] * s_filings +
            _WEIGHTS["call_strength"]  * s_call +
            _WEIGHTS["price_move"]     * s_price +
            _WEIGHTS["social_spread"]  * s_spread
        )

        if score < 1.5:
            continue  # below noise floor

        rows.append({
            "ticker": ticker,
            "score": round(score, 1),
            "in_watchlist": ticker in watchlist_set,
            "news_count": d["news_count"],
            "news_sentiment_avg": round(sent_avg, 3) if d["sentiment_n"] else None,
            "news_sources": sorted(d["news_sources"]),
            "reddit_count": d["reddit_count"],
            "reddit_score": d["reddit_score_sum"],
            "reddit_subs": sorted(d["reddit_subs"]),
            "filings_material": d["filings_material"],
            "filings_max_mat": d["filings_max_mat"],
            "best_call_conv": d["best_call_conv"] or None,
            "best_call_direction": d["best_call_direction"],
            "price_move_pct": round(price_move_pct, 2) if pc else None,
            "components": {
                "news":     round(s_news, 3),
                "sent":     round(s_sent, 3),
                "reddit":   round(s_reddit, 3),
                "filings":  round(s_filings, 3),
                "call":     round(s_call, 3),
                "price":    round(s_price, 3),
                "spread":   round(s_spread, 3),
            },
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:limit]
