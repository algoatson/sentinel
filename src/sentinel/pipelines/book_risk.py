"""Proactive book-risk — the dashboard→copilot leap.

Watches the user's OPEN paper positions and speaks up *only* when one is
actually in trouble. Three deterministic triggers per position:

  • drawdown  — unrealized P&L past a bucketed adverse threshold
  • earnings  — the name reports within _EARNINGS_DAYS (binary risk)
  • event     — a fresh material filing/news/why-moved on the name since we
                last flagged it (the thesis may be moving)

Discipline (why it's a copilot, not a nag):
- Detection is ARITHMETIC — drawdown %, a calendar date, an event tier.
  Never an LLM guess. The LLM only writes the *call* (cut/trim/hold/add)
  on top of facts that are already true.
- Resilient: the risk facts ALWAYS post if a trigger fires. The LLM read is
  an enhancement — a missed risk warning is far worse than a terse one, so
  (unlike reddit_feed) LLM-down does not silence the alert.
- Cooldown + escalation: once flagged, a ticker is muted for _COOLDOWN
  *unless the situation got materially worse* (deeper drawdown or a new
  trigger kind). Persistence doesn't re-nag; escalation breaks through.
- Each alert is logged as a `book_risk` NarrativeEvent on the ticker — both
  the dedup anchor AND so synthesis sees "we already warned about this".
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger

from .. import discord_client, portfolio, ui
from ..config import settings
from ..llm import LLM_ERROR_SENTINEL, get_llm
from ..narrative import recent_events, record_event
from ..prompts import get_prompt

_BR_KIND = "book_risk"
_MATERIAL_KINDS = {
    "filing", "why_moved", "convergence", "news_alert", "synthesis",
    # thesis_state covers thesis.review_cycle transitions (validated /
    # invalidated / matured). An open position whose thesis just got
    # invalidated is exactly the kind of thing book_risk should escalate.
    "thesis_state",
}
_DRAWDOWN_BUCKETS = (-8.0, -15.0, -25.0, -40.0)  # adverse pnl_pct → bucket 1..4
_EARNINGS_DAYS = 4          # report within N days → flag (binary risk)
_EVENT_LOOKBACK_DAYS = 5
_FRESH_WINDOW = timedelta(hours=36)   # "fresh event" window when no prior flag
_COOLDOWN = timedelta(hours=24)       # don't re-nag a non-worse situation


def _channel() -> int:
    return (
        settings.DISCORD_RISK_CHANNEL_ID
        or settings.DISCORD_PRIORITY_CHANNEL_ID
        or settings.DISCORD_META_CHANNEL_ID
    )


def _utc_naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _dd_bucket(pnl_pct: float | None) -> int:
    """0 = not in drawdown trouble; 1..4 = deepening adverse buckets.
    pnl_pct is already side-adjusted by portfolio (a short going against you
    is negative too), so one signed threshold works for long AND short."""
    if pnl_pct is None:
        return 0
    return sum(1 for thr in _DRAWDOWN_BUCKETS if pnl_pct <= thr)


def _sev_token(dd: int, kinds) -> str:
    return f"dd={dd};trg={','.join(sorted(kinds))}"


def _parse_sev(detail: str | None) -> tuple[int, set[str]]:
    """Defensive: an unparseable token → (0, ∅) so we fail TOWARD alerting
    (a false risk ping beats a missed one)."""
    dd, kinds = 0, set()
    try:
        for part in (detail or "").split(";"):
            if part.startswith("dd="):
                dd = int(part[3:] or 0)
            elif part.startswith("trg="):
                kinds = {x for x in part[4:].split(",") if x}
    except Exception:
        return 0, set()
    return dd, kinds


def _worse(cur: tuple[int, set[str]], prev: tuple[int, set[str]]) -> bool:
    """Did it escalate vs. the last alert? Deeper drawdown bucket OR a new
    trigger kind that wasn't there before. (Mere persistence is not worse.)"""
    (cdd, ck), (pdd, pk) = cur, prev
    return cdd > pdd or bool(ck - pk)


def _all_open_positions() -> list[dict]:
    """Unified open-positions iterator across the legacy PaperTrade
    store AND the autonomous fund book. book_risk previously only
    looked at PaperTrade, so the bot's own trades got no alerts.

    When the same ticker is held in both stores (or in multiple
    wallets), we collapse them per (ticker, side) and report the
    WORST single pnl_pct — that's the bar that matters for an alert
    tier. Aggregated qty + a comma-joined fund list go in the
    metadata so the embed can show "$NVDA (degen + catalyst)".
    """
    from .. import funds as _funds
    rows: list[dict] = []
    for p in portfolio.open_positions():
        rows.append({
            "ticker": p["ticker"], "side": p["side"], "qty": p["qty"],
            "entry": p["entry"], "mark": p.get("mark"),
            "pnl": p.get("pnl"), "pnl_pct": p.get("pnl_pct"),
            "fund": None,
        })
    for p in _funds.open_positions_all():
        rows.append({
            "ticker": p["ticker"], "side": p["side"], "qty": p["qty"],
            "entry": p["entry"], "mark": p.get("mark"),
            "pnl": p.get("upnl"), "pnl_pct": p.get("upnl_pct"),
            "fund": p.get("fund"),
        })

    # Collapse per (ticker, side) — worst-pnl-pct wins for the
    # alert tier; qty and fund list are aggregated for context.
    by_key: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["ticker"], r["side"])
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = {
                **r,
                "qty": float(r["qty"] or 0),
                "funds": [r["fund"]] if r.get("fund") else [],
            }
            continue
        cur["qty"] = (cur["qty"] or 0) + float(r["qty"] or 0)
        if r.get("fund") and r["fund"] not in cur["funds"]:
            cur["funds"].append(r["fund"])
        # Worst pnl_pct wins (most negative).
        cur_pp = cur.get("pnl_pct")
        new_pp = r.get("pnl_pct")
        if new_pp is not None and (cur_pp is None or new_pp < cur_pp):
            cur["pnl_pct"] = new_pp
            cur["entry"] = r["entry"]
            cur["mark"] = r["mark"]
            cur["pnl"] = r.get("pnl")
    return list(by_key.values())


