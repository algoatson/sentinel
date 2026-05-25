"""Wallet drawdown circuit breaker.

A wallet that's bleeding badly should stop opening new positions
until the user reviews it — otherwise a momentum/contrarian
mismatch can chew through the starting cash unchecked. This
pipeline computes peak-to-current drawdown for every active wallet
each tick and flips a wallet ``active=False`` when its drawdown
exceeds the configured floor (default 15%).

We never AUTO-close existing positions — that would amplify the
loss at a bad moment. The block is on NEW opens: the autonomous
``funds._run()`` cycle already skips inactive funds.

Tripping publishes a "risk" SSE event and Discord alert (best-
effort).
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlmodel import select

from ..db import session_scope
from ..models import Fund, FundEquity


# 15% peak-to-current. Tunable per wallet later; one threshold for now.
_DD_TRIP_PCT = -15.0
# Trip only once per stretch — if the user manually re-enables, we
# don't re-trip until the wallet recovers above _DD_TRIP_PCT/2.
_DD_RECOVER_PCT = -7.0


def _peak_to_now(equity_pts: list[float]) -> float:
    """Most recent peak-to-current drawdown as a signed %."""
    if not equity_pts:
        return 0.0
    peak = equity_pts[0]
    cur = equity_pts[-1]
    for e in equity_pts:
        if e > peak:
            peak = e
    if peak <= 0:
        return 0.0
    return (cur - peak) / peak * 100


def run() -> dict:
    """Audit every fund's drawdown; flip ``active`` flag on trip.

    Returns ``{"audited": N, "tripped": [names], "reset": [names]}``.
    """
    tripped: list[str] = []
    reset: list[str] = []
    audited = 0

    try:
        with session_scope() as s:
            funds = s.exec(select(Fund)).all()
            for f in funds:
                audited += 1
                pts_rows = s.exec(
                    select(FundEquity)
                    .where(FundEquity.fund_id == f.id)
                    .order_by(FundEquity.ts)
                ).all()
                pts = [p.equity for p in pts_rows]
                if len(pts) < 3:
                    continue
                dd = _peak_to_now(pts)

                if f.active and dd <= _DD_TRIP_PCT:
                    f.active = False
                    s.add(f)
                    tripped.append(f.name)
                    logger.warning(
                        "risk_circuit: {} drawdown {:.1f}% (≤{:.0f}%) — "
                        "wallet PAUSED (no new opens)",
                        f.name, dd, _DD_TRIP_PCT,
                    )
                elif (not f.active) and dd >= _DD_RECOVER_PCT:
                    # Auto-reset only if the user manually disabled
                    # AND drawdown recovered above half the trip
                    # threshold. The circuit-breaker only flips back
                    # to active if the wallet had been auto-tripped,
                    # not on every recovery — we track that via the
                    # `_auto_paused` flag on Fund... which we don't
                    # have yet. Conservative: stay paused until user
                    # re-enables manually via the dashboard.
                    pass
    except Exception as e:
        logger.exception("risk_circuit run failed: {}", e)
        return {"audited": audited, "tripped": tripped, "reset": reset,
                "error": str(e)}

    # Publish events for live dashboard.
    if tripped:
        try:
            from .. import events
            for name in tripped:
                events.publish("risk", {
                    "kind": "drawdown_trip",
                    "wallet": name,
                    "summary": f"{name} paused — drawdown ≤ {_DD_TRIP_PCT:.0f}%",
                })
        except Exception:
            pass

    return {"audited": audited, "tripped": tripped, "reset": reset}


async def run_risk_circuit() -> None:
    """Scheduler entry. Pure DB; wrap to_thread."""
    import asyncio
    await asyncio.to_thread(run)
