"""Analytics endpoints — hot tickers, calibration, attribution,
news clustering.

These wrap the pure-read modules in ``sentinel/analytics``."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..analytics import attribution as _attr
from ..analytics import calibration as _cal
from ..analytics import concentration as _conc
from ..analytics import correlation as _corr
from ..analytics import daily as _daily
from ..analytics import dedupe as _dedupe
from ..analytics import digest as _digest
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


@router.get("/analytics/daily")
def daily_pnl(days: int = Query(180, ge=14, le=730)) -> dict:
    """Day-by-day realised PnL summed across every wallet. Returns
    a dense cell grid (one entry per day) for a GitHub-style heatmap."""
    return _daily.daily_pnl(days=days)


@router.get("/analytics/drawdown")
def drawdown(days: int = Query(90, ge=14, le=730)) -> dict:
    """Per-wallet peak-to-current drawdown series."""
    return _daily.drawdown_curves(days=days)


@router.get("/analytics/correlation")
def correlation(
    tickers: str | None = Query(
        None,
        description="Comma-separated tickers; default = every open-position ticker",
    ),
    days: int = Query(30, ge=7, le=180),
) -> dict:
    """Pairwise daily-return correlation matrix. Defaults to the
    current open-position universe so the Analytics panel surfaces
    concentration risk without asking what to plot."""
    syms = None
    if tickers:
        syms = [
            t.strip().upper().lstrip("$") for t in tickers.split(",")
            if t.strip()
        ]
    return _corr.correlation_matrix(tickers=syms, days=days)


@router.get("/analytics/today")
def today() -> dict:
    """Pulse of the rolling last 24h — news/calls/filings/reddit
    counts, opened/closed trades, realised PnL today, best+worst
    closes, top conviction call, top material filing."""
    return _digest.today_pulse()
