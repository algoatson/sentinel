"""The accountability layer.

Every directional call the bot makes (synthesis / convergence / why_moved /
macro_themes) is logged with the price at the time, marked-to-market at
1d/5d/20d from the PriceBar history, and scored. `!scorecard` shows the
running hit rate, and synthesis reads its own track record back so it can
fade its weak signals.

It is also a CONTROL loop, not just a report: `record_call` mechanically
dampens the conviction of any source measured to have negative edge over a
meaningful sample (`_fade_conviction`). Because every call funnels through
that one chokepoint, a faded conviction propagates automatically to fund
sizing, fund min-conviction gates, the call_review notability bar and
wallet_meta — the bot gets more cautious on what it's measurably bad at,
with no other system needing to know.

A "hit" = the realized move agreed with the called direction. Marking uses
the close nearest the horizon; calls with no price at call time can't be
scored and are retired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlmodel import select

from .db import session_scope
from .models import PriceBar, PriceContext, TradingCall

_HORIZONS = {"ret_1d_pct": 1, "ret_5d_pct": 5, "ret_20d_pct": 20}
_GIVE_UP_DAYS = 25
# A horizon mark is only fair if there's a real bar reasonably close to it.
# Generous enough to absorb weekends/holiday clusters, tight enough that a
# dead feed (gap grows unbounded toward 20d) is decisively rejected — an
# unscoreable call must retire unscored, never get a fabricated grade off a
# weeks-stale close that would poison the hit rate + conviction calibration.
_MARK_STALE_DAYS = 7

# Auto-fade: a call source measured to have negative edge over a meaningful
# sample gets its conviction mechanically dampened at log time. This closes
# the loop — the scorecard stops being a passive report and becomes a control
# signal. Deliberately asymmetric and conservative: it can only REDUCE a
# conviction (never inflate), only acts at >= _FADE_MIN_SAMPLE scored calls
# (never on noise), and floors at 1 rather than suppressing — the source keeps
# being recorded and measured, so the rolling 90d window self-heals if its
# edge recovers (no feedback trap where a faded source can never come back).
_FADE_MIN_SAMPLE = 12


def _fade_conviction(
    source: str, conviction: int, by_source: dict
) -> tuple[int, str | None]:
    """Dampen conviction for a measurably weak source. Pure — the caller
    passes track_record_summary()['by_source']. Returns
    (adjusted_conviction, note|None). Only ever lowers; floors at 1."""
    rec = by_source.get(source)
    if not rec or rec.get("n", 0) < _FADE_MIN_SAMPLE:
        return conviction, None  # not enough scored calls — never act on noise
    n = rec["n"]
    hr = rec["hits"] / n
    if hr >= 0.45:
        return conviction, None  # within noise of break-even — leave it
    # Worse than a coin flip with a real sample → fade, harder the worse it is.
    penalty = 1 if hr >= 0.40 else 2 if hr >= 0.33 else conviction
    adjusted = max(1, conviction - penalty)
    if adjusted >= conviction:
        return conviction, None
    return adjusted, f"faded {source} {rec['hits']}/{n} ({hr * 100:.0f}%)"


def record_call(
    ticker: str, direction: str, source: str, thesis: str, conviction: int = 3
) -> None:
    """Log a directional call with the mark at call time. Best-effort —
    never breaks the posting pipeline."""
    if direction not in ("long", "short") or not ticker:
        return
    ticker = ticker.upper()
    try:
        # Auto-fade at the single chokepoint: a measurably weak source has its
        # conviction dampened here, which then propagates everywhere for free
        # (fund sizing ×conviction/5, fund min_conviction gates, call_review's
        # notable gate, wallet_meta buckets) with no change to those systems.
        by_source = track_record_summary()["by_source"]
        conviction, fade_note = _fade_conviction(source, conviction, by_source)
        thesis_out = thesis[:400]
        if fade_note:
            thesis_out = f"{thesis} · ⚖︎ {fade_note}"[:400]
            logger.info(
                "scorecard: {} {} conv→{} ({})",
                ticker, source, conviction, fade_note,
            )
        with session_scope() as s:
            # De-dup standing ideas: a 6h pipeline re-emitting the same
            # thesis must NOT count as a fresh call (it would inflate the
            # scorecard the octopus self-corrects from). Same
            # ticker+direction+source still maturing → it's the same call.
            dupe = s.exec(
                select(TradingCall)
                .where(TradingCall.ticker == ticker)
                .where(TradingCall.direction == direction)
                .where(TradingCall.source == source)
                .where(TradingCall.settled == False)  # noqa: E712
                .limit(1)
            ).first()
            if dupe is not None:
                return
            pc = s.get(PriceContext, ticker)
            call = TradingCall(
                ticker=ticker.upper(),
                direction=direction,
                conviction=max(1, min(5, conviction)),
                source=source,
                thesis=thesis_out,
                price_at_call=pc.last_price if pc is not None else None,
                created_at=datetime.now(timezone.utc),
            )
            s.add(call)
            s.flush()
            call_id = call.id
        # Broadcast (best-effort, outside the session so we don't hold a tx open).
        try:
            from . import events
            events.publish("call", {
                "id": call_id,
                "ticker": ticker.upper(),
                "direction": direction,
                "conviction": max(1, min(5, conviction)),
                "source": source,
                "thesis": thesis_out[:200],
            })
        except Exception as e:
            logger.debug("events.publish(call) failed: {}", e)
    except Exception as e:
        logger.debug("record_call({}, {}) failed: {}", ticker, source, e)


def _price_bar_asof(
    session, ticker: str, target: datetime
) -> tuple[float, datetime] | None:
    """(close, bar_ts) at/just before `target`, naive-UTC aware. Returns
    None if the nearest prior bar is staler than `_MARK_STALE_DAYS` — a
    far-stale close is not a fair mark for this horizon, so the call
    stays unmarked (and is eventually retired unscored) rather than
    scored off a dead price.

    Returning the bar timestamp lets callers distinguish "horizon has
    actually elapsed in market time" from "we have only the same pre-
    call close available", which the prior `_price_asof` couldn't and
    is the root cause of the closed-market-window 0% ret bug."""
    t = target.replace(tzinfo=None)
    floor = t - timedelta(days=_MARK_STALE_DAYS)
    bar = session.exec(
        select(PriceBar)
        .where(PriceBar.ticker == ticker)
        .where(PriceBar.ts <= t)
        .where(PriceBar.ts >= floor)
        .order_by(PriceBar.ts.desc())
        .limit(1)
    ).first()
    if bar is None:
        return None
    return bar.close, bar.ts


def _price_asof(session, ticker: str, target: datetime) -> float | None:
    """Back-compat scalar form. Use ``_price_bar_asof`` when the caller
    needs to know which bar produced the close."""
    res = _price_bar_asof(session, ticker, target)
    return res[0] if res is not None else None


def mark_open_calls() -> int:
    """Fill in 1d/5d/20d returns as each horizon matures. Returns #updated."""
    now = datetime.now(timezone.utc)
    updated = 0
    with session_scope() as s:
        calls = s.exec(
            select(TradingCall).where(TradingCall.settled == False)  # noqa: E712
        ).all()
        for c in calls:
            created = c.created_at.replace(tzinfo=None)
            if c.price_at_call is None or c.price_at_call == 0:
                # Never scoreable — retire so we stop scanning it.
                c.settled = True
                s.add(c)
                continue
            changed = False
            for field, days in _HORIZONS.items():
                if getattr(c, field) is not None:
                    continue
                horizon = created + timedelta(days=days)
                if now.replace(tzinfo=None) < horizon:
                    continue
                bar = _price_bar_asof(s, c.ticker, horizon)
                if bar is None:
                    continue
                px, bar_ts = bar
                # Closed-market guard: when the horizon falls on a
                # weekend/holiday, the most-recent bar is the same
                # pre-call close that price_at_call captured. Marking
                # a return off that bar always reads 0.00% and
                # silently kills the calibration hit rate. Require the
                # horizon bar to be strictly *after* the call was
                # created so we only score against post-call price
                # action. Skipped horizons retry on the next 2h tick;
                # if no bar ever arrives within _GIVE_UP_DAYS the call
                # retires unscored (the existing settle branch).
                if bar_ts <= created:
                    continue
                setattr(
                    c, field,
                    round((px - c.price_at_call) / c.price_at_call * 100, 2),
                )
                changed = True
            age_days = (now.replace(tzinfo=None) - created).days
            if c.ret_20d_pct is not None or age_days >= _GIVE_UP_DAYS:
                c.settled = True
                changed = True
            if changed:
                c.marked_at = now
                s.add(c)
                updated += 1
    if updated:
        logger.info("scorecard: marked {} calls", updated)
    return updated


