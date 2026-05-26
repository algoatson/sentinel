"""Pairwise daily-return correlation matrix.

Real risk view: two long positions on names that always move
together aren't two independent bets — they're one trade with twice
the size. Surface the correlation matrix of open-position tickers so
the user can spot hidden concentration ("you're long DELL AND HPQ,
which are 0.87 correlated").

Cheap to compute: one DB read across all relevant tickers, then
Pearson correlation in Python.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlmodel import select

from ..db import session_scope
from ..models import FundTrade, PriceBar


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation. None when there isn't enough data or the
    series has zero variance (a flat line correlates with nothing)."""
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _daily_returns(ticker: str, days: int) -> list[float]:
    """Daily log returns of `ticker` over `days` calendar days. Uses
    one bar per UTC day (last close)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days + 2)
    with session_scope() as s:
        bars = s.exec(
            select(PriceBar)
            .where(PriceBar.ticker == ticker)
            .where(PriceBar.ts >= cutoff)
            .order_by(PriceBar.ts)
        ).all()
    # Collapse intraday bars to daily closes
    by_day: dict[str, float] = {}
    for b in bars:
        d = b.ts.strftime("%Y-%m-%d")
        by_day[d] = b.close
    closes = [by_day[d] for d in sorted(by_day.keys())]
    if len(closes) < 2:
        return []
    rets: list[float] = []
    prev = closes[0]
    for c in closes[1:]:
        if prev > 0 and c > 0:
            import math
            rets.append(math.log(c / prev))
        prev = c
    return rets


def correlation_matrix(
    tickers: Iterable[str] | None = None,
    days: int = 30,
) -> dict:
    """Correlation matrix for `tickers` (or every open-position ticker
    if None). Returns:

      { tickers: [...], matrix: [[1, 0.4, ...], ...], days, n }

    The matrix is symmetric with 1.0 on the diagonal. Each cell is
    the Pearson correlation of daily log returns over the window.
    """
    if tickers is None:
        with session_scope() as s:
            tickers = sorted({
                t.ticker for t in s.exec(
                    select(FundTrade).where(FundTrade.status == "open")
                ).all()
            })
    tickers = list(tickers)
    if not tickers:
        return {"tickers": [], "matrix": [], "days": days, "n": 0}

    series = {t: _daily_returns(t, days=days) for t in tickers}
    matrix: list[list[float | None]] = []
    for a in tickers:
        row: list[float | None] = []
        for b in tickers:
            if a == b:
                row.append(1.0)
            else:
                row.append(_pearson(series[a], series[b]))
        matrix.append(row)

    return {
        "tickers": tickers,
        "matrix": matrix,
        "days": days,
        "n": len(tickers),
        "bars_used": {t: len(series[t]) for t in tickers},
    }
