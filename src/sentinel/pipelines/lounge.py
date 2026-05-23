"""The Lounge — relaxed, non-signal #general posts, twice a day, gated.

NOT a signal pipeline. It rides data the bot already has (macro news,
today's movers, recent Reddit chatter) and asks the LIGHT model for one
witty, *grounded* aside: a non-consensus geopolitics↔market connection, an
absurd-but-true observation, or a take on the liveliest community post.

Discipline (the whole reason this isn't bloat):
- Light model only — casual tone, no reasoning budget spent.
- Hard gate: the prompt returns `SKIP` when there's nothing real to say, and
  we post nothing. Silence beats filler.
- Continuity: recent lounge posts are fed back so it never repeats an angle
  (same pattern as synthesis self-continuity). Logged under the `__LOUNGE__`
  narrative key.
- Curated community highlight (no raw meme scraping): the featured post is
  picked deterministically here, preferring one whose ticker also moved today
  so the riff stays grounded; its permalink is attached as a field.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger
from sqlmodel import select

from .. import discord_client, ui
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import NewsItem, PriceContext, RedditMention
from ..narrative import record_event, recent_events
from ..prompts import get_prompt

# Narrative key for the bot's own lounge memory (mirrors synthesis __MARKET__).
_LOUNGE = "__LOUNGE__"

# Lively, banter-heavy subs only — where a "featured" post is fun, not noise.
_FUN_SUBS = {
    "wallstreetbets", "Shortsqueeze", "Superstonk", "pennystocks", "SPACs",
    "CryptoCurrency", "CryptoMarkets", "stocks", "StockMarket", "options",
    "Daytrading", "Bitcoin", "ethfinance", "AltStreetBets",
}


def _channel() -> int:
    return settings.DISCORD_GENERAL_CHANNEL_ID or settings.DISCORD_DIGEST_CHANNEL_ID


def _build_snapshot() -> dict | None:
    """Gather the grounding data. Returns None if there's simply nothing to
    talk about (no news, no moves, no chatter) — caller skips silently."""
    now = datetime.now(timezone.utc)
    news_cut = now - timedelta(hours=24)
    chat_cut = now - timedelta(hours=18)

    with session_scope() as s:
        macro = s.exec(
            select(NewsItem)
            .where(NewsItem.is_macro == True)  # noqa: E712
            .where(NewsItem.published_at >= news_cut)
            .order_by(NewsItem.published_at.desc())
            .limit(12)
        ).all()
        movers = s.exec(
            select(PriceContext).order_by(
                PriceContext.last_updated.desc()
            ).limit(120)
        ).all()
        chatter = s.exec(
            select(RedditMention)
            .where(RedditMention.created_at >= chat_cut)
            .order_by(RedditMention.created_at.desc())
            .limit(150)
        ).all()

    macro_lines = [n.title.strip() for n in macro if n.title]
    top_movers = sorted(
        (m for m in movers if m.change_1d_pct is not None),
        key=lambda m: abs(m.change_1d_pct),
        reverse=True,
    )[:8]
    mover_lines = [
        f"${m.ticker} {m.change_1d_pct * 100:+.1f}% (1d)" for m in top_movers
    ]
    mover_set = {m.ticker for m in top_movers}

    # De-dupe chatter by post, keep only lively subs.
    seen: set[str] = set()
    fun: list[RedditMention] = []
    for r in chatter:
        if r.subreddit not in _FUN_SUBS or r.post_id in seen:
            continue
        seen.add(r.post_id)
        fun.append(r)

    if not macro_lines and not mover_lines and not fun:
        return None

    # Featured = a lively post whose ticker also moved today (grounded tie-in),
    # else the most recent lively post.
    featured = next((r for r in fun if r.ticker in mover_set), None)
    if featured is None and fun:
        featured = fun[0]

    featured_str = "(none)"
    if featured is not None:
        featured_str = (
            f"[r/{featured.subreddit}] \"{featured.title.strip()}\" "
            f"(${featured.ticker})"
        )

    others = [
        f"[r/{r.subreddit}] {r.title.strip()}"
        for r in fun
        if featured is None or r.post_id != featured.post_id
    ][:8]

    prev = recent_events(_LOUNGE, days=7, limit=6)
    prev_lines = [f"{e.ts:%m-%d} {e.headline}" for e in prev]

    return {
        "macro_news": "\n".join(f"- {m}" for m in macro_lines) or "(quiet)",
        "movers": "\n".join(f"- {m}" for m in mover_lines) or "(quiet)",
        "featured": featured_str,
        "featured_url": featured.permalink if featured is not None else "",
        "featured_label": (
            f"r/{featured.subreddit}" if featured is not None else ""
        ),
        "community": "\n".join(f"- {c}" for c in others) or "(quiet)",
        "previous_lounge": "\n".join(f"- {p}" for p in prev_lines) or "(none yet)",
    }


async def run_lounge_cycle() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_lounge_cycle top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ lounge error: {e}")
        except Exception:
            pass


async def _run() -> None:
    snap = await asyncio.to_thread(_build_snapshot)
    if snap is None:
        logger.info("lounge: nothing to talk about, skipping")
        return

    tmpl = get_prompt("lounge")
    rendered = tmpl.safe_substitute(
        macro_news=snap["macro_news"],
        movers=snap["movers"],
        featured=snap["featured"],
        community=snap["community"],
        previous_lounge=snap["previous_lounge"],
    )
    body = await asyncio.to_thread(
        get_llm().complete, rendered, model="light", max_tokens=360
    )

    if not body or body == LLM_ERROR_SENTINEL:
        logger.info("lounge: LLM unavailable/empty, skipping")
        return
    if body.strip().upper().startswith("SKIP") or len(body.strip()) < 40:
        logger.info("lounge: model declined (nothing worth saying)")
        return

    from ..utils import highlight_markdown

    embed = discord.Embed(
        title="🛋️ The Lounge",
        description=highlight_markdown(body.strip())[:4000],
        color=ui.ACCENT,
    )
    if snap["featured_url"]:
        embed.add_field(
            name="From the trenches",
            value=f"[{snap['featured_label']}]({snap['featured_url']})",
            inline=False,
        )

    msg = await discord_client.post_embed(_channel(), embed)
    if msg is None:
        return
    first_line = body.strip().splitlines()[0][:140]
    record_event(
        _LOUNGE,
        "lounge",
        first_line,
        tier=1,
        detail=body.strip()[:800],
        channel_id=_channel(),
        message_id=str(msg.id),
    )
    logger.info("lounge: posted ({} chars)", len(body))
