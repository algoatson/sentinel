"""Social pulse pipeline per SPEC §7.

Hourly during market hours:
1. Count Reddit mentions per ticker in the last hour.
2. Compute a 7-day rolling baseline (mentions/hour) excluding the current hour.
3. Spike = current_hour > 3 * baseline AND baseline >= 1.0.
4. Exclude tickers with a Filing in the last 6 hours.
5. Call the heavy model with `social_pulse` prompt over the top-5 posts/ticker.
6. Persist SocialPulse rows and post one embed to #pulse.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import discord
import pandas as pd
import pandas_market_calendars as mcal
from loguru import logger
from sqlmodel import func, select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import Filing, RedditMention, SocialPulse, Watchlist
from ..prompts import get_prompt


_NYSE = mcal.get_calendar("XNYS")


def _is_market_open_now() -> bool:
    today = date.today()
    sched = _NYSE.schedule(start_date=today.isoformat(), end_date=today.isoformat())
    if sched.empty:
        return False
    now = pd.Timestamp.now(tz="UTC")
    return sched.iloc[0]["market_open"] <= now <= sched.iloc[0]["market_close"]


async def run_social_pulse() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_social_pulse top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ social pulse error: {e}")
        except Exception:
            pass


async def _run() -> None:
    if not _is_market_open_now():
        logger.info("social pulse: market closed, skipping")
        return

    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    seven_days_ago = now - timedelta(days=7)
    six_hours_ago = now - timedelta(hours=6)

    spikes: list[dict] = []
    with session_scope() as session:
        tickers = [
            r.ticker
            for r in session.exec(
                select(Watchlist).where(Watchlist.ticker.is_not(None))
            ).all()
            if r.ticker
        ]
        # Exclude tickers with recent filings.
        recent_filing_tickers = {
            r.ticker
            for r in session.exec(
                select(Filing).where(Filing.filed_at >= six_hours_ago).where(Filing.ticker.is_not(None))
            ).all()
            if r.ticker
        }

        for ticker in set(tickers):
            if ticker in recent_filing_tickers:
                continue
            current = session.exec(
                select(func.count())
                .select_from(RedditMention)
                .where(RedditMention.ticker == ticker)
                .where(RedditMention.created_at >= one_hour_ago)
            ).one() or 0
            if current < 4:
                continue
            historical = session.exec(
                select(func.count())
                .select_from(RedditMention)
                .where(RedditMention.ticker == ticker)
                .where(RedditMention.created_at >= seven_days_ago)
                .where(RedditMention.created_at < one_hour_ago)
            ).one() or 0
            baseline = float(historical) / (7 * 24 - 1)
            if baseline < 1.0:
                continue
            if current <= 3 * baseline:
                continue

            top_posts = session.exec(
                select(RedditMention)
                .where(RedditMention.ticker == ticker)
                .where(RedditMention.created_at >= one_hour_ago)
                .order_by(RedditMention.score.desc())
                .limit(5)
            ).all()
            spikes.append(
                {
                    "ticker": ticker,
                    "current_hour_mentions": int(current),
                    "baseline_mentions": round(baseline, 2),
                    "top_5_posts": [
                        {"title": p.title, "score": p.score} for p in top_posts
                    ],
                }
            )

    if not spikes:
        logger.info("social pulse: no spikes")
        return

    llm = get_llm()
    tmpl = get_prompt("social_pulse")
    rendered = tmpl.safe_substitute(spike_data_json=json.dumps(spikes))
    text = await asyncio.to_thread(
        llm.complete, rendered, model="heavy", max_tokens=600
    )
    if text == LLM_ERROR_SENTINEL:
        logger.error("social pulse LLM error")
        return

    # Persist one SocialPulse row per spike with the matching summary line.
    summaries_by_ticker: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        head, _, body = line.partition(":")
        summaries_by_ticker[head.strip().upper().lstrip("$")] = body.strip()

    with session_scope() as session:
        for spike in spikes:
            session.add(
                SocialPulse(
                    ticker=spike["ticker"],
                    mention_count=spike["current_hour_mentions"],
                    baseline=spike["baseline_mentions"],
                    ratio=spike["current_hour_mentions"] / max(spike["baseline_mentions"], 0.01),
                    summary=summaries_by_ticker.get(spike["ticker"].upper(), ""),
                    created_at=datetime.now(timezone.utc),
                )
            )

    embed = discord.Embed(
        title=f"📈 Social pulse — {len(spikes)} ticker(s)",
        description=text[:4000],
        color=0x3498DB,
    )
    msg = await discord_client.post_embed(
        settings.DISCORD_PULSE_CHANNEL_ID, embed, importance=2
    )
    if msg is None:
        return
    logger.info("social pulse: posted {} spikes", len(spikes))
    # Best-effort: stamp message_id onto the latest pulse rows.
    with session_scope() as session:
        rows = session.exec(
            select(SocialPulse)
            .order_by(SocialPulse.created_at.desc())
            .limit(len(spikes))
        ).all()
        for r in rows:
            r.message_id = str(msg.id)
            session.add(r)
