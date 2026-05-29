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
# Was 6 — trimmed because the top-ranked signals (most distinct
# triggers firing) are the ones worth LLM-ing; the long tail rarely
# adds an actionable read and is now skipped under the new tighter
# token budget. Convergence runs every 30 min, so 4 × 48 = 192
# max-candidate slots per day is still plenty.
_MAX_PER_CYCLE = 4


CONVERGENCE_PROMPT = Template("""\
Multi-signal convergence on this ticker. You're a paper-trading copilot.

Write 3-4 short sentences (≤ ~280 visible tokens total):
1. Thesis — fuse the signals into one read, don't enumerate them.
2. Direction (long / short / fade / wait) + the invalidator and the
   level/event that confirms or kills it.
3. The single thing to watch next.

Terse. State risk in a clause, no disclaimers, no hedging adverbs.

If directional, emit ONE machine line (logged + scored — omit if you
wouldn't actually take it):
CALL: $$TICKER LONG|SHORT <1-5>

Then end with EXACTLY this final line, nothing after:
IMPORTANCE: <1-5> — <≤10-word reason>

Data:
$payload_json
""")

# Lookup-mode prompts — used when convergence fires on a ticker whose
# evidence payload is shallow (few signals, or a non-equity asset class
# we don't have rich data on). Mirrors the why_moved tool-loop wiring.
CONV_TOOL_SYSTEM = (
    "You convert multi-signal alerts into a directional read for a "
    "private paper-trading copilot.\n"
    "Tools fetch chart / ATR / peers / news / filings / micro / "
    "correlation / next_earnings / narrative_timeline. Use AT MOST "
    "ONE if the data block is thin — most alerts answer off the "
    "data block alone. Check next_earnings before recommending a "
    "directional CALL on an equity; never anchor a thesis across "
    "an imminent print. narrative_timeline tells you what the bot "
    "has already said about this name — don't repeat yourself.\n\n"
    "FINAL ANSWER FORMAT (3-4 sentences, ≤ 280 visible tokens, optional "
    "CALL, mandatory IMPORTANCE). The exact format is in the user "
    "message."
)
CONV_TOOL_USER = Template("""\
Convergence on $TICKER — aligned signals: $signals_str.

Pre-loaded data (JSON):
$payload_json

Write a 3-4 sentence directional read (≤ 280 visible tokens):
thesis → lean (long/short/fade/wait) with invalidator → what to watch.
Terse. State risk in a clause, no disclaimers.

If directional:
CALL: $$TICKER LONG|SHORT <1-5>

End with EXACTLY:
IMPORTANCE: <1-5> — <≤10-word reason>
""")

