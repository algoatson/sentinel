"""Volatility helpers — ATR, realised vol.

Used by the position-open form's "suggest stop" hint and the
risk-drawer when the user wants to size off ATR rather than a hard
dollar stop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import PriceBar, PriceContext


def true_range(prev_close: float, high: float, low: float) -> float:
    """Wilder TR: max(high-low, |high-prev_close|, |low-prev_close|)."""
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
    )


def atr_for(ticker: str, period: int = 14) -> dict:
    """Compute the latest Average True Range over `period` daily bars.

    Returns:
      {
        ticker, period, last_close, atr, atr_pct,
        suggested_long_stop, suggested_short_stop, bars_used,
      }
    Suggested stops use 2× ATR — a common medium-term default
    that balances "wide enough to dodge noise" with "tight enough
    to lock risk." The UI labels them as suggestions, not forced.
    """
    ticker = (ticker or "").upper().lstrip("$").strip()
    if not ticker:
        return {"ticker": "", "atr": None, "atr_pct": None}

    cutoff = datetime.now(timezone.utc) - timedelta(days=period * 4 + 14)
    with session_scope() as s:
        bars = s.exec(
            select(PriceBar)
            .where(PriceBar.ticker == ticker)
            .where(PriceBar.ts >= cutoff)
            .order_by(PriceBar.ts)
        ).all()
        pc = s.get(PriceContext, ticker)

    # Collapse to one bar per UTC day (the bot stores intraday bars
    # too; ATR is a daily concept).
    by_day: dict[str, dict] = {}
    for b in bars:
        d = b.ts.strftime("%Y-%m-%d")
        cur = by_day.get(d)
        if cur is None:
            by_day[d] = {
                "high": b.high, "low": b.low, "close": b.close
            }
        else:
            cur["high"] = max(cur["high"], b.high)
            cur["low"] = min(cur["low"], b.low)
            cur["close"] = b.close   # last bar of the day = closing print

    days = sorted(by_day.keys())
    if len(days) < 2:
        return {
            "ticker": ticker,
            "period": period,
            "last_close": pc.last_price if pc else None,
            "atr": None,
            "atr_pct": None,
            "suggested_long_stop": None,
            "suggested_short_stop": None,
            "bars_used": len(days),
        }

    trs: list[float] = []
    prev_close = by_day[days[0]]["close"]
    for d in days[1:]:
        b = by_day[d]
        trs.append(true_range(prev_close, b["high"], b["low"]))
        prev_close = b["close"]
    trs = trs[-period:] if len(trs) >= period else trs
    atr = sum(trs) / len(trs)
    last_close = by_day[days[-1]]["close"]
    atr_pct = (atr / last_close * 100) if last_close > 0 else None

    return {
        "ticker": ticker,
        "period": period,
        "last_close": round(last_close, 4),
        "atr": round(atr, 4),
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        # 2× ATR is the medium-term stop default. UI shows both
        # 1.5× (tighter) and 2× (default) when surfacing.
        "suggested_long_stop": round(last_close - 2 * atr, 4),
        "suggested_short_stop": round(last_close + 2 * atr, 4),
        "suggested_long_stop_tight": round(last_close - 1.5 * atr, 4),
        "suggested_short_stop_tight": round(last_close + 1.5 * atr, 4),
        "bars_used": len(days),
    }


def top_movers(limit: int = 10) -> dict:
    """Top gainers + losers in the watchlist by 1d %. Uses PriceContext
    which carries the latest 1d change for every tracked symbol —
    cheap O(N) over the watchlist, no per-ticker bar reads."""
    from ..models import Watchlist
    out_gainers: list[dict] = []
    out_losers: list[dict] = []
    with session_scope() as s:
        wl = s.exec(select(Watchlist)).all()
        pcs = {
            pc.ticker: pc for pc in s.exec(select(PriceContext)).all()
        }
        rows: list[dict] = []
        for w in wl:
            if not w.ticker:
                continue
            pc = pcs.get(w.ticker)
            if pc is None or pc.change_1d_pct is None:
                continue
            rows.append({
                "ticker": w.ticker,
                "asset_class": w.asset_class or "—",
                "last_price": pc.last_price,
                "change_1d_pct": round((pc.change_1d_pct or 0) * 100, 2),
                "volume_vs_20d_avg": (
                    round(pc.volume_vs_20d_avg, 2) if pc.volume_vs_20d_avg else None
                ),
            })
    rows.sort(key=lambda r: r["change_1d_pct"], reverse=True)
    out_gainers = rows[:limit]
    out_losers = list(reversed(rows[-limit:]))
    # Only return losers that are actually negative (no point showing
    # "smallest gainer" as a loser when nothing red exists).
    out_losers = [r for r in out_losers if r["change_1d_pct"] < 0]
    return {"gainers": out_gainers, "losers": out_losers}
