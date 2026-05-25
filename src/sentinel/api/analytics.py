"""Analytics endpoints — hot tickers, calibration, attribution,
news clustering.

These wrap the pure-read modules in ``sentinel/analytics``."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..analytics import attribution as _attr
from ..analytics import calibration as _cal
from ..analytics import concentration as _conc
from ..analytics import dedupe as _dedupe
from ..analytics import hot as _hot
from ..analytics import monthly as _monthly
from ..analytics import sentiment_quality as _sq


router = APIRouter()


@router.get("/analytics/hot")
def hot(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(12, ge=1, le=50),
) -> list[dict]:
    return _hot.hot_tickers(hours=hours, limit=limit)


@router.get("/analytics/calibration")
def calibration(days: int = Query(90, ge=7, le=365)) -> dict:
    return _cal.calibration_summary(days=days)


@router.get("/analytics/attribution")
def attribution(days: int = Query(90, ge=7, le=365)) -> dict:
    return _attr.signal_attribution(days=days)


@router.get("/analytics/news-clusters")
def news_clusters(hours: int = Query(24, ge=1, le=168)) -> dict:
    """Map of fingerprint → [news_ids] for the recent window."""
    return {"clusters": _dedupe.cluster_recent(hours=hours)}


@router.get("/analytics/concentration")
def concentration() -> dict:
    """Per-wallet asset-class exposure of open positions."""
    return _conc.concentration_summary()


@router.get("/analytics/sentiment-quality")
def sentiment_quality(days: int = Query(60, ge=7, le=365)) -> dict:
    """Did the bot's news-sentiment scores predict next-day price?
    Per source, with overall."""
    return _sq.sentiment_quality(days=days)


@router.get("/analytics/monthly")
def monthly_pnl(months: int = Query(12, ge=1, le=24)) -> dict:
    """Month-over-month realised PnL per wallet."""
    return _monthly.monthly_pnl(months=months)
