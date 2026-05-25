"""Auto-spawn research_desk tasks for upcoming earnings.

When a watchlist ticker reports earnings in the next 3 days, the
bot drafts a research question and queues it through the existing
Research Desk path. The user wakes up to a ranked stack of "should
I trade $X into earnings" verdicts instead of remembering to ask.

Idempotent: skips a ticker that already has a research task in the
last 24h, and never queues the same earnings event twice. Bounded:
max 3 new tasks per run so a heavy earnings day doesn't burn the
heavy-LLM budget all at once.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlmodel import select

from ..db import session_scope
from ..models import ResearchTask, Watchlist


_WINDOW_DAYS = 3
_MAX_PER_RUN = 3
_RECENT_TASK_HOURS = 24


def _watchlist_tickers() -> set[str]:
    with session_scope() as s:
        return {
            w.ticker
            for w in s.exec(select(Watchlist)).all()
            if w.ticker
        }


def _recent_research_tickers(hours: int = _RECENT_TASK_HOURS) -> set[str]:
    """Tickers already studied recently. Crude — looks at the
    `rec_ticker` of the task (set after the LLM completes) AND
    scans the prompt body for the symbol."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_naive = cutoff.replace(tzinfo=None)
    out: set[str] = set()
    with session_scope() as s:
        rows = s.exec(
            select(ResearchTask).where(ResearchTask.created_at >= cutoff_naive)
        ).all()
        for r in rows:
            if r.rec_ticker:
                out.add(r.rec_ticker.upper())
            text = (r.prompt or "").upper()
            for sym in _scan_dollar_tags(text):
                out.add(sym)
    return out


def _scan_dollar_tags(text: str) -> list[str]:
    """Pull $XXX tags out of text (rough, no false positives matter much)."""
    import re
    return [t.group(1) for t in re.finditer(r"\$([A-Z]{1,6})\b", text)]


async def run() -> dict:
    """Single sweep. Returns summary."""
    try:
        from ..pipelines import catalysts
        text, events = await asyncio.to_thread(catalysts._run_sync)
    except Exception as e:
        logger.debug("auto_research_pre_earnings: catalysts unavailable: {}", e)
        return {"scanned": 0, "queued": 0, "error": str(e)}

    # Earnings entries from catalysts are {"ticker": ..., "date": "YYYY-MM-DD"}.
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=_WINDOW_DAYS)
    earnings = []
    for ev in events:
        t = ev.get("ticker")
        d_raw = ev.get("date")
        if not (t and d_raw):
            continue
        try:
            d = datetime.strptime(d_raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= horizon:
            earnings.append((t.upper(), d))

    watchlist = _watchlist_tickers()
    recent = _recent_research_tickers()

    candidates = [
        (t, d) for (t, d) in earnings
        if t in watchlist and t not in recent
    ]

    if not candidates:
        return {"scanned": len(earnings), "queued": 0, "candidates": 0}

    # Rank: earliest first (more urgent).
    candidates.sort(key=lambda x: x[1])

    from .. import research_desk
    queued: list[dict] = []
    for ticker, date in candidates[:_MAX_PER_RUN]:
        prompt = (
            f"$" + ticker + f" reports earnings on {date.isoformat()} — should I "
            f"trade it into the print? Consider: positioning (long vs short vs "
            f"flat), conviction, sizing. Pull the latest news, filings, options "
            f"flow (if any), and historical earnings-day moves. Be decisive."
        )
        try:
            task_id = await research_desk.run_research(prompt)
            queued.append({"task_id": task_id, "ticker": ticker, "date": date.isoformat()})
            logger.info(
                "auto_research_pre_earnings: queued #{} ({} on {})",
                task_id, ticker, date,
            )
        except Exception as e:
            logger.warning("auto_research_pre_earnings: {} failed — {}", ticker, e)

    # Best-effort SSE
    if queued:
        try:
            from .. import events as _ev
            for q in queued:
                _ev.publish("research_auto", {
                    "task_id": q["task_id"],
                    "ticker": q["ticker"],
                    "summary": f"pre-earnings ({q['date']})",
                })
        except Exception:
            pass

    return {
        "scanned": len(earnings),
        "watchlist_eligible": sum(1 for t, _ in earnings if t in watchlist),
        "queued": len(queued),
        "tasks": queued,
    }


async def run_auto_research_pre_earnings() -> None:
    """Scheduler entry point."""
    try:
        await run()
    except Exception as e:
        logger.exception("auto_research_pre_earnings failed: {}", e)
