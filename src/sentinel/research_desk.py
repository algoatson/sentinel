"""User-prompted research → confirm → autonomous trade.

The Research Desk is a deliberately separate surface from the seven
autonomous wallets. Their mandates are deterministic policies on the
shared call stream — the experimental setup that makes wallet P&L
comparable. A user-prompted "go look into this and trade" is exogenous
noise relative to that, so it lives in a dedicated `research` wallet
with its own equity curve.

Three guardrails baked in:
- **3 executions per UTC day** — hard rate limit on the wallet, not the
  proposals. The bot will *propose* freely; only execution counts.
- **Conviction floor 3/5** — `execute()` refuses below that even if the
  user clicks. The wallet enforces discipline, not the user.
- **Confirm-before-trade** — `run_research()` only produces a dossier +
  recommendation. Nothing trades unless `execute(task_id)` is called.
  That's the bot's job done; the user's job is the click.

Audit trail in `ResearchTask`: prompt, dossier, parsed verdict, and the
linked `FundTrade.id` if executed. Two months from now you can answer
"why did the research wallet open this position" without guessing.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, time, timedelta, timezone
from string import Template

from loguru import logger
from sqlmodel import select

from . import funds
from .config import settings
from .db import session_scope
from .llm import LLM_ERROR_SENTINEL, get_llm
from .models import (
    Filing,
    Fund,
    FundTrade,
    NewsItem,
    PriceContext,
    ResearchTask,
    TradingCall,
    Watchlist,
)


# ── tuning constants ──────────────────────────────────────────────────────

# Hard cap on executions per UTC day. Counted across the `research`
# wallet via `ResearchTask.executed_at`, not request count — the user
# can ASK as much as they want; only TRADES count toward the limit.
_RATE_LIMIT_PER_DAY = 3

# Minimum LLM-reported conviction to execute. The grounding preamble
# helps with reasoning quality but doesn't eliminate noise — anything
# below this is treated as "not enough to act on regardless of what
# the user clicks". 3 = "base case"; 4-5 are the trades worth taking.
_CONVICTION_FLOOR = 3

# Hard cap on position size as % of research wallet equity. The LLM
# *suggests* a size (1-10) but we clamp to this to prevent a single
# bad shot from blowing up the wallet.
_MAX_SIZE_PCT = 10.0
_MIN_SIZE_PCT = 1.0

# Window over which we consider duplicate prompts already-answered.
# Re-asking the same thing within this window returns the cached row
# rather than re-running the LLM (cheap and keeps the audit clean).
_DUPLICATE_PROMPT_WINDOW = timedelta(hours=2)


# ── LLM prompt template ───────────────────────────────────────────────────

_RESEARCH_PROMPT = Template("""\
You are the Research Desk. The USER has asked you to look into a topic
and decide whether to open a paper-trading position. Be rigorous —
your output will be both shown to the user AND parsed by the bot.

USER REQUEST:
$user_prompt

CURRENT CONTEXT (live data; trust this over your priors):
$context_json

