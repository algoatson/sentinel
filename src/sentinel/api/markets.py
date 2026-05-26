"""Markets page endpoints — watchlist, ticker chart, ticker stats."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import funds as _funds
from .. import portfolio as _portfolio


router = APIRouter()


@router.get("/markets/watchlist")
def watchlist() -> list[dict[str, Any]]:
    """Watchlist tickers with multi-period returns + day-of stats.
    Sorted by abs(1d %) descending."""
    rows = _portfolio.watchlist_returns()
    rows.sort(key=lambda r: abs(r.get("change_1d_pct") or 0), reverse=True)
    return rows


@router.get("/markets/{ticker}/chart")
def ticker_chart(
    ticker: str,
    days: int | None = Query(60, description="None = all history; 7/30/90/180/365 standard buckets"),
) -> dict[str, Any]:
    """Candlestick data + open position + recent closed trades on a
    ticker. `days=null` returns full PriceBar history."""
    if days is not None and days <= 0:
        days = None
    # Source open / closed trades from the autonomous fund book
    # (FundTrade) — the legacy portfolio.position_chart read only
    # PaperTrade, so the bot's own positions never showed on the
    # Symbol chart. Returns the same shape + a new `open_positions`
    # list (one row per wallet holding the ticker) so the SPA can
    # render entry/stop/target lines per position.
    data = _funds.position_chart(ticker, days)
    if data is None:
        raise HTTPException(404, f"no data for {ticker}")
    return data


@router.get("/markets/{ticker}/stats")
def ticker_stats(ticker: str, days: int = Query(365, ge=1)) -> dict | None:
    """TradingView-style summary: last price, day range, 52w range,
    volume, avg vol. None when ticker unknown."""
    return _portfolio.ticker_stats(ticker, days)


@router.get("/markets/{ticker}/atr")
def ticker_atr(ticker: str, period: int = Query(14, ge=2, le=60)) -> dict:
    """Latest ATR + 2× and 1.5× stop suggestions — fuel for the
    position-open form's "use ATR stop" hint."""
    from ..analytics import volatility as _vol
    return _vol.atr_for(ticker, period=period)


@router.get("/markets/top-movers")
def top_movers(limit: int = Query(8, ge=1, le=30)) -> dict:
    """Top gainers + losers in the watchlist by 1d %. Yahoo Finance
    style — feeds the Overview "Top movers" panel."""
    from ..analytics import volatility as _vol
    return _vol.top_movers(limit=limit)
