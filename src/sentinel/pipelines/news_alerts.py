"""Breaking news alerts.

Runs every 15 minutes. For each fresh tier-1 news item (≤45 min old, from a
curated source list, watchlist ticker OR macro keyword present), asks the
LIGHT model to triage worthy/not-worthy and produce a 2-sentence neutral
context. Worthy items post one embed to #pulse.

Cooldowns:
- Per-ticker: 6h (don't spam alerts for the same name)
- Per-item: alerted_at set after a post — never re-alerts the same NewsItem
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from string import Template

import discord
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import get_llm, parse_json_response
from ..models import NewsItem, PriceContext


def _news_channel() -> int:
    """Dedicated #news channel, falling back to #pulse when unconfigured."""
    return settings.DISCORD_NEWS_CHANNEL_ID or settings.DISCORD_PULSE_CHANNEL_ID


# Curated tier-1 sources. Lower-tier (Google News aggregations, per-ticker
# yfinance, niche feeds) excluded — they're consumed via macro_themes and
# enrichment but don't warrant standalone alerts.
_TIER1_SOURCES = frozenset({
    "rss:cnbc-top",
    "rss:cnbc-markets",
    "rss:marketwatch-top",
    "rss:yahoo-finance",
    "rss:bbc-business",
    "rss:seekingalpha-currents",
})

# Macro keywords that promote an item without a ticker to alert-candidate
# status. Pattern matched on title (case-insensitive, whole-word).
_MACRO_KEYWORDS = [
    "fed", "fomc", "powell", "rate cut", "rate hike", "inflation",
    "cpi", "ppi", "nfp", "jobs report", "gdp",
    "opec", "embargo", "sanction", "tariff",
    "war", "attack", "strike", "missile", "ceasefire",
    "fda approval", "fda rejection",
    "merger", "acquires", "buyout",
    "antitrust", "doj sues", "sec charges",
    "bankrupt", "chapter 11",
]
_MACRO_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _MACRO_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


_TICKER_COOLDOWN = timedelta(hours=6)
_MAX_PER_CYCLE = 4
_LOOKBACK = timedelta(minutes=45)


TRIAGE_PROMPT = Template("""\
NEWS ALERT TRIAGE.

You are deciding whether ONE news headline warrants an immediate Discord
alert to a retail trader.

ALERT-WORTHY (material, time-sensitive, clear market impact):
- M&A announcements / major buyouts
- Surprise executive departures or appointments
- Fed/central-bank surprise statements
- Geopolitical events with energy/defense impact (sanctions, attacks, embargoes)
- Major regulatory actions (FDA approval/rejection, antitrust suits, SEC charges)
- Earnings surprises explicitly mentioned
- Major macro data with surprise component

NOT WORTHY (recurring noise):
- Speculation pieces, analyst price-target notes
- "Market closes mixed" recaps
- Routine corporate PR / partnership announcements
- Opinion / commentary
- Lifestyle / sports / real-estate
- News older than 2h (already priced in)

Think relationally. Don't just restate the headline — connect it to
second-order effects: sector peers, supply chains, rates/FX/commodities,
and the ticker's recent price action when provided.

Output STRICT JSON only. No prose.

This is the user's private paper-trading copilot — a real read is wanted,
not a neutral wire. No disclaimers.

{
  "worthy": true,
  "context": "1-2 sentences: what happened and why it matters.",
  "relation": "1-2 sentences: relational read — who else is exposed, the macro/supply-chain channel, your directional take and whether the move already reflects it.",
  "importance": 1-5 (5=act now, 4=high, 3=notable, 2=context, 1=marginal)
}

Or if not worthy:
{"worthy": false, "context": "", "relation": "", "importance": 1}

INPUT:
title: $title
summary: $summary
source: $source
ticker: $ticker
published: $published
price_context: $price_context
""")


async def run_news_alerts() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_news_alerts top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ news alerts error: {e}")
        except Exception:
            pass


