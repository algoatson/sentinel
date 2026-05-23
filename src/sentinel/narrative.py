"""Narrative memory — the per-ticker story log.

Pipelines call record_event() whenever they post something material. That
gives the system a memory it otherwise lacks: synthesis and thread Q&A read
it back ("how has $X evolved?"), and it doubles as the de-dupe backbone for
story coalescing (a pipeline can ask "did we just post about this ticker?").

Cheap append-only table; reads are bounded by time + limit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlmodel import select

from .db import session_scope
from .models import NarrativeEvent


def record_event(
    ticker: str,
    kind: str,
    headline: str,
    *,
    tier: int = 1,
    detail: Optional[str] = None,
    channel_id: Optional[int] = None,
    message_id: Optional[str] = None,
) -> None:
    if not ticker:
        return
    try:
        with session_scope() as s:
            s.add(
                NarrativeEvent(
                    ticker=ticker.upper(),
                    ts=datetime.now(timezone.utc),
                    kind=kind,
                    tier=tier,
                    headline=headline[:300],
                    detail=(detail or None) and detail[:1200],
                    channel_id=channel_id,
                    message_id=message_id,
                )
            )
    except Exception as e:  # memory is best-effort, never break a post
        logger.debug("record_event({}, {}) failed: {}", ticker, kind, e)


def last_event(ticker: str, *, within: timedelta) -> Optional[NarrativeEvent]:
    """Most recent event for a ticker inside `within` — the coalescing probe."""
    cutoff = datetime.now(timezone.utc) - within
    with session_scope() as s:
        return s.exec(
            select(NarrativeEvent)
            .where(NarrativeEvent.ticker == ticker.upper())
            .where(NarrativeEvent.ts >= cutoff)
            .order_by(NarrativeEvent.ts.desc())
        ).first()


def is_superseded(
    ticker: str, tier: int, *, within: timedelta
) -> Optional[NarrativeEvent]:
    """Story-coalescing probe: return a recent event for this ticker of
    EQUAL-OR-HIGHER tier inside `within`, meaning a same-or-bigger post about
    this name already went out and a fresh full embed would just be noise.
    Returns None when this event is the biggest thing said about the ticker
    lately (so it should post).
    """
    cutoff = datetime.now(timezone.utc) - within
    with session_scope() as s:
        return s.exec(
            select(NarrativeEvent)
            .where(NarrativeEvent.ticker == ticker.upper())
            .where(NarrativeEvent.ts >= cutoff)
            .where(NarrativeEvent.tier >= tier)
            .order_by(NarrativeEvent.ts.desc())
        ).first()


def recent_events(
    ticker: str, *, days: int = 30, limit: int = 15
) -> list[NarrativeEvent]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as s:
        return s.exec(
            select(NarrativeEvent)
            .where(NarrativeEvent.ticker == ticker.upper())
            .where(NarrativeEvent.ts >= cutoff)
            .order_by(NarrativeEvent.ts.desc())
            .limit(limit)
        ).all()


def recent_for_tickers(
    tickers: list[str], *, days: int = 21, per: int = 4
) -> dict[str, list[str]]:
    """Compact timeline strings per ticker, for the synthesis snapshot."""
    out: dict[str, list[str]] = {}
    for t in tickers:
        evs = recent_events(t, days=days, limit=per)
        if evs:
            out[t.upper()] = [
                f"{e.ts:%m-%d} [{e.kind}] {e.headline}" for e in evs
            ]
    return out


def timeline_text(ticker: str, *, days: int = 30) -> str:
    evs = recent_events(ticker, days=days, limit=20)
    if not evs:
        return f"no recorded story for ${ticker.upper()} in the last {days}d."
    lines = [f"**🧵 ${ticker.upper()} — story (last {days}d)**"]
    for e in evs:
        lines.append(f"`{e.ts:%m-%d %H:%M}` **{e.kind}** — {e.headline}")
    return "\n".join(lines)[:2000]
