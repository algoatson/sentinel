"""Dedicated Reddit-stream channel — curated, not firehosed.

The reddit ingester stores every watchlist-ticker'd post, but that match is
noisy: it's a substring/symbol match, so "I'm OPEN to ideas" gets tagged
$OPEN, "use AI" gets tagged an AI stock, etc. The old version then *surfaced*
any such post whenever its (often spurious) ticker happened to move — which
is exactly the "unrelated posts" noise.

New shape: cheap heuristics do **recall only** (bounded candidate set), and a
light LLM is the **quality + relevance authority** — same pattern as
social_pulse / lounge. The model throws out coincidental ticker matches and
low-effort noise, keeps only genuinely important / interesting / funny / hype
posts, and writes the one-line hook that says *why it's here*.

Discipline:
- Opt-in: skips unless DISCORD_REDDIT_CHANNEL_ID is set.
- Bounded: only un-posted rows from the last 6h, top _LLM_BUDGET candidates
  by a cheap prior — the LLM never sees a flood.
- Silence > noise: LLM unavailable / invalid JSON → post nothing this cycle
  (don't fall back to the noisy heuristic — that's the bug we're fixing).
- Judged once: every candidate the LLM evaluated is stamped (kept or not),
  so it's never re-curated; idempotent, restart-safe.
- Capped: ≤ _MAX_KEEP cards/cycle, one per ticker.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger
from sqlmodel import select

from .. import discord_client, ui
from ..config import settings
from ..db import session_scope
from ..ingesters.reddit import direct_blocked, fetch_top_comments
from ..llm import get_llm, parse_json_response
from ..models import PriceContext, RedditMention
from ..prompts import get_prompt

_LOOKBACK_HOURS = 6       # only consider posts newer than this
_SURGE_WINDOW_HOURS = 18  # window for the "community is surging" count
_SURGE_POSTS = 4          # distinct posts about a ticker → show a buzz badge
_LLM_BUDGET = 24          # max candidates handed to the model per cycle
# The answers are often the signal. Enrich only the top-prior candidates
# (the realistic keep set) with their top replies — bounded so we never
# hammer Reddit's keyless JSON.
_COMMENT_FETCH_MAX = 16
_MAX_KEEP = 5             # hard cap on cards actually posted per cycle


def _candidates(session, now: datetime) -> list[dict]:
    """Bounded recall: one entry per un-posted thread in the last 6h, ranked
    by a cheap prior (move + buzz + has-body), truncated to _LLM_BUDGET. This
    only *narrows* — it deliberately does NOT decide quality/relevance; the
    LLM does. Pure: stamps nothing."""
    lookback = now - timedelta(hours=_LOOKBACK_HOURS)
    surge_cut = now - timedelta(hours=_SURGE_WINDOW_HOURS)

    rows = session.exec(
        select(RedditMention)
        .where(RedditMention.posted_at.is_(None))
        .where(RedditMention.created_at >= lookback)
        .order_by(RedditMention.created_at.desc())
    ).all()
    if not rows:
        return []

    # Collapse rows → one candidate per thread (a post can match >1 ticker).
    threads: dict[str, dict] = {}
    for r in rows:
        c = threads.get(r.post_id)
        if c is None:
            threads[r.post_id] = {
                "post_id": r.post_id,
                "subreddit": r.subreddit,
                "title": r.title,
                "body_excerpt": r.body_excerpt,
                "permalink": r.permalink,
                "created_at": r.created_at,
                "tickers": [r.ticker],
            }
        elif r.ticker not in c["tickers"]:
            c["tickers"].append(r.ticker)

    all_tickers = {t for c in threads.values() for t in c["tickers"]}
    moves: dict[str, float] = {}
    for pc in session.exec(
        select(PriceContext).where(PriceContext.ticker.in_(sorted(all_tickers)))
    ).all():
        if pc.change_1d_pct is not None:
            # PriceContext.change_1d_pct is a FRACTION (0.05 = 5%) despite the
            # name — normalise to true percent here so the card, the ranking
            # prior, and the curator prompt all see "+5.0%", not "+0.1%".
            moves[pc.ticker] = pc.change_1d_pct * 100

    surge: dict[str, int] = {}
    for t in all_tickers:
        ids = session.exec(
            select(RedditMention.post_id)
            .where(RedditMention.ticker == t)
            .where(RedditMention.created_at >= surge_cut)
        ).all()
        surge[t] = len(set(ids))

    for c in threads.values():
        lead = max(
            c["tickers"],
            key=lambda t: (abs(moves.get(t, 0.0)), surge.get(t, 0)),
        )
        c["lead"] = lead
        c["lead_move"] = moves.get(lead)
        c["surge_n"] = surge.get(lead, 0)
        c["_prior"] = (
            abs(moves.get(lead, 0.0))
            + 1.5 * surge.get(lead, 0)
            + (1.0 if (c["body_excerpt"] or "").strip() else 0.0)
        )

    ranked = sorted(
        threads.values(), key=lambda c: c["_prior"], reverse=True
    )
    return ranked[:_LLM_BUDGET]


def _enrich_comments(cands: list[dict]) -> None:
    """Lazily attach top replies to the highest-prior candidates so the
    curator can judge the *discussion*, not just the post. No-op while the
    Reddit breaker is open (never deepen a block). Runs in the worker thread
    — blocking politeness sleeps are fine here."""
    if direct_blocked():
        return
    for c in cands[:_COMMENT_FETCH_MAX]:
        c["top_comments"] = fetch_top_comments(c["permalink"])
        time.sleep(0.25 + random.uniform(0, 0.4))


def _render_candidates(cands: list[dict]) -> str:
    """Numbered block for the curator prompt (1-based, matches `i`)."""
    lines: list[str] = []
    for i, c in enumerate(cands, 1):
        mv = c.get("lead_move")
        mv_s = f"{mv:+.1f}% 1d" if mv is not None else "no px"
        extra = len(c["tickers"]) - 1
        tick = f"${c['lead']}" + (f" (+{extra} more)" if extra else "")
        buzz = f" · {c['surge_n']} posts/18h" if c["surge_n"] >= 2 else ""
        title = c["title"].strip()[:180]
        body = (c["body_excerpt"] or "").strip()[:220]
        block = f'{i}. {tick} {mv_s}{buzz} · r/{c["subreddit"]}\n   "{title}"'
        if body:
            block += f"\n   {body}"
        for cm in (c.get("top_comments") or [])[:3]:
            block += f"\n   ↳ {cm[:200]}"
        lines.append(block)
    return "\n".join(lines)


def _apply_curation(
    cands: list[dict], picks, *, max_keep: int
) -> list[dict]:
    """Pure mapper: LLM verdict (parsed JSON list) → final cards. Validates
    every index/category defensively (never trust the model blindly), keeps
    LLM order (best first), one card per lead ticker, capped."""
    if not isinstance(picks, list):
        return []
    out: list[dict] = []
    seen_tickers: set[str] = set()
    for p in picks:
        if not isinstance(p, dict):
            continue
        try:
            idx = int(p.get("i"))
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= len(cands)):
            continue  # hallucinated index — drop, never fabricate
        c = cands[idx - 1]
        if c["lead"] in seen_tickers:
            continue
        cat = str(p.get("category", "")).strip().lower()
        if cat not in ui.REDDIT_CATEGORIES:
            cat = "interesting"  # safe default; still a real bucket
        hook = str(p.get("hook", "")).strip()[:140]
        if not hook:
            continue  # the hook IS the value-add; no hook → not worth a card
        seen_tickers.add(c["lead"])
        out.append({**c, "category": cat, "hook": hook})
        if len(out) >= max_keep:
            break
    return out


def _card_embed(card: dict) -> discord.Embed:
    emoji, color = ui.REDDIT_CATEGORIES.get(card["category"], ui.REDDIT_FALLBACK)
    title = card["title"].strip()
    embed = discord.Embed(
        title=title[:240] + ("…" if len(title) > 240 else ""),
        url=card["permalink"],
        color=color,
    )
    embed.set_author(
        name=f"{emoji}  {card['category'].title()} · r/{card['subreddit']}"
    )
    desc = f"**{card['hook']}**"
    body = (card.get("body_excerpt") or "").strip()
    if body:
        desc += f"\n\n{body[:480]}" + ("…" if len(body) > 480 else "")
    embed.description = desc[:4000]

    mv = card.get("lead_move")
    mv_s = f" · {mv:+.1f}% 1d" if mv is not None else ""
    embed.add_field(name="Ticker", value=f"`${card['lead']}`{mv_s}", inline=True)
    if card.get("surge_n", 0) >= _SURGE_POSTS:
        embed.add_field(
            name="Buzz", value=f"🔥 {card['surge_n']} posts/18h", inline=True
        )
    tc = card.get("top_comments") or []
    if tc:
        embed.add_field(
            name="💬 Top reply", value=tc[0][:1000], inline=False
        )
    return embed


def _stamp_posted(post_ids: list[str]) -> None:
    """Mark every row of each thread so it is never re-curated/re-posted."""
    if not post_ids:
        return
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        for r in s.exec(
            select(RedditMention).where(RedditMention.post_id.in_(post_ids))
        ).all():
            r.posted_at = now
            s.add(r)


async def run_reddit_feed() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_reddit_feed top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ reddit_feed error: {e}")
        except Exception:
            pass


async def _run() -> None:
    chan = settings.DISCORD_REDDIT_CHANNEL_ID
    if not chan:
        logger.debug("reddit_feed: DISCORD_REDDIT_CHANNEL_ID unset, skipping")
        return

    def _pick() -> list[dict]:
        with session_scope() as s:
            cands = _candidates(s, datetime.now(timezone.utc))
        _enrich_comments(cands)  # network I/O — kept out of the pure selector
        return cands

    cands = await asyncio.to_thread(_pick)
    if not cands:
        logger.info("reddit_feed: no candidates, skipping")
        return

    prompt = get_prompt("reddit_curate").safe_substitute(
        max_keep=_MAX_KEEP, candidates=_render_candidates(cands)
    )
    raw = await asyncio.to_thread(
        # Reddit-pick selector is a structured JSON ranker — title +
        # ticker in, top-N picks out. Grounding preamble doesn't
        # change the ranking, so we skip the 250-token overhead.
        get_llm().complete, prompt, model="light", json_mode=True,
        max_tokens=600, grounded=False,
    )
    picks = parse_json_response(raw, expect=list)
    if picks is None:
        # LLM down / unparseable. Post nothing, stamp nothing — retry next
        # cycle. A silent channel beats reintroducing the noise.
        logger.info("reddit_feed: curator unavailable/invalid, skipping cycle")
        return

    cards = _apply_curation(cands, picks, max_keep=_MAX_KEEP)
    posted: list[str] = []
    for card in cards:
        msg = await discord_client.post_embed(chan, _card_embed(card))
        if msg is not None:
            posted.append(card["post_id"])
            await asyncio.to_thread(_stamp_posted, [card["post_id"]])

    # The curator judged every candidate this cycle. Stamp the ones it
    # declined too, so they're never re-sent to the model (cost + they were
    # already ruled on). Only reached on a valid verdict (picks is not None).
    rejected = [c["post_id"] for c in cands if c["post_id"] not in posted]
    await asyncio.to_thread(_stamp_posted, rejected)

    logger.info(
        "reddit_feed: curated {}/{} candidates → posted {}",
        len(cards), len(cands), len(posted),
    )
