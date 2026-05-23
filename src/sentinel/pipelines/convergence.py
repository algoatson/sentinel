"""Cross-source convergence detector.

Runs every 30 minutes. For each watchlist ticker, counts active signals:
  - filing in last 6h with materiality >= 2
  - |1d price change| >= 3% OR volume_vs_20d_avg >= 2
  - HnMention or RedditMention activity in last 24h above its baseline

When >=2 signals stack, the ticker is "converging" — posts a single notification
to #priority with an LLM-generated synthesis. Already-converged tickers are
not re-posted within 12h.
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


# In-memory cooldown — survives within a single process run. Acceptable: a
# restart re-fires convergence which is at worst mildly noisy.
_RECENT_POSTS: dict[str, datetime] = {}
_COOLDOWN = timedelta(hours=12)
# Each finding is a heavy-LLM call (~minutes on CPU). Once price history is
# backfilled, dozens of tickers can align at once — cap per cycle, strongest
# (most signals) first, so a cycle can't run for hours.
_MAX_PER_CYCLE = 6


CONVERGENCE_PROMPT = Template("""\
This ticker has multiple aligned signals (filing / price / social / news)
firing in the same window. You're the user's private paper-trading copilot —
give a real read, not a neutral note.

In 3-5 tight sentences:
1. What's happening — combine the signals into one thesis, don't list them.
2. Your read: is this actionable, and which way (long / short / fade / wait)?
   Name the trigger/level and what would invalidate it. Conviction stated.
3. The single most important thing to watch next.

Be terse and direct. Reasoned conviction over hedging; state risk in a clause,
don't refuse. No disclaimers.

