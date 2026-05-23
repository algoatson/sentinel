"""Channel routing — keep per-asset noise out of #priority / #news.

One place that decides where a ticker's content goes, so the rule is
consistent across convergence / why-moved / news-alerts / crypto-trending
instead of each pipeline hardcoding a channel.
"""

from __future__ import annotations

from functools import lru_cache

from sqlmodel import select

from .config import settings
from .db import session_scope
from .models import Watchlist


def _news_channel() -> int:
    return settings.DISCORD_NEWS_CHANNEL_ID or settings.DISCORD_PULSE_CHANNEL_ID


def crypto_channel() -> int:
    """Dedicated crypto channel, degrading to #news then #pulse."""
    return settings.DISCORD_CRYPTO_CHANNEL_ID or _news_channel()


@lru_cache(maxsize=2048)
def _asset_class_cached(ticker: str) -> str | None:
    with session_scope() as s:
        row = s.exec(
            select(Watchlist).where(Watchlist.ticker == ticker)
        ).first()
    return row.asset_class if row is not None else None


def asset_class_of(ticker: str | None) -> str | None:
    """Watchlist asset_class for a ticker, or None if not tracked. Cached —
    asset class is effectively immutable per ticker."""
    if not ticker:
        return None
    return _asset_class_cached(ticker)


def channel_for(ticker: str | None, *, equity_default: int) -> int:
    """Crypto → crypto channel; everything else → the caller's default."""
    if asset_class_of(ticker) == "crypto":
        return crypto_channel()
    return equity_default
