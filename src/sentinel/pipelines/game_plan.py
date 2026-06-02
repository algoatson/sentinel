"""Morning Game Plan — one ranked, book-centric action list.

The system over-produces: risk scans, maturing calls, catalysts, fresh
ideas, holdings news, a prose briefing — across many surfaces. The last-mile
problem is that the *user* has to synthesise all of it into "what do I actually
do this morning". This pipeline closes that gap with a single decision surface.

Two stages, strictly separated so numbers can never be fabricated:

1. `build_inputs()` — a **pure, deterministic** assembler. It fans out to the
   existing analytics/scorecard/catalysts/portfolio accessors and packs every
   real figure into a structured bundle. Each surfaced item carries a machine
   `trigger` string. No LLM here.
2. `run_game_plan()` (Phase 2) — hands that bundle to the heavy LLM, which only
   **ranks, dedupes and phrases** (selects the top items, writes a one-line
   action each, and a short overall read). It never fetches or alters a number.
   Fail-open: if the LLM is unavailable or returns junk, the deterministic
   bundle is persisted unranked rather than nothing.

Web-only — this never posts to Discord (a later post_embed is intentionally
deferred; see TODO(discord) in run_game_plan). Reuses existing accessors
throughout; it is a *consumer/synthesiser* of the other arms, not a new source.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from loguru import logger
from sqlmodel import select

from ..db import session_scope
from ..models import DailyPlan, EarningsDate, TradingCall, Watchlist

_ET = ZoneInfo("America/New_York")

# Windows — kept conservative so the plan stays a morning-decision surface,
# not a full firehose.
_FRESH_IDEA_HOURS = 24
_RESOLVED_LOOKBACK_DAYS = 3
_CATALYST_HORIZON_DAYS = 7
_MATURING_AGE_DAYS = 5  # a 5d-horizon call this old is about to score


def et_date_str() -> str:
    """Today's date in US/Eastern — the GamePlan primary key + the date the
    morning plan is *about* (markets run on ET)."""
    return datetime.now(_ET).date().isoformat()


def _safe(fn: Callable[[], Any], *, default: Any) -> Any:
    """Run one section builder, returning `default` on any failure. Mirrors the
    project's top-level catch-and-continue policy — a single flaky accessor must
    not sink the whole bundle."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — deliberate catch-all per policy
        logger.debug("game_plan section failed ({}): {}", getattr(fn, "__name__", fn), e)
        return default


def _held_tickers() -> set[str]:
    """Tickers in the live autonomous book (FundTrade) — the same set the
    risk/earnings/holdings accessors key off."""
    from .. import funds

    try:
        return {r["ticker"].upper() for r in funds.open_positions_all()}
    except Exception as e:  # noqa: BLE001
        logger.debug("game_plan held_tickers failed: {}", e)
        return set()


# ── sections ────────────────────────────────────────────────────────────────


def _book_risk_section() -> dict:
    from ..analytics.earnings_exposure import earnings_exposure
    from ..analytics.holdings_news import holdings_news
    from ..analytics.risk_monitor import risk_snapshot

    snap = risk_snapshot()
    ee = earnings_exposure(window_days=14)
    hn = holdings_news(hours=24, limit=12)

    near_stop = [{**r, "trigger": "near_stop"} for r in snap.get("near_stop", [])]
    near_target = [{**r, "trigger": "near_target"} for r in snap.get("near_target", [])]
    earnings_soon = [
        {
            "ticker": e["ticker"],
            "days_until": e["days_until"],
            "report_date": e["report_date"],
            "notional": e["notional"],
            "funds": e["funds"],
            "trigger": f"earnings_{e['days_until']}d",
        }
        for e in ee.get("upcoming", [])
        if e.get("days_until") is not None and e["days_until"] <= 7
    ]
    fresh_news = [
        {
            "ticker": n["ticker"],
            "title": n["title"],
            "url": n["url"],
            "ts": n["ts"],
            "sentiment": n.get("sentiment"),
            "trigger": "held_news",
        }
        for n in hn.get("news", [])[:8]
    ]
    fresh_filings = [
        {
            "ticker": f["ticker"],
            "form_type": f["form_type"],
            "url": f["url"],
            "materiality_score": f.get("materiality_score"),
            "trigger": "held_filing",
        }
        for f in hn.get("filings", [])[:6]
    ]
    return {
        "n_open": snap.get("n_open", 0),
        "dollar_at_risk": snap.get("dollar_at_risk", 0.0),
        "pct_book_at_risk": snap.get("pct_book_at_risk", 0.0),
        "naked_count": len(snap.get("naked", [])),
        "near_stop": near_stop,
        "near_target": near_target,
        "earnings_soon": earnings_soon,
        "fresh_news": fresh_news,
        "fresh_filings": fresh_filings,
    }


