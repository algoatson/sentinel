"""Crypto-specific dashboard endpoints.

Surfaces the funding-squeeze detector's findings (recorded as TradingCalls
with source="funding_squeeze") plus the current BTC regime, so the dashboard
shows what the crypto leg is seeing — funding extremes, OI surges, book skew
— instead of those only living in Discord embeds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlmodel import select

from ..crypto_regime import market_regime
from ..db import session_scope
from ..models import CryptoMicro, TradingCall

router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/crypto/signals")
def crypto_signals(
    hours: int = Query(72, ge=1, le=336),
    limit: int = Query(8, ge=1, le=30),
) -> dict:
    """Recent funding-squeeze findings (newest first) + the live BTC regime.

    Each finding carries the point-in-time evidence (from the call's thesis)
    plus the coin's CURRENT microstructure, so the card shows both what fired
    and where funding/OI/book sit right now."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None)
    out: list[dict] = []
    with session_scope() as s:
        reg = market_regime(s)
        rows = s.exec(
            select(TradingCall)
            .where(TradingCall.source == "funding_squeeze")
            .where(TradingCall.created_at >= cutoff)
            .order_by(TradingCall.created_at.desc())
            .limit(limit)
        ).all()
        for c in rows:
            # thesis is "headline: evidence" (see funding_squeeze.record_call)
            headline, _, evidence = (c.thesis or "").partition(": ")
            cm = s.get(CryptoMicro, c.ticker)
            micro = None
            if cm is not None:
                micro = {
                    "funding_rate_pct": (
                        round(cm.funding_rate * 100, 4)
                        if cm.funding_rate is not None else None
                    ),
                    "oi_change_24h_pct": (
                        round(cm.oi_change_24h_pct * 100, 2)
                        if cm.oi_change_24h_pct is not None else None
                    ),
                    "orderbook_imbalance": cm.orderbook_imbalance,
                }
            out.append({
                "id": c.id,
                "ticker": c.ticker,
                "direction": c.direction,
                "conviction": c.conviction,
                "headline": headline or (c.thesis or ""),
                "evidence": evidence,
                "ts": _aware_iso(c.created_at),
                "ret_1d_pct": c.ret_1d_pct,
                "settled": c.settled,
                "micro": micro,
            })
    return {
        "regime": {
            "state": reg.state,
            "btc_1d_pct": reg.btc_1d_pct,
            "btc_5d_pct": reg.btc_5d_pct,
            "reason": reg.reason,
        },
        "signals": out,
    }
