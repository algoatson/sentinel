"""Mover-driven discovery pipeline.

Two roles:

1. Surface watchlist tickers with notable price/volume moves and no
   corresponding recent filing — generate an LLM hypothesis from available
   context (filings + HN + Reddit + price) and post to #pulse.

2. Wider-universe discovery: pull yfinance's day-gainers list, find any
   ticker not on the watchlist that's gaining strongly, resolve to CIK
   and activity-promote for 30d so subsequent cycles cover it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from string import Template

import discord
import yfinance as yf
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..edgar.client import EdgarClient
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import Filing, HnMention, PriceContext, RedditMention, Watchlist


_MIN_ABS_PCT = 0.05  # 5% absolute move triggers surfacing
_MIN_VOL_RATIO = 2.0
_MAX_SURFACE = 8


MOVER_HYPOTHESIS_PROMPT = Template("""\
The following tickers had unusual price/volume action today with no obvious
SEC filing trigger. For each, write ONE sentence stating what's plausibly
going on based on the recent social/HN/news signal provided. If the signal
is too thin to support any hypothesis, output: "$$TICKER: signal too thin."

Be skeptical and don't invent catalysts — but if the signal supports a
directional read, say it; don't hedge it away.

Output one line per ticker:
$$TICKER: <one sentence>

Data (JSON):
$mover_json
""")


async def run_movers_cycle() -> None:
    try:
        await _run_watchlist_movers()
        # yfinance.screen() + ticker map fetch are sync — push to thread.
        await asyncio.to_thread(_run_universe_discovery)
    except Exception as e:
        logger.exception("run_movers_cycle top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ movers cycle error: {e}")
        except Exception:
            pass


async def _run_watchlist_movers() -> None:
    now = datetime.now(timezone.utc)
    six_h = now - timedelta(hours=6)
    one_d = now - timedelta(hours=24)

    movers: list[dict] = []
    with session_scope() as session:
        contexts = session.exec(select(PriceContext)).all()
        for pc in contexts:
            abs_pct = abs(pc.change_1d_pct or 0.0)
            vol = pc.volume_vs_20d_avg or 0.0
            if abs_pct < _MIN_ABS_PCT and vol < _MIN_VOL_RATIO:
                continue

            # Skip if a recent filing already explains the move.
            had_filing = session.exec(
                select(Filing)
                .where(Filing.ticker == pc.ticker)
                .where(Filing.filed_at >= six_h)
            ).first()
            if had_filing is not None:
                continue

            hn_titles = [
                h.title
                for h in session.exec(
                    select(HnMention)
                    .where(HnMention.ticker == pc.ticker)
                    .where(HnMention.created_at >= one_d)
                    .order_by(HnMention.points.desc())
                    .limit(3)
                ).all()
            ]
            reddit_titles = [
                r.title
                for r in session.exec(
                    select(RedditMention)
                    .where(RedditMention.ticker == pc.ticker)
                    .where(RedditMention.created_at >= one_d)
                    .order_by(RedditMention.score.desc())
                    .limit(3)
                ).all()
            ]

            movers.append(
                {
                    "ticker": pc.ticker,
                    "change_1d_pct": round((pc.change_1d_pct or 0) * 100, 2),
                    "change_5d_pct": round((pc.change_5d_pct or 0) * 100, 2),
                    "volume_ratio": round(vol, 2),
                    "hn_titles": hn_titles,
                    "reddit_titles": reddit_titles,
                }
            )

    if not movers:
        logger.info("movers: nothing notable")
        return

    movers.sort(
        key=lambda m: (abs(m["change_1d_pct"]) + m["volume_ratio"] * 5),
        reverse=True,
    )
    movers = movers[:_MAX_SURFACE]

    llm = get_llm()
    rendered = MOVER_HYPOTHESIS_PROMPT.safe_substitute(
        mover_json=json.dumps(movers, default=str)
    )
    body = await asyncio.to_thread(
        llm.complete, rendered, model="heavy", max_tokens=800
    )
    if body == LLM_ERROR_SENTINEL:
        logger.error("movers: LLM error")
        return

    embed = discord.Embed(
        title=f"📈 Movers — {len(movers)} ticker(s) without filing trigger",
        description=body[:4000],
        color=0xE67E22,
    )
    if await discord_client.post_embed(
        settings.DISCORD_PULSE_CHANNEL_ID, embed, importance=3
    ):
        logger.info("movers: posted {} entries", len(movers))


def _run_universe_discovery() -> None:
    """Wider-universe discovery via yfinance day_gainers — promote unseen
    high-momentum tickers to the watchlist for 30d so they enter the loop.
    """
    try:
        gainers = yf.screen("day_gainers", count=25)
    except Exception as e:
        logger.debug("yfinance day_gainers unavailable: {}", e)
        return

    quotes = (gainers or {}).get("quotes") or []
    if not quotes:
        return

    edgar = EdgarClient()
    try:
        ticker_map = edgar.get_ticker_to_cik_map()
    except Exception as e:
        logger.warning("ticker map fetch failed during discovery: {}", e)
        return

    now = datetime.now(timezone.utc)
    promoted = 0
    with session_scope() as session:
        for q in quotes:
            sym = str(q.get("symbol") or "").upper()
            if not sym:
                continue
            existing = session.exec(
                select(Watchlist).where(Watchlist.ticker == sym)
            ).first()
            if existing is not None:
                continue
            cik = ticker_map.get(sym)
            if not cik:
                continue
            session.add(
                Watchlist(
                    cik=cik,
                    ticker=sym,
                    source="activity",
                    added_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )
            promoted += 1

    if promoted:
        logger.info("movers discovery: promoted {} new tickers from day_gainers", promoted)