def _maturing_section() -> dict:
    from .. import scorecard

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_RESOLVED_LOOKBACK_DAYS)
    open_out: list[dict] = []
    resolved_out: list[dict] = []
    with session_scope() as s:
        open_calls = s.exec(
            select(TradingCall)
            .where(TradingCall.settled == False)  # noqa: E712
            .order_by(TradingCall.created_at)
        ).all()
        for c in open_calls:
            created = c.created_at if c.created_at.tzinfo else c.created_at.replace(tzinfo=timezone.utc)
            age_d = (now - created).days
            maturing = age_d >= _MATURING_AGE_DAYS and c.ret_5d_pct is None
            open_out.append({
                "id": c.id,
                "ticker": c.ticker,
                "direction": c.direction,
                "conviction": c.conviction,
                "source": c.source,
                "age_days": age_d,
                "ret_1d_pct": c.ret_1d_pct,
                "maturing_today": maturing,
                "trigger": "maturing" if maturing else "open_call",
            })
        resolved = s.exec(
            select(TradingCall)
            .where(TradingCall.settled == True)  # noqa: E712
            .where(TradingCall.marked_at != None)  # noqa: E711
            .where(TradingCall.marked_at >= cutoff)
            .order_by(TradingCall.marked_at.desc())
            .limit(15)
        ).all()
        for c in resolved:
            resolved_out.append({
                "id": c.id,
                "ticker": c.ticker,
                "direction": c.direction,
                "conviction": c.conviction,
                "source": c.source,
                "ret_1d_pct": c.ret_1d_pct,
                "ret_5d_pct": c.ret_5d_pct,
                "trigger": "resolved",
            })

    tr = scorecard.track_record_summary()
    return {
        "open": open_out,
        "maturing_today": [c for c in open_out if c["maturing_today"]],
        "resolved_recent": resolved_out,
        "track_record": tr.get("overall", {}),
        "by_source": tr.get("by_source", {}),
    }


def _catalysts_section(held: set[str]) -> list[dict]:
    from . import catalysts

    today = date.today()
    horizon = today + timedelta(days=_CATALYST_HORIZON_DAYS)
    out: list[dict] = []

    # Pure date-math events (OPEX / FOMC / CPI / macro calendar) — no network.
    try:
        events = catalysts._computed_events(today, horizon) + catalysts._macro_events(today, horizon)
        for d, label in events:
            out.append({
                "date": d.isoformat(),
                "label": label,
                "kind": "macro",
                "trigger": "catalyst_macro",
            })
    except Exception as e:  # noqa: BLE001
        logger.debug("game_plan macro catalysts failed: {}", e)

    # Earnings for held + watchlist names, from the persisted EarningsDate table
    # (network-free; populated by the catalyst radar).
    held_u = {h.upper() for h in held}
    with session_scope() as s:
        wl = {
            t.upper()
            for (t,) in s.exec(
                select(Watchlist.ticker).where(Watchlist.ticker != None)  # noqa: E711
            ).all()
            if t
        }
        names = held_u | wl
        for ed in s.exec(select(EarningsDate)).all():
            tk = (ed.ticker or "").upper()
            if tk not in names or ed.report_date is None:
                continue
            days = (ed.report_date - today).days
            if 0 <= days <= _CATALYST_HORIZON_DAYS:
                out.append({
                    "date": ed.report_date.isoformat(),
                    "label": f"{tk} earnings",
                    "ticker": tk,
                    "kind": "earnings",
                    "days_until": days,
                    "held": tk in held_u,
                    "trigger": f"catalyst_earnings_{days}d",
                })

    out.sort(key=lambda e: e["date"])
    return out


