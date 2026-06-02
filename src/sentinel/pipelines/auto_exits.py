"""Position auto-exit pipeline — enforce user-set stops and targets.

Scans every open FundTrade each tick and closes any position whose
live mark has crossed its `stop_price` (loss-cut), `target_price`
(take-profit), or `trailing_stop_pct` (trailing stop measured off
the running watermark).

Trailing-stop math: watermark = the most favourable price seen
since entry (highest for longs, lowest for shorts). If the current
mark has retraced more than `trailing_stop_pct` from the watermark,
trigger. Watermark is updated on every cycle so the stop ratchets.

Auto-exits are the only path other than user-clicked Close that
removes positions, so they publish `trade` SSE events with a
distinctive close_reason ("stop hit @ 123.45", "target hit", etc).
"""

from __future__ import annotations


from loguru import logger
from sqlmodel import select

from ..db import session_scope
from ..models import Fund, FundTrade
from . import movers  # noqa  - keep import group tidy


def run() -> dict:
    """One sweep. Returns counts of {checked, stops, targets, trails,
    skipped_no_mark}."""
    from ..funds import _close, _mark  # late import to avoid cycle

    checked = 0
    stops = targets = trails = 0
    skipped_no_mark = 0
    closed_events: list[dict] = []

    try:
        with session_scope() as s:
            opens = s.exec(
                select(FundTrade).where(FundTrade.status == "open")
            ).all()
            funds_by_id = {f.id: f for f in s.exec(select(Fund)).all()}
            for t in opens:
                checked += 1
                # No stop / target / trailing configured → nothing to do
                if (
                    t.stop_price is None
                    and t.target_price is None
                    and t.trailing_stop_pct is None
                ):
                    continue
                fund = funds_by_id.get(t.fund_id)
                if fund is None:
                    continue
                mark = _mark(s, t.ticker)
                if mark is None:
                    skipped_no_mark += 1
                    continue

                close_reason: str | None = None

                # Trailing-stop watermark first — updates BEFORE we test
                # the trail trigger so the latest favourable move counts.
                if t.trailing_stop_pct is not None:
                    wm = t.watermark_price or t.entry_price
                    if t.side == "long" and mark > wm:
                        wm = mark
                    elif t.side == "short" and mark < wm:
                        wm = mark
                    if wm != t.watermark_price:
                        t.watermark_price = wm
                        s.add(t)
                    # Trigger check
                    retrace = (
                        (wm - mark) / wm if t.side == "long" and wm > 0
                        else (mark - wm) / wm if t.side == "short" and wm > 0
                        else 0
                    )
                    if retrace >= t.trailing_stop_pct:
                        close_reason = (
                            f"trailing stop hit ({retrace * 100:.1f}% off "
                            f"{wm:.2f} watermark)"
                        )

                # Hard stop (loss-cut)
                if close_reason is None and t.stop_price is not None:
                    hit = (
                        mark <= t.stop_price if t.side == "long"
                        else mark >= t.stop_price
                    )
                    if hit:
                        close_reason = f"stop hit @ {mark:.2f} (target {t.stop_price:.2f})"

                # Target (take-profit)
                if close_reason is None and t.target_price is not None:
                    hit = (
                        mark >= t.target_price if t.side == "long"
                        else mark <= t.target_price
                    )
                    if hit:
                        close_reason = f"target hit @ {mark:.2f} (target {t.target_price:.2f})"

                if close_reason is None:
                    continue

                _close(s, fund, t, mark, close_reason)
                if "stop" in close_reason and "trailing" in close_reason:
                    trails += 1
                elif "stop hit" in close_reason:
                    stops += 1
                elif "target hit" in close_reason:
                    targets += 1
                closed_events.append({
                    "trade_id": t.id,
                    "ticker": t.ticker,
                    "side": t.side,
                    "realized_pnl": t.realized_pnl,
                    "fund": fund.name,
                    "summary": close_reason,
                })
                logger.info(
                    "auto_exits: closed #{} ({} {}) — {}",
                    t.id, t.side, t.ticker, close_reason,
                )
    except Exception as e:
        logger.exception("auto_exits run failed: {}", e)
        return {"checked": checked, "stops": stops, "targets": targets,
                "trails": trails, "skipped_no_mark": skipped_no_mark,
                "error": str(e)}

    # SSE — best-effort, never raises out
    if closed_events:
        try:
            from .. import events
            for ev in closed_events:
                events.publish("trade", ev)
        except Exception:
            pass

    return {
        "checked": checked,
        "stops": stops,
        "targets": targets,
        "trails": trails,
        "skipped_no_mark": skipped_no_mark,
        "closed": len(closed_events),
    }


async def run_auto_exits() -> None:
    """Scheduler entry — pure DB; wrapped in to_thread."""
    import asyncio
    await asyncio.to_thread(run)
