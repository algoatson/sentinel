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
from typing import Any

import discord
from loguru import logger
from sqlmodel import func, select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..models import (
    Filing, HnMention, NewsItem, PriceBar, PriceContext, RedditMention, Watchlist,
)
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
- If `data_coverage` is "thin" (no per-ticker news/filings/social), state
  that plainly. Anchor your read off the BENCHMARKS and PEER MOVES blocks —
  they tell you whether this is idiosyncratic or part of a cohort move.
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

# Lookup-mode prompt. Slightly more compact than the one-shot version
# because the tool-calling model burns context fast across iterations.
WHY_TOOL_SYSTEM = (
    "You explain sharp asset moves for a private paper-trading copilot.\n"
    "You have tools to pull extra context. Use them sparingly — only when "
    "the evidence below doesn't tell you what you need. After at most a "
    "couple of tool calls, write the final answer.\n\n"
    "Format the final answer EXACTLY as the user instructions specify "
    "(3-5 sentences, optional CALL line, mandatory IMPORTANCE line)."
)
WHY_TOOL_USER = Template("""\
$TICKER moved $chg_str (1d) with volume ${vol}× the 20d average.
Asset class: $asset_class. Data coverage on this name: $coverage.

Explain the likely driver and give a forward read.

- If `data_coverage` is "thin", anchor off BENCHMARKS + PEERS in the
  evidence below. Use the tools to pull anything else you need
  (chart window, recent news, peer movers, ATR, filings, correlation).
- Don't invent a catalyst. If nothing explains it, say so and call
  it macro/sector/flow.
- 3-5 sentences. $$TICKER form.

If your forward read is directional, emit one machine line:
CALL: $$TICKER LONG|SHORT <conviction 1-5>

End with EXACTLY this final line, nothing after:
IMPORTANCE: <1-5> — <≤10-word reason>
(5 = act now; 4 = high; 3 = notable; 2 = context; 1 = marginal)

Pre-loaded evidence (JSON):
$payload_json
""")

# Per-cycle cap on how many "thin"-coverage explanations get the
# full tool-loop. Tool loops are 2-4× the wire cost of the one-shot
# path, so we don't want a noisy day where 20 small-caps each pump
# 8% to burn through the LLM budget.
_TOOL_LOOP_BUDGET_PER_CYCLE = 2


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


# Benchmark tickers per asset class — anchor reads for the LLM so a
# move like "ZEST-USD +8% on a day BTC was -1%" is contextualised as
# idiosyncratic, not just "crypto pumped". Tickers must match what
# the price ingester actually stores in PriceContext.
_BENCHMARKS = {
    "crypto": ("BTC-USD", "ETH-USD"),
    "equity": ("SPY", "QQQ"),
    "future": ("ES=F", "NQ=F"),
    "rate": ("^TNX", "^IRX"),
}
# Peer-move snippet: top N same-class movers (by abs(1d %)) the
# bot already has fresh context on. Tells the LLM whether the
# subject is alone or part of a cohort.
_PEER_LIMIT = 5


def _gather_market_context(session, ticker: str, asset_class: str) -> dict:
    """Benchmarks + peer-move snippet pulled from PriceContext.

    Returns:
      benchmarks: [{ticker, change_1d_pct, change_5d_pct}]
      peers: [{ticker, change_1d_pct, volume_vs_20d_avg}] — same
        asset_class, sorted by abs(1d %), subject excluded.
    """
    out: dict[str, list[dict]] = {"benchmarks": [], "peers": []}
    bench = _BENCHMARKS.get(asset_class, ())
    if bench:
        for b in bench:
            pc = session.get(PriceContext, b)
            if pc is None:
                continue
            out["benchmarks"].append({
                "ticker": b,
                "change_1d_pct": round((pc.change_1d_pct or 0) * 100, 2),
                "change_5d_pct": round((pc.change_5d_pct or 0) * 100, 2),
            })

    rows = session.exec(
        select(PriceContext, Watchlist.asset_class).join(
            Watchlist, Watchlist.ticker == PriceContext.ticker
        )
    ).all()
    peers = []
    for pc, cls in rows:
        if (cls or "equity") != asset_class:
            continue
        if pc.ticker == ticker:
            continue
        chg = pc.change_1d_pct or 0.0
        # Drop noise — at least 1% move to count as a "peer move".
        if abs(chg) < 0.01:
            continue
        peers.append({
            "ticker": pc.ticker,
            "change_1d_pct": round(chg * 100, 2),
            "volume_vs_20d_avg": round(pc.volume_vs_20d_avg or 0, 2),
        })
    peers.sort(key=lambda d: abs(d["change_1d_pct"]), reverse=True)
    out["peers"] = peers[:_PEER_LIMIT]
    return out


