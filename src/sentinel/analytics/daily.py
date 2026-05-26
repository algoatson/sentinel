"""Daily P&L + drawdown analytics.

Two read-side helpers:

- ``daily_pnl(days)`` — realised P&L aggregated by UTC date across
  every wallet. Powers the GitHub-style calendar heatmap (green
  shades for green days, red for red days) on the Analytics page.

- ``drawdown_curves(days)`` — per-wallet equity peak-to-current
  drawdown series. Surface alongside the equity curve: "Catalyst's
  drawdown bottomed at -8.2% on 2026-05-12 and has clawed back to
  -2.1%."
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import Fund, FundEquity, FundTrade


def daily_pnl(days: int = 365) -> dict:
    """Per-day realised P&L summed across every wallet."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_naive = cutoff.replace(tzinfo=None)
    by_day: dict[str, dict] = defaultdict(
        lambda: {"realized_pnl": 0.0, "closed": 0, "wins": 0, "losses": 0}
    )
    with session_scope() as s:
        trades = s.exec(
            select(FundTrade)
            .where(FundTrade.status == "closed")
            .where(FundTrade.exit_at.is_not(None))
            .where(FundTrade.exit_at >= cutoff_naive)
        ).all()
        for t in trades:
            if t.exit_at is None:
                continue
            day = t.exit_at.date().isoformat()
            b = by_day[day]
            pnl = t.realized_pnl or 0.0
            b["realized_pnl"] += pnl
            b["closed"] += 1
            if pnl > 0:
                b["wins"] += 1
            elif pnl < 0:
                b["losses"] += 1
    today = date.today()
    earliest = (today - timedelta(days=days - 1)).isoformat()
    # Fill missing days with zero so the UI grid is dense.
    cells: list[dict] = []
    cursor = today - timedelta(days=days - 1)
    while cursor <= today:
        iso = cursor.isoformat()
        b = by_day.get(iso) or {
            "realized_pnl": 0.0, "closed": 0, "wins": 0, "losses": 0
        }
        cells.append({
            "date": iso,
            "weekday": cursor.isoweekday(),  # 1=Mon … 7=Sun
            "realized_pnl": round(b["realized_pnl"], 2),
            "closed": b["closed"],
            "wins": b["wins"],
            "losses": b["losses"],
        })
        cursor += timedelta(days=1)

    nonzero = [c for c in cells if c["closed"] > 0]
    max_abs = max((abs(c["realized_pnl"]) for c in nonzero), default=0.0)
    best = max(nonzero, key=lambda c: c["realized_pnl"], default=None)
    worst = min(nonzero, key=lambda c: c["realized_pnl"], default=None)
    active = len(nonzero)
    return {
        "days": days,
        "from": earliest,
        "to": today.isoformat(),
        "cells": cells,
        "max_abs": round(max_abs, 2),
        "active_days": active,
        "total_realized": round(sum(c["realized_pnl"] for c in cells), 2),
        "best_day": best,
        "worst_day": worst,
    }


def drawdown_curves(days: int = 90) -> dict:
    """Per-wallet drawdown-from-peak time series. One point per
    recorded equity mark within the window. Returns:

      [{fund, starting, points: [{ts, equity, peak, drawdown_pct}, ...]}]
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    with session_scope() as s:
        funds = s.exec(
            select(Fund).where(Fund.active.is_(True)).order_by(Fund.name)
        ).all()
        for f in funds:
            pts_rows = s.exec(
                select(FundEquity)
                .where(FundEquity.fund_id == f.id)
                .where(FundEquity.ts >= cutoff)
                .order_by(FundEquity.ts)
            ).all()
            peak = -float("inf")
            points = []
            for p in pts_rows:
                if p.equity > peak:
                    peak = p.equity
                dd = (
                    ((p.equity - peak) / peak * 100)
                    if peak > 0 else 0.0
                )
                ts = p.ts if p.ts.tzinfo else p.ts.replace(tzinfo=timezone.utc)
                points.append({
                    "ts": ts.isoformat(),
                    "equity": round(p.equity, 2),
                    "peak": round(peak, 2),
                    "drawdown_pct": round(dd, 2),
                })
            current_dd = points[-1]["drawdown_pct"] if points else 0.0
            max_dd = min(
                (p["drawdown_pct"] for p in points), default=0.0
            )
            out.append({
                "fund": f.name,
                "starting": f.starting_cash,
                "points": points,
                "current_dd_pct": current_dd,
                "max_dd_pct": max_dd,
            })
    return {"window_days": days, "wallets": out}
