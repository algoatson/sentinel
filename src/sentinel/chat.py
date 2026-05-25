"""Discord chat interface — interactive commands + @mention Q&A.

The user can:
  - Type !status / !ticker NVDA / !recent / !filing <accession_number>
  - @-mention the bot with any free-form question — the bot retrieves
    relevant DB context and asks the LLM to answer.

This is a single-user bot; we do not add prompt-injection guards. The user
controls the Discord server.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from string import Template
from typing import Optional

import discord
from loguru import logger
from sqlmodel import func, select

from . import research
from .config import settings
from .db import session_scope
from .interactions import SEED_PENDING, extract_post_text
from .llm import get_llm
from .utils import TICKER_BLOCKLIST, chunk_text, highlight_markdown
from . import portfolio
from .models import (
    Filing,
    HnMention,
    NewsItem,
    PriceContext,
    RedditMention,
    SocialPulse,
    Watchlist,
)


_CMD_PREFIX = "!"
_TICKER_RE = re.compile(r"\$?([A-Z]{1,5})\b")
_THREAD_HISTORY_LIMIT = 12


CHAT_PROMPT = Template("""\
You are Sentinel — the user's personal trading copilot. Private,
single-user, paper-trading: opinions and calls are wanted, not disclaimed.
The JSON below is retrieved context (filings, prices, news) to ground
specifics; reason with your own market knowledge on top of it.

Rules:
- Lead with the answer and a real view. When asked what looks good or what
  you'd do, give a direct lean + the reasoning + what would flip it.
- Prefer provided data for hard facts; where it's silent, reason from
  general knowledge and label inference vs. known — don't refuse with
  "no data".
- Engage hypotheticals, price scenarios and recommendations fully. Be candid
  about risk, but don't hedge into uselessness or refuse.
- Voice: sound like a crypto-native trader who actually knows markets —
  light, natural slang ("ser", "degen", "bags", "conviction play", "rekt",
  "LFG", "fade", "send it") sprinkled in, not forced. Subtle, earned, never
  cringe or parody — the analysis always leads, the tone just colors it.
- Use $TICKER notation. No length cap — answer as fully as the question
  deserves, but dense and padding-free. Bullets OK for lists.

User question: $question

Data (JSON, may be truncated):
$context_json
""")


THREAD_CHAT_PROMPT = Template("""\
You are Sentinel — the user's personal trading copilot, talking in a
thread. This is a private, single-user, paper-trading tool: the user WANTS
your opinions, calls, and conviction. Talk like a sharp trader friend, not a
hedging compliance bot.

How to engage:
- Give a real answer with a real view. When asked what you'd do or what looks
  good, say it directly — a lean (buy/sell/avoid/wait), rough levels or
  ranges if you have a basis, position-sizing intuition, and the catalyst or
  invalidation that would change your mind.