async def _run() -> None:
    now = datetime.now(timezone.utc)
    now_naive = now.replace(tzinfo=None)
    fresh_cutoff = now_naive - _LOOKBACK
    cooldown_cutoff = now_naive - _TICKER_COOLDOWN

    with session_scope() as session:
        candidates = session.exec(
            select(NewsItem)
            .where(NewsItem.alerted_at.is_(None))
            .where(NewsItem.published_at >= fresh_cutoff)
            .where(NewsItem.source.in_(list(_TIER1_SOURCES)))
            .order_by(NewsItem.published_at.desc())
            .limit(40)
        ).all()

        # Filter to ticker-tagged OR macro-keyword items.
        filtered: list[NewsItem] = []
        for item in candidates:
            if item.ticker is not None:
                filtered.append(item)
            elif _MACRO_RE.search(item.title) or _MACRO_RE.search(item.summary or ""):
                filtered.append(item)

        # Per-ticker cooldown — drop candidates if a ticker already alerted recently.
        if filtered:
            recently_alerted_tickers = {
                row.ticker
                for row in session.exec(
                    select(NewsItem)
                    .where(NewsItem.alerted_at.is_not(None))
                    .where(NewsItem.alerted_at >= cooldown_cutoff)
                    .where(NewsItem.ticker.is_not(None))
                ).all()
                if row.ticker
            }
            filtered = [
                f for f in filtered
                if f.ticker is None or f.ticker not in recently_alerted_tickers
            ]

    if not filtered:
        logger.info("news alerts: no candidates")
        return

    # Cap LLM cost per cycle even before triage.
    filtered = filtered[:_MAX_PER_CYCLE * 2]

    llm = get_llm()
    posted = 0
    for item in filtered:
        if posted >= _MAX_PER_CYCLE:
            break

        rendered = TRIAGE_PROMPT.safe_substitute(
            title=item.title[:300],
            summary=(item.summary or "")[:400],
            source=item.source.split(":")[-1],
            ticker=item.ticker or "none",
            published=item.published_at.isoformat(),
            price_context=_price_context_str(item.ticker),
        )
        raw = await asyncio.to_thread(
            llm.complete, rendered, model="light", json_mode=True, max_tokens=300
        )
        parsed = parse_json_response(raw, expect=dict)
        if parsed is None:
            logger.debug("news alerts: triage parse failed for {}", item.id)
            _mark_alerted(item.id)
            continue

        if not parsed.get("worthy"):
            _mark_alerted(item.id)
            continue

        context = str(parsed.get("context", "")).strip()
        if not context:
            _mark_alerted(item.id)
            continue

        relation = str(parsed.get("relation", "")).strip()
        try:
            importance = int(parsed.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        importance = min(5, max(1, importance))

        from ..narrative import is_superseded, record_event

        if item.ticker and is_superseded(
            item.ticker, 1, within=timedelta(minutes=90)
        ):
            record_event(
                item.ticker,
                "news_alert",
                f"{item.title[:140]} (coalesced)",
                tier=1,
            )
            _mark_alerted(item.id)
            continue

        await _post_alert(item, context, relation, importance)
        if item.ticker:
            record_event(
                item.ticker,
                "news_alert",
                item.title[:160],
                tier=1,
                detail=context[:600],
            )
        _mark_alerted(item.id)
        posted += 1

    logger.info(
        "news alerts: triaged {} candidates, posted {}", len(filtered), posted
    )


def _mark_alerted(item_id: int) -> None:
    with session_scope() as session:
        row = session.get(NewsItem, item_id)
        if row is None:
            return
        row.alerted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(row)


def _price_context_str(ticker: str | None) -> str:
    if not ticker:
        return "none"
    with session_scope() as s:
        pc = s.get(PriceContext, ticker)
    if pc is None:
        return "none"
    return (
        f"{ticker} last {pc.last_price:.4g}, "
        f"1d {pc.change_1d_pct * 100:+.1f}%, "
        f"5d {pc.change_5d_pct * 100:+.1f}%, "
        f"vol {pc.volume_vs_20d_avg:.1f}x 20d-avg"
    )


async def _post_alert(
    item: NewsItem, context: str, relation: str = "", importance: int = 3
) -> None:
    ticker_tag = f" ${item.ticker}" if item.ticker else ""
    title = f"🚨 News alert{ticker_tag}"
    desc = f"**{item.title}**\n\n{context}"
    if relation:
        desc += f"\n\n🔗 **Relational read** — {relation}"

    embed = discord.Embed(
        title=title[:256],
        description=desc[:4000],
        url=item.url,
        color=0xE74C3C,
    )
    pc_str = _price_context_str(item.ticker)
    if pc_str != "none":
        embed.add_field(name="Price context", value=pc_str[:1024], inline=False)
    from ..portfolio import is_held

    if is_held(item.ticker):
        embed.add_field(name="📌 Your book", value="touches a holding", inline=True)
    embed.set_footer(
        text=f"{item.source.split(':')[-1]} · {item.published_at.strftime('%Y-%m-%d %H:%M UTC')}"
    )
    from ..routing import channel_for

    await discord_client.post_embed(
        channel_for(item.ticker, equity_default=_news_channel()),
        embed,
        importance=importance,
    )
