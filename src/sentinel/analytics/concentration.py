"""Position-concentration risk.

A wallet with 6 open positions all in semiconductors is one
sector-shock away from a coordinated drawdown. This module groups
open trades by ``Watchlist.asset_class`` (the closest thing to a
"sector" the bot tracks today) and surfaces concentrations.

Pure read; no writes. The UI overlays the result onto Portfolio or
Risk pages so the user can see "you're 60% NVDA + AMD right now".
"""

from __future__ import annotations

from collections import defaultdict

from sqlmodel import select

from ..db import session_scope
from ..models import Fund, FundTrade, Watchlist


def concentration_summary() -> dict:
    """Open positions grouped by wallet × asset_class, with notional
    exposure (qty × entry) and as a % of wallet equity."""
    out: dict[str, dict] = {}
    with session_scope() as s:
        funds = {f.id: f for f in s.exec(select(Fund)).all()}
        wl = {w.ticker: (w.asset_class or "unknown")
              for w in s.exec(select(Watchlist)).all()}
        opens = s.exec(
            select(FundTrade).where(FundTrade.status == "open")
        ).all()
        per_fund: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        per_fund_total: dict[str, float] = defaultdict(float)
        per_fund_tickers: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for t in opens:
            f = funds.get(t.fund_id)
            if f is None:
                continue
            cls = wl.get(t.ticker, "unknown")
            notional = t.entry_price * t.qty
            per_fund[f.name][cls] += notional
            per_fund_total[f.name] += notional
            per_fund_tickers[f.name][cls].append(t.ticker)
        for name, classes in per_fund.items():
            total = per_fund_total[name] or 1.0
            fund = next((f for f in funds.values() if f.name == name), None)
            out[name] = {
                "mandate": fund.mandate if fund else None,
                "total_notional": round(total, 2),
                "groups": [
                    {
                        "asset_class": cls,
                        "notional": round(amt, 2),
                        "pct": round(amt / total * 100, 1),
                        "tickers": sorted(set(per_fund_tickers[name][cls])),
                        "count": len(per_fund_tickers[name][cls]),
                    }
                    for cls, amt in sorted(
                        classes.items(), key=lambda kv: -kv[1]
                    )
                ],
            }
    return {"wallets": out}
