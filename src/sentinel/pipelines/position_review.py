"""Pre-market position review.

Runs once at 08:00 ET, before the briefing. For each open FundTrade,
decides whether to ``hold``, ``trim``, ``close``, or ``flag`` (flag =
"escalate to the user, the model isn't sure").

Economic by design:
  1. A *deterministic* per-position gate picks the subset worth
     re-reasoning. Positions where nothing meaningful changed since
     entry get auto-tagged ``hold`` with NO LLM call.
  2. The flagged subset is sent in ONE batched heavy LLM call (not
     N per-position calls). The model sees the whole book at once
     so it can spot cross-position context (correlated longs going
     into a Fed week, etc).
  3. On a quiet morning → 0 tokens. On a busy morning → a single
     heavy call, regardless of position count.

Advisory-only. Verdicts are appended to FundTrade.notes (the journal
field, also surfaced on /book + /journal), broadcast as an SSE event
for the dashboard, and a narrative event for the timeline. Auto-
execution is intentionally NOT wired here — the user closes manually
or via /book if they agree.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from string import Template

from loguru import logger
from sqlmodel import select

from .. import earnings
from ..db import session_scope
from ..funds import _atr_in_session, _mark, open_positions_all, trade_lifecycle
from ..llm import LLM_ERROR_SENTINEL, get_llm, parse_json_response
from ..models import Filing, FundTrade, Thesis, TradingCall


# Gate thresholds — kept conservative so the LLM only fires on
# positions where SOMETHING meaningful happened since entry.

# ATR-multiple of adverse move that warrants a re-read. 1.5× ATR
# means "the position is hurting but the stop hasn't fired yet" —
# the most useful "do I trim early?" moment.
_ADVERSE_ATR_MULT = 1.5

# Days-until-earnings inside which we always flag (binary risk).
# 3 days is post the existing pre-blackout (2d) and gives the model
# one day to recommend trim/close before the print.
_EARNINGS_FLAG_DAYS = 3

# Material filing threshold to count as a re-read trigger.
_MATERIAL_FILING_SCORE = 2

# Opposite-direction call conviction that counts as a re-read
# trigger. 4 = "high conviction"; anything less is noise.
_OPPOSITE_CALL_CONVICTION = 4

# Cap how many flagged positions go into one LLM call. If the book
# is huge (rare), we trim to the most-pressing N by gate weight.
_MAX_PER_CALL = 12


_REVIEW_PROMPT = Template("""\
You're a paper-trading copilot's morning position-review function.
You have ${n} flagged open positions — each carries the original
thesis, the events since entry, and the reason the gate flagged
it. For each, pick ONE verdict and write ONE short reason.

VERDICTS:
  - hold:  thesis still holds, no action — explain why the flag isn't fatal
  - trim:  partial close (cut size in half) — risk is rising but core thesis intact
  - close: thesis is broken or invalidated — exit now
  - flag:  you genuinely can't tell — escalate to the user

Be decisive. "flag" should be rare — use it only when the evidence is
genuinely mixed, not as a hedge.

Return STRICT JSON ONLY (no fences, no prose):
[
  {"trade_id": 42, "verdict": "hold|trim|close|flag", "reason": "≤ 25 words"}
]