# Per-cycle cap matches why_moved — convergence fires less often
# than why_moved so 2 lookups per cycle is plenty.
_TOOL_LOOP_BUDGET_PER_CYCLE = 2


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
        # Asset class is needed downstream to decide whether a thin
        # evidence payload should escalate to the tool-loop (non-equity
        # names have fewer pre-loaded streams, so the LLM benefits from
        # the extra pull). Carry it through the per-ticker loop.
        watch_rows = session.exec(
            select(Watchlist.ticker, Watchlist.asset_class)
            .where(Watchlist.ticker.is_not(None))
        ).all()
        by_ticker: dict[str, str] = {}
        for tk, ac in watch_rows:
            if tk and tk not in by_ticker:
                by_ticker[tk] = ac or "equity"
        watch_tickers = sorted(by_ticker.keys())

        for ticker in watch_tickers:
            if _is_on_cooldown(ticker, now):
                continue

            asset_class = by_ticker[ticker]
            signals: list[str] = []
            evidence: dict = {"ticker": ticker, "asset_class": asset_class}

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
            # ≥ 2 items OR 1 tagged-significant (non-zero sentiment OR
            # measurable 1d impact). A single noise headline with no
            # sentiment/impact tag was previously enough to add the
            # "news" signal — combined with a small price blip that's
            # 2 signals and an LLM call. The narrower gate stops that.
            news_rows = session.exec(
                select(NewsItem)
                .where(NewsItem.ticker == ticker)
                .where(NewsItem.published_at >= cutoff_24h)
                .order_by(NewsItem.published_at.desc())
                .limit(5)
            ).all()
            news_significant = False
            if news_rows:
                if len(news_rows) >= 2:
                    news_significant = True
                else:
                    n = news_rows[0]
                    has_sentiment = (n.sentiment or 0) != 0
                    has_impact = (
                        (n.impact_1d_pct or 0) != 0
                        or (n.impact_1h_pct or 0) != 0
                    )
                    news_significant = has_sentiment or has_impact
            if news_rows and news_significant:
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
    from ..llm_tools import tool_loop
    from ..market_tools import default_registry
    from ..narrative import is_superseded, record_event

    tool_registry = default_registry()
    tool_budget = _TOOL_LOOP_BUDGET_PER_CYCLE
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

        # Tool-loop path: shallow evidence (≤2 distinct signal types
        # OR a non-equity asset class — those have fewer pre-loaded
        # streams) gets the small budget to pull extra context. The
        # rest stay on the cheaper one-shot path.
        signals = evidence.get("signals") or []
        asset_class = evidence.get("asset_class") or "equity"
        shallow = (
            len(set(signals)) <= 2 or asset_class != "equity"
        )
        synthesis = ""
        if shallow and tool_budget > 0:
            tool_budget -= 1
            rendered_user = CONV_TOOL_USER.safe_substitute(
                TICKER=ticker,
                signals_str=", ".join(signals) or "n/a",
                payload_json=json.dumps(evidence, default=str),
            )
            loop_res = await asyncio.to_thread(
                tool_loop,
                user_prompt=rendered_user,
                system_prompt=CONV_TOOL_SYSTEM,
                registry=tool_registry,
                model="heavy",
                # 700/2: heavy reasoning model spends ~200-300 tokens
                # thinking before the visible answer; 500 was cutting
                # mid-sentence. 700 leaves ~400 for the 3-4 sentence
                # read + CALL + IMPORTANCE — comfortable headroom.
                max_tokens=700,
                max_iterations=2,
                pipeline="convergence",
                ticker=ticker,
            )
            synthesis = loop_res.text
            if loop_res.tool_calls:
                logger.info(
                    "convergence[{}]: tool_loop iterations={}, tools={}",
                    ticker, loop_res.iterations,
                    [t["name"] for t in loop_res.tool_calls],
                )
            if not synthesis:
                logger.warning(
                    "convergence[{}]: tool_loop empty ({}), falling back",
                    ticker, loop_res.error,
                )

        if not synthesis:
            rendered = CONVERGENCE_PROMPT.safe_substitute(
                payload_json=json.dumps(evidence, default=str)
            )
            # 750 budget = ~200-300 reasoning tokens (heavy model
            # thinks before answering) + ~350-400 visible tokens for
            # the 3-4 sentence read + CALL + IMPORTANCE. A 500 cap
            # was leaving the answer truncated mid-sentence because
            # the model's reasoning ate too much of the budget.
            synthesis = await asyncio.to_thread(
                llm.complete, rendered, model="heavy", max_tokens=750
            )
        if not synthesis or synthesis == LLM_ERROR_SENTINEL:
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
    # Crypto convergence → #crypto. Equity convergence has its own dedicated
    # home (#convergence) so multi-source agreement gets a visible track
    # instead of mixing into the broader #priority feed; falls back to
    # #priority when the dedicated channel isn't configured.
    channel = channel_for(
        ticker,
        equity_default=(
            settings.DISCORD_CONVERGENCE_CHANNEL_ID
            or settings.DISCORD_PRIORITY_CHANNEL_ID
        ),
    )
    await discord_client.post_embed(
        channel, embed, importance=level or 3, importance_note=why
    )


def _is_on_cooldown(ticker: str, now: datetime) -> bool:
    last = _RECENT_POSTS.get(ticker)
    return last is not None and (now - last) < _COOLDOWN
