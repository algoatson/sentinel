"""Why-did-it-move — reverse-causality explainer.

The inverse of news_impact: instead of "this news → that move", it watches
for an unexplained move/volume spike on any watchlist asset and back-fills
the likely cause by scanning the filings/news/social in the window.

It explains what already happened from the recorded evidence, then commits
to a forward read (continuation / fade / wait) with conviction — that read
is logged as a TradingCall and scored. When the data has no catalyst it says
so plainly rather than invent one.

Detection is cheap (PriceContext scan); the LLM only fires on the few biggest
fresh moves per cycle (light model, capped). In-memory cooldown keyed by
ticker; a move is only re-explained if it has materially changed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from string import Template

import discord
from loguru import logger
from sqlmodel import func, select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import Filing, HnMention, NewsItem, PriceContext, RedditMention, Watchlist
from ..portfolio import is_held


# Move thresholds (abs 1d %) by asset class — crypto is noisier so the bar
# is higher. Volume ≥ this ×20d-avg triggers regardless of price.
_PCT_THRESHOLD = {"equity": 0.07, "crypto": 0.15, "future": 0.04, "rate": 0.05}
_VOL_THRESHOLD = 3.0
# Heavy model is the reasoning model but slow on CPU; keep the per-cycle cap
# tight so a cycle can't run for an hour.
_MAX_PER_CYCLE = 2
_COOLDOWN = timedelta(hours=8)
# Re-explain only if the 1d move shifted by ≥ this since last explained.
_RE_EXPLAIN_DELTA = 0.04

# ticker -> (explained_change_1d_pct, ts)
_RECENT: dict[str, tuple[float, datetime]] = {}


WHY_PROMPT = Template("""\
An asset moved sharply. You're the user's private paper-trading copilot:
explain the likely cause AND give a forward read.

