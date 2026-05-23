"""Macro desk — the news→finance reasoning layer.

Every 4h, pulls macro/geopolitical NewsItems from the last 24h and asks the
heavy model to do what a desk does: cluster the news, walk the real
transmission chain to specific names, and commit to a read. Directional
reads are emitted as machine CALL lines and logged via record_call, so the
bot's news-driven ideas are scored exactly like synthesis/why_moved/
convergence (scorecard, call_review, wallet_meta all pick `macro_themes`
up automatically — funds simply don't trade that source unless a policy
opts in, by design).

Noise control kept deliberately: posts only when the headline set has
materially changed (content hash) and only every 4h.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm, parse_calls
from ..models import NewsItem, Watchlist
from ..prompts import get_prompt

# Cooldown via content hash — only post if the headline set has changed.
_LAST_HASH: dict[str, str] = {"value": ""}


def _usable(body: str) -> bool:
    """Lenient sanity gate for the richer prose format: real content, not a
    refusal/sentinel/format-collapse. (The old rigid regex would reject the
    new chain+read prose; we only guard against genuinely broken output.)"""
    if not body or body == LLM_ERROR_SENTINEL:
        return False
    b = body.strip()
    if len(b) < 120:
        return False
    return ("**" in b) or ("Exposed:" in b) or ("CALL:" in b)


def _book_str() -> str:
    """Compact view of the user's actual book so the desk leads with what
    touches it."""
    from ..portfolio import held_tickers, open_positions

    held = sorted(held_tickers())
    pos = open_positions()
    parts = []
    if held:
        parts.append("held: " + " ".join(f"${t}" for t in held[:40]))
    if pos:
        parts.append(
            "positions: "
            + ", ".join(
                f"{p['side'].upper()} ${p['ticker']}"
                + (
                    f" ({p['pnl_pct']:+.1f}%)"
                    if p.get("pnl_pct") is not None
                    else ""
                )
                for p in pos[:20]
            )
        )
    return " | ".join(parts) or "(empty — no positions or holdings)"


async def run_macro_themes() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_macro_themes top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ macro themes error: {e}")
        except Exception:
            pass


async def _run() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    with session_scope() as session:
        items = session.exec(
            select(NewsItem)
            .where(NewsItem.is_macro == True)  # noqa: E712
            .where(NewsItem.published_at >= cutoff)
            .order_by(NewsItem.published_at.desc())
            .limit(28)
        ).all()
        watch_tickers = sorted({
            r.ticker
            for r in session.exec(
                select(Watchlist).where(Watchlist.ticker.is_not(None))
            ).all()
            if r.ticker
        })

    if len(items) < 5:
        logger.info("macro desk: only {} macro items in 24h, skipping", len(items))
        return

    titles_for_hash = sorted({n.title for n in items})
    content_hash = hashlib.sha256(
        "\n".join(titles_for_hash).encode()
    ).hexdigest()
    if content_hash == _LAST_HASH["value"]:
        logger.info("macro desk: unchanged headline set, skipping post")
        return

    headlines = [
        {
            "source": n.source.split(":")[-1],
            "title": n.title,
            "summary": (n.summary or "")[:200],
            "published": n.published_at.isoformat(),
        }
        for n in items
    ]
    book = await asyncio.to_thread(_book_str)
    rendered = get_prompt("macro_themes").safe_substitute(
        headlines_json=json.dumps(headlines)[:13000],
        watchlist_sample=", ".join(watch_tickers[:200]),
        book=book,
    )
    body = await asyncio.to_thread(
        get_llm().complete,
        rendered,
        model="heavy",
        max_tokens=1200,
        fallback_light=True,
    )
    if not _usable(body):
        logger.warning("macro desk: unusable LLM output, not posting")
        return

    # Pull machine CALL lines out, post the prose, log the calls for scoring.
    from ..scorecard import record_call

    clean, calls = parse_calls(body)
    if not _usable(clean):
        clean = body  # everything was CALL lines? keep the raw body

    embed = discord.Embed(
        title="🌍 Macro desk — news → markets",
        description=clean[:4000],
        color=0x16A085,
    )
    news_channel = (
        settings.DISCORD_NEWS_CHANNEL_ID or settings.DISCORD_PULSE_CHANNEL_ID
    )
    if not await discord_client.post_embed(news_channel, embed, importance=3):
        logger.warning("macro desk: post failed, will retry next cycle")
        return

    for c in calls:
        record_call(
            c["ticker"], c["direction"], "macro_themes",
            clean[:400], c["conviction"],
        )
    _LAST_HASH["value"] = content_hash
    logger.info(
        "macro desk: posted ({} chars, {} scored calls, {} items)",
        len(clean), len(calls), len(items),
    )