def _hit(direction: str, ret: float) -> bool:
    return ret > 0 if direction == "long" else ret < 0


def track_record_summary(*, days: int = 90) -> dict:
    """Hit rate over recently-created, marked calls — overall + by source.
    Uses the 5d return (falls back to 1d) as the scoring horizon."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as s:
        calls = s.exec(
            select(TradingCall).where(TradingCall.created_at >= cutoff)
        ).all()
    overall = [0, 0]  # hits, n
    by_source: dict[str, list[int]] = {}
    by_conv: dict[str, list[int]] = {"low": [0, 0], "med": [0, 0], "high": [0, 0]}
    for c in calls:
        ret = c.ret_5d_pct if c.ret_5d_pct is not None else c.ret_1d_pct
        if ret is None:
            continue
        h = 1 if _hit(c.direction, ret) else 0
        overall[0] += h
        overall[1] += 1
        b = by_source.setdefault(c.source, [0, 0])
        b[0] += h
        b[1] += 1
        bucket = "low" if c.conviction <= 2 else "high" if c.conviction >= 4 else "med"
        by_conv[bucket][0] += h
        by_conv[bucket][1] += 1
    return {
        "overall": {"hits": overall[0], "n": overall[1]},
        "by_source": {
            k: {"hits": v[0], "n": v[1]} for k, v in by_source.items()
        },
        "by_conviction": {
            k: {"hits": v[0], "n": v[1]} for k, v in by_conv.items()
        },
    }


def _calibration_note(by_conv: dict) -> str | None:
    """A flag when stated conviction doesn't track reality (overconfidence)."""
    hi, lo = by_conv.get("high", {}), by_conv.get("low", {})
    if hi.get("n", 0) >= 4 and lo.get("n", 0) >= 4:
        hr = hi["hits"] / hi["n"]
        lr = lo["hits"] / lo["n"]
        if hr + 0.10 < lr:
            return (
                f"OVERCONFIDENT: high-conviction {hi['hits']}/{hi['n']} "
                f"({hr * 100:.0f}%) underperforms low {lo['hits']}/{lo['n']} "
                f"({lr * 100:.0f}%) — your strong calls are your weak ones."
            )
    return None