- Lead with the most probable driver (use the evidence; extend with market
  knowledge where it's thin). If there's no clear catalyst, say so and call
  it macro/sector/flow — don't invent a story.
- Then your read: is this the start of something or noise/exhaustion? A
  lean (continuation / fade / wait), the level or event that confirms or
  kills it. Conviction with the risk stated in a clause — no disclaimers.
- 3-5 sentences. $$TICKER form.

If your forward read is directional, emit one machine line (logged & scored
— only if you'd stand behind it; omit otherwise):
CALL: $$TICKER LONG|SHORT <conviction 1-5>

Then end with EXACTLY this final line, nothing after:
IMPORTANCE: <1-5> — <≤10-word reason>
(5 = act now; 4 = high; 3 = notable; 2 = context; 1 = marginal)

Evidence (JSON):
$payload_json
""")


async def run_why_moved_cycle() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("why_moved top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ why_moved error: {e}")
        except Exception:
            pass


def _triggered() -> list[dict]:
    """Assets whose latest context crosses the move/volume bar and aren't on
    cooldown. Returns evidence-bearing dicts, biggest move first."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    with session_scope() as s:
        rows = s.exec(
            select(PriceContext, Watchlist.asset_class).join(
                Watchlist, Watchlist.ticker == PriceContext.ticker
            )
        ).all()
    seen: set[str] = set()
    for pc, asset_class in rows:
        if pc.ticker in seen:
            continue
        seen.add(pc.ticker)
        cls = asset_class or "equity"
        chg = pc.change_1d_pct or 0.0
        vol = pc.volume_vs_20d_avg or 0.0
        if abs(chg) < _PCT_THRESHOLD.get(cls, 0.07) and vol < _VOL_THRESHOLD:
            continue

        prev = _RECENT.get(pc.ticker)
        if prev is not None:
            last_chg, last_ts = prev
            if now - last_ts < _COOLDOWN and abs(chg - last_chg) < _RE_EXPLAIN_DELTA:
                continue

        out.append({
            "ticker": pc.ticker,
            "asset_class": cls,
            "change_1d_pct": round(chg * 100, 2),
            "change_5d_pct": round((pc.change_5d_pct or 0) * 100, 2),
            "volume_vs_20d_avg": round(vol, 2),
            "_chg_raw": chg,
        })
    out.sort(key=lambda d: abs(d["change_1d_pct"]), reverse=True)
    return out[:_MAX_PER_CYCLE]


def _gather_evidence(ticker: str) -> dict:
    now = datetime.now(timezone.utc)
    cut_48h = now - timedelta(hours=48)
    cut_24h = now - timedelta(hours=24)
    with session_scope() as s:
        filings = s.exec(
            select(Filing)
            .where(Filing.ticker == ticker)
            .where(Filing.filed_at >= cut_48h)
            .order_by(Filing.filed_at.desc())
            .limit(3)
        ).all()
        news = s.exec(
            select(NewsItem)
            .where(NewsItem.ticker == ticker)
            .where(NewsItem.published_at >= cut_48h)
            .order_by(NewsItem.published_at.desc())
            .limit(4)
        ).all()
        reddit = s.exec(
            select(RedditMention.title)
            .where(RedditMention.ticker == ticker)
            .where(RedditMention.created_at >= cut_48h)
            .order_by(RedditMention.created_at.desc())
            .limit(3)
        ).all()
        hn_n = s.exec(
            select(func.count())
            .select_from(HnMention)
            .where(HnMention.ticker == ticker)
            .where(HnMention.created_at >= cut_48h)
        ).one() or 0
        macro = s.exec(
            select(NewsItem)
            .where(NewsItem.is_macro == True)  # noqa: E712
            .where(NewsItem.published_at >= cut_24h)
            .order_by(NewsItem.published_at.desc())
            .limit(4)
        ).all()
    return {
        "filings_48h": [
            {
                "form_type": f.form_type,
                "score": f.materiality_score,
                "reason": f.materiality_reason,
                "summary": (f.summary or "")[:160],
            }
            for f in filings
        ],
        "news_48h": [{"src": n.source.split(":")[-1], "title": n.title} for n in news],
        "reddit_titles_48h": list(reddit),
        "hn_count_48h": hn_n,
        "macro_news_24h": [n.title for n in macro],
    }


async def _run() -> None:
    candidates = _triggered()
    if not candidates:
        logger.info("why_moved: no qualifying moves")
        return

    llm = get_llm()
    from datetime import timedelta

    from ..narrative import is_superseded, record_event

    posted = 0
    coalesced = 0
    for c in candidates:
        ticker = c["ticker"]

        # Story coalescing: if a same-or-bigger post about this ticker just
        # went out (filing / convergence / earlier why_moved), the move is
        # already covered — record it to memory but don't add a noise post.
        block = is_superseded(ticker, 1, within=timedelta(minutes=90))
        if block is not None:
            record_event(
                ticker,
                "why_moved",
                f"moved {c['change_1d_pct']:+.1f}% (coalesced into {block.kind})",
                tier=1,
            )
            _RECENT[ticker] = (c["_chg_raw"], datetime.now(timezone.utc))
            coalesced += 1
            continue

        evidence = _gather_evidence(ticker)
        if c["asset_class"] == "crypto":
            from ..ingesters.crypto_micro import micro_for

            m = micro_for(ticker)
            if m:
                evidence["microstructure"] = m
        payload = {**{k: v for k, v in c.items() if not k.startswith("_")}, **evidence}
        rendered = WHY_PROMPT.safe_substitute(
            payload_json=json.dumps(payload, default=str)
        )
        # Heavy = the reasoning model. The light model returns empty on this
        # evidence-heavy prompt; convergence (same shape) already uses heavy.
        body = await asyncio.to_thread(
            llm.complete, rendered, model="heavy", max_tokens=500,
            fallback_light=True,
        )
        if not body or body == LLM_ERROR_SENTINEL:
            logger.error("why_moved: LLM error on {}", ticker)
            continue

        await _post(ticker, c, body)
        record_event(
            ticker,
            "why_moved",
            f"moved {c['change_1d_pct']:+.1f}% 1d ({c['asset_class']})",
            tier=1,
            detail=body[:600],
        )
        _RECENT[ticker] = (c["_chg_raw"], datetime.now(timezone.utc))
        posted += 1

    logger.info(
        "why_moved: {} candidates, {} explained, {} coalesced",
        len(candidates),
        posted,
        coalesced,
    )


async def _post(ticker: str, c: dict, body: str) -> None:
    from ..llm import parse_calls, parse_trailing_importance
    from ..scorecard import record_call

    body, calls = parse_calls(body)
    body, level, why = parse_trailing_importance(body)
    for call in calls:
        record_call(
            call["ticker"], call["direction"], "why_moved",
            body[:400], call["conviction"],
        )
    arrow = "🟢▲" if c["change_1d_pct"] >= 0 else "🔴▼"
    book = " 📌" if is_held(ticker) else ""
    embed = discord.Embed(
        title=f"❓ Why did ${ticker} move?{book}",
        description=body[:4000],
        color=0x16A085 if c["change_1d_pct"] >= 0 else 0xC0392B,
    )
    embed.add_field(
        name="Move",
        value=(
            f"{arrow} {c['change_1d_pct']:+.1f}% 1d · "
            f"{c['change_5d_pct']:+.1f}% 5d · vol {c['volume_vs_20d_avg']:.1f}x · "
            f"{c['asset_class']}"
        ),
        inline=False,
    )
    from ..routing import channel_for

    news = settings.DISCORD_NEWS_CHANNEL_ID or settings.DISCORD_PULSE_CHANNEL_ID
    await discord_client.post_embed(
        channel_for(ticker, equity_default=news),
        embed,
        importance=level or 3,
        importance_note=why,
    )