If you have a directional lean, emit one machine line (logged & scored —
only if you'd stand behind it; omit otherwise):
CALL: $$TICKER LONG|SHORT <conviction 1-5>

Then end with EXACTLY this final line, nothing after:
IMPORTANCE: <1-5> — <≤10-word reason>
(5 = act now; 4 = high; 3 = notable; 2 = context; 1 = marginal)

Data:
$payload_json
""")


async def run_convergence_cycle() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("convergence top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ convergence error: {e}")
        except Exception:
            pass


async def _run() -> None:
    now = datetime.now(timezone.utc)
    cutoff_6h = now - timedelta(hours=6)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    findings: list[dict] = []

    with session_scope() as session:
        watch_tickers = sorted({
            r.ticker
            for r in session.exec(
                select(Watchlist).where(Watchlist.ticker.is_not(None))
            ).all()
            if r.ticker
        })

        for ticker in watch_tickers:
            if _is_on_cooldown(ticker, now):
                continue

            signals: list[str] = []
            evidence: dict = {"ticker": ticker}

            # Signal 1: recent material filing.
            filing = session.exec(
                select(Filing)
                .where(Filing.ticker == ticker)
                .where(Filing.filed_at >= cutoff_6h)
                .where(Filing.materiality_score.is_not(None))
                .order_by(Filing.materiality_score.desc(), Filing.filed_at.desc())
                .limit(1)
            ).first()
            if filing is not None and (filing.materiality_score or 0) >= 2:
                signals.append("filing")
                evidence["filing"] = {
                    "form_type": filing.form_type,
                    "score": filing.materiality_score,
                    "reason": filing.materiality_reason,
                    "summary": (filing.summary or "")[:300],
                }

            # Signal 2: price/volume anomaly.
            pc = session.get(PriceContext, ticker)
            if pc is not None:
                abs_pct = abs(pc.change_1d_pct or 0.0)
                vol = pc.volume_vs_20d_avg or 0.0
                if abs_pct >= 0.03 or vol >= 2.0:
                    signals.append("price")
                    evidence["price"] = {
                        "change_1d_pct": round(pc.change_1d_pct * 100, 2),
                        "change_5d_pct": round(pc.change_5d_pct * 100, 2),
                        "volume_ratio": round(vol, 2),
                    }

            # Signal 3: social activity vs. baseline.
            reddit_24h = session.exec(
                select(func.count()).select_from(RedditMention)
                .where(RedditMention.ticker == ticker)
                .where(RedditMention.created_at >= cutoff_24h)
            ).one() or 0
            reddit_baseline = session.exec(
                select(func.count()).select_from(RedditMention)
                .where(RedditMention.ticker == ticker)
                .where(RedditMention.created_at >= cutoff_7d)
                .where(RedditMention.created_at < cutoff_24h)
            ).one() or 0
            reddit_baseline_per_day = float(reddit_baseline) / 6.0
            hn_24h = session.exec(
                select(func.count()).select_from(HnMention)
                .where(HnMention.ticker == ticker)
                .where(HnMention.created_at >= cutoff_24h)
            ).one() or 0

            social_hot = (
                (reddit_baseline_per_day >= 1.0 and reddit_24h >= 2 * reddit_baseline_per_day)
                or hn_24h >= 2
            )
            # Signal 4: ticker-tagged news in last 24h.
            news_rows = session.exec(
                select(NewsItem)
                .where(NewsItem.ticker == ticker)
                .where(NewsItem.published_at >= cutoff_24h)
                .order_by(NewsItem.published_at.desc())
                .limit(5)
            ).all()
            if news_rows:
                signals.append("news")
                evidence["news"] = {
                    "count_24h": len(news_rows),
                    "top_titles": [n.title for n in news_rows[:3]],
                }

            if social_hot:
                signals.append("social")
                top_reddit = [
                    r.title
                    for r in session.exec(
                        select(RedditMention)
                        .where(RedditMention.ticker == ticker)
                        .where(RedditMention.created_at >= cutoff_24h)
                        .order_by(RedditMention.score.desc())
                        .limit(3)
                    ).all()
                ]
                top_hn = [
                    h.title
                    for h in session.exec(
                        select(HnMention)
                        .where(HnMention.ticker == ticker)
                        .where(HnMention.created_at >= cutoff_24h)
                        .order_by(HnMention.points.desc())
                        .limit(2)
                    ).all()
                ]
                evidence["social"] = {
                    "reddit_24h": reddit_24h,
                    "reddit_baseline_per_day": round(reddit_baseline_per_day, 2),
                    "hn_24h": hn_24h,
                    "top_reddit_titles": top_reddit,
                    "top_hn_titles": top_hn,
                }

            if len(signals) >= 2:
                evidence["signals"] = signals
                findings.append(evidence)

    if not findings:
        logger.info("convergence: nothing aligning")
        return

    total = len(findings)
    findings.sort(key=lambda e: len(e["signals"]), reverse=True)
    findings = findings[:_MAX_PER_CYCLE]
    logger.info(
        "convergence: {} tickers aligning, processing top {}",
        total,
        len(findings),
    )
    from ..narrative import is_superseded, record_event

    llm = get_llm()
    for evidence in findings:
        ticker = evidence["ticker"]

        # Coalesce: a material filing (tier 3) or another convergence (tier 2)
        # in the last 90m already tells this story — don't double-post.
        block = is_superseded(ticker, 2, within=timedelta(minutes=90))
        if block is not None:
            record_event(
                ticker,
                "convergence",
                f"{' + '.join(evidence['signals'])} (coalesced into {block.kind})",
                tier=2,
            )
            _RECENT_POSTS[ticker] = datetime.now(timezone.utc)
            continue

        rendered = CONVERGENCE_PROMPT.safe_substitute(payload_json=json.dumps(evidence, default=str))
        synthesis = await asyncio.to_thread(
            llm.complete, rendered, model="heavy", max_tokens=400
        )
        if synthesis == LLM_ERROR_SENTINEL:
            logger.error("convergence LLM error on {}", ticker)
            continue

        await _post_convergence(ticker, evidence["signals"], synthesis)
        record_event(
            ticker,
            "convergence",
            " + ".join(evidence["signals"]),
            tier=2,
            detail=synthesis[:600],
        )
        _RECENT_POSTS[ticker] = datetime.now(timezone.utc)


async def _post_convergence(ticker: str, signals: list[str], synthesis: str) -> None:
    from ..llm import parse_calls, parse_trailing_importance
    from ..portfolio import is_held
    from ..routing import channel_for
    from ..scorecard import record_call

    body, calls = parse_calls(synthesis)
    body, level, why = parse_trailing_importance(body)
    for c in calls:
        record_call(
            c["ticker"], c["direction"], "convergence",
            body[:400], c["conviction"],
        )
    sig_str = " + ".join(signals)
    book = " 📌" if is_held(ticker) else ""
    embed = discord.Embed(
        title=f"🎯 Convergence — ${ticker}{book}  [{sig_str}]",
        description=body[:4000],
        color=0xC0392B,
    )
    # Crypto convergence → #crypto; equities keep #priority.
    channel = channel_for(
        ticker, equity_default=settings.DISCORD_PRIORITY_CHANNEL_ID
    )
    await discord_client.post_embed(
        channel, embed, importance=level or 3, importance_note=why
    )


def _is_on_cooldown(ticker: str, now: datetime) -> bool:
    last = _RECENT_POSTS.get(ticker)
    return last is not None and (now - last) < _COOLDOWN
