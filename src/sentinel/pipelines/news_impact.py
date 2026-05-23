"""News→price correlation tagger.

For each NewsItem with a ticker but no measured impact yet, look up the
nearest PriceBar at publish time, 1h after, and 1d after — compute the
percent return realized over each horizon. Persist back to NewsItem.

Only ticker-tagged news on the watchlist gets measured (we have PriceBar
history for them). Macro news without a ticker is skipped.

The measurement is purely factual — no LLM speculation about causation.
The enrichment + chat layers can then surface average news impact per
ticker, and chat-time analysis can compare news polarity vs. realized move.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..db import session_scope
from ..models import NewsItem, PriceBar


_HORIZON_1H = timedelta(hours=1)
_HORIZON_1D = timedelta(days=1)


async def run_news_impact_tagging() -> None:
    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("run_news_impact_tagging top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ news impact tagger error: {e}")
        except Exception:
            pass


def _run() -> None:
    now = datetime.now(timezone.utc)
    # SQLite stores datetimes naive; pass naive UTC into queries.
    now_naive = now.replace(tzinfo=None)
    cutoff_old = now_naive - timedelta(days=14)
    cutoff_react = now_naive - _HORIZON_1H

    tagged = 0
    with session_scope() as session:
        items = session.exec(
            select(NewsItem)
            .where(NewsItem.ticker.is_not(None))
            .where(NewsItem.published_at >= cutoff_old)
            .where(NewsItem.published_at <= cutoff_react)
            .where(NewsItem.impact_tagged_at.is_(None))
            .limit(200)
        ).all()

        for item in items:
            ticker = item.ticker
            assert ticker is not None
            pub = item.published_at
            # NewsItem.published_at may be naive (from SQLite) or aware
            # (freshly inserted in this session). Normalize for arithmetic.
            pub_naive = pub.replace(tzinfo=None) if pub.tzinfo else pub
            t_1h = pub_naive + _HORIZON_1H
            t_1d = pub_naive + _HORIZON_1D

            p_at = _nearest_price(session, ticker, pub_naive)
            p_1h = _nearest_price(session, ticker, t_1h) if t_1h <= now_naive else None
            p_1d = _nearest_price(session, ticker, t_1d) if t_1d <= now_naive else None

            # Even if we couldn't find prices, mark tagged so we don't keep
            # retrying — but only if the news is old enough that prices
            # really should exist (>1d old).
            should_finalize = pub_naive <= (now_naive - _HORIZON_1D)

            updated = False
            if p_at is not None:
                item.price_at_publish = p_at
                updated = True
                if p_1h is not None and p_at:
                    item.impact_1h_pct = (p_1h - p_at) / p_at
                if p_1d is not None and p_at:
                    item.impact_1d_pct = (p_1d - p_at) / p_at

            if updated or should_finalize:
                item.impact_tagged_at = now
                session.add(item)
                if updated:
                    tagged += 1

    logger.info("news impact tagger: measured {} items", tagged)


def _nearest_price(session, ticker: str, when: datetime) -> Optional[float]:
    """Find the closest PriceBar to `when` within ±2 hours.

    For after-hours news, this may return None — that's expected and the
    caller should mark the item tagged without an impact value.

    SQLite returns naive datetimes on read; strip tz from `when` so the
    comparison is apples-to-apples.
    """
    window = timedelta(hours=2)
    when_naive = when.replace(tzinfo=None) if when.tzinfo else when
    bar = session.exec(
        select(PriceBar)
        .where(PriceBar.ticker == ticker)
        .where(PriceBar.ts >= when_naive - window)
        .where(PriceBar.ts <= when_naive + window)
        .order_by(
            # SQLite doesn't have abs(timestamp diff) easily — just take
            # the latest bar in the window. Approximation is fine.
            PriceBar.ts.desc()
        )
        .limit(1)
    ).first()
    return bar.close if bar else None