POSITIONS:
$payload_json
""")


def _opposite(side: str) -> str:
    return "short" if side == "long" else "long"


def _gate_position(
    session, p: dict, now: datetime,
) -> tuple[bool, list[str]]:
    """Deterministic check: is this position worth a re-read?

    Returns (should_review, reasons). Reasons are short tag strings
    for the LLM payload so the model knows WHY each position is on
    its desk.
    """
    reasons: list[str] = []
    ticker = p["ticker"]
    side = p["side"]
    entry = p["entry"]
    entry_at = datetime.fromisoformat(p["entry_at"])
    entry_naive = entry_at.replace(tzinfo=None)

    # ── Trigger 1: material filing since entry ─────────────────────
    filing = session.exec(
        select(Filing)
        .where(Filing.ticker == ticker)
        .where(Filing.filed_at >= entry_naive)
        .where(Filing.materiality_score.is_not(None))
        .where(Filing.materiality_score >= _MATERIAL_FILING_SCORE)
        .order_by(Filing.materiality_score.desc(), Filing.filed_at.desc())
        .limit(1)
    ).first()
    if filing is not None:
        reasons.append(
            f"material {filing.form_type} (score {filing.materiality_score})"
        )

    # ── Trigger 2: high-conviction OPPOSITE-direction call ─────────
    op = _opposite(side)
    opp_call = session.exec(
        select(TradingCall)
        .where(TradingCall.ticker == ticker)
        .where(TradingCall.direction == op)
        .where(TradingCall.created_at >= entry_naive)
        .where(TradingCall.conviction >= _OPPOSITE_CALL_CONVICTION)
        .order_by(TradingCall.created_at.desc())
        .limit(1)
    ).first()
    if opp_call is not None:
        reasons.append(
            f"opposite {op} call (conv {opp_call.conviction}, {opp_call.source})"
        )

    # ── Trigger 3: > 1.5 ATR adverse move ──────────────────────────
    mark_val = _mark(session, ticker)
    if mark_val is not None and mark_val > 0 and entry > 0:
        adverse_per_share = (
            (entry - mark_val) if side == "long" else (mark_val - entry)
        )
        if adverse_per_share > 0:  # only when actually losing
            atr = _atr_in_session(session, ticker)
            if atr and atr > 0:
                atr_units = adverse_per_share / atr
                if atr_units >= _ADVERSE_ATR_MULT:
                    reasons.append(
                        f"adverse {atr_units:.1f}× ATR move"
                    )

    # ── Trigger 4: earnings within 3 days ──────────────────────────
    edays = earnings.days_until_earnings(ticker, now.date())
    if edays is not None and 0 <= edays <= _EARNINGS_FLAG_DAYS:
        reasons.append(f"earnings in {edays}d")

    # ── Trigger 5: any active thesis invalidated since entry ───────
    invalidated = session.exec(
        select(Thesis)
        .where(Thesis.ticker == ticker)
        .where(Thesis.state == "invalidated")
        .where(Thesis.updated_at >= entry_naive)
        .order_by(Thesis.updated_at.desc())
        .limit(1)
    ).first()
    if invalidated is not None:
        reasons.append(f"thesis #{invalidated.id} invalidated")

    return bool(reasons), reasons


def _build_position_payload(
    session, p: dict, reasons: list[str], now: datetime,
) -> dict:
    """Slim payload for one flagged position. Mirrors the lifecycle
    drill-in but caps each list — the LLM doesn't need 50 news items
    per position; the few most relevant since entry are enough."""
    lifecycle = trade_lifecycle(p["id"]) or {}

    # Look up the originating call's thesis (most useful single piece
    # of context — "what did I think when I opened this?").
    original_thesis: str | None = None
    call_id = p.get("call_id")
    if call_id is not None:
        call = session.get(TradingCall, call_id)
        if call is not None:
            original_thesis = (call.thesis or "")[:400]

    return {
        "trade_id": p["id"],
        "fund": p["fund"],
        "ticker": p["ticker"],
        "side": p["side"],
        "qty": p["qty"],
        "entry": p["entry"],
        "mark": p.get("mark"),
        "upnl_pct": p.get("upnl_pct"),
        "stop_price": p.get("stop_price"),
        "target_price": p.get("target_price"),
        "age_h": p.get("age_h"),
        "r_multiple": p.get("r_multiple"),
        "flag_reasons": reasons,
        "original_thesis": original_thesis,
        "news_since_entry": [
            {"title": n["title"], "source": n["source"], "ts": n["ts"]}
            for n in (lifecycle.get("news") or [])[:5]
        ],
        "filings_since_entry": [
            {
                "form_type": f["form_type"],
                "filed_at": f["filed_at"],
                "score": f.get("materiality_score"),
            }
            for f in (lifecycle.get("filings") or [])[:3]
        ],
        "calls_since_entry": [
            {
                "direction": c["direction"],
                "conviction": c["conviction"],
                "thesis": (c.get("thesis") or "")[:200],
                "source": c["source"],
                "created_at": c["created_at"],
            }
            for c in (lifecycle.get("calls") or [])[:5]
        ],
    }


def _persist_verdicts(verdicts: list[dict], now: datetime) -> None:
    """Append the verdict line to FundTrade.notes, record a narrative
    event (tier 2 so it floats up alongside convergence on /book), and
    publish an SSE event so the dashboard can flash a banner."""
    if not verdicts:
        return
    stamp = now.strftime("%Y-%m-%d")
    with session_scope() as s:
        for v in verdicts:
            t = s.get(FundTrade, v["trade_id"])
            if t is None:
                continue
            verdict = v.get("verdict", "?")
            reason = (v.get("reason") or "").strip()[:240]
            line = f"\n[{stamp} morning review · {verdict.upper()}]\n{reason}"
            t.notes = ((t.notes or "") + line)[:2000]
            s.add(t)

    try:
        from ..narrative import record_event
        for v in verdicts:
            t = v.get("ticker")
            if not t:
                continue
            record_event(
                t,
                "position_review",
                f"{v.get('verdict', '?').upper()}: {(v.get('reason') or '')[:140]}",
                tier=2,
            )
    except Exception as e:
        logger.debug("position_review record_event failed: {}", e)

    try:
        from .. import events
        events.publish("position_review", {
            "as_of": now.isoformat(),
            "count": len(verdicts),
            "verdicts": verdicts,
        })
    except Exception as e:
        logger.debug("position_review SSE publish failed: {}", e)


def _normalise_verdicts(parsed: list, lookup: dict[int, dict]) -> list[dict]:
    """Normalise the LLM output: drop bad shapes, clamp verdicts, fill
    in ticker from the lookup so downstream record_event has it.
    Lookup is {trade_id → flagged-payload}."""
    out: list[dict] = []
    valid = {"hold", "trim", "close", "flag"}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            tid = int(item.get("trade_id"))
        except (TypeError, ValueError):
            continue
        if tid not in lookup:
            continue
        verdict = str(item.get("verdict", "")).lower().strip()
        if verdict not in valid:
            verdict = "flag"
        reason = str(item.get("reason", "")).strip()[:240]
        out.append({
            "trade_id": tid,
            "verdict": verdict,
            "reason": reason,
            "ticker": lookup[tid]["ticker"],
            "fund": lookup[tid].get("fund"),
        })
    return out


def run() -> dict:
    """One pre-market sweep. Returns the verdict summary so the
    scheduler / tests can introspect."""
    now = datetime.now(timezone.utc)
    try:
        positions = open_positions_all()
    except Exception as e:
        logger.exception("position_review: open_positions_all failed: {}", e)
        return {"flagged": 0, "verdicts": [], "error": str(e)}

    if not positions:
        return {
            "as_of": now.isoformat(),
            "reviewed": 0,
            "flagged_to_llm": 0,
            "auto_holds": 0,
            "by_verdict": {},
            "verdicts": [],
            "skipped": "no open positions",
        }

    flagged_payloads: list[dict] = []
    auto_holds: list[dict] = []
    with session_scope() as s:
        for p in positions:
            try:
                triggered, reasons = _gate_position(s, p, now)
            except Exception as e:
                logger.warning(
                    "position_review gate failed on #{}: {}", p.get("id"), e,
                )
                continue
            if not triggered:
                auto_holds.append({
                    "trade_id": p["id"],
                    "verdict": "hold",
                    "reason": "nothing material since entry",
                    "ticker": p["ticker"],
                    "fund": p.get("fund"),
                })
                continue
            payload = _build_position_payload(s, p, reasons, now)
            flagged_payloads.append(payload)

    # Cap LLM input size — pick the highest-pressure (most reasons)
    # first if we're over the cap.
    if len(flagged_payloads) > _MAX_PER_CALL:
        flagged_payloads.sort(
            key=lambda d: len(d.get("flag_reasons") or []), reverse=True,
        )
        flagged_payloads = flagged_payloads[:_MAX_PER_CALL]

    verdicts: list[dict] = []
    if flagged_payloads:
        lookup = {p["trade_id"]: p for p in flagged_payloads}
        rendered = _REVIEW_PROMPT.safe_substitute(
            n=len(flagged_payloads),
            payload_json=json.dumps(flagged_payloads, default=str),
        )
        # Heavy + JSON mode. 1500 tokens covers ~12 verdict rows + an
        # array wrapper; the model gets one shot, no retries — a
        # parse failure just leaves the flagged positions on "flag"
        # so the user still gets a heads-up.
        raw = get_llm().complete(
            rendered, model="heavy", json_mode=True, max_tokens=1500,
            fallback_light=True,
        )
        if raw and raw != LLM_ERROR_SENTINEL:
            parsed = parse_json_response(raw, expect=list)
            if parsed:
                verdicts = _normalise_verdicts(parsed, lookup)
            else:
                logger.warning("position_review: parse failed, defaulting to flag")
        if not verdicts:
            # LLM unreachable or unparseable — flag every triggered
            # position so the user sees the gate fired even if the
            # model couldn't decide.
            verdicts = [
                {
                    "trade_id": p["trade_id"],
                    "verdict": "flag",
                    "reason": "gate fired but LLM unavailable — review manually: " + ", ".join((p.get("flag_reasons") or [])[:3]),
                    "ticker": p["ticker"],
                    "fund": p.get("fund"),
                }
                for p in flagged_payloads
            ]

    # Persist + broadcast all verdicts (including the auto-holds, so
    # the dashboard's morning-review banner shows the full picture).
    all_verdicts = verdicts + auto_holds
    _persist_verdicts(all_verdicts, now)

    by_verdict: dict[str, int] = {}
    for v in all_verdicts:
        by_verdict[v["verdict"]] = by_verdict.get(v["verdict"], 0) + 1

    logger.info(
        "position_review: {} positions reviewed, {} flagged for LLM, "
        "verdicts={}",
        len(positions), len(flagged_payloads), by_verdict,
    )
    return {
        "as_of": now.isoformat(),
        "reviewed": len(positions),
        "flagged_to_llm": len(flagged_payloads),
        "auto_holds": len(auto_holds),
        "by_verdict": by_verdict,
        "verdicts": all_verdicts,
    }


async def run_position_review() -> None:
    """Scheduler entry — pure DB + one LLM call. Wrapped in to_thread
    since the LLM client is sync."""
    try:
        await asyncio.to_thread(run)
    except Exception as e:
        logger.exception("run_position_review top-level failure: {}", e)
