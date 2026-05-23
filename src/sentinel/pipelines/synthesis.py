"""The synthesis core — the "central octopus".

Every other pipeline looks at one arm: filings, social, price, news. This one
runs every SYNTHESIS_HOURS, pulls a system-wide snapshot across ALL arms and
ALL asset classes (equity / crypto / future / rate), and asks the heavy model
to write one connected narrative — dominant story, cross-arm convergences,
cross-asset divergences, and open questions to watch.

It posts to the #news channel and (via post_embed's default) opens a thread,
so the user can interrogate the brain directly: "why do you think the rates
move is connected to that filing?" — chat.py answers in-thread with the post
as grounding.

Self-continuous: each run reads its own previous reads + how its prior calls
resolved, so it writes an *update* ("the X thesis played out / I was wrong on
Y / here's what's new"), not a cold take. If nothing material changed it says
so in a couple of lines rather than padding — no hash gate.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger
from sqlmodel import func, select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import (
    Filing,
    HnMention,
    NewsItem,
    PriceContext,
    RedditMention,
    SocialPulse,
    TradingCall,
    Watchlist,
)
from ..prompts import get_prompt


# Sentinel "ticker" for the octopus's own narrative log — its memory of
# what it said last time, read back for continuity.
_MARKET = "__MARKET__"

_MOVER_CAPS = {"equity": 8, "crypto": 8, "future": 12, "rate": 10}


async def run_synthesis_cycle() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("synthesis top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ synthesis error: {e}")
        except Exception:
            pass


def _build_snapshot() -> dict:
    from ..portfolio import held_tickers, open_positions

    held = held_tickers()
    positions = open_positions()
    now = datetime.now(timezone.utc)
    cut_24h = now - timedelta(hours=24)

    with session_scope() as s:
        filings = s.exec(
            select(Filing)
            .where(Filing.filed_at >= cut_24h)
            .where(Filing.materiality_score.is_not(None))
            .where(Filing.materiality_score >= 2)
            .order_by(Filing.materiality_score.desc(), Filing.filed_at.desc())
            .limit(15)
        ).all()

        pulses = s.exec(
            select(SocialPulse)
            .where(SocialPulse.created_at >= cut_24h)
            .order_by(SocialPulse.ratio.desc())
            .limit(10)
        ).all()

        # Social buzz: mention volume by ticker across Reddit + HN.
        reddit_counts = dict(
            s.exec(
                select(RedditMention.ticker, func.count(RedditMention.id))
                .where(RedditMention.created_at >= cut_24h)
                .group_by(RedditMention.ticker)
            ).all()
        )
        hn_counts = dict(
            s.exec(
                select(HnMention.ticker, func.count(HnMention.id))
                .where(HnMention.created_at >= cut_24h)
                .group_by(HnMention.ticker)
            ).all()
        )
        buzz_tickers = sorted(
            set(reddit_counts) | set(hn_counts),
            key=lambda t: reddit_counts.get(t, 0) + hn_counts.get(t, 0),
            reverse=True,
        )[:12]
        social_buzz = []
        for t in buzz_tickers:
            subs = s.exec(
                select(RedditMention.subreddit)
                .where(RedditMention.ticker == t)
                .where(RedditMention.created_at >= cut_24h)
                .distinct()
                .limit(4)
            ).all()
            sample = s.exec(
                select(RedditMention.title)
                .where(RedditMention.ticker == t)
                .where(RedditMention.created_at >= cut_24h)
                .order_by(RedditMention.created_at.desc())
                .limit(1)
            ).first()
            social_buzz.append({
                "ticker": t,
                "reddit_24h": reddit_counts.get(t, 0),
                "hn_24h": hn_counts.get(t, 0),
                "subreddits": list(subs),
                "sample": (sample or "")[:140],
            })

        # Price/volume moves split by asset class for cross-asset reasoning.
        rows = s.exec(
            select(PriceContext, Watchlist.asset_class).join(
                Watchlist, Watchlist.ticker == PriceContext.ticker
            )
        ).all()
        by_class: dict[str, dict[str, dict]] = {}
        for pc, asset_class in rows:
            cls = asset_class or "equity"
            bucket = by_class.setdefault(cls, {})
            if pc.ticker in bucket:
                continue
            bucket[pc.ticker] = {
                "ticker": pc.ticker,
                "chg_1d_pct": round((pc.change_1d_pct or 0) * 100, 2),
                "chg_5d_pct": round((pc.change_5d_pct or 0) * 100, 2),
                "vol_x": round(pc.volume_vs_20d_avg or 0, 2),
                "held": pc.ticker in held,
            }
        movers_by_asset_class = {}
        for cls, bucket in by_class.items():
            ranked = sorted(
                bucket.values(),
                key=lambda r: abs(r["chg_1d_pct"]),
                reverse=True,
            )
            movers_by_asset_class[cls] = ranked[: _MOVER_CAPS.get(cls, 8)]

        macro_news = s.exec(
            select(NewsItem)
            .where(NewsItem.is_macro == True)  # noqa: E712
            .where(NewsItem.published_at >= cut_24h)
            .order_by(NewsItem.published_at.desc())
            .limit(20)
        ).all()

        moving_news = s.exec(
            select(NewsItem)
            .where(NewsItem.impact_1d_pct.is_not(None))
            .where(NewsItem.published_at >= cut_24h)
            .order_by(NewsItem.published_at.desc())
            .limit(12)
        ).all()

    for b in social_buzz:
        b["held"] = b["ticker"] in held

    # Crypto microstructure (funding/OI/book) on crypto movers — the context
    # that explains squeeze/flow moves.
    if movers_by_asset_class.get("crypto"):
        from ..ingesters.crypto_micro import micro_for

        for row in movers_by_asset_class["crypto"]:
            mm = micro_for(row["ticker"])
            if mm:
                row["microstructure"] = mm

    # Narrative memory: how the focus names have evolved over recent weeks.
    from ..narrative import recent_for_tickers

    focus = (
        set(held)
        | {f.ticker for f in filings if f.ticker}
        | {b["ticker"] for b in social_buzz}
        | {n.ticker for n in moving_news if n.ticker}
    )
    timelines = recent_for_tickers(sorted(focus)[:15], days=21, per=4)

    from .. import earnings as _earn

    _today = now.date()
    earnings_window = sorted(
        (
            {
                "ticker": t,
                "date": d.isoformat(),
                "in_days": (d - _today).days,
            }
            for t in focus
            if (d := _earn.next_earnings(t)) is not None
            and 0 <= (d - _today).days <= 14
        ),
        key=lambda r: r["in_days"],
    )

    from ..funds import funds_brief, wallet_edge_brief
    from ..scorecard import track_record_brief

    track_record = track_record_brief()
    fund_scoreboard = funds_brief()
    wallet_edge = wallet_edge_brief()

    # Self-continuity: the octopus's own prior reads + how the calls it made
    # since the last read have since resolved.
    from ..narrative import recent_events

    prior = recent_events(_MARKET, days=4, limit=2)
    previous_reads = [
        {
            "age_h": round(
                (now - e.ts.replace(tzinfo=timezone.utc)).total_seconds() / 3600, 1
            ),
            "read": e.headline,
        }
        for e in prior
    ]
    since = prior[0].ts.replace(tzinfo=timezone.utc) if prior else None
    resolved_since_last: list[dict] = []
    if since is not None:
        with session_scope() as s:
            done = s.exec(
                select(TradingCall)
                .where(TradingCall.source == "synthesis")
                .where(TradingCall.created_at >= since)
                .order_by(TradingCall.created_at.desc())
                .limit(12)
            ).all()
        for c in done:
            ret = c.ret_5d_pct if c.ret_5d_pct is not None else c.ret_1d_pct
            if ret is None:
                continue
            hit = (ret > 0) == (c.direction == "long")
            resolved_since_last.append(
                {
                    "ticker": c.ticker,
                    "dir": c.direction,
                    "ret_pct": ret,
                    "verdict": "hit" if hit else "miss",
                }
            )

    return {
        "as_of": now.isoformat(),
        "previous_reads": previous_reads,
        "resolved_since_last": resolved_since_last,
        "your_holdings": sorted(held),
        "your_positions": [
            {
                "ticker": p["ticker"],
                "side": p["side"],
                "pnl_pct": p["pnl_pct"],
            }
            for p in positions
        ],
        "narrative_timeline": timelines,
        "earnings_window": earnings_window,
        "track_record": track_record,
        "fund_scoreboard": fund_scoreboard,
        "wallet_edge": wallet_edge,
        "material_filings": [
            {
                "ticker": f.ticker,
                "form_type": f.form_type,
                "score": f.materiality_score,
                "reason": f.materiality_reason,
                "summary": (f.summary or "")[:240],
                "held": f.ticker in held,
            }
            for f in filings
        ],
        "social_buzz": social_buzz,
        "social_pulses": [
            {"ticker": p.ticker, "ratio": round(p.ratio, 2), "summary": p.summary}
            for p in pulses
        ],
        "movers_by_asset_class": movers_by_asset_class,
        "macro_news": [
            {"source": n.source.split(":")[-1], "title": n.title}
            for n in macro_news
        ],
        "market_moving_news": [
            {
                "ticker": n.ticker,
                "title": n.title,
                "impact_1h_pct": (
                    round(n.impact_1h_pct * 100, 2)
                    if n.impact_1h_pct is not None
                    else None
                ),
                "impact_1d_pct": (
                    round(n.impact_1d_pct * 100, 2)
                    if n.impact_1d_pct is not None
                    else None
                ),
                "held": n.ticker in held,
            }
            for n in moving_news
        ],
    }


def _has_substance(snap: dict) -> bool:
    return bool(
        snap["material_filings"]
        or snap["social_buzz"]
        or snap["social_pulses"]
        or snap["market_moving_news"]
        or any(snap["movers_by_asset_class"].values())
    )


async def _run() -> None:
    snapshot = await asyncio.to_thread(_build_snapshot)

    if not _has_substance(snapshot):
        logger.info("synthesis: nothing substantive across arms, skipping")
        return

    # No hash gate: the model has its previous reads and is told to keep it
    # short when nothing changed — that's better dedup than byte-equality.
    llm = get_llm()
    tmpl = get_prompt("synthesis")
    rendered = tmpl.safe_substitute(
        snapshot_json=json.dumps(snapshot, default=str)
    )
    body = await asyncio.to_thread(
        llm.complete, rendered, model="heavy", max_tokens=2000,
        fallback_light=True,
    )
    if not body or body == LLM_ERROR_SENTINEL:
        logger.error("synthesis: LLM error")
        return

    from ..llm import parse_calls, parse_trailing_importance
    from ..scorecard import record_call

    body, calls = parse_calls(body)
    body, level, why = parse_trailing_importance(body)
    for c in calls:
        record_call(
            c["ticker"], c["direction"], "synthesis",
            body[:400], c["conviction"],
        )

    embed = discord.Embed(
        title="🐙 Synthesis — the connected picture",
        description=body[:4000],
        color=0x6C5CE7,
    )
    embed.set_footer(text=f"system-wide read · {snapshot['as_of'][:16]}Z")

    channel = settings.DISCORD_NEWS_CHANNEL_ID or settings.DISCORD_PULSE_CHANNEL_ID
    if await discord_client.post_embed(
        channel, embed, importance=level or 3, importance_note=why
    ):
        # Remember this read so the next run can update against it.
        from ..narrative import record_event

        headline = " ".join(body.split())[:240]
        record_event(
            _MARKET, "synthesis", headline, tier=2, detail=body[:1200]
        )
        logger.info("synthesis: posted ({} chars)", len(body))
