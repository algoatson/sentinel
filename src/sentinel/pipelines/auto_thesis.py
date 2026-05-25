"""Auto-thesis pipeline: promote a 5/5-conviction TradingCall into a
maintained Thesis.

The bot already runs a heavy-LLM thesis generator daily, but high-
conviction calls don't survive between generator cycles unless the
LLM specifically picks them up. This pipeline closes the gap: any
TradingCall scoring 5/5 in the last 12 hours becomes a Thesis
immediately (best-effort, idempotent).

Idempotent: dedups against existing active theses on the same
ticker + direction. No LLM calls — the call's `thesis` field is
used verbatim as the body, the conviction inherited, and a sensible
invalidation_criteria is composed from direction + ticker. The
human-readable thesis text is short by design (≤ 400 chars from
scorecard.record_call) but better than nothing as a starting point;
the daily generator will re-cover this ticker if it's still relevant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlmodel import select

from ..db import session_scope
from ..models import Thesis, TradingCall


_MIN_CONVICTION = 5     # only the absolute strongest calls auto-promote
_LOOKBACK_HOURS = 12    # rolling window so a missed cycle still catches recent
_MAX_PER_RUN = 4        # safety cap


def run() -> dict:
    """Sweep recent high-conviction calls and create missing theses.

    Returns ``{"scanned": N, "created": M, "skipped_dup": K, ...}``.
    Never raises — logs and returns a payload on any error.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_LOOKBACK_HOURS)
    cutoff_naive = cutoff.replace(tzinfo=None)
    created_ids: list[int] = []
    skipped_dup = 0
    scanned = 0

    try:
        with session_scope() as s:
            calls = s.exec(
                select(TradingCall)
                .where(TradingCall.created_at >= cutoff_naive)
                .where(TradingCall.conviction >= _MIN_CONVICTION)
                .order_by(TradingCall.created_at.desc())
            ).all()
            scanned = len(calls)

            now = datetime.now(timezone.utc)
            for c in calls:
                if len(created_ids) >= _MAX_PER_RUN:
                    break
                # Dedup: existing active thesis on same ticker + direction?
                dup = s.exec(
                    select(Thesis)
                    .where(Thesis.ticker == c.ticker)
                    .where(Thesis.direction == c.direction)
                    .where(Thesis.state == "active")
                    .limit(1)
                ).first()
                if dup is not None:
                    skipped_dup += 1
                    continue

                body = (c.thesis or "")[:1200]
                title = (
                    f"{c.direction.upper()} {c.ticker} via {c.source}"
                )[:200]

                # Default invalidation: directional move >5% against,
                # or 5d return reversing. Generator can rewrite later.
                invalidation = (
                    f"{c.ticker} closes >5% against the {c.direction} "
                    f"thesis for 2 consecutive sessions; or 5d return "
                    f"flips sign with elevated volume."
                )[:500]

                t = Thesis(
                    ticker=c.ticker,
                    direction=c.direction,
                    title=title,
                    body=body,
                    invalidation_criteria=invalidation,
                    conviction=c.conviction,
                    target_price=None,
                    horizon_days=20,  # 5d-call-derived; 20d holding by default
                    state="active",
                    source_event=f"auto-from-call:{c.id}",
                    model="auto",
                    created_at=now,
                    updated_at=now,
                )
                s.add(t)
                s.flush()
                created_ids.append(t.id)
                logger.info(
                    "auto_thesis: created #{} from call #{} ({} {} 5/5)",
                    t.id, c.id, c.ticker, c.direction,
                )
    except Exception as e:
        logger.exception("auto_thesis run failed: {}", e)
        return {"scanned": scanned, "created": 0, "skipped_dup": skipped_dup,
                "error": str(e)}

    # Best-effort event publish for each created thesis.
    if created_ids:
        try:
            from .. import events
            for tid in created_ids:
                events.publish("thesis", {
                    "id": tid,
                    "summary": "auto-thesis created from 5/5 call",
                })
        except Exception:
            pass

    return {
        "scanned": scanned,
        "created": len(created_ids),
        "ids": created_ids,
        "skipped_dup": skipped_dup,
    }


async def run_auto_thesis() -> None:
    """Scheduler entry point. The work is pure DB; wrap in to_thread
    so we don't block the asyncio loop on the SQLite ops."""
    import asyncio
    await asyncio.to_thread(run)
