"""Month-over-month performance breakdown.

Aggregates closed FundTrade realized PnL into per-month, per-wallet
totals. Useful for the "am I trending up or grinding sideways?"
question that a daily curve obscures."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import Fund, FundTrade


def monthly_pnl(months: int = 12) -> dict:
    """Per-(wallet, YYYY-MM) realized PnL + closed-count over the
    last `months` calendar months. Returns a wide-format response so
    the UI can render a horizontal bar chart per wallet."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 31)
    cutoff_naive = cutoff.replace(tzinfo=None)

    by_wallet_month: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"realized_pnl": 0.0, "closed": 0, "wins": 0}
    )

    with session_scope() as s:
        funds_by_id = {
            f.id: f for f in s.exec(select(Fund)).all()
        }
        trades = s.exec(
            select(FundTrade)
            .where(FundTrade.status == "closed")
            .where(FundTrade.exit_at.is_not(None))
            .where(FundTrade.exit_at >= cutoff_naive)
        ).all()
        for t in trades:
            f = funds_by_id.get(t.fund_id)
            if f is None or t.exit_at is None:
                continue
            ym = t.exit_at.strftime("%Y-%m")
            b = by_wallet_month[(f.name, ym)]
            b["realized_pnl"] += t.realized_pnl or 0.0
            b["closed"] += 1
            if (t.realized_pnl or 0) > 0:
                b["wins"] += 1

    # Build the dense matrix: every wallet × every month.
    wallets = sorted({k[0] for k in by_wallet_month.keys()})
    months_set = sorted({k[1] for k in by_wallet_month.keys()})

    rows = []
    for w in wallets:
        cells = []
        total_pnl = 0.0
        total_closed = 0
        total_wins = 0
        for ym in months_set:
            b = by_wallet_month.get((w, ym), {"realized_pnl": 0.0, "closed": 0, "wins": 0})
            cells.append({
                "month": ym,
                "realized_pnl": round(b["realized_pnl"], 2),
                "closed": b["closed"],
                "wins": b["wins"],
            })
            total_pnl += b["realized_pnl"]
            total_closed += b["closed"]
            total_wins += b["wins"]
        rows.append({
            "wallet": w,
            "cells": cells,
            "total_pnl": round(total_pnl, 2),
            "total_closed": total_closed,
            "total_wins": total_wins,
        })

    return {"months": months_set, "wallets": rows}