- Reason it out loud: market-cap math, base rates, what must be true, and
  push back on flawed premises (e.g. "a 50x to $$1M doesn't make 'everybody'
  rich — only people holding a meaningful amount *now*").
- Engage hypotheticals and price scenarios fully. Don't deflect with "I only
  have data X" or "that's speculative" — reason about it anyway.
- The post / thread / retrieved facts are context to draw on, not a fence;
  use your broad market knowledge freely.
- Stay intellectually honest: conviction where the logic is strong, explicit
  about risk and uncertainty where it isn't. A confident call WITH its risks
  stated is the goal — not refusal, not blind hype.
- Voice: talk like a crypto-native trader who genuinely knows markets —
  light, natural slang ("ser", "degen", "bags", "conviction play", "rekt",
  "LFG", "fade", "send it", "gm") woven in, not piled on. Keep it subtle and
  earned: the reasoning carries the message, the tone just flavors it. Never
  tip into parody or cringe.
- Use $$TICKER notation. No length cap; scale with the question — tight for
  small ones, go as deep as a big one needs. No padding either way.

ORIGINAL POST:
$post

RETRIEVED COMPANY FACTS (looked up just now):
$research

Thread so far:
$history

User's latest message:
$latest
""")


def _resolve_ticker(raw: str) -> str:
    """Normalize a user-typed ticker against the watchlist. Lets the user say
    `BTC` and get `BTC-USD`, or `ES` and get `ES=F`, without memorizing
    yfinance notation. Falls back to the raw upper form when nothing matches.
    """
    t = raw.strip().upper().lstrip("$")
    if not t:
        return t
    with session_scope() as s:
        if s.exec(select(Watchlist).where(Watchlist.ticker == t)).first():
            return t
        for cand in (f"{t}-USD", f"{t}=F"):
            if s.exec(select(Watchlist).where(Watchlist.ticker == cand)).first():
                return cand
    return t


# ── consistent command presentation ────────────────────────────────────────
# One palette, one builder. Every command returns a discord.Embed so the
# command surface looks like the rest of the bot instead of raw markdown.
_COL_DATA = 0x34495E   # neutral data readout
_COL_OK = 0x2ECC71     # success ack
_COL_WARN = 0xE67E22   # usage / not-found
_COL_INFO = 0x5865F2   # help / reference


def _embed(
    title: Optional[str],
    description: Optional[str] = None,
    *,
    color: int = _COL_DATA,
    footer: Optional[str] = None,
    url: Optional[str] = None,
) -> discord.Embed:
    e = discord.Embed(color=color)
    if title:
        e.title = title[:256]
    if description:
        e.description = description[:4096]
    if footer:
        e.set_footer(text=footer[:2048])
    if url:
        e.url = url
    return e


def _usage(text: str) -> discord.Embed:
    return _embed("Usage", text, color=_COL_WARN)


def _ack(text: str, *, ok: bool = True) -> discord.Embed:
    return _embed(None, text, color=_COL_OK if ok else _COL_WARN)


def _cmd_hold(arg: str) -> discord.Embed:
    parts = arg.split()
    if not parts:
        return _usage(
            "`!hold TICKER [qty]` — e.g. `!hold BTC 0.05`, `!hold PLTR 12`"
        )
    ticker = _resolve_ticker(parts[0])
    qty: Optional[float] = None
    if len(parts) > 1:
        try:
            qty = float(parts[1])
        except ValueError:
            return _usage(
                f"couldn't parse quantity `{parts[1]}` — give a number."
            )
    res = portfolio.add_hold(ticker, qty)
    if not res["ok"]:
        return _ack(res["message"], ok=False)
    verb = "Added" if res["created"] else "Updated"
    qstr = f" ×{res['qty']:g}" if res["qty"] is not None else ""
    return _ack(
        f"📌 {verb} **${res['ticker']}**{qstr} — "
        f"tagged & prioritized everywhere."
    )


def _cmd_unhold(arg: str) -> discord.Embed:
    if not arg.strip():
        return _usage("`!unhold TICKER`")
    ticker = _resolve_ticker(arg)
    res = portfolio.remove_hold(ticker)
    if not res["ok"]:
        return _ack(f"${ticker} isn't in your book.", ok=False)
    return _ack(f"🗑️ Removed **${res['ticker']}** from your book.")


def _cmd_holdings() -> discord.Embed:
    rows = portfolio.list_holds()
    if not rows:
        return _embed(
            "📒 Your book",
            "Empty. Add with `!hold TICKER [qty]`.",
            color=_COL_WARN,
        )
    lines = []
    for h in rows:
        qty = f" ×{h['qty']:g}" if h["qty"] is not None else ""
        if h["price"] is not None:
            lines.append(
                f"`${h['ticker']}`{qty} — {h['price']:.4g} "
                f"({h['change_1d_pct']:+.1f}% 1d · "
                f"{h['change_5d_pct']:+.1f}% 5d)"
            )
        else:
            lines.append(
                f"`${h['ticker']}`{qty} — no price context yet"
            )
    return _embed(
        f"📒 Your book — {len(rows)} position(s)",
        "\n".join(lines),
        footer="paper book",
    )


async def _cmd_watch(arg: str) -> discord.Embed:
    if not arg.strip():
        return _usage(
            "`!watch <plain English>` — e.g.\n"
            "`!watch tell me if any insider buys >$1M at a name r/wallstreetbets is hyping`"
        )
    from .pipelines import watches

    return _embed("🔔 Watch", await watches.add_watch(arg.strip()), color=_COL_OK)


def _cmd_list_watches() -> discord.Embed:
    from .pipelines import watches

    rows = watches.list_watches()
    if not rows:
        return _embed(
            "🔔 Watches", "None set. Add with `!watch <plain English>`.",
            color=_COL_WARN,
        )
    lines = []
    for w in rows:
        state = "" if w["active"] else " _(paused)_"
        last = (
            f" · last {w['last_triggered_at']:%m-%d %H:%M}"
            if w["last_triggered_at"]
            else ""
        )
        lines.append(
            f"`#{w['id']}`{state} {w['raw_text'][:140]} "
            f"— ×{w['trigger_count']}{last}"
        )
    return _embed(f"🔔 Watches — {len(rows)}", "\n".join(lines))


def _cmd_unwatch(arg: str) -> discord.Embed:
    from .pipelines import watches

    wid = arg.strip().lstrip("#")
    if not wid.isdigit():
        return _usage("`!unwatch <id>` (see `!watches`)")
    res = watches.remove_watch(int(wid))
    if not res["ok"]:
        return _ack(f"No watch #{wid}.", ok=False)
    return _ack(f"🗑️ Removed watch #{res['watch_id']}.")


def _cmd_open(arg: str, side: str) -> discord.Embed:
    parts = arg.split()
    verb = "buy" if side == "long" else "short"
    if len(parts) < 2:
        return _usage(
            f"`!{verb} TICKER QTY [price] [note]` — e.g. `!{verb} NVDA 10` "
            f"(price defaults to last mark)"
        )
    ticker = _resolve_ticker(parts[0])
    qty_raw = parts[1]
    price: Optional[float] = None
    note_start = 2
    if len(parts) > 2:
        try:
            price = float(parts[2])
            note_start = 3
        except ValueError:
            price = None
    note = " ".join(parts[note_start:])[:200] or None

    try:
        qty = float(qty_raw)
    except ValueError:
        return _usage(f"couldn't parse qty `{qty_raw}` — give a number.")

    res = portfolio.open_paper_position(
        ticker, side, qty, price=price, note=note, opened_by="manual",
    )
    if not res["ok"]:
        return _ack(res["message"], ok=False)
    emoji = "🟢" if side == "long" else "🔴"
    return _ack(f"{emoji} {res['message']}")


def _cmd_close(arg: str) -> discord.Embed:
    if not arg.strip():
        return _usage("`!close TICKER` (or `!sell TICKER`)")
    ticker = _resolve_ticker(arg.split()[0])
    p = portfolio.close_position(ticker, mark=None)
    if p is None:
        return _ack(f"No open position on ${ticker}.", ok=False)
    sign = "🟢" if (p.realized_pnl or 0) >= 0 else "🔴"
    return _ack(
        f"{sign} Closed **{p.side} {p.qty:g} ${ticker}** @ {p.exit_price:.4g} "
        f"— realized {p.realized_pnl:+.2f}"
    )


def _cmd_positions() -> discord.Embed:
    pos = portfolio.open_positions()
    if not pos:
        return _embed(
            "💼 Positions", "No open paper positions. `!buy TICKER QTY`.",
            color=_COL_WARN,
        )
    from . import tradingview

    lines = []
    cost_basis = 0.0
    for p in pos:
        mark = f"{p['mark']:.4g}" if p["mark"] is not None else "—"
        if p["pnl"] is None:
            pnl = "—"
        else:
            pnl = f"{p['pnl']:+.2f} ({p['pnl_pct']:+.1f}%)"
        side = "🟢L" if p["side"] == "long" else "🔴S"
        link = tradingview.chart_url(p["ticker"])
        cost_basis += p["entry"] * p["qty"]
        lines.append(
            f"{side} [`${p['ticker']}`]({link}) {p['qty']:g} @ {p['entry']:.4g} "
            f"→ {mark} · {pnl}"
        )
    tot = sum(p["pnl"] for p in pos if p["pnl"] is not None)
    tot_pct = f" ({tot / cost_basis * 100:+.1f}%)" if cost_basis else ""
    return _embed(
        f"💼 Positions — {len(pos)} open",
        "\n".join(lines),
        footer=f"unrealized {tot:+.2f}{tot_pct} · tap a ticker for its TV chart",
    )


def _cmd_pnl() -> discord.Embed:
    pos = portfolio.open_positions()
    unreal = sum(p["pnl"] for p in pos if p["pnl"] is not None)
    r = portfolio.realized_summary()
    wr = (
        f"{r['wins']}/{r['closed']} ({r['wins'] / r['closed'] * 100:.0f}%)"
        if r["closed"]
        else "—"
    )
    net = unreal + r["realized_pnl"]
    e = _embed("📈 P&L", color=_COL_DATA)
    e.add_field(
        name="Open", value=f"{len(pos)} pos · unrealized {unreal:+.2f}", inline=True
    )
    e.add_field(
        name="Closed",
        value=f"realized {r['realized_pnl']:+.2f} · win {wr}",
        inline=True,
    )
    sign = "🟢" if net >= 0 else "🔴"
    e.add_field(
        name="Net", value=f"{sign} {net:+.2f} (realized + unrealized)", inline=False
    )
    return e


def _cmd_calls() -> discord.Embed:
    """Itemized call sheet — open (maturing) + recently resolved verdicts.
    The per-call companion to `!scorecard` (which is the aggregate)."""
    from .models import TradingCall
    from .scorecard import _hit

    with session_scope() as s:
        opens = s.exec(
            select(TradingCall)
            .where(TradingCall.settled == False)  # noqa: E712
            .order_by(TradingCall.created_at.desc())
            .limit(10)
        ).all()
        done = s.exec(
            select(TradingCall)
            .where(TradingCall.resolved_posted_at.is_not(None))
            .order_by(TradingCall.resolved_posted_at.desc())
            .limit(10)
        ).all()

    e = _embed("📒 Calls", color=_COL_DATA)
    if opens:
        e.add_field(
            name=f"Maturing ({len(opens)})",
            value="\n".join(
                f"`${c.ticker}` {c.direction.upper()} · conv{c.conviction} "
                f"· `{c.source}`"
                for c in opens
            )[:1024],
            inline=False,
        )
    res_lines = []
    for c in done:
        ret = c.ret_5d_pct if c.ret_5d_pct is not None else c.ret_1d_pct
        if ret is None:
            continue
        mark = "✅" if _hit(c.direction, ret) else "❌"
        res_lines.append(
            f"{mark} `${c.ticker}` {c.direction.upper()} {ret:+.1f}% "
            f"· `{c.source}`"
        )
    if res_lines:
        e.add_field(
            name="Recently resolved",
            value="\n".join(res_lines)[:1024],
            inline=False,
        )
    if not opens and not res_lines:
        e.description = "No calls logged yet."
    return e


def _cmd_tv() -> discord.Embed:
    """Importable TradingView watchlist of the live book + chart deep-links.

    TradingView has no API to receive our positions/P&L, so this is the
    read-side bridge: paste the block into TV → Watchlist → ⋯ → Import.
    """
    from . import tradingview

    pos = portfolio.open_positions()
    held = sorted(portfolio.held_tickers())
    if not pos and not held:
        return _embed(
            "📊 TradingView", "Book is empty — nothing to chart yet.",
            color=_COL_WARN,
        )
    export = tradingview.watchlist_export(
        [p["ticker"] for p in pos] + held
    )
    e = _embed(
        "📊 TradingView — live book",
        f"```\n{export}\n```",
        footer="paste into TV → Watchlist → ⋯ → Import list",
        color=_COL_DATA,
    )
    if pos:
        e.add_field(
            name="Open positions",
            value=_trunc([
                f"{'🟢L' if p['side'] == 'long' else '🔴S'} "
                f"[${p['ticker']}]({tradingview.chart_url(p['ticker'])})"
                for p in pos
            ]),
            inline=False,
        )
    return e


_RESEARCH_CASHTAG_RE = re.compile(r"\$([A-Z]{1,6})")
_RESEARCH_BARE_RE = re.compile(r"\b([A-Z]{2,6})\b")


def _research_symbols(*texts: str) -> list[str]:
    """Symbols worth a company lookup, pulled from the question + post.

    Cashtags and watchlist names rank first; bare uppercase tokens (e.g. a
    user typing "what does ARX do") are accepted too but capped, with
    obvious English/jargon filtered via the shared blocklist.
    """
    blob = " ".join(t for t in texts if t)
    cash = [s for s in _RESEARCH_CASHTAG_RE.findall(blob) if s not in TICKER_BLOCKLIST]
    bare = [
        s
        for s in _RESEARCH_BARE_RE.findall(blob)
        if s not in TICKER_BLOCKLIST and s not in cash
    ]
    ordered: list[str] = []
    for s in cash + bare:
        if s not in ordered:
            ordered.append(s)
    return ordered[:3]


def _render_profiles(profiles: list[dict]) -> str:
    if not profiles:
        return "(nothing found for the symbols mentioned)"
    out: list[str] = []
    for p in profiles:
        lines = [f"### ${p['symbol']}"]
        if p.get("name"):
            lines.append(f"Name: {p['name']}")
        bits = [
            p.get(k)
            for k in ("sector", "industry", "sic_industry", "exchange", "country")
            if p.get(k)
        ]
        if bits:
            lines.append("Classification: " + " · ".join(map(str, bits)))
        if p.get("market_cap"):
            lines.append(f"Market cap: {p['market_cap']:,}")
        if p.get("website"):
            lines.append(f"Site: {p['website']}")
        if p.get("business_summary"):
            lines.append(f"Business: {p['business_summary']}")
        if p.get("recent_filings"):
            lines.append("Recent filings: " + " | ".join(p["recent_filings"]))
        if p.get("recent_news"):
            lines.append("Recent news: " + " | ".join(p["recent_news"]))
        from .narrative import recent_events

        evs = recent_events(p["symbol"], days=30, limit=6)
        if evs:
            lines.append(
                "Story so far: "
                + " | ".join(f"{e.ts:%m-%d} {e.kind}: {e.headline}" for e in evs)
            )
        out.append("\n".join(lines))
    return "\n\n".join(out)[:5500]


def register_chat_handler(bot: discord.Client) -> None:
    @bot.event
    async def on_message(msg: discord.Message) -> None:
        try:
            await _handle(msg, bot)
        except Exception as e:
            logger.exception("chat handler failure: {}", e)

    logger.info("chat handler registered")


async def _handle(msg: discord.Message, bot: discord.Client) -> None:
    if msg.author == bot.user or msg.author.bot:
        return

    content = msg.content.strip()
    if not content:
        return

    # DMs: commands only (conversation now lives in post threads). A bare DM
    # gets a pointer rather than silence.
    if msg.guild is None:
        if content.startswith(_CMD_PREFIX):
            await _handle_command(msg, content[1:].strip())
        else:
            await msg.reply(
                "DMs are command-only (`!help`). For Q&A, reply in the "
                "thread under any post — I answer there with full context."
            )
        return

    # Server messages: enforce single-guild guard.
    if msg.guild.id != settings.DISCORD_GUILD_ID:
        return

    if content.startswith(_CMD_PREFIX):
        await _handle_command(msg, content[1:].strip())
        return

    # Inside a thread the bot opened under a post: every message is a
    # follow-up — no @-mention needed, conversation is implicit.
    if isinstance(msg.channel, discord.Thread) and _is_bot_thread(msg.channel, bot):
        await _handle_thread(msg, bot)
        return

    # Elsewhere in the server, @-mentions trigger free-form Q&A.
    if bot.user in msg.mentions:
        question = re.sub(rf"<@!?{bot.user.id}>", "", content).strip()
        if question:
            await _handle_ask(msg, question)


def _is_bot_thread(thread: discord.Thread, bot: discord.Client) -> bool:
    """True if this thread hangs off one of our posts. Threads created via
    Message.create_thread are owned by the message author (the bot)."""
    return thread.owner_id == getattr(bot.user, "id", None)


async def _handle_command(msg: discord.Message, body: str) -> None:
    """Parse `!cmd args`, resolve to a single discord.Embed, reply once.

    `ask` is the only command that streams its own reply (LLM); everything
    else returns an embed through this one path so presentation is uniform.
    """
    parts = body.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "ask":
        if not arg:
            await msg.reply(embed=_usage("`!ask <question>` — or just @-mention me."))
            return
        await _handle_ask(msg, arg)
        return

    emb: discord.Embed
    if cmd in ("status", "stats"):
        emb = _status_summary()
    elif cmd in ("recent", "latest"):
        emb = _recent_filings(max(1, min(20, int(arg))) if arg.isdigit() else 5)
    elif cmd in ("ticker", "t"):
        emb = (
            _ticker_dossier(_resolve_ticker(arg))
            if arg.strip()
            else _usage("`!ticker NVDA` — also `!ticker BTC`, `!ticker ES`")
        )
    elif cmd == "news":
        t, n = _parse_news_arg(arg)
        emb = _news_summary(t, limit=n)
    elif cmd == "hold":
        emb = _cmd_hold(arg)
    elif cmd == "unhold":
        emb = _cmd_unhold(arg)
    elif cmd in ("holdings", "watch-list"):
        emb = _cmd_holdings()
    elif cmd == "buy":
        emb = _cmd_open(arg, "long")
    elif cmd == "short":
        emb = _cmd_open(arg, "short")
    elif cmd in ("close", "sell", "cover"):
        emb = _cmd_close(arg)
    elif cmd in ("positions", "book", "portfolio"):
        emb = _cmd_positions()
    elif cmd == "pnl":
        emb = _cmd_pnl()
    elif cmd in ("tv", "tradingview", "charts"):
        emb = _cmd_tv()
    elif cmd in ("scorecard", "record", "track"):
        from . import scorecard

        emb = _embed(None, scorecard.scorecard_text(), color=_COL_INFO)
    elif cmd in ("calls", "callsheet"):
        emb = _cmd_calls()
    elif cmd in ("health", "heartbeat"):
        from . import health

        emb = _embed(None, health.health_text(), color=_COL_INFO)
    elif cmd in ("theses", "thesis"):
        # `!theses` → list active running theses (compact). `!thesis <id>`
        # → detail view (body + invalidation criteria + event count).
        # Full timeline / close actions live on the dashboard's Theses
        # tab; Discord is a read surface.
        from . import thesis as _thesis_mod

        target = arg.strip()
        if target and target.isdigit():
            t = _thesis_mod.get_thesis(int(target))
            if t is None:
                emb = _ack(f"thesis #{target} not found", ok=False)
            else:
                direction = (t.get("direction") or "neutral").upper()
                state = (t.get("state") or "active").upper()
                lines = [
                    f"**{direction} ${t['ticker']}** — {t['title']}",
                    f"_{state} · conv {t.get('conviction')}/5_",
                    "",
                    t.get("body") or "",
                ]
                if t.get("target_price"):
                    lines.append(f"_Target: {t['target_price']:.4g}_")
                if t.get("horizon_days"):
                    lines.append(f"_Horizon: {t['horizon_days']}d_")
                if t.get("invalidation_criteria"):
                    lines.append("")
                    lines.append(f"**Kills it:** {t['invalidation_criteria']}")
                lines.append("")
                lines.append(
                    f"_Linked events: +{t.get('supporting_events') or 0} "
                    f"supporting / -{t.get('challenging_events') or 0} "
                    f"challenging_"
                )
                emb = _embed(
                    f"🧠 Thesis #{target}",
                    "\n".join(lines)[:3800],
                    color=_COL_INFO,
                )
        else:
            active = _thesis_mod.list_active()
            if not active:
                emb = _embed(
                    "🧠 Theses",
                    "_No active theses. The generator runs daily at 08:15 "
                    "ET, or trigger one now via `--run-once "
                    "thesis_generate`._",
                    color=_COL_INFO,
                )
            else:
                lines = ["**🧠 Active running theses**", ""]
                for t in active[:12]:
                    direction = (t.get("direction") or "neutral").upper()
                    arrow = (
                        "🟢" if direction == "LONG"
                        else "🔴" if direction == "SHORT"
                        else "⚪"
                    )
                    sup = t.get("supporting_events") or 0
                    chal = t.get("challenging_events") or 0
                    target_str = (
                        f" → {t['target_price']:.4g}"
                        if t.get("target_price") else ""
                    )
                    lines.append(
                        f"{arrow} `#{t['id']:>3}` "
                        f"**${t['ticker']}**{target_str} · "
                        f"conv {t.get('conviction')}/5 · "
                        f"+{sup} / -{chal} · "
                        f"{t['title'][:80]}"
                    )
                lines.append("")
                lines.append(
                    "_Read full body + event timeline on the dashboard's "
                    "Theses tab, or `!thesis <id>` for one._"
                )
                emb = _embed(
                    None,
                    "\n".join(lines)[:3800],
                    color=_COL_INFO,
                )
    elif cmd in ("research", "desk"):
        # `!research <prompt>` — kicks off a Research Desk task. The user
        # then opens the dashboard's Research tab to read the dossier and
        # (optionally) execute. Discord-side stays read-only on this for
        # the same reason browser-only audio works fine: the *confirm* UX
        # belongs on the dashboard where a single tap settles it.
        from . import research_desk

        text = arg.strip()
        if not text:
            emb = _embed(
                "🔬 Research desk",
                ("Usage: `!research <prompt>` — kicks off a research task. "
                 "Read the dossier and execute (or not) from the dashboard's "
                 "Research tab. "
                 f"Limits: ≤{research_desk._RATE_LIMIT_PER_DAY}/day · "
                 f"conviction floor {research_desk._CONVICTION_FLOOR}/5."),
                color=_COL_INFO,
            )
        else:
            try:
                task_id = await research_desk.run_research(text)
                emb = _embed(
                    "🔬 Research desk",
                    (f"Task **#{task_id}** running. Open the dashboard's "
                     "Research tab to see the dossier when it's ready "
                     "(~10-30s) and (if applicable) execute."),
                    color=_COL_OK,
                )
            except Exception as e:
                emb = _ack(f"research failed: {e}", ok=False)
    elif cmd in ("world", "anchor", "grounding"):
        # Dumps the LLM grounding preamble — the date-stamped "trust the
        # data" rules + world anchor that's prepended to every reasoning
        # call. Useful when the bot is saying weird things and you want
        # to verify it's running with the world state you expect.
        from . import grounding

        emb = _embed(
            "🌐 LLM grounding preamble",
            "```\n" + grounding.block()[:3800] + "\n```",
            color=_COL_INFO,
        )
    elif cmd in ("funds", "leaderboard"):
        from . import funds as _f

        # `!funds meta` → the edge readout; bare `!funds` → standings.
        emb = _embed(
            None,
            _f.meta_text() if arg.strip().lower() == "meta"
            else _f.standings_text(),
            color=_COL_INFO,
        )
    elif cmd in ("meta", "edge"):
        from . import funds as _f

        emb = _embed(None, _f.meta_text(), color=_COL_INFO)
    elif cmd == "fund":
        from . import funds as _f

        emb = (
            _embed(None, _f.fund_detail_text(arg), color=_COL_INFO)
            if arg.strip()
            else _usage("`!fund degen` (or catalyst / macro)")
        )
    elif cmd == "watch":
        emb = await _cmd_watch(arg)
    elif cmd in ("watches", "watchlist"):
        emb = _cmd_list_watches()
    elif cmd in ("unwatch", "delwatch"):
        emb = _cmd_unwatch(arg)
    elif cmd in ("timeline", "story", "history"):
        if not arg.strip():
            emb = _usage("`!timeline TICKER`")
        else:
            from .narrative import timeline_text

            emb = _embed(None, timeline_text(_resolve_ticker(arg)))
    elif cmd in ("catalysts", "calendar", "radar"):
        from .pipelines import catalysts

        emb = _embed(None, catalysts.catalysts_text())
    elif cmd == "filing":
        emb = (
            _filing_detail(arg.strip())
            if arg.strip()
            else _usage("`!filing 0001193125-26-...`")
        )
    elif cmd in ("help", "h", "?"):
        emb = _help_text()
    else:
        emb = _usage(f"Unknown command `{cmd}`. Try `!help`.")

    await msg.reply(embed=emb)


def _join_within(lines: list[str], limit: int = 4096) -> str:
    """Join lines with ``\\n``, dropping trailing lines **whole** until the
    result fits. Naive ``"\\n".join(lines)[:limit]`` slices mid-character
    and turns a long URL inside ``[title](url)`` into a half-link that
    Discord and the dashboard render as literal markdown — exactly the
    "broken last entry" symptom on long Google-News URLs. Adds a single
    ``…`` line when content had to be dropped, so the cap is visible."""
    out: list[str] = []
    used = 0
    truncated = False
    for line in lines:
        add = len(line) + (1 if out else 0)
        if used + add > limit:
            truncated = True
            break
        out.append(line)
        used += add
    if truncated:
        # make room for the trailing `…` line so the cap is always visible
        while out and used + 2 > limit:
            removed = out.pop()
            used -= len(removed) + (1 if out else 0)
        out.append("…")
    return "\n".join(out)


def _md_link(label: str, url: str | None) -> str:
    """Wrap `label` in a markdown link if `url` is non-empty, else return
    the bare label. Renders as a clickable link in both Discord embeds
    and the dashboard's markdown renderer."""
    if not url:
        return label
    safe = (label or "").replace("[", "(").replace("]", ")")
    return f"[{safe}]({url})"


def _embed_to_text(embed: discord.Embed) -> str:
    """Flatten a discord.Embed back to displayable markdown text.

    The dashboard's Lookup panel wants strings, not Discord embeds, but
    every read-only `!cmd` already returns a fully-formatted embed. This
    is the smallest adapter that keeps Discord and the dashboard pointed
    at the same source of truth (no parallel formatter that drifts).
    """
    parts: list[str] = []
    if embed.title:
        parts.append(f"**{embed.title}**")
    if embed.description:
        parts.append(embed.description)
    for f in embed.fields:
        parts.append(f"**{f.name}**\n{f.value}")
    return "\n\n".join(p for p in parts if p)


def _md_hardbreaks(text: str) -> str:
    """Force a markdown hard line-break at every single newline.

    Discord lets a bare ``\\n`` be a line break inside embed text; CommonMark
    (which the dashboard's markdown renderer follows) collapses it to a
    space, which is exactly the "no newlines anywhere" symptom on the
    Lookup panel. Appending two trailing spaces to each non-empty line is
    the canonical markdown hard-break marker — blank lines are kept as
    paragraph separators.
    """
    if not text:
        return text
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        out.append("" if line.strip() == "" else line.rstrip() + "  ")
    return "\n".join(out)


# Sentinels the Lookup panel renders verbatim — keep them tiny and uniform.
_LOOKUP_NEEDS = {
    "ticker":   "_enter a ticker first_",
    "filing":   "_enter an accession number first_",
    "timeline": "_enter a ticker first_",
}


def lookup(kind: str, arg: str = "") -> str:
    """Dashboard adapter — one entrypoint for every read-only `!cmd`.

    Returns markdown text **with hard line-breaks already applied**, so the
    dashboard's strict CommonMark renderer keeps every line on its own
    line. Reuses the same internals as Discord; new embed fields are
    inherited for free.
    """
    arg = (arg or "").strip()
    needs = _LOOKUP_NEEDS.get(kind)
    if needs and not arg:
        return needs

    if kind == "ticker":
        text = _embed_to_text(_ticker_dossier(_resolve_ticker(arg)))
    elif kind == "news":
        t, n = _parse_news_arg(arg)
        text = _embed_to_text(_news_summary(t, limit=n))
    elif kind == "filing":
        text = _embed_to_text(_filing_detail(arg))
    elif kind == "timeline":
        from .narrative import timeline_text
        text = timeline_text(_resolve_ticker(arg))
    elif kind == "recent":
        n = int(arg) if arg.isdigit() else 5
        text = _embed_to_text(_recent_filings(max(1, min(20, n))))
    elif kind == "catalysts":
        from .pipelines import catalysts
        text = catalysts.catalysts_text()
    elif kind == "status":
        text = _embed_to_text(_status_summary())
    else:
        return f"_unknown lookup kind: `{kind}`_"
    return _md_hardbreaks(text)


def _trunc(lines: list[str], limit: int = 1024) -> str:
    """Join lines, trimming to a Discord field's 1024-char ceiling."""
    out = "\n".join(lines)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _help_text() -> discord.Embed:
    e = _embed("📡 Sentinel — commands", color=_COL_INFO)
    e.add_field(
        name="Data",
        value=(
            "`!status` — counts, watchlist size, latest filing\n"
            "`!health` — job runs / failures / ingest volumes (24h)\n"
            "`!recent [N]` — last N posted filings (default 5)\n"
            "`!ticker NVDA` — full dossier (also `BTC`, `ES`)\n"
            "`!news [TICKER]` — recent news, ticker or macro\n"
            "`!filing <accession>` — one filing in full\n"
            "`!timeline TICKER` — the recorded story over time\n"
            "`!catalysts` — earnings / macro / OPEX radar\n"
            "`!ask <question>` — free-form Q&A (or @-mention me)"
        ),
        inline=False,
    )
    e.add_field(
        name="Paper trading",
        value=(
            "`!buy TICKER QTY [price]` · `!short TICKER QTY` · "
            "`!close TICKER`\n"
            "`!positions` · `!pnl` — book + net unrealized/realized\n"
            "`!tv` — importable TradingView watchlist + chart links\n"
            "`!scorecard` — bot's call track record (aggregate)\n"
            "`!calls` — itemized call sheet (maturing + resolved verdicts)\n"
            "`!funds` · `!fund <name>` · `!meta` — wallets + edge readout"
        ),
        inline=False,
    )
    e.add_field(
        name="Watch & alerts",
        value=(
            "`!hold TICKER` · `!unhold TICKER` · `!holdings` — relevance watch\n"
            "`!watch <plain English>` · `!watches` · `!unwatch <id>`"
        ),
        inline=False,
    )
    e.set_footer(text="Tip: reply in any post's thread to dig in there.")
    return e


def _status_summary() -> discord.Embed:
    now = datetime.now(timezone.utc)
    one_d = now - timedelta(hours=24)
    with session_scope() as s:
        wl = s.exec(select(func.count()).select_from(Watchlist)).one()
        filings_24h = s.exec(
            select(func.count()).select_from(Filing).where(Filing.filed_at >= one_d)
        ).one()
        posted_24h = s.exec(
            select(func.count()).select_from(Filing)
            .where(Filing.posted_at.is_not(None))
            .where(Filing.posted_at >= one_d)
        ).one()
        prio = s.exec(
            select(func.count()).select_from(Filing)
            .where(Filing.materiality_score == 3)
            .where(Filing.filed_at >= one_d)
        ).one()
        latest = s.exec(
            select(Filing).order_by(Filing.filed_at.desc()).limit(1)
        ).first()

    e = _embed("📊 Status", color=_COL_DATA, footer=now.strftime("%Y-%m-%d %H:%M UTC"))
    e.add_field(name="Watchlist", value=str(wl), inline=True)
    e.add_field(
        name="Filings (24h)",
        value=f"{filings_24h} in · {posted_24h} posted · {prio} priority",
        inline=True,
    )
    e.add_field(
        name="Latest",
        value=(
            f"${latest.ticker or latest.cik} {latest.form_type} · "
            f"{latest.filed_at:%m-%d %H:%M} UTC"
            if latest
            else "—"
        ),
        inline=False,
    )
    return e


def _recent_filings(n: int) -> discord.Embed:
    with session_scope() as s:
        rows = s.exec(
            select(Filing)
            .where(Filing.posted_at.is_not(None))
            .order_by(Filing.filed_at.desc())
            .limit(n)
        ).all()
    if not rows:
        return _embed("🗂️ Recent filings", "None posted yet.", color=_COL_WARN)
    lines = [
        f"`{f.filed_at:%m-%d %H:%M}` **${f.ticker or f.cik}** "
        f"{_md_link(f.form_type, f.primary_doc_url)} · "
        f"{f.materiality_score if f.materiality_score is not None else '—'}/3"
        f" → #{f.channel}"
        for f in rows
    ]
    return _embed(
        f"🗂️ Recent filings — {len(rows)}", _join_within(lines)
    )


def _ticker_dossier(ticker: str) -> discord.Embed:
    from .ingesters.crypto_micro import micro_for
    from .portfolio import is_held

    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    cutoff_1d = now - timedelta(hours=24)

    with session_scope() as s:
        filings = s.exec(
            select(Filing)
            .where(Filing.ticker == ticker)
            .where(Filing.filed_at >= cutoff_30d)
            .order_by(Filing.filed_at.desc())
            .limit(8)
        ).all()
        pc = s.get(PriceContext, ticker)
        reddit_24h = s.exec(
            select(func.count()).select_from(RedditMention)
            .where(RedditMention.ticker == ticker)
            .where(RedditMention.created_at >= cutoff_1d)
        ).one()
        hn_24h = s.exec(
            select(func.count()).select_from(HnMention)
            .where(HnMention.ticker == ticker)
            .where(HnMention.created_at >= cutoff_1d)
        ).one()
        pulses = s.exec(
            select(SocialPulse)
            .where(SocialPulse.ticker == ticker)
            .where(SocialPulse.created_at >= cutoff_30d)
            .order_by(SocialPulse.created_at.desc())
            .limit(3)
        ).all()
        news_items = s.exec(
            select(NewsItem)
            .where(NewsItem.ticker == ticker)
            .where(NewsItem.published_at >= cutoff_1d)
            .order_by(NewsItem.published_at.desc())
            .limit(5)
        ).all()

    held = is_held(ticker)
    e = _embed(
        f"{'📌 ' if held else ''}${ticker} — 30-day dossier",
        color=_COL_OK if held else _COL_DATA,
    )

    if pc:
        price = (
            f"{pc.last_price:.4g} · 1d {pc.change_1d_pct * 100:+.1f}% · "
            f"5d {pc.change_5d_pct * 100:+.1f}% · vol {pc.volume_vs_20d_avg:.1f}x"
        )
    else:
        price = "no context yet"
    e.add_field(name="Price", value=price, inline=False)

    m = micro_for(ticker)
    if m:
        bits = []
        if "funding_rate_pct" in m:
            bits.append(f"funding {m['funding_rate_pct']:+.4f}%")
        if "oi_change_24h_pct" in m:
            bits.append(f"OI {m['oi_change_24h_pct']:+.1f}%/24h")
        if "orderbook_imbalance" in m:
            bits.append(f"book {m['orderbook_imbalance']:+.2f}")
        if bits:
            e.add_field(
                name=f"Microstructure ({m['venue']})",
                value=" · ".join(bits),
                inline=False,
            )

    e.add_field(
        name="Social (24h)", value=f"Reddit {reddit_24h} · HN {hn_24h}", inline=True
    )

    if filings:
        e.add_field(
            name=f"Filings ({len(filings)})",
            value=_trunc([
                f"`{f.filed_at:%m-%d}` **{f.form_type}** "
                f"[{f.materiality_score if f.materiality_score is not None else '—'}/3] "
                f"{(f.summary or '')[:110].replace(chr(10), ' ')}"
                for f in filings
            ]),
            inline=False,
        )

    if news_items:
        rows = []
        for n in news_items:
            impact = ""
            if n.impact_1d_pct is not None:
                impact = f" → {n.impact_1d_pct * 100:+.1f}% 1d"
            elif n.impact_1h_pct is not None:
                impact = f" → {n.impact_1h_pct * 100:+.1f}% 1h"
            rows.append(
                f"`{n.published_at:%m-%d %H:%M}` "
                f"*{n.source.split(':')[-1]}* "
                f"{_md_link(n.title[:110], n.url)}{impact}"
            )
        e.add_field(name="News (24h)", value=_trunc(rows), inline=False)

    if pulses:
        e.add_field(
            name="Recent pulses",
            value=_trunc([
                f"`{p.created_at:%m-%d}` {p.summary[:140]}" for p in pulses
            ]),
            inline=False,
        )
    return e


_NEWS_DEFAULT_N = 15
_NEWS_MAX_N = 100


def _is_known_ticker(t: str) -> bool:
    """True iff `t` resolves to a row in Watchlist. Cheap (single indexed
    lookup) — used to disambiguate `_parse_news_arg` for tickers that
    happen to look numeric (Asian markets: 7203 Toyota, 0700 Tencent).
    """
    if not t:
        return False
    with session_scope() as s:
        return s.exec(
            select(Watchlist).where(Watchlist.ticker == t)
        ).first() is not None


def _parse_news_arg(arg: str) -> tuple[Optional[str], int]:
    """Pull (ticker, count) out of a free-form arg.

    Prefers watchlist match over `isdigit()` so a (hypothetical) all-digit
    HK/JP ticker isn't silently misread as a count. Falls back to digits →
    count, otherwise the first alpha token is the ticker. Empty → macro
    feed, default count.
    """
    ticker: Optional[str] = None
    count = _NEWS_DEFAULT_N
    for p in (arg or "").split():
        cand = _resolve_ticker(p)
        if ticker is None and _is_known_ticker(cand):
            ticker = cand
        elif p.isdigit():
            count = max(1, min(_NEWS_MAX_N, int(p)))
        elif ticker is None:
            ticker = cand
    return ticker, count


def _news_summary(
    ticker: Optional[str], *, limit: int = _NEWS_DEFAULT_N
) -> discord.Embed:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    with session_scope() as s:
        q = select(NewsItem).where(NewsItem.published_at >= cutoff)
        if ticker:
            q = q.where(NewsItem.ticker == ticker)
        else:
            q = q.where(NewsItem.is_macro == True)  # noqa: E712
        items = s.exec(
            q.order_by(NewsItem.published_at.desc()).limit(limit)
        ).all()
    title = f"📰 News — ${ticker} (24h)" if ticker else "📰 Macro news (24h)"
    if not items:
        return _embed(title, "Nothing in the last 24h.", color=_COL_WARN)
    lines = []
    for n in items:
        tag = f" ${n.ticker}" if n.ticker and not ticker else ""
        lines.append(
            f"`{n.published_at:%m-%d %H:%M}`{tag} "
            f"**{n.source.split(':')[-1]}** "
            f"{_md_link(n.title[:120], n.url)}"
        )
    return _embed(title, _join_within(lines))


def _filing_detail(accession_number: str) -> discord.Embed:
    with session_scope() as s:
        f = s.exec(
            select(Filing).where(Filing.accession_number == accession_number)
        ).first()
    if f is None:
        return _embed(
            "Filing not found",
            f"No filing with accession `{accession_number}` in the DB.",
            color=_COL_WARN,
        )
    e = _embed(
        f"${f.ticker or f.cik} {f.form_type}",
        (f.summary or "(no summary)")[:4096],
        url=f.primary_doc_url,
        footer=f.accession_number,
    )
    e.add_field(
        name="Filed", value=f"{f.filed_at:%Y-%m-%d %H:%M} UTC", inline=True
    )
    e.add_field(
        name="Materiality",
        value=(
            f"{f.materiality_score}/3 — {f.materiality_reason or ''}"
            if f.materiality_score is not None
            else "not scored"
        )[:1024],
        inline=True,
    )
    return e


async def _starter_post_text(thread: discord.Thread) -> str:
    """The post a thread hangs off of. For message-created threads the thread
    id equals the starter message id, so we can fetch it from the parent even
    when it isn't cached.
    """
    starter = thread.starter_message
    if starter is None:
        try:
            parent = thread.parent
            if parent is not None:
                starter = await parent.fetch_message(thread.id)
        except Exception as e:
            logger.debug("starter fetch failed for thread {}: {}", thread.id, e)
            return ""
    return extract_post_text(starter) if starter else ""


async def _seed_still_pending(channel, bot) -> bool:
    """True while the Ask-AI brief is still generating (the placeholder seed
    message is present). Answering a user message in this window is exactly
    the 'you talk → it briefs → it answers you' inversion — so we hold off
    until the brief has replaced the placeholder."""
    try:
        async for m in channel.history(limit=12):
            if m.author == bot.user and (m.content or "").strip() == SEED_PENDING:
                return True
    except Exception:
        return False
    return False


async def _handle_thread(msg: discord.Message, bot: discord.Client) -> None:
    """Answer a follow-up inside a post's discussion thread, grounded in the
    original post plus the thread's running history.
    """
    if await _seed_still_pending(msg.channel, bot):
        # Brief not posted yet — let it land first; it addresses the post,
        # then the user's follow-ups flow in order.
        return

    async with msg.channel.typing():
        post_text = await _starter_post_text(msg.channel)

        history_lines: list[str] = []
        try:
            async for m in msg.channel.history(limit=_THREAD_HISTORY_LIMIT, before=msg):
                speaker = "Bot" if m.author == bot.user else "User"
                text = (m.content or "").strip()
                if not text:
                    continue
                history_lines.append(f"{speaker}: {text[:600]}")
        except discord.Forbidden:
            history_lines = []
        history = "\n".join(reversed(history_lines)) or "(start of thread)"

        symbols = _research_symbols(msg.content, post_text)
        profiles = (
            await asyncio.to_thread(research.profiles_for, symbols)
            if symbols
            else []
        )

        rendered = THREAD_CHAT_PROMPT.safe_substitute(
            post=(post_text or "(original post unavailable)")[:2500],
            research=_render_profiles(profiles),
            history=history[:5000],
            latest=msg.content[:1000],
        )
        reply = await asyncio.to_thread(
            get_llm().complete, rendered, model="light", max_tokens=1600
        )
        if not reply or reply.startswith("[LLM_ERROR]"):
            await msg.reply("LLM unreachable — try again in a moment.")
            return
        parts = chunk_text(highlight_markdown(reply))
        await msg.reply(parts[0])
        for _p in parts[1:]:
            await msg.channel.send(_p)


async def answer_question(question: str, *, max_tokens: int = 1600) -> str:
    """The single free-form Q&A path: retrieve grounding context, ask the
    LLM, return the raw answer text.

    Both Discord (`!ask` / @-mention) and the dashboard chatbox call this so
    they share one context builder, one prompt, and one voice — a divergence
    here would mean "the bot answers differently depending where you ask",
    which is exactly the kind of split this codebase avoids. Callers own
    presentation (Discord chunks for its 2000-char cap; the web renders
    markdown directly). Returns the sentinel `"[LLM_ERROR]"` on failure so
    callers decide how to surface it.
    """
    question = (question or "").strip()
    if not question:
        return ""
    context = _build_context(question)
    symbols = _research_symbols(question)
    if symbols:
        context["company_profiles"] = await asyncio.to_thread(
            research.profiles_for, symbols
        )
    rendered = CHAT_PROMPT.safe_substitute(
        question=question[:500],
        context_json=json.dumps(context, default=str)[:9000],
    )
    reply = await asyncio.to_thread(
        get_llm().complete, rendered, model="light", max_tokens=max_tokens
    )
    if not reply or reply.startswith("[LLM_ERROR]"):
        return "[LLM_ERROR]"
    return reply


async def _handle_ask(msg: discord.Message, question: str) -> None:
    async with msg.channel.typing():
        reply = await answer_question(question)
        if reply == "[LLM_ERROR]" or not reply:
            await msg.reply("LLM unreachable — try again in a moment.")
            return
        # Discord caps a message at 2000 chars — chunk, never truncate.
        parts = chunk_text(highlight_markdown(reply))
        await msg.reply(parts[0])
        for _p in parts[1:]:
            await msg.channel.send(_p)


def _build_context(question: str) -> dict:
    """Pull relevant DB rows for an open-ended question.

    Strategy: extract any cashtags/bare tickers from the question; if found,
    include ticker-specific data. Always include a snapshot of the day's
    high-materiality filings as general background.
    """
    candidates = {m.upper() for m in _TICKER_RE.findall(question.upper())}
    # Filter to actual watchlist tickers.
    tickers: list[str] = []
    if candidates:
        with session_scope() as s:
            rows = s.exec(
                select(Watchlist).where(Watchlist.ticker.in_(list(candidates)))
            ).all()
            tickers = sorted({r.ticker for r in rows if r.ticker})

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    context: dict = {"as_of": now.isoformat(), "tickers_in_question": tickers}

    with session_scope() as s:
        # General background: today's material filings.
        today_material = s.exec(
            select(Filing)
            .where(Filing.filed_at >= cutoff_24h)
            .where(Filing.materiality_score.is_not(None))
            .order_by(Filing.materiality_score.desc(), Filing.filed_at.desc())
            .limit(15)
        ).all()
        context["recent_filings"] = [_compact_filing(f) for f in today_material]

        # Recent pulses.
        recent_pulses = s.exec(
            select(SocialPulse)
            .where(SocialPulse.created_at >= cutoff_7d)
            .order_by(SocialPulse.created_at.desc())
            .limit(10)
        ).all()
        context["recent_pulses"] = [
            {"ticker": p.ticker, "summary": p.summary, "ts": p.created_at}
            for p in recent_pulses
        ]

        # Macro news headlines (always include for geopolitical context).
        macro_news = s.exec(
            select(NewsItem)
            .where(NewsItem.is_macro == True)  # noqa: E712
            .where(NewsItem.published_at >= cutoff_24h)
            .order_by(NewsItem.published_at.desc())
            .limit(20)
        ).all()
        context["macro_news"] = [
            {"source": n.source.split(":")[-1], "title": n.title}
            for n in macro_news
        ]

        # Ticker-specific drill-down.
        if tickers:
            per_ticker: dict[str, dict] = {}
            for t in tickers:
                filings = s.exec(
                    select(Filing)
                    .where(Filing.ticker == t)
                    .where(Filing.filed_at >= cutoff_7d)
                    .order_by(Filing.filed_at.desc())
                    .limit(5)
                ).all()
                pc = s.get(PriceContext, t)
                news = s.exec(
                    select(NewsItem)
                    .where(NewsItem.ticker == t)
                    .where(NewsItem.published_at >= cutoff_7d)
                    .order_by(NewsItem.published_at.desc())
                    .limit(5)
                ).all()
                per_ticker[t] = {
                    "filings": [_compact_filing(f) for f in filings],
                    "price": (
                        {
                            "last": pc.last_price,
                            "change_1d_pct": pc.change_1d_pct,
                            "change_5d_pct": pc.change_5d_pct,
                            "volume_ratio": pc.volume_vs_20d_avg,
                        }
                        if pc
                        else None
                    ),
                    "news": [
                        {
                            "source": n.source.split(":")[-1],
                            "title": n.title,
                            "impact_1h_pct": n.impact_1h_pct,
                            "impact_1d_pct": n.impact_1d_pct,
                        }
                        for n in news
                    ],
                }
            context["per_ticker"] = per_ticker

    return context


def _compact_filing(f: Filing) -> dict:
    return {
        "ticker": f.ticker,
        "form_type": f.form_type,
        "filed_at": f.filed_at,
        "score": f.materiality_score,
        "reason": f.materiality_reason,
        "summary": (f.summary or "")[:300],
    }