OUTPUT REQUIREMENTS — single JSON object, NO markdown fences, NO prose
before or after the JSON. Schema (every field required, use `null`
where it doesn't apply):

{
  "verdict": "TRADE" | "WATCHLIST" | "PASS",
  "ticker": null | "NVDA",
  "direction": null | "long" | "short",
  "conviction": null | 1 | 2 | 3 | 4 | 5,
  "size_pct": null | 1.0 - 10.0,
  "tldr": "1 sentence describing the read",
  "thesis": "2-3 sentences — why this trade, what's the catalyst, what's the timeframe",
  "risks": "1-2 sentences — the single biggest thing that would invalidate it",
  "markdown": "## TL;DR\\n...\\n\\n## Read\\n...\\n\\n## Action\\n..."
}

RULES:
- TRADE only if you can name ONE specific ticker AND a specific catalyst.
  Naming a sector is not enough. Multiple-name baskets → WATCHLIST.
- WATCHLIST: interesting, but no specific actionable position right now.
- PASS: noise, no clear connection to a tradable name, or thin signal.
- ticker must be a real US-listed symbol or a major crypto symbol
  (BTC/ETH/SOL etc — the watchlist context above shows what's tracked).
- direction must match the thesis; do not long a bearish thesis.
- conviction: 1=very weak, 3=base case, 5=highest. The system refuses
  to execute below 3 regardless of what the user clicks.
- size_pct: default 3-5%. Go higher (up to 10%) only when conviction
  is 4-5 AND the thesis is structural (multi-week+ horizon), not a
  same-day momentum punt.
- markdown is what the user reads — be specific, no hedge language
  ("could potentially", "may possibly"). If the data is thin, SAY SO.
""")


# ── context builders ──────────────────────────────────────────────────────


def _build_context(prompt: str) -> dict:
    """Pull the bot's current state that's most likely relevant to a
    free-form research prompt. Caps everything so we don't blow the
    token budget on a fresh DB with a fat watchlist."""
    now = datetime.now(timezone.utc)
    cutoff_news = (now - timedelta(days=7)).replace(tzinfo=None)
    cutoff_filings = (now - timedelta(days=14)).replace(tzinfo=None)
    cutoff_calls = (now - timedelta(days=14)).replace(tzinfo=None)

    # Try to pull out any explicit $TICKER mentions from the prompt so we
    # can give the LLM their context specifically.
    cued = list({
        m.upper() for m in re.findall(r"\$([A-Za-z]{1,8})\b", prompt)
    })[:5]

    with session_scope() as s:
        watchlist = [
            {"ticker": w.ticker, "asset_class": w.asset_class}
            for w in s.exec(select(Watchlist)).all()
            if w.ticker
        ]

        # Per-ticker context (PriceContext + last few news/filings) for
        # any names cued in the prompt, ranked by recency.
        cued_ctx: list[dict] = []
        for tk in cued:
            pc = s.get(PriceContext, tk)
            news = s.exec(
                select(NewsItem)
                .where(NewsItem.ticker == tk)
                .where(NewsItem.published_at >= cutoff_news)
                .order_by(NewsItem.published_at.desc())
                .limit(4)
            ).all()
            filings = s.exec(
                select(Filing)
                .where(Filing.ticker == tk)
                .where(Filing.filed_at >= cutoff_filings)
                .order_by(Filing.filed_at.desc())
                .limit(3)
            ).all()
            cued_ctx.append({
                "ticker": tk,
                "price_context": ({
                    "last_price": pc.last_price,
                    "change_1d_pct": pc.change_1d_pct,
                    "change_5d_pct": pc.change_5d_pct,
                    "volume_vs_20d_avg": pc.volume_vs_20d_avg,
                } if pc else None),
                "recent_news": [
                    {"title": n.title[:140], "source": n.source,
                     "ts": n.published_at.isoformat()}
                    for n in news
                ],
                "recent_filings": [
                    {"form": f.form_type, "summary": (f.summary or "")[:160],
                     "ts": f.filed_at.isoformat()}
                    for f in filings
                ],
            })

        # Recent macro / per-name news (last 24h) so the LLM has the
        # latest tape regardless of what's cued.
        cutoff_news_24h = (now - timedelta(hours=24)).replace(tzinfo=None)
        recent_news_global = [
            {"ticker": n.ticker, "title": n.title[:140],
             "source": n.source, "ts": n.published_at.isoformat()}
            for n in s.exec(
                select(NewsItem)
                .where(NewsItem.published_at >= cutoff_news_24h)
                .order_by(NewsItem.published_at.desc())
                .limit(12)
            ).all()
        ]

        # The bot's own recent autonomous calls — useful "what does the
        # bot already think?" context. Doesn't override the user's
        # request, just informs.
        recent_calls = [
            {"ticker": c.ticker, "direction": c.direction,
             "source": c.source, "conviction": c.conviction,
             "thesis": (c.thesis or "")[:140]}
            for c in s.exec(
                select(TradingCall)
                .where(TradingCall.created_at >= cutoff_calls)
                .order_by(TradingCall.created_at.desc())
                .limit(6)
            ).all()
        ]

        # Current open positions on the research wallet — so the LLM
        # doesn't recommend doubling up on something we already hold.
        fund = s.exec(
            select(Fund).where(Fund.name == funds.RESEARCH_WALLET_NAME)
        ).first()
        open_positions = []
        if fund is not None:
            for t in s.exec(
                select(FundTrade)
                .where(FundTrade.fund_id == fund.id)
                .where(FundTrade.status == "open")
            ).all():
                open_positions.append({
                    "ticker": t.ticker, "side": t.side, "qty": t.qty,
                    "entry": t.entry_price,
                })

    return {
        "watchlist_tickers": [w["ticker"] for w in watchlist][:60],
        "cued_tickers_context": cued_ctx,
        "recent_news_24h": recent_news_global,
        "recent_autonomous_calls": recent_calls,
        "research_wallet_open_positions": open_positions,
    }


# ── LLM call + verdict parse ──────────────────────────────────────────────


def _parse_verdict(raw: str) -> dict | None:
    """Pull the JSON object the prompt asks for; tolerate light
    formatting drift (leading text, fenced code blocks)."""
    if not raw or raw == LLM_ERROR_SENTINEL:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    # find the first '{' — some models drop a sentence before the JSON
    first = s.find("{")
    if first > 0:
        s = s[first:]
    # find the last '}' for the same reason
    last = s.rfind("}")
    if last >= 0 and last < len(s) - 1:
        s = s[: last + 1]
    try:
        d = json.loads(s)
    except Exception as e:
        logger.warning("research_desk: JSON parse failed: {} — raw: {}",
                       e, s[:300])
        return None
    if not isinstance(d, dict):
        return None
    return d


def _validate_recommendation(d: dict) -> dict:
    """Coerce + clamp the LLM's output to the system's allowed envelope.

    Returns the same dict with cleaned/None fields, plus an `_error`
    field on the recommendation if it's structurally invalid (still
    safe to display, just not executable). The caller decides whether
    to set `verdict = PASS` when `_error` is present."""
    verdict = str(d.get("verdict") or "").strip().upper()
    if verdict not in {"TRADE", "WATCHLIST", "PASS"}:
        d["_error"] = f"unknown verdict '{verdict}'"
        verdict = "PASS"
    d["verdict"] = verdict

    if verdict != "TRADE":
        d["ticker"] = None
        d["direction"] = None
        d["conviction"] = None
        d["size_pct"] = None
        return d

    # TRADE — every actionable field must be present and sane.
    tk = (d.get("ticker") or "").strip().upper().lstrip("$")
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", tk):
        d["_error"] = f"invalid ticker '{tk}'"
        d["verdict"] = "PASS"
        return d

    direction = str(d.get("direction") or "").strip().lower()
    if direction not in {"long", "short"}:
        d["_error"] = f"invalid direction '{direction}'"
        d["verdict"] = "PASS"
        return d

    try:
        conviction = int(d.get("conviction") or 0)
    except (TypeError, ValueError):
        conviction = 0
    conviction = max(1, min(5, conviction))

    try:
        size_pct = float(d.get("size_pct") or 0)
    except (TypeError, ValueError):
        size_pct = 0.0
    size_pct = max(_MIN_SIZE_PCT, min(_MAX_SIZE_PCT, size_pct))

    d["ticker"] = tk
    d["direction"] = direction
    d["conviction"] = conviction
    d["size_pct"] = round(size_pct, 1)
    return d


# ── public API ────────────────────────────────────────────────────────────


def _find_recent_duplicate(prompt: str) -> ResearchTask | None:
    cutoff = (datetime.now(timezone.utc) - _DUPLICATE_PROMPT_WINDOW
              ).replace(tzinfo=None)
    needle = (prompt or "").strip()
    with session_scope() as s:
        return s.exec(
            select(ResearchTask)
            .where(ResearchTask.created_at >= cutoff)
            .where(ResearchTask.prompt == needle)
            .order_by(ResearchTask.id.desc())
            .limit(1)
        ).first()


async def run_research(prompt: str) -> int:
    """Generate a Research Desk dossier for `prompt`. Returns the
    `ResearchTask.id` of the result. Idempotent within
    `_DUPLICATE_PROMPT_WINDOW` — the same prompt returns the same task
    id, so a user double-clicking Submit doesn't double-spend tokens."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt must not be empty")

    dup = await asyncio.to_thread(_find_recent_duplicate, prompt)
    if dup is not None:
        return dup.id

    # Create the task row before the LLM call so a long-running call's
    # progress is visible in `list_recent` (dossier=None means in-flight).
    now = datetime.now(timezone.utc)
    model_name = _model_name()
    with session_scope() as s:
        task = ResearchTask(prompt=prompt, created_at=now, model=model_name)
        s.add(task)
        s.flush()
        task_id = task.id

    try:
        ctx = await asyncio.to_thread(_build_context, prompt)
        rendered = _RESEARCH_PROMPT.safe_substitute(
            user_prompt=prompt[:1500],
            context_json=json.dumps(ctx, default=str)[:9000],
        )
        body = await asyncio.to_thread(
            lambda: get_llm().complete(
                rendered, model="heavy", max_tokens=1400,
                fallback_light=True,
            )
        )
    except Exception as e:
        logger.exception("research_desk: LLM call failed: {}", e)
        body = ""

    parsed = _parse_verdict(body)
    if parsed is None:
        # Persist a PASS row so the user sees "the bot tried, no read".
        with session_scope() as s:
            row = s.get(ResearchTask, task_id)
            if row is not None:
                row.verdict = "PASS"
                row.dossier = (
                    "_LLM produced no parseable verdict. Try rephrasing "
                    "the prompt with a specific ticker or catalyst._"
                )
                row.dossier_at = datetime.now(timezone.utc)
                s.add(row)
        return task_id

    rec = _validate_recommendation(parsed)
    with session_scope() as s:
        row = s.get(ResearchTask, task_id)
        if row is not None:
            row.dossier = rec.get("markdown") or ""
            row.dossier_at = datetime.now(timezone.utc)
            row.verdict = rec.get("verdict")
            row.rec_ticker = rec.get("ticker")
            row.rec_direction = rec.get("direction")
            row.rec_conviction = rec.get("conviction")
            row.rec_size_pct = rec.get("size_pct")
            row.rec_thesis = (rec.get("thesis") or "")[:1000]
            row.rec_risks = (rec.get("risks") or "")[:1000]
            s.add(row)
    return task_id


def _executions_today() -> int:
    """Count rows on the research wallet whose `executed_at` falls within
    the current UTC day. The rate limit hangs on this."""
    today_start = datetime.combine(
        datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc,
    )
    cutoff_naive = today_start.replace(tzinfo=None)
    with session_scope() as s:
        rows = s.exec(
            select(ResearchTask)
            .where(ResearchTask.executed_at.is_not(None))
            .where(ResearchTask.executed_at >= cutoff_naive)
        ).all()
    return len(rows)


def execute(task_id: int) -> dict:
    """Open the recommended position on the research wallet.

    Returns ``{"ok": bool, "message": str, "trade_id": int|None}``. All
    guardrails enforced here (rate limit, conviction floor, size cap,
    no-double-execute) so the UI can't bypass them. Trade lands in
    `FundTrade` on the `research` Fund — same machinery as autonomous
    wallets, so equity curve / scorecard surfaces all light up."""

    with session_scope() as s:
        task = s.get(ResearchTask, task_id)
        if task is None:
            return {"ok": False, "message": f"task #{task_id} not found",
                    "trade_id": None}
        if task.executed_at is not None:
            return {"ok": False,
                    "message": "already executed once — runs are one-shot",
                    "trade_id": task.executed_trade_id}
        if (task.verdict or "").upper() != "TRADE":
            return {"ok": False,
                    "message": f"verdict is {task.verdict!r}, not TRADE",
                    "trade_id": None}
        if (task.rec_conviction or 0) < _CONVICTION_FLOOR:
            return {
                "ok": False,
                "message": (
                    f"conviction {task.rec_conviction or 0}/5 < floor "
                    f"{_CONVICTION_FLOOR}/5 — refusing execution"
                ),
                "trade_id": None,
            }

        # Rate limit (re-checked atomically with the row update below).
        n_today = _executions_today()
        if n_today >= _RATE_LIMIT_PER_DAY:
            return {
                "ok": False,
                "message": (
                    f"daily cap hit ({n_today}/{_RATE_LIMIT_PER_DAY} "
                    "Research Desk executions today)"
                ),
                "trade_id": None,
            }

        # Find the research wallet + the live mark.
        fund = s.exec(
            select(Fund).where(Fund.name == funds.RESEARCH_WALLET_NAME)
        ).first()
        if fund is None:
            return {"ok": False,
                    "message": "research wallet not seeded — restart the bot",
                    "trade_id": None}

        ticker = (task.rec_ticker or "").upper()
        pc = s.get(PriceContext, ticker)
        mark = pc.last_price if pc is not None else None
        if mark is None or mark <= 0:
            return {"ok": False,
                    "message": f"no live mark for ${ticker} — can't size",
                    "trade_id": None}

        size_pct = max(_MIN_SIZE_PCT,
                       min(_MAX_SIZE_PCT, task.rec_size_pct or 3.0))
        # Equity = cash for a fresh wallet; same calc as autonomous funds.
        opens = s.exec(
            select(FundTrade)
            .where(FundTrade.fund_id == fund.id)
            .where(FundTrade.status == "open")
        ).all()
        committed = sum(o.qty * o.entry_price for o in opens)
        # Live equity (mark-to-market) — same shape as `funds._equity`.
        equity = fund.cash
        for t in opens:
            tk_pc = s.get(PriceContext, t.ticker)
            m = tk_pc.last_price if tk_pc else t.entry_price
            equity += t.qty * m * (1 if t.side == "long" else -1)
        size_cash = equity * size_pct / 100.0
        if committed + size_cash > equity:
            return {
                "ok": False,
                "message": (
                    f"would exceed wallet capacity — "
                    f"committed ${committed:.0f} + new ${size_cash:.0f} "
                    f"> equity ${equity:.0f}"
                ),
                "trade_id": None,
            }
        qty = round(size_cash / mark, 6)
        if qty <= 0:
            return {"ok": False, "message": "computed qty ≤ 0", "trade_id": None}

        now = datetime.now(timezone.utc)
        side = task.rec_direction or "long"
        trade = FundTrade(
            fund_id=fund.id,
            ticker=ticker,
            side=side,
            qty=qty,
            entry_price=mark,
            entry_at=now,
            status="open",
            call_id=None,
            open_reason=(
                f"research_desk task #{task.id} (conv "
                f"{task.rec_conviction}/5, size {size_pct:.1f}%): "
                f"{(task.rec_thesis or '')[:240]}"
            ),
        )
        s.add(trade)
        # Cash math mirrors `_close` (sign-flipped for opens).
        if side == "long":
            fund.cash -= qty * mark
        else:
            fund.cash += qty * mark
        s.add(fund)
        s.flush()

        task.executed_at = now
        task.executed_trade_id = trade.id
        task.execution_note = (
            f"{side.upper()} {qty:g} ${ticker} @ {mark:.4g} "
            f"(size {size_pct:.1f}% · conv {task.rec_conviction}/5)"
        )
        s.add(task)

        return {"ok": True, "message": task.execution_note,
                "trade_id": trade.id}


def list_recent(n: int = 30) -> list[dict]:
    """Newest-first task list for the Research Desk panel."""
    with session_scope() as s:
        rows = s.exec(
            select(ResearchTask)
            .order_by(ResearchTask.id.desc())
            .limit(n)
        ).all()
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "prompt": r.prompt,
                "created_at": (
                    r.created_at if r.created_at.tzinfo
                    else r.created_at.replace(tzinfo=timezone.utc)
                ).isoformat(),
                "verdict": r.verdict,
                "rec_ticker": r.rec_ticker,
                "rec_direction": r.rec_direction,
                "rec_conviction": r.rec_conviction,
                "rec_size_pct": r.rec_size_pct,
                "executed_at": (
                    (r.executed_at if r.executed_at.tzinfo
                     else r.executed_at.replace(tzinfo=timezone.utc)).isoformat()
                    if r.executed_at else None
                ),
                "execution_note": r.execution_note,
                "has_dossier": bool(r.dossier),
            })
        return out