def _fresh_ideas_section(held: set[str]) -> list[dict]:
    """Recent (≤24h) grounded calls, ranked by conviction × source-edge
    multiplier, deduped against the open book and to one row per
    ticker+direction."""
    from .. import funds

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_FRESH_IDEA_HOURS)
    held_u = {h.upper() for h in held}
    with session_scope() as s:
        calls = s.exec(
            select(TradingCall)
            .where(TradingCall.created_at >= cutoff)
            .order_by(TradingCall.created_at.desc())
        ).all()

    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for c in calls:
        if c.grounded is False:  # explicitly contradicted by the verifier — drop
            continue
        tk = c.ticker.upper()
        if tk in held_u:  # already in the book — not a fresh idea
            continue
        key = (tk, c.direction)
        if key in seen:
            continue
        seen.add(key)
        mult = funds._source_edge_mult(c.source, now)
        out.append({
            "id": c.id,
            "ticker": tk,
            "direction": c.direction,
            "conviction": c.conviction,
            "source": c.source,
            "edge_mult": round(mult, 3),
            "score": round(c.conviction * mult, 3),
            "thesis": (c.thesis or "")[:240],
            "grounded": c.grounded,
            "trigger": "fresh_idea",
        })
    out.sort(key=lambda x: -x["score"])
    return out[:10]


def _prior_section(held: set[str], idea_tickers: list[str]) -> dict:
    from .. import narrative

    with session_scope() as s:
        plan = s.get(DailyPlan, date.today())
        plan_text = (plan.body if plan else "") or ""

    tickers = sorted(set(h.upper() for h in held) | {t.upper() for t in idea_tickers})[:40]
    narr = narrative.recent_for_tickers(tickers, days=3, per=3) if tickers else {}
    keys: list[str] = []
    for t, evs in narr.items():
        for e in evs:
            keys.append(f"{t}: {e}")
    return {"daily_plan": plan_text[:2000], "recent_narrative": keys[:40]}


# ── public assembler ──────────────────────────────────────────────────────


def build_inputs(session=None) -> dict:
    """Deterministic, real-numbers-only input bundle for the morning plan.

    `session` is accepted for API parity; the section builders open their own
    read scopes (and reuse the cached accessor queries), so it is not threaded
    through. Never raises — each section is independently fail-soft."""
    held = _held_tickers()

    book_risk = _safe(_book_risk_section, default={
        "n_open": 0, "dollar_at_risk": 0.0, "pct_book_at_risk": 0.0,
        "naked_count": 0, "near_stop": [], "near_target": [],
        "earnings_soon": [], "fresh_news": [], "fresh_filings": [],
    })
    maturing = _safe(_maturing_section, default={
        "open": [], "maturing_today": [], "resolved_recent": [],
        "track_record": {}, "by_source": {},
    })
    catalysts_list = _safe(lambda: _catalysts_section(held), default=[])
    fresh = _safe(lambda: _fresh_ideas_section(held), default=[])
    idea_tickers = [i["ticker"] for i in fresh]
    prior = _safe(lambda: _prior_section(held, idea_tickers), default={
        "daily_plan": "", "recent_narrative": [],
    })

    return {
        "et_date": et_date_str(),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "held_tickers": sorted(held),
        "book_risk": book_risk,
        "maturing": maturing,
        "catalysts": catalysts_list,
        "fresh_ideas": fresh,
        "prior": prior,
    }