def _vol_context(session, ticker: str, change_1d_pct: float) -> dict:
    """ATR(14) and recent-bars snapshot for `ticker`.

    `change_1d_pct` is the *percent* (e.g. 8.0 for an 8% move), so we
    can express today as a multiple of the daily ATR — a 3-ATR move on
    a normally calm ticker is a much bigger signal than a 1-ATR move
    on a high-vol crypto.

    Returns:
      atr_14d, atr_pct, move_vs_atr, bars_7d (one row per day,
      newest first; up to 7 entries).
    """
    out: dict[str, Any] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=20)
    bars = session.exec(
        select(PriceBar)
        .where(PriceBar.ticker == ticker)
        .where(PriceBar.ts >= cutoff)
        .order_by(PriceBar.ts)
    ).all()
    if len(bars) < 2:
        return out

    # Collapse intraday → one bar per UTC day. Same shape the ATR
    # helper uses; replicated here so we keep one db round-trip.
    by_day: dict[str, dict] = {}
    for b in bars:
        d = b.ts.strftime("%Y-%m-%d")
        cur = by_day.get(d)
        if cur is None:
            by_day[d] = {
                "date": d, "open": b.open, "high": b.high,
                "low": b.low, "close": b.close,
                "volume": int(b.volume or 0),
            }
        else:
            cur["high"] = max(cur["high"], b.high)
            cur["low"] = min(cur["low"], b.low)
            cur["close"] = b.close
            cur["volume"] = int(cur["volume"]) + int(b.volume or 0)

    days = sorted(by_day.keys())
    if len(days) < 2:
        return out

    # Wilder ATR over the last 14 daily bars.
    trs: list[float] = []
    prev_close = by_day[days[0]]["close"]
    for d in days[1:]:
        b = by_day[d]
        trs.append(
            max(b["high"] - b["low"],
                abs(b["high"] - prev_close),
                abs(b["low"] - prev_close))
        )
        prev_close = b["close"]
    trs = trs[-14:] if len(trs) >= 14 else trs
    if not trs:
        return out
    atr = sum(trs) / len(trs)
    last_close = by_day[days[-1]]["close"]
    atr_pct = (atr / last_close * 100) if last_close > 0 else None
    out["atr_14d"] = round(atr, 4)
    if atr_pct is not None:
        out["atr_pct"] = round(atr_pct, 2)
        # Today's move expressed as a multiple of ATR%. Tells the
        # model "this is a 3.4× ATR move" or "this is barely 0.8×
        # — within normal range".
        out["move_vs_atr"] = (
            round(change_1d_pct / atr_pct, 2)
            if atr_pct > 0 else None
        )

    # Mini OHLCV summary — last 7 days, newest first. Lets the LLM see
    # whether today's move is a breakout, a gap, or noise without
    # spending a tool call on it.
    out["bars_7d"] = [
        {
            "date": by_day[d]["date"],
            "o": round(by_day[d]["open"], 4),
            "h": round(by_day[d]["high"], 4),
            "l": round(by_day[d]["low"], 4),
            "c": round(by_day[d]["close"], 4),
            "v": by_day[d]["volume"],
        }
        for d in days[-7:][::-1]
    ]
    return out


