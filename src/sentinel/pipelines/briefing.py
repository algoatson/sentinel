"""Pre-market briefing pipeline.

Runs at 08:30 ET on trading days. Summarizes overnight activity (filings
posted after market close yesterday and so far this morning) plus any
HN/Reddit chatter since yesterday's close.

Posts to #digest. Separate from the end-of-day digest by time + framing
(forward-looking "what to watch" vs. backward-looking "what happened").
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from string import Template
from zoneinfo import ZoneInfo

import discord
import pandas_market_calendars as mcal
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import Filing, HnMention, PriceContext, SocialPulse


_ET = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("XNYS")


BRIEFING_PROMPT = Template("""\
You are writing a pre-market briefing for a retail trader. Today's date: $today.

Inputs (JSON):
$payload_json

You're the user's private paper-trading copilot. Write a 240-360 word
pre-market brief that actually takes positions, not a neutral wire.

1. The dominant overnight story and what it means for today's tape.
2. 2-4 short paragraphs by theme (after-hours filings, insider clusters,
   HN/Reddit chatter, sector rotation, crypto/rates). Use $TICKER form.
3. "Game plan today" — 2-4 specific names with a direction/lean, the
   trigger or level, and what invalidates it. Commit to a view.

Rules:
- No bullet points. No hedging filler. State conviction; put risk in a
  clause, don't disclaim or refuse.
- If the night was genuinely quiet, say so in ~100 words and still give the
  one thing worth watching.

End with EXACTLY this final line, nothing after:
IMPORTANCE: <1-5> — <≤10-word reason>
(5 = act now; 4 = high; 3 = notable; 2 = context; 1 = quiet)
""")


def _is_trading_day(d: date) -> bool:
    sched = _NYSE.schedule(start_date=d.isoformat(), end_date=d.isoformat())
    return not sched.empty


def _overnight_window_utc() -> tuple[datetime, datetime]:
    """Yesterday 16:00 ET → today 08:30 ET, in UTC."""
    today_et = datetime.now(_ET).date()
    yesterday_close = datetime.combine(
        today_et - timedelta(days=1), time(16, 0), tzinfo=_ET
    )
    now_et = datetime.now(_ET)
    return yesterday_close.astimezone(timezone.utc), now_et.astimezone(timezone.utc)


async def run_premarket_briefing() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_premarket_briefing top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ briefing error: {e}")
        except Exception:
            pass


async def _run() -> None:
    today_et = datetime.now(_ET).date()
    if not _is_trading_day(today_et):
        logger.info("briefing: not a trading day, skipping")
        return

    start, end = _overnight_window_utc()
    with session_scope() as session:
        filings = session.exec(
            select(Filing)
            .where(Filing.filed_at >= start, Filing.filed_at <= end)
            .where(Filing.materiality_score.is_not(None))
            .order_by(Filing.materiality_score.desc(), Filing.filed_at.desc())
            .limit(20)
        ).all()
        hn = session.exec(
            select(HnMention)
            .where(HnMention.created_at >= start)
            .order_by(HnMention.points.desc())
            .limit(10)
        ).all()
        pulses = session.exec(
            select(SocialPulse)
            .where(SocialPulse.created_at >= start)
            .order_by(SocialPulse.created_at.desc())
        ).all()
        # Pre-market biggest gainers/losers from price context.
        movers = session.exec(
            select(PriceContext)
            .order_by(PriceContext.last_updated.desc())
            .limit(50)
        ).all()
        top_movers = sorted(
            movers,
            key=lambda p: abs(p.change_1d_pct or 0),
            reverse=True,
        )[:8]

    payload = {
        "filings": [
            {
                "ticker": f.ticker,
                "form_type": f.form_type,
                "score": f.materiality_score,
                "summary": (f.summary or "")[:300],
            }
            for f in filings
        ],
        "hn_top": [{"ticker": h.ticker, "title": h.title} for h in hn],
        "social_pulses": [{"ticker": p.ticker, "summary": p.summary} for p in pulses],
        "top_movers": [
            {
                "ticker": p.ticker,
                "change_1d_pct": round((p.change_1d_pct or 0) * 100, 2),
                "volume_ratio": round(p.volume_vs_20d_avg or 0, 2),
            }
            for p in top_movers
        ],
    }

    # If everything is empty, post a "quiet night" note rather than nothing.
    if not (payload["filings"] or payload["hn_top"] or payload["social_pulses"]):
        logger.info("briefing: quiet overnight, sending quiet note")
        payload["note"] = "quiet"

    llm = get_llm()
    rendered = BRIEFING_PROMPT.safe_substitute(
        today=today_et.isoformat(),
        payload_json=json.dumps(payload, default=str),
    )
    body = await asyncio.to_thread(
        llm.complete, rendered, model="heavy", max_tokens=1000
    )
    if body == LLM_ERROR_SENTINEL:
        logger.error("briefing: LLM error")
        return

    from ..llm import parse_trailing_importance

    body, level, why = parse_trailing_importance(body)
    embed = discord.Embed(
        title=f"🌅 Pre-market briefing — {today_et.isoformat()}",
        description=body[:4000],
        color=0xF39C12,
    )
    if await discord_client.post_embed(
        settings.DISCORD_DIGEST_CHANNEL_ID,
        embed,
        importance=level or 3,
        importance_note=why,
    ):
        logger.info("briefing posted ({} chars)", len(body))
