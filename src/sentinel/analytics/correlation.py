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
    """Pearson correlation on two parallel series of equal length.

    Callers MUST align xs and ys by the same observation index before
    passing in (in this module, date-aligned daily returns). Earlier
    versions used min(len(xs), len(ys)) and truncated to the prefix
    of each — a silent correctness hole when the two series start on
    different dates (e.g. a 30d-old ticker vs a fresh listing), since
    the first N indices were *different calendar days* on each side.
    """
    n = len(xs)
    if n != len(ys) or n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _daily_returns(ticker: str, days: int) -> dict[str, float]:
    """Daily log returns of `ticker` keyed by YYYY-MM-DD. Uses one bar
    per UTC day (last close).

    Returns a dict (not a list) so callers can intersect by date with
    another ticker's series before computing correlation — index-by-
    index alignment was the bug behind the spurious correlations.
    """
    import math
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
    days_sorted = sorted(by_day.keys())
    if len(days_sorted) < 2:
        return {}
    rets: dict[str, float] = {}
    prev = by_day[days_sorted[0]]
    for d in days_sorted[1:]:
        c = by_day[d]
        if prev > 0 and c > 0:
            rets[d] = math.log(c / prev)
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
                continue
            # Date-intersection alignment — only days BOTH tickers have
            # a return for. Without this two series with different
            # listing dates would have been zipped by index and the
            # correlation would have been computed on unrelated calendar
            # days. Sorted so the order is deterministic and parallel
            # for both legs of the pair.
            sa, sb = series[a], series[b]
            common = sorted(set(sa) & set(sb))
            xs = [sa[d] for d in common]
            ys = [sb[d] for d in common]
            row.append(_pearson(xs, ys))
        matrix.append(row)

    return {
        "tickers": tickers,
        "matrix": matrix,
        "days": days,
        "n": len(tickers),
        "bars_used": {t: len(series[t]) for t in tickers},
    }