def _assess(now: datetime, *, earnings_of) -> list[dict]:
    """Pure-ish core: which open positions to alert on this cycle, after the
    cooldown/escalation gate. `earnings_of` is injected (a ticker→date|None
    callable) so this is unit-testable without network."""
    today = now.date()
    flagged: list[dict] = []
    for p in _all_open_positions():
        ticker = p["ticker"]
        triggers: dict[str, str] = {}

        dd = _dd_bucket(p["pnl_pct"])
        if dd:
            triggers["drawdown"] = f"{p['pnl_pct']:+.1f}% unrealized"

        ed = earnings_of(ticker)
        if ed is not None:
            days = (ed - today).days
            if 0 <= days <= _EARNINGS_DAYS:
                triggers["earnings"] = (
                    f"reports in {days}d ({ed.isoformat()})"
                )

        evs = recent_events(ticker, days=_EVENT_LOOKBACK_DAYS, limit=25)
        brs = [e for e in evs if e.kind == _BR_KIND]  # ts desc → [0] newest
        last_br = brs[0] if brs else None
        anchor = (
            _utc_naive(last_br.ts) if last_br is not None
            else _utc_naive(now) - _FRESH_WINDOW
        )
        fresh = next(
            (
                e for e in evs
                if e.kind in _MATERIAL_KINDS and _utc_naive(e.ts) > anchor
            ),
            None,
        )
        if fresh is not None:
            triggers["event"] = f"{fresh.kind}: {fresh.headline[:90]}"

        if not triggers:
            continue

        cur = (dd, set(triggers))
        if last_br is not None:
            age = _utc_naive(now) - _utc_naive(last_br.ts)
            if age < _COOLDOWN and not _worse(cur, _parse_sev(last_br.detail)):
                continue  # within cooldown, not escalated → don't nag

        flagged.append(
            {
                "ticker": ticker,
                "side": p["side"],
                "qty": p["qty"],
                "entry": p["entry"],
                "mark": p["mark"],
                "pnl": p["pnl"],
                "pnl_pct": p["pnl_pct"],
                "funds": p.get("funds") or [],
                "triggers": triggers,
                "dd": dd,
                "sev": _sev_token(dd, triggers.keys()),
            }
        )

    flagged.sort(key=lambda f: (f["dd"], -(f["pnl_pct"] or 0.0)), reverse=True)
    return flagged


_TAG = {"drawdown": "📉", "earnings": "⚠️ earnings", "event": "🆕"}


def _line(f: dict) -> str:
    arrow = "🟢L" if f["side"] == "long" else "🔴S"
    mark = f"{f['mark']:.4g}" if f["mark"] is not None else "—"
    pnl = f"{f['pnl_pct']:+.1f}%" if f["pnl_pct"] is not None else "n/a"
    flags = " · ".join(
        f"{_TAG.get(k, '•')} {v}" for k, v in f["triggers"].items()
    )
    return (
        f"{arrow} `${f['ticker']}` {f['qty']:g}@{f['entry']:.4g}→{mark} "
        f"**{pnl}**\n   {flags}"
    )


def _facts_block(flagged: list[dict]) -> str:
    return "\n".join(_line(f) for f in flagged)


def _safe_earnings(ticker: str):
    from . import catalysts

    try:
        return catalysts._next_earnings(ticker)
    except Exception:
        return None


def _collect() -> list[dict]:
    return _assess(datetime.now(timezone.utc), earnings_of=_safe_earnings)


def _record(flagged: list[dict], channel_id: int, message_id: str) -> None:
    for f in flagged:
        record_event(
            f["ticker"],
            _BR_KIND,
            f"risk flagged: {', '.join(f['triggers'])}",
            tier=2,
            detail=f["sev"],
            channel_id=channel_id,
            message_id=message_id,
        )


async def run_book_risk() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_book_risk top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ book_risk error: {e}")
        except Exception:
            pass


async def _run() -> None:
    chan = _channel()
    if not chan:
        logger.debug("book_risk: no channel resolved, skipping")
        return

    flagged = await asyncio.to_thread(_collect)
    if not flagged:
        logger.info("book_risk: book clean, nothing to flag")
        return

    facts = _facts_block(flagged)
    prompt = get_prompt("book_risk").safe_substitute(positions=facts)
    raw = await asyncio.to_thread(
        get_llm().complete, prompt, model="light", max_tokens=500
    )
    read = "" if (not raw or raw == LLM_ERROR_SENTINEL) else raw.strip()

    desc = facts
    if read:
        desc += f"\n\n__The read__\n{read}"
    embed = discord.Embed(
        title=f"⚠️ Book risk — {len(flagged)} position(s) need eyes",
        description=desc[:4000],
        color=ui.BEARISH,
    )

    msg = await discord_client.post_embed(chan, embed, importance=4)
    if msg is None:
        # Posting failed — do NOT record the dedup anchors, so the whole
        # alert retries next cycle. A missed risk warning is unacceptable.
        logger.warning("book_risk: post failed, will retry next cycle")
        return
    await asyncio.to_thread(_record, flagged, chan, str(msg.id))
    logger.info("book_risk: flagged {} position(s)", len(flagged))