def _gather_evidence(
    ticker: str, asset_class: str, change_1d_pct: float = 0.0
) -> dict:
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
        # Market-context block — pulled inside the same session so the
        # whole prompt assembles off one connection.
        mkt = _gather_market_context(s, ticker, asset_class)
        # Vol-regime context — every dossier gets ATR + move_vs_atr +
        # a 7-day OHLCV mini-summary now, not just thin-coverage
        # candidates. The LLM doesn't have to burn a tool call to
        # know whether today's move is a 3-ATR breakout or a 0.8-ATR
        # nothing-burger.
        vol = _vol_context(s, ticker, change_1d_pct)

    filings_out = [
        {
            "form_type": f.form_type,
            "score": f.materiality_score,
            "reason": f.materiality_reason,
            "summary": (f.summary or "")[:160],
        }
        for f in filings
    ]
    news_out = [{"src": n.source.split(":")[-1], "title": n.title} for n in news]
    reddit_out = list(reddit)
    macro_out = [n.title for n in macro]

    # Coverage hint — when nothing per-ticker came back the LLM should
    # explicitly anchor off benchmarks/peers and refuse to invent a
    # catalyst. The prompt has matching guidance for this flag.
    coverage = (
        "thin"
        if not filings_out and not news_out and not reddit_out and hn_n == 0
        else "ok"
    )

    return {
        "filings_48h": filings_out,
        "news_48h": news_out,
        "reddit_titles_48h": reddit_out,
        "hn_count_48h": hn_n,
        "macro_news_24h": macro_out,
        "benchmarks": mkt["benchmarks"],
        "peers": mkt["peers"],
        **vol,
        "data_coverage": coverage,
    }


async def _run() -> None:
    candidates = _triggered()
    if not candidates:
        logger.info("why_moved: no qualifying moves")
        return

    llm = get_llm()
    from datetime import timedelta

    from ..llm_tools import tool_loop
    from ..market_tools import default_registry
    from ..narrative import is_superseded, record_event

    tool_registry = default_registry()
    tool_budget = _TOOL_LOOP_BUDGET_PER_CYCLE
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

        evidence = _gather_evidence(
            ticker, c["asset_class"], c["change_1d_pct"]
        )
        if c["asset_class"] == "crypto":
            from ..ingesters.crypto_micro import micro_for

            m = micro_for(ticker)
            if m:
                evidence["microstructure"] = m
        payload = {**{k: v for k, v in c.items() if not k.startswith("_")}, **evidence}

        # Tool-loop path: thin-coverage names get a small budget to
        # pull extra context (chart window / peer movers / news /
        # filings / correlation / micro). Anything else stays on the
        # one-shot path — it's faster and cheaper, and the pre-loaded
        # evidence already covers the well-tracked tickers.
        use_tools = (
            evidence.get("data_coverage") == "thin"
            and tool_budget > 0
        )
        if use_tools:
            tool_budget -= 1
            chg_str = f"{c['change_1d_pct']:+.2f}%"
            rendered_user = WHY_TOOL_USER.safe_substitute(
                TICKER=ticker,
                chg_str=chg_str,
                vol=c.get("volume_vs_20d_avg", 0),
                asset_class=c["asset_class"],
                coverage=evidence.get("data_coverage"),
                payload_json=json.dumps(payload, default=str),
            )
            loop_res = await asyncio.to_thread(
                tool_loop,
                user_prompt=rendered_user,
                system_prompt=WHY_TOOL_SYSTEM,
                registry=tool_registry,
                model="heavy",
                max_tokens=700,
                max_iterations=3,
            )
            body = loop_res.text
            if loop_res.tool_calls:
                logger.info(
                    "why_moved[{}]: tool_loop iterations={}, tools={}",
                    ticker, loop_res.iterations,
                    [t["name"] for t in loop_res.tool_calls],
                )
            if not body:
                logger.warning(
                    "why_moved[{}]: tool_loop empty ({}), falling back to one-shot",
                    ticker, loop_res.error,
                )
        else:
            body = ""

        if not body:
            rendered = WHY_PROMPT.safe_substitute(
                payload_json=json.dumps(payload, default=str)
            )
            # Heavy = the reasoning model. The light model returns empty on
            # this evidence-heavy prompt; convergence (same shape) already
            # uses heavy. 800 tokens leaves headroom for narrative + CALL +
            # IMPORTANCE on thin-coverage hits where the model wants to
            # explain what it couldn't find.
            body = await asyncio.to_thread(
                llm.complete, rendered, model="heavy", max_tokens=800,
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