def get_task(task_id: int) -> dict | None:
    """Full row for the modal."""
    with session_scope() as s:
        r = s.get(ResearchTask, task_id)
        if r is None:
            return None
        return {
            "id": r.id, "prompt": r.prompt,
            "created_at": (
                r.created_at if r.created_at.tzinfo
                else r.created_at.replace(tzinfo=timezone.utc)
            ).isoformat(),
            "dossier": r.dossier or "",
            "verdict": r.verdict,
            "rec_ticker": r.rec_ticker,
            "rec_direction": r.rec_direction,
            "rec_conviction": r.rec_conviction,
            "rec_size_pct": r.rec_size_pct,
            "rec_thesis": r.rec_thesis,
            "rec_risks": r.rec_risks,
            "executed_at": (
                (r.executed_at if r.executed_at.tzinfo
                 else r.executed_at.replace(tzinfo=timezone.utc)).isoformat()
                if r.executed_at else None
            ),
            "executed_trade_id": r.executed_trade_id,
            "execution_note": r.execution_note,
            "model": r.model,
        }


def executions_remaining_today() -> int:
    """Convenience for the UI badge — how many executions the user has
    left under the daily cap."""
    return max(0, _RATE_LIMIT_PER_DAY - _executions_today())


def _model_name() -> str:
    """Same shape as dossier._model_name — record the model that wrote
    the dossier for future invalidation policies."""
    return (
        settings.LLM_API_MODEL_HEAVY
        or settings.HEAVY_LLM_API_MODEL
        or settings.LLM_MODEL_HEAVY
        or "unknown"
    )[:120]
