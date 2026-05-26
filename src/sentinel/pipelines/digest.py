"""Daily digest pipeline per SPEC §7.

At DIGEST_HOUR_ET:DIGEST_MINUTE_ET on trading days, pulls today's high-
materiality filings + insider activity + social pulses and asks the heavy
model for a 350–450 word summary.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import discord
import pandas_market_calendars as mcal
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import Filing, SocialPulse
from ..prompts import get_prompt


_ET = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("XNYS")
_INSIDER_FORMS = ("4", "4/A", "13F-HR", "13F-HR/A")


def _today_window_utc() -> tuple[datetime, datetime]:
    today_et = datetime.now(_ET).date()
    start = datetime.combine(today_et, time.min, tzinfo=_ET).astimezone(timezone.utc)
    end = datetime.combine(today_et, time.max, tzinfo=_ET).astimezone(timezone.utc)
    return start, end


def _is_trading_day(d: date) -> bool:
    sched = _NYSE.schedule(start_date=d.isoformat(), end_date=d.isoformat())
    return not sched.empty


async def write_daily_digest() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("write_daily_digest top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ daily digest error: {e}")
        except Exception:
            pass


async def _run() -> None:
    today_et = datetime.now(_ET).date()
    if not _is_trading_day(today_et):
        logger.info("digest: not a trading day, skipping")
        return

    start, end = _today_window_utc()

    with session_scope() as session:
        mat_3 = session.exec(
            select(Filing)
            .where(Filing.filed_at >= start, Filing.filed_at <= end)
            .where(Filing.materiality_score == 3)
            .order_by(Filing.filed_at.desc())
        ).all()
        mat_2 = session.exec(
            select(Filing)
            .where(Filing.filed_at >= start, Filing.filed_at <= end)
            .where(Filing.materiality_score == 2)
            .order_by(Filing.filed_at.desc())
        ).all()
        insiders = session.exec(
            select(Filing)
            .where(Filing.filed_at >= start, Filing.filed_at <= end)
            .where(Filing.form_type.in_(_INSIDER_FORMS))
            .where(Filing.materiality_score >= 2)
        ).all()
        pulses = session.exec(
            select(SocialPulse)
            .where(SocialPulse.created_at >= start, SocialPulse.created_at <= end)
        ).all()

    if not (mat_3 or mat_2 or insiders or pulses):
        logger.info("digest: no material activity today, skipping")
        return

    payload = {
        "date": today_et.isoformat(),
        "filings_materiality_3": [_filing_dict(f) for f in mat_3],
        "filings_materiality_2": [_filing_dict(f) for f in mat_2],
        "insider_activity": [
            {"ticker": f.ticker, "summary": f.summary} for f in insiders
        ],
        "social_pulses": [
            {"ticker": p.ticker, "summary": p.summary} for p in pulses
        ],
    }

    llm = get_llm()
    tmpl = get_prompt("daily_digest")
    rendered = tmpl.safe_substitute(input_json=json.dumps(payload, default=str))
    body = await asyncio.to_thread(
        # Digest output runs ~700-900 tokens of dense markdown. 1500
        # was paying for slack we never used; 1000 still has comfortable
        # headroom and runs only once a day so the overall impact is
        # modest — kept for hygiene.
        llm.complete, rendered, model="heavy", max_tokens=1000
    )
    if body == LLM_ERROR_SENTINEL:
        logger.error("digest: LLM error")
        return

    embed = discord.Embed(
        title=f"Daily Digest — {today_et.isoformat()}",
        description=body[:4000],
        color=0x34495E,
    )
    if await discord_client.post_embed(
        settings.DISCORD_DIGEST_CHANNEL_ID, embed, importance=3
    ):
        logger.info("digest posted ({} chars)", len(body))


def _filing_dict(f: Filing) -> dict:
    return {
        "ticker": f.ticker,
        "form_type": f.form_type,
        "summary": (f.summary or "")[:400],
        "reason": f.materiality_reason,
    }
