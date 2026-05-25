"""Realised P&L attribution.

Aggregates settled TradingCalls into "who actually made me money?"
slices: by source, conviction bucket, direction, and ticker.
Realised PnL per call is approximated by ``ret_5d_pct`` × hypothetical
1-unit notional — we don't have per-call sizing in the call table
itself (sizing happens downstream in funds.py), so this is a signal-
level attribution, not portfolio-level.

For portfolio P&L breakdown by wallet see ``portfolio.realized_curve``
and ``funds.trade_history``."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import TradingCall


def _signed_return(c: TradingCall) -> float:
    """Direction-adjusted 5d return. None becomes 0 (caller filters)."""
    r = c.ret_5d_pct
    if r is None:
        return 0.0
    return r if c.direction == "long" else -r


def signal_attribution(days: int = 90) -> dict:
    """Per-source / per-conviction / per-direction P&L attribution
    on a 1-unit-per-call basis (ret_5d_pct as the proxy)."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).replace(tzinfo=None)

    by_source: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "ret_sum": 0.0, "best": 0.0, "worst": 0.0}
    )
    by_conv: dict[int, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "ret_sum": 0.0}
    )
    by_direction: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "ret_sum": 0.0}
    )
    by_ticker: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "ret_sum": 0.0}
    )

    with session_scope() as s:
        rows = s.exec(
            select(TradingCall)
            .where(TradingCall.created_at >= cutoff_naive)
            .where(TradingCall.settled == True)  # noqa: E712
            .where(TradingCall.ret_5d_pct.is_not(None))
        ).all()
        for c in rows:
            r = _signed_return(c)
            win = r > 0

            d = by_source[c.source]
            d["n"] += 1
            d["wins"] += int(win)
            d["ret_sum"] += r
            d["best"] = max(d["best"], r)
            d["worst"] = min(d["worst"], r)

            bc = by_conv[c.conviction]
            bc["n"] += 1
            bc["wins"] += int(win)
            bc["ret_sum"] += r

            bd = by_direction[c.direction]
            bd["n"] += 1
            bd["wins"] += int(win)
            bd["ret_sum"] += r

            bt = by_ticker[c.ticker]
            bt["n"] += 1
            bt["wins"] += int(win)
            bt["ret_sum"] += r

    def _shape(d: dict) -> dict:
        n = d["n"]
        return {
            "n": n,
            "wins": d["wins"],
            "hit_rate": (d["wins"] / n) if n else None,
            "ret_avg_pct": (d["ret_sum"] / n) if n else None,
            "ret_sum_pct": d["ret_sum"],
        }

    return {
        "window_days": days,
        "by_source": [
            {"source": k, **_shape(v),
             "best_pct": v["best"], "worst_pct": v["worst"]}
            for k, v in sorted(
                by_source.items(), key=lambda kv: kv[1]["ret_sum"], reverse=True
            )
        ],
        "by_conviction": [
            {"conviction": k, **_shape(v)}
            for k, v in sorted(by_conv.items())
        ],
        "by_direction": [
            {"direction": k, **_shape(v)}
            for k, v in by_direction.items()
        ],
        "top_tickers": [
            {"ticker": k, **_shape(v)}
            for k, v in sorted(
                by_ticker.items(), key=lambda kv: kv[1]["ret_sum"], reverse=True
            )[:10]
        ],
        "bottom_tickers": [
            {"ticker": k, **_shape(v)}
            for k, v in sorted(
                by_ticker.items(), key=lambda kv: kv[1]["ret_sum"]
            )[:10]
            if v["ret_sum"] < 0
        ],
    }
