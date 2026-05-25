"""Markets page endpoints — watchlist, ticker chart, ticker stats."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

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
    data = _portfolio.position_chart(ticker, days)
    if data is None:
        raise HTTPException(404, f"no data for {ticker}")
    return data


@router.get("/markets/{ticker}/stats")
def ticker_stats(ticker: str, days: int = Query(365, ge=1)) -> dict | None:
    """TradingView-style summary: last price, day range, 52w range,
    volume, avg vol. None when ticker unknown."""
    return _portfolio.ticker_stats(ticker, days)