def track_record_brief() -> str:
    """One-line-per-source summary for the synthesis snapshot."""
    tr = track_record_summary()
    if not tr["overall"]["n"]:
        return "no scored calls yet"
    o = tr["overall"]
    parts = [f"overall {o['hits']}/{o['n']} ({o['hits'] / o['n'] * 100:.0f}%)"]
    for src, v in sorted(tr["by_source"].items()):
        if v["n"]:
            parts.append(f"{src} {v['hits']}/{v['n']}")
    note = _calibration_note(tr.get("by_conviction", {}))
    if note:
        parts.append(note)
    return "; ".join(parts)


def scorecard_text() -> str:
    tr = track_record_summary()
    o = tr["overall"]
    if not o["n"]:
        return (
            "**🎯 Scorecard**\nNo calls scored yet — needs a few days of "
            "marked 1d/5d returns to populate."
        )
    lines = [
        "**🎯 Scorecard** (last 90d, 5d-return basis)",
        f"Overall: **{o['hits']}/{o['n']}** "
        f"({o['hits'] / o['n'] * 100:.0f}% hit rate)",
        "",
    ]
    for src, v in sorted(tr["by_source"].items()):
        if v["n"]:
            lines.append(
                f"`{src}` — {v['hits']}/{v['n']} "
                f"({v['hits'] / v['n'] * 100:.0f}%)"
            )
    bc = tr.get("by_conviction", {})
    conv_bits = [
        f"{k} {bc[k]['hits']}/{bc[k]['n']}"
        for k in ("high", "med", "low")
        if bc.get(k, {}).get("n")
    ]
    if conv_bits:
        lines.append("\nBy conviction: " + " · ".join(conv_bits))
    note = _calibration_note(bc)
    if note:
        lines.append(f"⚠️ {note}")
    with session_scope() as s:
        pending = s.exec(
            select(TradingCall).where(TradingCall.settled == False)  # noqa: E712
        ).all()
    lines.append(f"\n_{len(pending)} calls still maturing._")
    return "\n".join(lines)[:4000]


async def run_mark_calls() -> None:
    """Scheduler entry — marking is pure DB/price math, fast, no LLM."""
    import asyncio

    try:
        await asyncio.to_thread(mark_open_calls)
    except Exception as e:
        logger.exception("run_mark_calls failure: {}", e)
