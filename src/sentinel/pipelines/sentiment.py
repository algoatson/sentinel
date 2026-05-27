"""Sentiment tagging pipeline per SPEC §7.

Hourly: pulls untagged RedditMention rows from the last 24h in batches of 25,
asks the light model to return one JSON object per row, applies the results.
On parse failure, defaults a row to sentiment=0, is_thesis=False so we don't
retry it indefinitely.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..db import session_scope
from ..llm import get_llm, parse_json_response
from ..models import RedditMention
from ..prompts import get_prompt


# 16 — empirically the largest batch DeepSeek-flash returns reliably
# as a JSON array without dropping trailing items. Doubling from 8
# halves the per-item prompt-overhead (system + JSON wrapper + few-shot
# scaffolding) on a busy day's untagged-mentions backlog. Past 16 the
# model occasionally truncates the array; we leave a small safety
# margin.
_BATCH_SIZE = 16


async def tag_recent_mentions() -> None:
    try:
        # _run does sync LLM calls in a loop. Push to a thread.
        await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("tag_recent_mentions top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ sentiment tagger error: {e}")
        except Exception:
            pass


def _run() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    while True:
        with session_scope() as session:
            batch = session.exec(
                select(RedditMention)
                .where(RedditMention.sentiment.is_(None))
                .where(RedditMention.created_at >= cutoff)
                .limit(_BATCH_SIZE)
            ).all()

        if not batch:
            logger.info("sentiment tagger: no untagged mentions")
            return

        _tag_batch(batch)


def _tag_batch(rows: list[RedditMention]) -> None:
    llm = get_llm()
    tmpl = get_prompt("tag_sentiment")
    numbered = "\n\n".join(
        f"{i + 1}. {r.title}\n{r.body_excerpt}" for i, r in enumerate(rows)
    )
    rendered = tmpl.safe_substitute(numbered_items=numbered)

    # 16-item batches return ~16 small dicts (~40 tokens each = 640).
    # 1000 leaves comfortable headroom for the array wrapper + trailing
    # whitespace + the rare longer-reason cases without truncation.
    # `grounded=False`: this is a pure text classifier — sentiment per
    # post — so the 250-token "today's date + world anchor" preamble
    # adds zero accuracy and a lot of input cost when batched hourly.
    raw = llm.complete(
        rendered, model="light", json_mode=True, max_tokens=1000,
        grounded=False,
    )
    parsed = parse_json_response(raw, expect=list)
    if parsed is None:
        logger.warning("sentiment LLM error or parse failure, marking batch defaults")
        _mark_defaults(rows)
        return

    with session_scope() as session:
        for i, row in enumerate(rows):
            persisted = session.get(RedditMention, row.id)
            if persisted is None:
                continue
            if i >= len(parsed) or not isinstance(parsed[i], dict):
                persisted.sentiment = 0
                persisted.is_thesis = False
            else:
                try:
                    s = int(parsed[i].get("sentiment", 0))
                    persisted.sentiment = s if s in (-1, 0, 1) else 0
                    persisted.is_thesis = bool(parsed[i].get("is_thesis", False))
                except (TypeError, ValueError):
                    persisted.sentiment = 0
                    persisted.is_thesis = False
            session.add(persisted)


def _mark_defaults(rows: list[RedditMention]) -> None:
    with session_scope() as session:
        for row in rows:
            persisted = session.get(RedditMention, row.id)
            if persisted is None:
                continue
            persisted.sentiment = 0
            persisted.is_thesis = False
            session.add(persisted)
