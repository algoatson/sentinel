"""Autonomous paper funds.

Three accounts trade the *same* TradingCall stream under three different
deterministic policies — so their P&L is a clean apples-to-apples read on
which mandate actually works on this bot's ideas. No LLM in the loop: entries
and exits are pure rules over calls + marks, fast and reproducible.

Cash convention (keeps long/short accounting consistent):
  long  open: cash -= qty*entry   close: cash += qty*exit
  short open: cash += qty*entry   close: cash -= qty*exit
  equity     = cash + Σ open_long qty*mark − Σ open_short qty*mark
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger
from sqlmodel import select

from . import discord_client, earnings, ui
from .config import settings
from .db import session_scope
from .models import (
    Fund,
    FundEquity,
    FundTrade,
    PriceBar,
    PriceContext,
    RedditMention,
    TradingCall,
)
from .crypto_regime import blocks_entry, market_regime
from .routing import asset_class_of

_POLICIES: dict[str, dict] = {
    "degen": {
        "mandate": "🦍 Degen — fast momentum off why-moved/convergence, "
        "crypto-friendly, aggressive sizing, tight leash.",
        "sources": {"why_moved", "convergence"},
        "min_conviction": 3,
        "asset_classes": None,  # any
        "size_pct": 0.20,
        "max_positions": 6,
        "stop_pct": -0.15,
        "take_pct": 0.40,
        "max_hold_days": 5,
    },
    "catalyst": {
        "mandate": "🎯 Catalyst — convergence/synthesis on equities, high "
        "conviction only, patient.",
        "sources": {"convergence", "synthesis"},
        "min_conviction": 4,
        "asset_classes": {"equity"},
        "size_pct": 0.12,
        "max_positions": 8,
        "stop_pct": -0.08,
        "take_pct": 0.25,
        "max_hold_days": 10,
    },
    "macro": {
        "mandate": "🌐 Macro — synthesis cross-asset reads only, a few big "
        "positions, long hold.",
        "sources": {"synthesis"},
        "min_conviction": 3,
        "asset_classes": None,
        "size_pct": 0.25,
        "max_positions": 4,
        "stop_pct": -0.12,
        "take_pct": 0.50,
        "max_hold_days": 20,
    },
    "crypto": {
        "mandate": "🪙 Crypto — coins only, 24/7, tighter sizing + faster "
        "cuts than the old wide-band setup. Requires funding/OI data on the "
        "ticker at open time (blind crypto = bad crypto).",
        # `funding_squeeze` is the new deterministic detector — when it
        # fires the conviction came from a deeply-extreme funding rate,
        # not a generic price move, so this wallet wants in on those.
        "sources": {"why_moved", "convergence", "synthesis", "funding_squeeze"},
        "min_conviction": 3,
        "asset_classes": {"crypto"},
        # Notable tighten from the prior (0.15 / -0.20 / 7d) defaults. The
        # old config let one losing position bleed 3% of equity (20% adverse
        # × 15% notional) and 8 of those concurrently dominated the wallet.
        # Smaller bets + faster cuts × 5:1 R:R is the better shape for an
        # asset class where most moves are noise inside ±15% range.
        "size_pct": 0.10,
        "max_positions": 8,
        "stop_pct": -0.12,
        "take_pct": 0.60,
        # Crypto moves fast; a thesis that hasn't played out in 4 days is
        # almost always wrong. Old 7d held capital in stale theses for
        # an extra half-week of opportunity cost.
        "max_hold_days": 4,
        # Crypto policy is the only wallet that gates on microstructure
        # data. Read by _run() via pol.get("require_micro"); without
        # funding/OI/orderbook context the bot is flying blind on a
        # coin whose price action is dominated by perpetual-futures
        # flow.
        "require_micro": True,
    },
    "sniper": {
        "mandate": "🔭 Sniper — ONLY 5/5-conviction convergence/synthesis, a "
        "few big shots: are the bot's strongest calls actually elite?",
        "sources": {"convergence", "synthesis"},
        "min_conviction": 5,
        "asset_classes": None,
        "size_pct": 0.30,
        "max_positions": 3,
        "stop_pct": -0.10,
        "take_pct": 0.35,
        "max_hold_days": 12,
    },
    "contrarian": {
        "mandate": "🪞 Contrarian — FADES the bot's own momentum calls. If "
        "this outruns degen, the momentum edge is a mirage.",
        "sources": {"why_moved", "convergence"},
        "min_conviction": 3,
        "asset_classes": None,
        "invert": True,
        "size_pct": 0.15,
        "max_positions": 5,
        "stop_pct": -0.15,
        "take_pct": 0.40,
        "max_hold_days": 5,
    },
    "hype": {
        "mandate": "🚀 Hype — only takes momentum calls the crowd is ALSO "
        "loud about (≥4 r/ posts/18h); fast, crypto-friendly. Does retail "
        "confirmation sharpen or dull the bot's edge?",
        "sources": {"why_moved", "convergence"},
        "min_conviction": 3,
        "asset_classes": None,
        "require_social_surge": 4,
        "size_pct": 0.15,
        "max_positions": 6,
        "stop_pct": -0.18,
        "take_pct": 0.50,
        "max_hold_days": 4,
    },
}


# No fund OPENS (or flips into) a fresh position when the name reports
# within this many days — a print is uncontrolled binary risk. Existing
# holds ride; book_risk warns the user about their own book separately.
_EARNINGS_BLACKOUT_DAYS = 2

# Window for the hype wallet's "is the crowd also loud about this?" check.
_SOCIAL_SURGE_HOURS = 18


def _social_surge(session, ticker: str, now: datetime) -> int:
    """Distinct Reddit posts mentioning `ticker` in the last
    _SOCIAL_SURGE_HOURS — the crowd-corroboration signal for the hype fund."""
    cut = now - timedelta(hours=_SOCIAL_SURGE_HOURS)
    ids = session.exec(
        select(RedditMention.post_id)
        .where(RedditMention.ticker == ticker)
        .where(RedditMention.created_at >= cut)
    ).all()
    return len(set(ids))


RESEARCH_WALLET_NAME = "research"
# Keep this terse (≤ ~100 chars) — the dashboard's Portfolio card
# renders mandates as a 1-line caption next to the wallet name. The
# implementation details (`_POLICIES` skips this fund, etc) belong
# in code comments, not in the user-facing mandate. The bot's
# Research-Desk path is what makes this fund "research".
RESEARCH_WALLET_MANDATE = (
    "🔬 Research — user-directed via Research Desk; no autonomous policy."
)


def seed_funds() -> None:
    """Create any configured fund that's absent. New funds start from the
    *current* call cursor so they trade forward, not backfill history — so
    adding a wallet to _POLICIES auto-seeds it on the next cycle/boot.

    The `research` wallet is seeded here too, but kept *out* of
    `_POLICIES` so the autonomous `_run()` loop skips it (see the
    `_POLICIES.get(fund.name)` guard in `_run`). It only trades when
    `research_desk.execute()` opens a position on it."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        latest = s.exec(
            select(TradingCall.id).order_by(TradingCall.id.desc()).limit(1)
        ).first()
        cursor = latest or 0
        for name, pol in _POLICIES.items():
            if s.exec(select(Fund).where(Fund.name == name)).first():
                continue
            s.add(
                Fund(
                    name=name,
                    mandate=pol["mandate"],
                    starting_cash=settings.FUND_STARTING_CASH,
                    cash=settings.FUND_STARTING_CASH,
                    last_call_id=cursor,
                    created_at=now,
                )
            )
            logger.info("fund seeded: {}", name)
        # Research wallet — same starting cash, no policy entry.
        existing_research = s.exec(
            select(Fund).where(Fund.name == RESEARCH_WALLET_NAME)
        ).first()
        if existing_research is None:
            s.add(
                Fund(
                    name=RESEARCH_WALLET_NAME,
                    mandate=RESEARCH_WALLET_MANDATE,
                    starting_cash=settings.FUND_STARTING_CASH,
                    cash=settings.FUND_STARTING_CASH,
                    last_call_id=cursor,
                    created_at=now,
                )
            )
            logger.info("fund seeded: {} (user-directed, no autonomous policy)",
                        RESEARCH_WALLET_NAME)

        # NOTE: an earlier version of this function "self-healed" every
        # fund's `mandate` text against `_POLICIES[name]["mandate"]` on
        # boot. That stomped the wallet policy editor (task #189) —
        # any user-edited mandate would silently revert on the next
        # restart. Code mandate is the SEED at first creation only;
        # once a fund exists, the DB row is authoritative.


def _mark(session, ticker: str) -> float | None:
    pc = session.get(PriceContext, ticker)
    return pc.last_price if pc is not None else None


def _marks_asof(session, tickers: list[str]) -> datetime | None:
    """Freshest PriceContext.last_updated across these tickers — i.e. how
    current the marks (hence the displayed P&L) actually are."""
    if not tickers:
        return None
    rows = session.exec(
        select(PriceContext.last_updated).where(
            PriceContext.ticker.in_(tickers)
        )
    ).all()
    return max(rows) if rows else None


def _ago(then: datetime, now: datetime) -> str:
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - then).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _open_trades(session, fund_id: int) -> list[FundTrade]:
    return session.exec(
        select(FundTrade)
        .where(FundTrade.fund_id == fund_id)
        .where(FundTrade.status == "open")
    ).all()


def _equity(session, fund: Fund, opens: list[FundTrade]) -> float:
    eq = fund.cash
    for t in opens:
        mark = _mark(session, t.ticker) or t.entry_price
        eq += t.qty * mark * (1 if t.side == "long" else -1)
    return eq


def _within_budget(
    opens: list[FundTrade], equity: float, size_cash: float
) -> bool:
    """No leverage — symmetric across long/short. A fund's total committed
    notional (both sides, at entry) plus the new position must stay within
    its equity.

    Why not just `size_cash <= cash`: a *short* open credits cash
    (`cash += qty*mark`), so a cash-based, long-only check let short proceeds
    masquerade as buying power and back-door leverage into subsequent longs —
    which silently breaks the whole point of the funds (a clean,
    same-bankroll comparison of mandates). Gating on committed-vs-equity
    closes that and treats both sides identically.
    """
    committed = sum(o.qty * o.entry_price for o in opens)
    return committed + size_cash <= equity


def _close(session, fund: Fund, t: FundTrade, mark: float, reason: str) -> None:
    t.status = "closed"
    t.exit_price = mark
    t.exit_at = datetime.now(timezone.utc)
    t.close_reason = reason
    if t.side == "long":
        fund.cash += t.qty * mark
        t.realized_pnl = round(t.qty * (mark - t.entry_price), 2)
    else:
        fund.cash -= t.qty * mark
        t.realized_pnl = round(t.qty * (t.entry_price - mark), 2)
    session.add(t)
    session.add(fund)


def _move_close(fund: Fund, t: FundTrade, age_days: int) -> dict:
    """Narration event for a position the engine just closed (fields already
    set by _close)."""
    return {
        "fund": fund.name,
        "kind": "close",
        "ticker": t.ticker,
        "side": t.side,
        "qty": t.qty,
        "entry": t.entry_price,
        "exit": t.exit_price,
        "realized": t.realized_pnl or 0.0,
        "reason": t.close_reason,
        "held_days": age_days,
        "open_reason": t.open_reason or "",
    }


def _effective_policy(fund: Fund) -> dict | None:
    """Resolved policy for a fund: code defaults from `_POLICIES`
    overlaid with any non-NULL DB overrides on the Fund row.

    Returns None when the fund has no policy in code AND no DB row
    overrides — i.e. the research wallet, which deliberately has no
    autonomous policy. Non-policy wallets still get an equity tick
    in `_run()`; they just skip the trade-placement branch.
    """
    base = _POLICIES.get(fund.name)
    # A fund that's not in _POLICIES *and* has no DB overrides stays
    # non-autonomous (research). Once any knob is set in the DB the
    # wallet starts trading — that's the "create a wallet from the UI"
    # path. Until then, return None.
    db_has = any(
        getattr(fund, k, None) is not None
        for k in (
            "size_pct", "max_positions", "stop_pct", "take_pct",
            "max_hold_days", "min_conviction", "max_opens_per_day",
        )
    )
    if base is None and not db_has:
        return None
    pol = dict(base) if base else {}
    for k in (
        "size_pct", "max_positions", "stop_pct", "take_pct",
        "max_hold_days", "min_conviction", "max_opens_per_day",
    ):
        v = getattr(fund, k, None)
        if v is not None:
            pol[k] = v
    # Sensible defaults for fields a DB-created wallet might miss.
    pol.setdefault("sources", set())
    pol.setdefault("asset_classes", None)
    return pol


# ── Trade-placement helpers ────────────────────────────────────────────
# These cluster everything the trading engine needs to size + risk a new
# position properly. Each is a pure function that returns numbers; the
# `_run()` loop below threads them together. The goals (against the
# previous behaviour, which size_pct'd the equity into every position
# regardless of vol):
#   1. Bound dollar risk per trade by stopping at 2×ATR, then sizing
#      backwards from that stop (`_fixed_risk_qty`).
#   2. Store the computed stop + target on the FundTrade row so the
#      already-running auto_exits pipeline and the Risk Monitor see them.
#   3. Soft-taper size as a fund draws down, well before the −15%
#      circuit-breaker (`_drawdown_scale`).
#   4. Don't burst-open on a noisy news day (`_opens_today`).
#   5. Avoid the day-after-earnings gap (`_post_earnings_blackout`).

# Target risk per trade as a fraction of equity, BEFORE the conviction
# and drawdown scalars are applied. Picked so the median trade ends up
# risking ~0.8% (small enough that a 20-trade losing streak only costs
# ~15% of the bankroll). Multiplied by (conviction/5) and the drawdown
# scale; never exceeds 2% × (size_pct / 0.20) on top conviction.
_BASE_RISK_PCT = 0.008

# Stop = mark ± _ATR_STOP_MULT × ATR(14). The same multiplier the
# manual /book ATR-suggest button uses, so the bot and the user share
# the same definition of "noise" room.
_ATR_STOP_MULT = 2.0
# Crypto trades around the clock and routinely gaps through a tight
# 2×ATR stop during off-hours liquidity holes. 2.8× gives the position
# room to survive the regime without becoming structurally directional.
# This is still tighter than the legacy 20% policy-pct fallback.
_ATR_STOP_MULT_CRYPTO = 2.8

# No fund opens more than this many positions in one UTC day. Stops
# news-cascade churn (a CPI print can generate a dozen calls in an
# hour). Can be overridden per-policy via `max_opens_per_day`.
_DEFAULT_MAX_OPENS_PER_DAY = 4

# Skip entries when the ticker reported earnings in the last N days.
# The pre-earnings blackout already protects the future side; this
# protects the post-report drift / gap volatility.
_POST_EARNINGS_BLACKOUT_DAYS = 1


# ── per-source edge multiplier (the "press on winners" control loop) ────
#
# The autonomous engine takes the same risk_pct on every call regardless
# of which source produced it. Meanwhile attribution.signal_attribution
# already measures, per source, the avg signed return per call over the
# last 90d. We use that measurement to SCALE the per-trade risk budget:
# proven-positive sources get a boost (more $ at risk per call), proven-
# negative sources get a fade (less $ at risk per call).
#
# This is asymmetric and conservative on purpose:
#
#   * Floor at 0.3× — it's easier to know a source is bleeding than to
#     know it has a long-run edge. The auto_fade in scorecard already
#     lowers conviction on bad sources; this adds a notional shrink on
#     top.
#   * Ceiling at 1.5× — never let one good streak ramp size past 1.5×
#     the baseline. Single-source concentration risk is the silent
#     killer here.
#   * Sample-confidence shrinkage — multipliers attenuate toward 1.0
#     when n is small. Only at 30+ scored calls do you get the full
#     effect. A 12-call streak with +5% avg lands at ~1.2×, a 60-call
#     streak at the full 1.5×.
#   * Cached for an hour — the engine cycle runs every few minutes; the
#     90d attribution window doesn't move meaningfully on that timescale.
#
# Drives a separate dial from auto_fade (which dampens conviction).
# Conviction dampening is "trust the signal less"; risk multiplier is
# "size into / out of the source's pocket." Both push the same way for
# losing sources, but the multiplier also lets us PRESS on winning
# sources — auto_fade never does that.
_EDGE_MIN_SAMPLE = 12       # below this n, multiplier is 1.0 (no data)
_EDGE_FULL_CONF_N = 30      # at this n, confidence shrinkage is 1.0
_EDGE_FLOOR = 0.3
_EDGE_CEILING = 1.5
# Breakpoints on avg_r_pct (signed direction-adjusted 5d return per call):
#   avg_r ≤ -2.0% → 0.3× (clear bleeder)
#   avg_r ∈ (-2, 0) → linear 0.3 → 1.0
#   avg_r ∈ (0, 3) → linear 1.0 → 1.5
#   avg_r ≥ 3.0% → 1.5×
_EDGE_BAD_THR = -2.0
_EDGE_GOOD_THR = 3.0
_EDGE_CACHE_TTL = timedelta(hours=1)

_EDGE_MULTS: dict[str, float] = {}
_EDGE_DIAG: dict[str, dict] = {}  # diagnostics shown on /system
_EDGE_TS: datetime | None = None


def _edge_raw_mult(avg_r_pct: float) -> float:
    """Map a source's avg signed return (per-call %) to its raw size
    multiplier, before sample-confidence shrinkage. Piecewise linear
    so behaviour at the breakpoints is smooth and intuitive."""
    if avg_r_pct <= _EDGE_BAD_THR:
        return _EDGE_FLOOR
    if avg_r_pct >= _EDGE_GOOD_THR:
        return _EDGE_CEILING
    if avg_r_pct <= 0:
        # -2 → 0.3, 0 → 1.0
        frac = (avg_r_pct - _EDGE_BAD_THR) / (0 - _EDGE_BAD_THR)
        return _EDGE_FLOOR + frac * (1.0 - _EDGE_FLOOR)
    # 0 → 1.0, 3 → 1.5
    frac = avg_r_pct / _EDGE_GOOD_THR
    return 1.0 + frac * (_EDGE_CEILING - 1.0)


def _refresh_edge_mults(now: datetime) -> None:
    """Recompute per-source multipliers from the attribution window.

    Pure and safe to call any time; results land in module globals
    that `_source_edge_mult` reads. Failures are logged at debug and
    leave the prior cache intact — a sizing pipeline must never crash
    on an analytics hiccup.
    """
    global _EDGE_MULTS, _EDGE_DIAG, _EDGE_TS
    try:
        from .analytics.attribution import signal_attribution
        attr = signal_attribution(days=90)
    except Exception as e:
        logger.debug("edge multipliers: attribution unavailable: {}", e)
        _EDGE_TS = now
        return
    new_mults: dict[str, float] = {}
    new_diag: dict[str, dict] = {}
    for entry in attr.get("by_source") or []:
        src = entry.get("source") or ""
        if not src:
            continue
        n = int(entry.get("n") or 0)
        avg_r = entry.get("ret_avg_pct")
        if n < _EDGE_MIN_SAMPLE or avg_r is None:
            new_mults[src] = 1.0
            new_diag[src] = {
                "n": n, "avg_r_pct": avg_r,
                "mult": 1.0, "shrunk": False,
                "reason": "below min-sample",
            }
            continue
        raw = _edge_raw_mult(float(avg_r))
        # Sample-confidence shrinkage: pull `raw` toward 1.0 when n is
        # small. At n=_EDGE_FULL_CONF_N+ the multiplier is fully applied.
        conf = min(1.0, n / _EDGE_FULL_CONF_N)
        mult = 1.0 + (raw - 1.0) * conf
        # Final clamp — belt and braces; the math above already
        # respects floor/ceiling but bounding here makes the invariant
        # explicit for the caller.
        mult = max(_EDGE_FLOOR, min(_EDGE_CEILING, mult))
        new_mults[src] = round(mult, 3)
        new_diag[src] = {
            "n": n, "avg_r_pct": float(avg_r),
            "raw_mult": round(raw, 3),
            "confidence": round(conf, 3),
            "mult": round(mult, 3),
            "shrunk": conf < 1.0,
        }
    _EDGE_MULTS = new_mults
    _EDGE_DIAG = new_diag
    _EDGE_TS = now


def _source_edge_mult(source: str, now: datetime) -> float:
    """Cached per-source risk multiplier. Returns 1.0 (the baseline) on
    unknown sources or insufficient data — never raises out, never
    returns NaN."""
    if (
        _EDGE_TS is None
        or (now - _EDGE_TS) > _EDGE_CACHE_TTL
    ):
        _refresh_edge_mults(now)
    return _EDGE_MULTS.get(source, 1.0)


def edge_multipliers() -> dict:
    """Public read-only snapshot — powers the /system edge-control panel
    and the wallet_meta payload. Cheap; no DB read on a warm cache."""
    now = datetime.now(timezone.utc)
    if (
        _EDGE_TS is None
        or (now - _EDGE_TS) > _EDGE_CACHE_TTL
    ):
        _refresh_edge_mults(now)
    return {
        "as_of": (_EDGE_TS or now).isoformat(),
        "min_sample": _EDGE_MIN_SAMPLE,
        "full_conf_n": _EDGE_FULL_CONF_N,
        "floor": _EDGE_FLOOR,
        "ceiling": _EDGE_CEILING,
        "by_source": _EDGE_DIAG.copy(),
    }


def _atr_in_session(session, ticker: str, period: int = 14) -> float | None:
    """Wilder ATR over `period` daily bars, computed inside the caller's
    open session — duplicates analytics.volatility.atr_for() so we don't
    nest session_scopes (SQLite WAL tolerates it, but explicit is safer
    and lets the loop stay batchable later)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=period * 4 + 14)
    bars = session.exec(
        select(PriceBar)
        .where(PriceBar.ticker == ticker)
        .where(PriceBar.ts >= cutoff)
        .order_by(PriceBar.ts)
    ).all()
    if len(bars) < 2:
        return None
    # Collapse intraday bars to one bar per UTC day so ATR stays a
    # daily concept.
    by_day: dict[str, dict] = {}
    for b in bars:
        d = b.ts.strftime("%Y-%m-%d")
        cur = by_day.get(d)
        if cur is None:
            by_day[d] = {"high": b.high, "low": b.low, "close": b.close}
        else:
            cur["high"] = max(cur["high"], b.high)
            cur["low"] = min(cur["low"], b.low)
            cur["close"] = b.close
    days = sorted(by_day.keys())
    if len(days) < 2:
        return None
    trs: list[float] = []
    prev_close = by_day[days[0]]["close"]
    for d in days[1:]:
        b = by_day[d]
        trs.append(
            max(
                b["high"] - b["low"],
                abs(b["high"] - prev_close),
                abs(b["low"] - prev_close),
            )
        )
        prev_close = b["close"]
    trs = trs[-period:] if len(trs) >= period else trs
    if not trs:
        return None
    return sum(trs) / len(trs)


def _compute_stop_target(
    *, side: str, mark: float, atr: float | None, pol: dict,
    asset_class: str | None = None,
) -> tuple[float | None, float | None]:
    """(stop_price, target_price) for a new position.

    Strategy:
      - If ATR is available → stop is `mult × ATR` off the mark on the
        loss side, where `mult` is 2 for equities and 2.8 for crypto
        (24/7 vol regime gaps right through a tight 2×ATR stop). Target
        preserves the policy's risk-reward ratio (`take_pct / |stop_pct|`)
        so the long-run R-multiple intent of the wallet stays intact
        while the *distance* scales with actual volatility.
      - Else fall back to the legacy fixed-percent rule from policy
        (stop_pct / take_pct), so funds keep trading on tickers
        without enough bar history yet (fresh crypto, new IPOs).
    Returns (None, None) when neither side has a sensible value
    (mark ≤ 0, or some pathological config) — the caller is
    expected to skip the trade in that case."""
    if mark <= 0:
        return None, None
    stop_pct = abs(pol.get("stop_pct", 0.0))
    take_pct = abs(pol.get("take_pct", 0.0))
    rr = (take_pct / stop_pct) if stop_pct > 0 else 0.0

    if atr is not None and atr > 0:
        atr_mult = (
            _ATR_STOP_MULT_CRYPTO if asset_class == "crypto"
            else _ATR_STOP_MULT
        )
        stop_dist = atr_mult * atr
        target_dist = stop_dist * rr if rr > 0 else stop_dist * 2.0
    else:
        # Legacy: distance is mark × policy pct. Guarantees the
        # downstream realised return == the policy pct on hit.
        stop_dist = mark * stop_pct if stop_pct > 0 else 0.0
        target_dist = mark * take_pct if take_pct > 0 else 0.0

    if stop_dist <= 0:
        return None, None

    if side == "long":
        stop = mark - stop_dist
        target = mark + target_dist if target_dist > 0 else None
    else:
        stop = mark + stop_dist
        target = mark - target_dist if target_dist > 0 else None
    if stop is None or stop <= 0:
        return None, None
    return round(stop, 4), round(target, 4) if target is not None else None


def _fixed_risk_qty(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float | None,
) -> float:
    """Position size that risks `risk_pct × equity` between entry and
    stop. Falls back to 0 (caller skips) when stop is missing or on
    the wrong side. This is the textbook "fixed-fractional" rule —
    same dollar risk on a tight-stop high-vol name as on a wide-stop
    blue chip, the only sane way to compare edges across the book."""
    if entry <= 0:
        return 0.0
    if stop is None or stop <= 0:
        return 0.0
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0:
        return 0.0
    dollars_at_risk = max(0.0, equity) * max(0.0, risk_pct)
    return dollars_at_risk / per_share_risk


def _drawdown_scale(session, fund_id: int, now: datetime) -> float:
    """Return a 0..1 multiplier that shrinks new-position size as the
    fund's drawdown deepens. Continuous, not stepped, so behaviour at
    the edges is smooth.

      0%   …  −5%  → 1.0   (full size)
      −5%  …  −10% → linear 1.0 → 0.6
      −10% … −15%  → linear 0.6 → 0.3
      ≤ −15%       → 0.3 (the circuit-breaker takes over below this)
    """
    cutoff = now - timedelta(days=90)
    pts = session.exec(
        select(FundEquity.equity)
        .where(FundEquity.fund_id == fund_id)
        .where(FundEquity.ts >= cutoff)
        .order_by(FundEquity.ts)
    ).all()
    if not pts:
        return 1.0
    peak = pts[0]
    for v in pts:
        if v > peak:
            peak = v
    cur = pts[-1]
    if peak <= 0:
        return 1.0
    dd = (cur - peak) / peak  # negative or zero
    if dd >= -0.05:
        return 1.0
    if dd >= -0.10:
        # 1.0 at -5%, 0.6 at -10%
        return 1.0 - ((-dd - 0.05) / 0.05) * 0.4
    if dd >= -0.15:
        # 0.6 at -10%, 0.3 at -15%
        return 0.6 - ((-dd - 0.10) / 0.05) * 0.3
    return 0.3


def _opens_today(session, fund_id: int, now: datetime) -> int:
    """Count of positions this fund has *opened* since UTC midnight.
    Used for the per-day cap so a news-cascade can't snowball into
    six fresh entries in one cycle."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_naive = midnight.replace(tzinfo=None)
    rows = session.exec(
        select(FundTrade.id)
        .where(FundTrade.fund_id == fund_id)
        .where(FundTrade.entry_at >= midnight_naive)
    ).all()
    return len(rows)


def _post_earnings_blackout(ticker: str, now: datetime) -> bool:
    """True iff `ticker` reported earnings in the last N days.

    Sources the most-recent print from EarningsDate.last_report_date —
    that field is preserved by upsert_earnings when the catalyst
    pipeline rolls report_date forward to the next quarter (without
    it, the prior date was wiped immediately and this blackout was
    structurally dead). 0 = "reported today", which we also block.
    """
    d = earnings.days_since_last_earnings(ticker, now.date())
    if d is None:
        return False
    return 0 <= d <= _POST_EARNINGS_BLACKOUT_DAYS


def _run() -> list[dict]:
    """Run one trading cycle for every active fund. Returns the move events
    (opens with their triggering thesis, closes with reason/realized) so the
    caller can narrate *why* — the reasoning already exists on the call, this
    just surfaces it. Pure DB; no LLM.

    Trade placement (see helpers above for the math):
      1. Risk exits: use the stored stop_price / target_price on the
         FundTrade row when present (set at open time), otherwise fall
         back to the policy's fixed stop_pct / take_pct for legacy rows.
      2. Entries: ATR(14)-based stop on the loss side at 2×ATR, target
         at the policy's risk-reward distance. Size by fixed-risk —
         qty = (equity × risk_pct × conv-bias × dd-scale) / |entry−stop|.
         Falls back to the legacy notional sizing only when ATR is
         unavailable AND there's no usable stop.
      3. Soft drawdown taper before the −15% circuit-breaker fires
         (see _drawdown_scale); plus a per-day opens cap so a news
         cascade can't snowball.
    """
    seed_funds()
    now = datetime.now(timezone.utc)
    moves: list[dict] = []
    with session_scope() as s:
        funds = s.exec(select(Fund).where(Fund.active == True)).all()  # noqa: E712
        # Market-regime read once per cycle (BTC is the proxy). Gates
        # counter-trend crypto entries across EVERY source — the engine-side
        # backstop to the same gate funding_squeeze applies at the detector.
        crypto_regime = market_regime(s, now)
        for fund in funds:
            pol = _effective_policy(fund)
            opens = _open_trades(s, fund.id)
            if pol is None:
                # Non-policy wallets (the user-driven `research` wallet)
                # don't auto-trade, but they DO need an equity tick so
                # their /portfolio card has a curve to draw. Previously
                # we skipped them entirely, leaving the FundEquity table
                # empty for research → the sparkline showed "no equity
                # history yet" forever.
                s.add(FundEquity(
                    fund_id=fund.id,
                    ts=now,
                    equity=round(_equity(s, fund, opens), 2),
                ))
                continue
            by_ticker = {t.ticker: t for t in opens}

            # 1. Risk exits on existing positions.
            for t in list(opens):
                mark = _mark(s, t.ticker)
                age = (now - t.entry_at.replace(tzinfo=timezone.utc)).days

                if mark is None or mark <= 0 or t.entry_price <= 0:
                    # No *usable* price: auto-pruned coin, delisted, dead
                    # feed, or a bad 0/negative tick. stop/take can't be
                    # judged without inventing P&L (a 0 mark would read as
                    # −100% and fake-liquidate the position). max_hold is
                    # time-only — enforce it at entry_price (realized ≈ 0) so
                    # a dead/garbage-priced ticker neither becomes an immortal
                    # position nor gets stopped out at a fabricated loss.
                    if age >= pol["max_hold_days"]:
                        logger.info(
                            "funds[{}]: force-closing stale ${} at entry "
                            "(no usable price, held {}d ≥ max_hold)",
                            fund.name, t.ticker, age,
                        )
                        _close(s, fund, t, t.entry_price, "max_hold_stale")
                        moves.append(_move_close(fund, t, age))
                        opens.remove(t)
                        by_ticker.pop(t.ticker, None)
                    continue

                # Prefer the stop/target stored on the trade row when set
                # (post-refactor opens always set them; legacy opens may
                # not). When stored values exist they win — they're the
                # vol-adjusted distances and the policy pct is a coarser
                # ratchet over them.
                reason: str | None = None
                d = 1 if t.side == "long" else -1
                if t.stop_price is not None:
                    hit = (
                        mark <= t.stop_price if t.side == "long"
                        else mark >= t.stop_price
                    )
                    if hit:
                        reason = "stop"
                if reason is None and t.target_price is not None:
                    hit = (
                        mark >= t.target_price if t.side == "long"
                        else mark <= t.target_price
                    )
                    if hit:
                        reason = "take"
                if reason is None:
                    ret = (mark - t.entry_price) / t.entry_price * d
                    # Policy-pct fallback only when no stored band fired
                    # (legacy rows that pre-date the refactor).
                    if t.stop_price is None and ret <= pol["stop_pct"]:
                        reason = "stop"
                    elif t.target_price is None and ret >= pol["take_pct"]:
                        reason = "take"
                if reason is None and age >= pol["max_hold_days"]:
                    reason = "max_hold"
                if reason:
                    _close(s, fund, t, mark, reason)
                    moves.append(_move_close(fund, t, age))
                    opens.remove(t)
                    by_ticker.pop(t.ticker, None)

            # Per-fund cycle context — computed once, not per call.
            dd_scale = _drawdown_scale(s, fund.id, now)
            opens_today = _opens_today(s, fund.id, now)
            max_per_day = pol.get("max_opens_per_day", _DEFAULT_MAX_OPENS_PER_DAY)

            # 2. New calls → flips + entries.
            new_calls = s.exec(
                select(TradingCall)
                .where(TradingCall.id > fund.last_call_id)
                .order_by(TradingCall.id)
            ).all()
            max_id = fund.last_call_id
            for call in new_calls:
                max_id = max(max_id, call.id)
                if call.source not in pol["sources"]:
                    continue
                if call.conviction < pol["min_conviction"]:
                    continue
                acls = asset_class_of(call.ticker)
                if pol["asset_classes"] and acls not in pol["asset_classes"]:
                    continue
                mark = _mark(s, call.ticker)
                if mark is None or mark <= 0:
                    continue

                # Crypto-only gate: don't open without microstructure
                # data on the coin. Funding rate / OI / orderbook
                # imbalance is the context that explains *why* a coin
                # ripped — without it the bot is taking a directional
                # bet on what's often pure perp-flow that the price
                # series alone can't see. The crypto_micro ingester
                # covers ~25 curated coins; anything outside that set
                # isn't suitable autonomous-trading material for this
                # wallet. Equities/futures don't have an analogue, so
                # the gate only fires for asset_class=crypto.
                if pol.get("require_micro") and acls == "crypto":
                    from .ingesters.crypto_micro import micro_for
                    micro = micro_for(call.ticker)
                    if micro is None:
                        logger.debug(
                            "funds[{}]: skip ${} — no microstructure data "
                            "(require_micro)",
                            fund.name, call.ticker,
                        )
                        continue

                # Earnings blackout — no fund INITIATES exposure into a
                # binary print (also blocks a flip into one: don't churn
                # into uncontrolled risk).
                edays = earnings.days_until_earnings(call.ticker, now.date())
                if edays is not None and 0 <= edays <= _EARNINGS_BLACKOUT_DAYS:
                    logger.debug(
                        "funds[{}]: skip ${} — earnings in {}d (pre-blackout)",
                        fund.name, call.ticker, edays,
                    )
                    continue
                # Post-earnings cooldown — same blackout window in
                # reverse so we don't trade the drift/gap chop right
                # after a print.
                if _post_earnings_blackout(call.ticker, now):
                    logger.debug(
                        "funds[{}]: skip ${} — earnings in last {}d (post-blackout)",
                        fund.name, call.ticker, _POST_EARNINGS_BLACKOUT_DAYS,
                    )
                    continue

                # Hype wallet only acts when the crowd is ALSO surging on the
                # name — social corroboration of the bot's momentum call.
                need = pol.get("require_social_surge")
                if need and _social_surge(s, call.ticker, now) < need:
                    continue

                # A `contrarian`-style fund FADES the call: it wants the
                # opposite side. Compute the desired side once and use it for
                # alignment, sizing, cash and the open — long/short are
                # already symmetric everywhere, so this is the only change.
                invert = pol.get("invert", False)
                want = call.direction
                if invert:
                    want = "short" if call.direction == "long" else "long"

                # Market-regime gate (crypto only): no new LONGS when the
                # complex is risk-off, no new SHORTS when it's risk-on. Alts
                # are ~80% BTC beta, so a counter-trend per-coin entry is the
                # classic way the crypto leg bleeds. Evaluated on `want` so a
                # contrarian fund's inverted side is judged correctly; BTC
                # itself is never gated against its own regime.
                if acls == "crypto" and blocks_entry(crypto_regime, want, call.ticker):
                    logger.debug(
                        "funds[{}]: skip ${} {} — counter-regime ({})",
                        fund.name, call.ticker, want, crypto_regime.reason,
                    )
                    continue

                held = by_ticker.get(call.ticker)
                if held is not None:
                    if held.side == want:
                        continue  # already aligned — no pyramiding
                    age = (now - held.entry_at.replace(tzinfo=timezone.utc)).days
                    _close(s, fund, held, mark, "flip")
                    moves.append(_move_close(fund, held, age))
                    opens.remove(held)
                    by_ticker.pop(call.ticker, None)

                if len(opens) >= pol["max_positions"]:
                    continue
                if opens_today >= max_per_day:
                    logger.debug(
                        "funds[{}]: skip ${} — daily open cap {} reached",
                        fund.name, call.ticker, max_per_day,
                    )
                    continue

                # ── sizing & risk bands ─────────────────────────────────
                equity = _equity(s, fund, opens)
                atr = _atr_in_session(s, call.ticker)
                stop, target = _compute_stop_target(
                    side=want, mark=mark, atr=atr, pol=pol,
                    asset_class=acls,
                )
                # Conviction- and drawdown-scaled risk budget. Capped by
                # the original size_pct on top conviction so a c5 sniper
                # trade can still scale into a meaningful position.
                # Per-source edge multiplier (the new control loop): if
                # `call.source` has a measured edge over the last 90d,
                # scale risk up toward 1.5×; if it's been bleeding, scale
                # down toward 0.3×. 1.0 = no data / insufficient sample.
                conv_bias = call.conviction / 5
                edge_mult = _source_edge_mult(call.source, now)
                risk_pct = (
                    _BASE_RISK_PCT * conv_bias * dd_scale * edge_mult
                    * max(1.0, pol["size_pct"] / 0.20)
                )
                qty = _fixed_risk_qty(
                    equity=equity, risk_pct=risk_pct,
                    entry=mark, stop=stop,
                )
                # Fall back to legacy notional sizing only when we don't
                # have a usable stop. Saves freshly-listed tickers (no
                # bar history yet → no ATR) from being silently dropped.
                # Edge multiplier rides on this path too — otherwise
                # ATR-less tickers would escape the per-source allocation.
                if qty <= 0:
                    size_cash = (
                        equity * pol["size_pct"]
                        * conv_bias * dd_scale * edge_mult
                    )
                    qty = size_cash / mark
                    if qty <= 0:
                        continue
                # Notional ceiling — never let fixed-risk sizing balloon
                # past the policy's original notional limit on a very
                # tight stop. (e.g. a stop 0.5% away on a degen would
                # otherwise demand 20× the wallet to risk 1%.) Edge mult
                # applies here too so a faded source can't slip past the
                # cap via a tight-stop trade.
                cap_cash = (
                    equity * pol["size_pct"]
                    * conv_bias * dd_scale * edge_mult
                )
                if qty * mark > cap_cash:
                    qty = cap_cash / mark
                if qty <= 0:
                    continue
                size_cash = qty * mark
                if not _within_budget(opens, equity, size_cash):
                    continue  # no leverage — symmetric long/short

                open_reason = (
                    f"{'fade ' if invert else ''}{call.source} "
                    f"c{call.conviction}"
                )
                t = FundTrade(
                    fund_id=fund.id,
                    ticker=call.ticker,
                    side=want,
                    qty=qty,
                    entry_price=mark,
                    entry_at=now,
                    call_id=call.id,
                    open_reason=open_reason,
                    stop_price=stop,
                    target_price=target,
                )
                if want == "long":
                    fund.cash -= qty * mark
                else:
                    fund.cash += qty * mark
                s.add(t)
                opens.append(t)
                by_ticker[call.ticker] = t
                opens_today += 1
                moves.append(
                    {
                        "fund": fund.name,
                        "kind": "open",
                        "ticker": call.ticker,
                        "side": want,
                        "qty": qty,
                        "price": mark,
                        "source": call.source,
                        "conviction": call.conviction,
                        "thesis": (call.thesis or "").strip(),
                        "invert": invert,
                        "stop": stop,
                        "target": target,
                        "atr": atr,
                        "dd_scale": dd_scale,
                        "edge_mult": edge_mult,
                    }
                )

            fund.last_call_id = max_id
            s.add(
                FundEquity(
                    fund_id=fund.id, ts=now, equity=round(_equity(s, fund, opens), 2)
                )
            )
            s.add(fund)

    return moves


def _moves_embed(moves: list[dict]) -> discord.Embed | None:
    """Deterministic narration of what every wallet did this cycle and WHY —
    the triggering call's own thesis verbatim (never an LLM re-guess) plus the
    mechanical exit reason. None when nothing traded."""
    by_fund: dict[str, list[dict]] = {}
    for m in moves:
        by_fund.setdefault(m["fund"], []).append(m)
    if not by_fund:
        return None
    blocks: list[str] = []
    for name in sorted(by_fund):
        lines = [f"__**{name}**__"]
        for m in by_fund[name]:
            if m["kind"] == "open":
                tag = "🔄 FADE " if m.get("invert") else ""
                arrow = "🟢" if m["side"] == "long" else "🔴"
                thesis = m["thesis"] or "(no thesis on the call)"
                if len(thesis) > 240:
                    thesis = thesis[:240].rstrip() + "…"
                # Surface the size scalars when they're non-trivial so the
                # user sees WHY this position was sized the way it was. A
                # trade at the baseline (edge≈1.0, dd≈1.0) shows nothing
                # extra; a faded or boosted size reads e.g. "× 0.6 edge" or
                # "× 1.4 edge · × 0.7 dd".
                size_bits: list[str] = []
                em = m.get("edge_mult")
                if em is not None and abs(em - 1.0) >= 0.05:
                    size_bits.append(f"×{em:.2f} edge")
                dd = m.get("dd_scale")
                if dd is not None and dd < 0.95:
                    size_bits.append(f"×{dd:.2f} dd")
                size_suffix = (" · " + " · ".join(size_bits)) if size_bits else ""
                lines.append(
                    f"{arrow} {tag}**{m['side'].upper()}** "
                    f"`${m['ticker']}` {m['qty']:.4g}@{m['price']:.4g} "
                    f"· {m['source']} c{m['conviction']}{size_suffix}\n"
                    f"   ↳ {thesis}"
                )
            else:
                r = m["realized"] or 0.0
                em = "✅" if r > 0 else "🔻" if r < 0 else "➖"
                lines.append(
                    f"⛔ closed {m['side'].upper()} `${m['ticker']}` "
                    f"{em} {r:+,.0f} · {m['reason']} · held "
                    f"{m['held_days']}d (was {m['open_reason'] or '?'})"
                )
        blocks.append("\n".join(lines))
    desc = "\n\n".join(blocks)
    if len(desc) > 4000:
        desc = desc[:3960].rstrip() + "\n\n…(truncated)"
    return discord.Embed(
        title=f"🧠 Wallet moves & reasoning — {len(moves)} this cycle",
        description=desc,
        color=ui.ACCENT,
    )


def _funds_channel() -> int:
    return (
        settings.DISCORD_FUNDS_CHANNEL_ID
        or settings.DISCORD_DIGEST_CHANNEL_ID
        or settings.DISCORD_META_CHANNEL_ID
    )


async def run_funds_cycle() -> None:
    try:
        moves = await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("run_funds_cycle failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ funds cycle error: {e}")
        except Exception:
            pass
        return

    # Trades are already committed. Narrating them is best-effort: a post
    # failure must not look like a trading failure or spam #meta.
    embed = _moves_embed(moves)
    chan = _funds_channel()
    if embed is None or not chan:
        return
    try:
        await discord_client.post_embed(chan, embed)
    except Exception as e:
        logger.warning("funds narration post failed: {}", e)


# ── reporting ───────────────────────────────────────────────────────────────


def fund_standings() -> list[dict]:
    out = []
    with session_scope() as s:
        for fund in s.exec(select(Fund).order_by(Fund.name)).all():
            opens = _open_trades(s, fund.id)
            eq = _equity(s, fund, opens)
            upnl = 0.0
            for t in opens:
                mark = _mark(s, t.ticker) or t.entry_price
                d = 1 if t.side == "long" else -1
                upnl += (t.qty * (mark - t.entry_price) * d) or 0.0
            closed = s.exec(
                select(FundTrade)
                .where(FundTrade.fund_id == fund.id)
                .where(FundTrade.status == "closed")
            ).all()
            wins = sum(1 for t in closed if (t.realized_pnl or 0) > 0)
            out.append(
                {
                    "name": fund.name,
                    "mandate": fund.mandate,
                    "equity": round(eq, 2),
                    "start": fund.starting_cash,
                    "ret_pct": round(
                        (eq - fund.starting_cash) / fund.starting_cash * 100, 2
                    ),
                    "open": len(opens),
                    "upnl": round(upnl, 2),
                    "closed": len(closed),
                    "wins": wins,
                }
            )
    out.sort(key=lambda d: d["ret_pct"], reverse=True)
    return out


def equity_curve(name: str | None = None, days: int = 30) -> list[dict]:
    """Equity-curve points per active fund — the dashboard plots these.

    Returns one entry per fund (or just the named one):
    ``[{"fund": "degen", "mandate": "...", "starting": 100,
        "points":  [{"ts": iso, "equity": float}, ...],
        "trades":  [{"ts": iso, "ticker", "side", "pnl"}, ...]}]``.

    `days` filters out anything older than that to keep the chart sharp on
    the recent shape (the old marks are still in the DB; this is just the
    default render window). The `trades` array is closed FundTrade rows in
    the same window — the dashboard renders them as time-axis markers on
    each fund's line so the user can see where realised PnL came from.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_naive = cutoff.replace(tzinfo=None)
    out: list[dict] = []
    with session_scope() as s:
        funds = s.exec(
            select(Fund).where(Fund.active.is_(True)).order_by(Fund.name)
        ).all()
        if name:
            funds = [f for f in funds if f.name == name]
        for f in funds:
            pts = s.exec(
                select(FundEquity)
                .where(FundEquity.fund_id == f.id)
                .where(FundEquity.ts >= cutoff)
                .order_by(FundEquity.ts)
            ).all()
            trades = s.exec(
                select(FundTrade)
                .where(FundTrade.fund_id == f.id)
                .where(FundTrade.status == "closed")
                .where(FundTrade.exit_at >= cutoff_naive)
                .order_by(FundTrade.exit_at)
            ).all()
            out.append({
                "fund": f.name,
                "mandate": f.mandate,
                "starting": f.starting_cash,
                "points": [
                    {
                        "ts": (
                            p.ts if p.ts.tzinfo
                            else p.ts.replace(tzinfo=timezone.utc)
                        ).isoformat(),
                        "equity": round(p.equity, 2),
                    }
                    for p in pts
                ],
                "trades": [
                    {
                        "ts": (
                            t.exit_at if t.exit_at.tzinfo
                            else t.exit_at.replace(tzinfo=timezone.utc)
                        ).isoformat(),
                        "ticker": t.ticker,
                        "side": t.side,
                        "pnl": (
                            round(t.realized_pnl, 2)
                            if t.realized_pnl is not None else None
                        ),
                        "close_reason": t.close_reason,
                    }
                    for t in trades
                ],
            })
    return out


def standings_text() -> str:
    rows = fund_standings()
    if not rows:
        return "**🏦 Funds**\nNot seeded yet — they start on the next cycle."
    lines = ["**🏦 Autonomous funds — standings**", ""]
    medal = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        wr = f"{r['wins']}/{r['closed']}" if r["closed"] else "0/0"
        upnl = (
            f" · uPnL {r['upnl']:+,.0f}" if r["open"] else ""
        )
        lines.append(
            f"{medal[i] if i < 3 else '·'} **{r['name']}** "
            f"${r['equity']:,.0f} ({r['ret_pct']:+.1f}%) · "
            f"{r['open']} open{upnl} · W/L {wr}"
        )
    return "\n".join(lines)[:4000]


def fund_detail_text(name: str) -> str:
    name = name.strip().lower()
    if name not in _POLICIES:
        return f"Unknown fund `{name}`. Try: {', '.join(_POLICIES)}."
    with session_scope() as s:
        fund = s.exec(select(Fund).where(Fund.name == name)).first()
        if fund is None:
            return f"Fund `{name}` not seeded yet."
        opens = _open_trades(s, fund.id)
        eq = _equity(s, fund, opens)
        recent = s.exec(
            select(FundTrade)
            .where(FundTrade.fund_id == fund.id)
            .where(FundTrade.status == "closed")
            .order_by(FundTrade.exit_at.desc())
            .limit(6)
        ).all()
        pos_lines = []
        open_upnl = 0.0
        for t in opens:
            mark = _mark(s, t.ticker) or t.entry_price
            d = 1 if t.side == "long" else -1
            # `or 0.0` normalises IEEE negative zero (a flat short computes
            # 0.0*-1 = -0.0, which would render as a misleading "-0").
            upnl = (t.qty * (mark - t.entry_price) * d) or 0.0
            open_upnl += upnl
            pos_lines.append(
                f"{'🟢L' if t.side == 'long' else '🔴S'} ${t.ticker} "
                f"{t.qty:.4g}@{t.entry_price:.4g}→{mark:.4g} "
                f"({upnl:+.0f})"
            )
        clo_lines = [
            f"`{t.exit_at:%m-%d}` ${t.ticker} {t.side} "
            f"{t.realized_pnl:+.0f} ({t.close_reason})"
            for t in recent
        ]
        asof = _marks_asof(s, [t.ticker for t in opens])
    body = [
        f"**🏦 {fund.name}** — {fund.mandate}",
        f"Equity **${eq:,.2f}** "
        f"({(eq - fund.starting_cash) / fund.starting_cash * 100:+.1f}% "
        f"from ${fund.starting_cash:,.0f}) · cash ${fund.cash:,.0f}",
    ]
    if asof is not None:
        _n = datetime.now(timezone.utc)
        _a = asof if asof.tzinfo else asof.replace(tzinfo=timezone.utc)
        if _n - _a > timedelta(hours=12):
            body.append(
                f"_marks as of {_ago(asof, _n)} ago · ⚠️ market likely "
                f"closed — P&L is frozen at entry until it reopens_"
            )
        else:
            body.append(f"_marks live · updated {_ago(asof, _n)} ago_")
    if pos_lines:
        body += [
            "",
            f"__Open__ · unrealized **{open_upnl:+,.0f}**",
        ] + pos_lines
    if clo_lines:
        body += ["", "__Recent closed__"] + clo_lines
    return "\n".join(body)[:4000]


def trade_history(name: str, days: int = 90) -> dict | None:
    """All trades (open + closed within `days`) on a wallet, with
    open_reason / close_reason populated — the audit surface for
    "what did this wallet do and why".

    Unlike `fund_positions`, this accepts ANY wallet name (including
    `research`, which deliberately has no `_POLICIES` entry — see
    `seed_funds`). The user-facing Research tab uses this to render the
    full history of executed Research Desk trades, including closed
    ones with the reason they closed — the answer to "where did my
    trade go?"."""
    name = (name or "").strip().lower()
    now = datetime.now(timezone.utc)
    cutoff_naive = (now - timedelta(days=days)).replace(tzinfo=None)
    with session_scope() as s:
        fund = s.exec(select(Fund).where(Fund.name == name)).first()
        if fund is None:
            return None
        opens = _open_trades(s, fund.id)
        closed = s.exec(
            select(FundTrade)
            .where(FundTrade.fund_id == fund.id)
            .where(FundTrade.status == "closed")
            .where(FundTrade.exit_at >= cutoff_naive)
            .order_by(FundTrade.exit_at.desc())
        ).all()
        eq = _equity(s, fund, opens)

        def _iso(t):
            return (
                t if t is None or t.tzinfo
                else t.replace(tzinfo=timezone.utc)
            )

        def _open_row(t: FundTrade) -> dict:
            mark_val = _mark(s, t.ticker)
            mark = mark_val if mark_val is not None else t.entry_price
            d = 1 if t.side == "long" else -1
            upnl = (t.qty * (mark - t.entry_price) * d) or 0.0
            cost = t.entry_price * t.qty
            return {
                "id": t.id,
                "ticker": t.ticker, "side": t.side, "qty": t.qty,
                "entry": t.entry_price,
                "entry_at": _iso(t.entry_at).isoformat(),
                "mark": mark,
                "mark_live": mark_val is not None,
                "upnl": round(upnl, 2),
                "upnl_pct": (
                    round(upnl / cost * 100, 2) if cost else 0.0
                ),
                "open_reason": t.open_reason,
                "call_id": t.call_id,
            }

        def _closed_row(t: FundTrade) -> dict:
            cost = t.entry_price * t.qty
            return {
                "id": t.id,
                "ticker": t.ticker, "side": t.side, "qty": t.qty,
                "entry": t.entry_price,
                "entry_at": _iso(t.entry_at).isoformat(),
                "exit": t.exit_price,
                "exit_at": (
                    _iso(t.exit_at).isoformat() if t.exit_at else None
                ),
                "realized_pnl": (
                    round(t.realized_pnl, 2)
                    if t.realized_pnl is not None else None
                ),
                "realized_pct": (
                    round((t.realized_pnl or 0) / cost * 100, 2)
                    if cost else 0.0
                ),
                "open_reason": t.open_reason,
                "close_reason": t.close_reason,
                "call_id": t.call_id,
            }

        return {
            "name": fund.name,
            "mandate": fund.mandate,
            "cash": round(fund.cash, 2),
            "equity": round(eq, 2),
            "starting": fund.starting_cash,
            "ret_pct": round(
                (eq - fund.starting_cash) / fund.starting_cash * 100, 2
            ) if fund.starting_cash else 0.0,
            "open": [_open_row(t) for t in opens],
            "closed": [_closed_row(t) for t in closed],
            "as_of": now.isoformat(),
        }


def fund_positions(name: str) -> dict | None:
    """Structured snapshot of one wallet's **open** positions for the cockpit.

    Sibling of `fund_detail_text` — same data and helpers, but returns a
    dict so the UI can render real tiles/tables. The text fn is left
    untouched (Discord still calls it). Returns None for an unknown or
    not-yet-seeded fund so the caller can show a clean empty state.
    """
    name = (name or "").strip().lower()
    if name not in _POLICIES:
        return None
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        fund = s.exec(select(Fund).where(Fund.name == name)).first()
        if fund is None:
            return None
        opens = _open_trades(s, fund.id)
        eq = _equity(s, fund, opens)
        positions: list[dict] = []
        open_upnl = 0.0
        for t in opens:
            mark_val = _mark(s, t.ticker)
            mark = mark_val if mark_val is not None else t.entry_price
            d = 1 if t.side == "long" else -1
            # `or 0.0` normalises -0.0 on a flat short (0 * -1 = -0.0).
            upnl = (t.qty * (mark - t.entry_price) * d) or 0.0
            open_upnl += upnl
            cost = t.entry_price * t.qty
            positions.append({
                "ticker": t.ticker,
                "side": t.side,
                "qty": t.qty,
                "entry": t.entry_price,
                "mark": mark,
                "mark_live": mark_val is not None,
                "upnl": round(upnl, 2),
                "upnl_pct": (
                    round(upnl / cost * 100, 2) if cost else 0.0
                ),
            })
        asof = _marks_asof(s, [t.ticker for t in opens])
    asof_utc = (
        asof if (asof is None or asof.tzinfo)
        else asof.replace(tzinfo=timezone.utc)
    )
    marks_stale = bool(
        asof_utc is not None and (now - asof_utc) > timedelta(hours=12)
    )
    return {
        "name": fund.name,
        "mandate": fund.mandate,
        "equity": round(eq, 2),
        "starting_cash": fund.starting_cash,
        "ret_pct": round(
            (eq - fund.starting_cash) / fund.starting_cash * 100, 2
        ),
        "cash": round(fund.cash, 2),
        "open_upnl": round(open_upnl, 2),
        "marks_asof": asof_utc,
        "marks_ago": _ago(asof_utc, now) if asof_utc is not None else None,
        "marks_stale": marks_stale,
        "positions": positions,
    }


def open_positions_all() -> list[dict]:
    """Every open trade across every wallet, with live marks + uPnL.
    Single batched query over FundTrade; useful for the dashboard's
    unified book view. One row per open trade."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    with session_scope() as s:
        funds_by_id = {
            f.id: f for f in s.exec(select(Fund)).all()
        }
        opens = s.exec(
            select(FundTrade).where(FundTrade.status == "open")
            .order_by(FundTrade.entry_at)
        ).all()
        for t in opens:
            fund = funds_by_id.get(t.fund_id)
            if fund is None:
                continue
            mark_val = _mark(s, t.ticker)
            mark = mark_val if mark_val is not None else t.entry_price
            d = 1 if t.side == "long" else -1
            upnl = (t.qty * (mark - t.entry_price) * d) or 0.0
            cost = t.entry_price * t.qty
            entry_at = t.entry_at if t.entry_at.tzinfo else (
                t.entry_at.replace(tzinfo=timezone.utc)
            )
            # Pre-compute "distance to stop / target" so the UI can
            # render a risk-reward bar without doing math itself.
            # For long: stop is below entry, target above. Short: reversed.
            dist_to_stop_pct: float | None = None
            dist_to_target_pct: float | None = None
            r_multiple: float | None = None
            if t.stop_price is not None and t.stop_price > 0 and mark > 0:
                if t.side == "long":
                    dist_to_stop_pct = round((mark - t.stop_price) / mark * 100, 2)
                else:
                    dist_to_stop_pct = round((t.stop_price - mark) / mark * 100, 2)
            if t.target_price is not None and t.target_price > 0 and mark > 0:
                if t.side == "long":
                    dist_to_target_pct = round((t.target_price - mark) / mark * 100, 2)
                else:
                    dist_to_target_pct = round((mark - t.target_price) / mark * 100, 2)
            # R-multiple = current PnL / initial risk per share. Tells
            # you "this trade is up 2.3R" — universal across position
            # sizing, the metric every trader watches.
            if t.stop_price is not None and t.stop_price > 0:
                initial_risk = (
                    (t.entry_price - t.stop_price) if t.side == "long"
                    else (t.stop_price - t.entry_price)
                )
                if initial_risk > 0:
                    current_per_share = (
                        (mark - t.entry_price) if t.side == "long"
                        else (t.entry_price - mark)
                    )
                    r_multiple = round(current_per_share / initial_risk, 2)
            # % of wallet equity exposed by this position.
            pct_of_equity = round(cost / max(1e-9, _equity(s, fund, [t])) * 100, 1)

            out.append({
                "id": t.id,
                "fund": fund.name,
                "fund_mandate": fund.mandate,
                "ticker": t.ticker,
                "asset_class": asset_class_of(t.ticker),
                "side": t.side,
                "qty": t.qty,
                "entry": t.entry_price,
                "entry_at": entry_at.isoformat(),
                "age_h": round((now - entry_at).total_seconds() / 3600, 1),
                "mark": mark,
                "mark_live": mark_val is not None,
                "upnl": round(upnl, 2),
                "upnl_pct": round(upnl / cost * 100, 2) if cost else 0.0,
                "open_reason": t.open_reason,
                "call_id": t.call_id,
                "stop_price": t.stop_price,
                "target_price": t.target_price,
                "trailing_stop_pct": t.trailing_stop_pct,
                "watermark_price": t.watermark_price,
                "notes": t.notes,
                "dist_to_stop_pct": dist_to_stop_pct,
                "dist_to_target_pct": dist_to_target_pct,
                "r_multiple": r_multiple,
                "pct_of_equity": pct_of_equity,
                "notional": round(cost, 2),
            })
    return out


def position_chart(ticker: str, days: int | None = 60) -> dict:
    """Symbol page chart payload, sourced from the autonomous FundTrade
    book (the v1 PaperTrade store was missing the bot's actual trades).

    Returns ``{ticker, bars, open_positions[], closed[], context}`` where
    ``open_positions`` is one row per WALLET currently holding the
    ticker (multiple funds can hold the same name) with the
    risk-management bands (stop / target / trailing) so the chart can
    render levels per position.

    Closed trades come from the same FundTrade table — one entry/exit
    pair per round-trip, capped to the chart's time window so an
    ancient trade doesn't pin a label to today's frame.

    ``days=None`` returns full PriceBar history."""
    from .models import PriceBar  # local; PriceBar already imported above
    ticker = (ticker or "").upper().lstrip("$").strip()
    if not ticker:
        return {
            "ticker": "", "bars": [], "open_positions": [],
            "closed": [], "context": None,
            "open_position": None,  # legacy: kept for SPA compatibility
        }
    now = datetime.now(timezone.utc)
    bars_cutoff = (
        now - timedelta(days=days) if days is not None else None
    )
    # Closed trades clamped to ≤365d regardless of bars window so a
    # long-history "All" view doesn't paint 5-year-old markers onto a
    # week of bars.
    closed_cutoff_naive = (
        now - timedelta(days=min(days, 365))
        if days is not None
        else now - timedelta(days=365)
    ).replace(tzinfo=None)

    with session_scope() as s:
        bars_q = select(PriceBar).where(PriceBar.ticker == ticker)
        if bars_cutoff is not None:
            bars_q = bars_q.where(PriceBar.ts >= bars_cutoff)
        bars = s.exec(bars_q.order_by(PriceBar.ts)).all()

        funds_by_id = {f.id: f for f in s.exec(select(Fund)).all()}
        open_rows = s.exec(
            select(FundTrade)
            .where(FundTrade.ticker == ticker)
            .where(FundTrade.status == "open")
            .order_by(FundTrade.entry_at)
        ).all()
        closed_rows = s.exec(
            select(FundTrade)
            .where(FundTrade.ticker == ticker)
            .where(FundTrade.status == "closed")
            .where(FundTrade.exit_at >= closed_cutoff_naive)
            .order_by(FundTrade.exit_at)
        ).all()
        pc = s.get(PriceContext, ticker)
        mark = pc.last_price if pc is not None else None

    def _iso(dt: datetime) -> str:
        return (
            dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        ).isoformat()

    open_positions: list[dict] = []
    for t in open_rows:
        fund = funds_by_id.get(t.fund_id)
        cost = t.entry_price * t.qty
        d = 1 if t.side == "long" else -1
        pnl = None
        pnl_pct = None
        if mark is not None and mark > 0:
            pnl = round(t.qty * (mark - t.entry_price) * d, 2)
            pnl_pct = round(pnl / cost * 100, 2) if cost else None
        open_positions.append({
            "id": t.id,
            "fund": fund.name if fund else None,
            "side": t.side,
            "qty": t.qty,
            "entry": t.entry_price,
            "entry_at": _iso(t.entry_at),
            "mark": mark,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "stop_price": t.stop_price,
            "target_price": t.target_price,
            "trailing_stop_pct": t.trailing_stop_pct,
            "watermark_price": t.watermark_price,
            "open_reason": t.open_reason,
        })

    closed_out: list[dict] = []
    for t in closed_rows:
        fund = funds_by_id.get(t.fund_id)
        closed_out.append({
            "id": t.id,
            "fund": fund.name if fund else None,
            "side": t.side,
            "qty": t.qty,
            "entry": t.entry_price,
            "entry_at": _iso(t.entry_at),
            "exit": t.exit_price,
            "exit_at": _iso(t.exit_at) if t.exit_at else None,
            "pnl": (
                round(t.realized_pnl, 2)
                if t.realized_pnl is not None else None
            ),
            "close_reason": t.close_reason,
        })

    # Legacy `open_position` (singular) — the SPA's existing CandleChart
    # used this shape. Picks the most-recent open as a reasonable single
    # representative until the SPA is migrated to the list payload.
    legacy_open = open_positions[-1] if open_positions else None

    return {
        "ticker": ticker,
        "bars": [
            {
                "ts": _iso(b.ts), "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume,
            }
            for b in bars
        ],
        "open_positions": open_positions,
        "closed": closed_out,
        "open_position": legacy_open,
        "context": (
            {
                "last_price": pc.last_price,
                "change_1d_pct": pc.change_1d_pct,
                "change_5d_pct": pc.change_5d_pct,
                "volume_vs_20d_avg": pc.volume_vs_20d_avg,
                "last_updated": _iso(pc.last_updated),
            } if pc is not None else None
        ),
    }


def book_summary() -> dict:
    """Cross-wallet KPI rollup for the Overview ribbon — open position
    count, total unrealised PnL on open positions (live marks; falls
    back to entry when the mark is missing, so this never reads as a
    fake −100% on a dead-priced ticker), realised PnL on all closed
    trades, win count, closed count.

    Used by /api/overview/kpi — was previously sourced from the legacy
    PaperTrade store, which only the v1 cockpit writes to, so for a
    user running the autonomous funds the ribbon read empty / stale."""
    with session_scope() as s:
        opens = s.exec(
            select(FundTrade).where(FundTrade.status == "open")
        ).all()
        unrealized = 0.0
        for t in opens:
            mark = _mark(s, t.ticker)
            if mark is None or mark <= 0:
                mark = t.entry_price
            d = 1 if t.side == "long" else -1
            unrealized += t.qty * (mark - t.entry_price) * d
        closed = s.exec(
            select(FundTrade).where(FundTrade.status == "closed")
        ).all()
    wins = sum(1 for t in closed if (t.realized_pnl or 0) > 0)
    realized = sum(t.realized_pnl or 0.0 for t in closed)
    return {
        "open": len(opens),
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl": round(realized, 2),
        "wins": wins,
        "closed": len(closed),
    }


def realized_curve_funds() -> list[dict]:
    """Cumulative realised P&L across every wallet, oldest closed first.

    Same shape as portfolio.realized_curve (the PaperTrade-only one) so
    the dashboard's overview hero can swap to this without a frontend
    change: ``[{ts, ticker, side, fund, pnl, cumulative}]``."""
    with session_scope() as s:
        funds_by_id = {f.id: f for f in s.exec(select(Fund)).all()}
        rows = s.exec(
            select(FundTrade)
            .where(FundTrade.status == "closed")
            .order_by(FundTrade.exit_at)
        ).all()
    out: list[dict] = []
    cum = 0.0
    for t in rows:
        if t.exit_at is None or t.realized_pnl is None:
            continue
        cum += t.realized_pnl
        fund = funds_by_id.get(t.fund_id)
        exit_at = (
            t.exit_at if t.exit_at.tzinfo
            else t.exit_at.replace(tzinfo=timezone.utc)
        )
        out.append({
            "ts": exit_at.isoformat(),
            "ticker": t.ticker,
            "side": t.side,
            "fund": fund.name if fund else None,
            "pnl": round(t.realized_pnl, 2),
            "cumulative": round(cum, 2),
        })
    return out


def closed_trades_recent(
    limit: int = 100,
    fund_name: str | None = None,
) -> list[dict]:
    """Closed trades across every wallet (or just `fund_name`), newest
    first. Returns the full lifecycle payload: entry/exit prices and
    timestamps, hold duration, R-multiple if a stop was set, realized
    pnl + pnl%, open/close reasons, and the user's notes — everything
    the /journal page needs to surface a post-trade reflection.

    This is the read side of the trade journal. The notes field is
    user-editable via `update_trade_journal` (no status restriction)
    so the trader can reflect on a position long after it's closed.
    """
    fname = (fund_name or "").strip().lower() or None
    out: list[dict] = []
    with session_scope() as s:
        funds_by_id = {f.id: f for f in s.exec(select(Fund)).all()}
        q = (
            select(FundTrade)
            .where(FundTrade.status == "closed")
        )
        if fname:
            fid = next(
                (fid for fid, f in funds_by_id.items() if f.name == fname),
                None,
            )
            if fid is None:
                return []
            q = q.where(FundTrade.fund_id == fid)
        q = q.order_by(FundTrade.exit_at.desc()).limit(max(1, min(limit, 500)))
        rows = s.exec(q).all()

        for t in rows:
            fund = funds_by_id.get(t.fund_id)
            if fund is None:
                continue
            cost = t.entry_price * t.qty
            entry_at = (
                t.entry_at if t.entry_at.tzinfo
                else t.entry_at.replace(tzinfo=timezone.utc)
            )
            exit_at = None
            hold_h: float | None = None
            if t.exit_at is not None:
                exit_at = (
                    t.exit_at if t.exit_at.tzinfo
                    else t.exit_at.replace(tzinfo=timezone.utc)
                )
                hold_h = round(
                    (exit_at - entry_at).total_seconds() / 3600, 1
                )
            r_multiple: float | None = None
            if t.stop_price is not None and t.stop_price > 0 and t.exit_price:
                initial_risk = (
                    (t.entry_price - t.stop_price) if t.side == "long"
                    else (t.stop_price - t.entry_price)
                )
                if initial_risk > 0:
                    realized_per_share = (
                        (t.exit_price - t.entry_price) if t.side == "long"
                        else (t.entry_price - t.exit_price)
                    )
                    r_multiple = round(realized_per_share / initial_risk, 2)
            realized_pct = (
                round((t.realized_pnl or 0) / cost * 100, 2)
                if cost else 0.0
            )
            out.append({
                "id": t.id,
                "fund": fund.name,
                "ticker": t.ticker,
                "side": t.side,
                "qty": t.qty,
                "entry": t.entry_price,
                "entry_at": entry_at.isoformat(),
                "exit": t.exit_price,
                "exit_at": exit_at.isoformat() if exit_at else None,
                "hold_h": hold_h,
                "realized_pnl": (
                    round(t.realized_pnl, 2)
                    if t.realized_pnl is not None else None
                ),
                "realized_pct": realized_pct,
                "open_reason": t.open_reason,
                "close_reason": t.close_reason,
                "call_id": t.call_id,
                "stop_price": t.stop_price,
                "target_price": t.target_price,
                "r_multiple": r_multiple,
                "notes": t.notes,
                "notional": round(cost, 2),
            })
    return out


def trade_lifecycle(trade_id: int) -> dict | None:
    """Everything the bot saw about a trade's ticker between entry
    and (exit or now). Powers the journal drill-in: "what news, calls
    and filings happened while I was in this position?".

    Returns:
      trade: slim trade summary
      news: list[{id, title, url, source, ts, sentiment, impact_1d_pct}]
      filings: list[{id, form_type, filed_at, url, materiality_score}]
      calls: list[{id, source, direction, conviction, thesis, created_at}]
    """
    with session_scope() as s:
        t = s.get(FundTrade, trade_id)
        if t is None:
            return None
        fund = s.get(Fund, t.fund_id)
        entry_at = (
            t.entry_at if t.entry_at.tzinfo
            else t.entry_at.replace(tzinfo=timezone.utc)
        )
        exit_at_aware: datetime | None = None
        if t.exit_at is not None:
            exit_at_aware = (
                t.exit_at if t.exit_at.tzinfo
                else t.exit_at.replace(tzinfo=timezone.utc)
            )
        # SQLite stores naive datetimes; compare in naive UTC.
        entry_naive = entry_at.replace(tzinfo=None)
        exit_naive = (exit_at_aware or datetime.now(timezone.utc)).replace(tzinfo=None)
        ticker = (t.ticker or "").upper()

        # News (singular ticker match OR multi-ticker csv).
        from .models import NewsItem, Filing, TradingCall  # local import to keep top tidy
        from sqlalchemy import or_
        news_q = (
            select(NewsItem)
            .where(NewsItem.published_at >= entry_naive)
            .where(NewsItem.published_at <= exit_naive)
            .where(or_(
                NewsItem.ticker == ticker,
                NewsItem.tickers_csv.contains(f",{ticker},"),
            ))
            .order_by(NewsItem.published_at.desc())
            .limit(80)
        )
        filings_q = (
            select(Filing)
            .where(Filing.filed_at >= entry_naive)
            .where(Filing.filed_at <= exit_naive)
            .where(Filing.ticker == ticker)
            .order_by(Filing.filed_at.desc())
            .limit(40)
        )
        calls_q = (
            select(TradingCall)
            .where(TradingCall.created_at >= entry_naive)
            .where(TradingCall.created_at <= exit_naive)
            .where(TradingCall.ticker == ticker)
            .order_by(TradingCall.created_at.desc())
            .limit(40)
        )

        def _aware(dt: datetime | None) -> str | None:
            if dt is None:
                return None
            return (
                dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            ).isoformat()

        news = [
            {
                "id": n.id, "title": n.title, "url": n.url,
                "source": n.source, "ts": _aware(n.published_at),
                "sentiment": n.sentiment,
                "impact_1d_pct": n.impact_1d_pct,
            }
            for n in s.exec(news_q).all()
        ]
        filings = [
            {
                "id": f.id, "form_type": f.form_type,
                "filed_at": _aware(f.filed_at),
                "url": f.primary_doc_url,
                "materiality_score": f.materiality_score,
            }
            for f in s.exec(filings_q).all()
        ]
        calls = [
            {
                "id": c.id, "source": c.source, "direction": c.direction,
                "conviction": c.conviction, "thesis": c.thesis,
                "created_at": _aware(c.created_at),
                "ret_1d_pct": c.ret_1d_pct,
                "ret_5d_pct": c.ret_5d_pct,
                "ret_20d_pct": c.ret_20d_pct,
            }
            for c in s.exec(calls_q).all()
        ]

        return {
            "trade": {
                "id": t.id,
                "ticker": ticker,
                "side": t.side,
                "fund": fund.name if fund else None,
                "entry_at": entry_at.isoformat(),
                "exit_at": exit_at_aware.isoformat() if exit_at_aware else None,
                "status": t.status,
            },
            "news": news,
            "filings": filings,
            "calls": calls,
        }


def update_trade_journal(trade_id: int, notes: str | None) -> dict:
    """Edit a trade's free-form journal note. Unlike `update_trade_risk`,
    this works on CLOSED trades too — the whole point of a post-trade
    reflection is to come back to it after the position is gone.
    `notes=None` (or empty after strip) clears the field."""
    with session_scope() as s:
        t = s.get(FundTrade, trade_id)
        if t is None:
            return {"ok": False, "message": f"no trade #{trade_id}"}
        if notes is None or not notes.strip():
            t.notes = None
        else:
            t.notes = notes.strip()[:2000]
        s.add(t)
    return {"ok": True, "trade_id": trade_id, "message": "journal saved"}


def update_trade_risk(
    trade_id: int,
    *,
    stop_price: float | None = None,
    target_price: float | None = None,
    trailing_stop_pct: float | None = None,
    notes: str | None = None,
    clear: list[str] | None = None,
) -> dict:
    """Update risk-management fields on an open trade. Pass None to
    leave a field alone; pass `clear=["stop_price"]` to explicitly
    null out a field. Returns `{"ok": bool, "message": str, ...}`."""
    clear = set(clear or [])
    with session_scope() as s:
        t = s.get(FundTrade, trade_id)
        if t is None:
            return {"ok": False, "message": f"no trade #{trade_id}"}
        if t.status != "open":
            return {"ok": False,
                    "message": f"trade #{trade_id} is {t.status}"}
        if "stop_price" in clear:
            t.stop_price = None
        elif stop_price is not None:
            if stop_price <= 0:
                return {"ok": False, "message": "stop_price must be positive"}
            t.stop_price = stop_price
        if "target_price" in clear:
            t.target_price = None
        elif target_price is not None:
            if target_price <= 0:
                return {"ok": False, "message": "target_price must be positive"}
            t.target_price = target_price
        if "trailing_stop_pct" in clear:
            t.trailing_stop_pct = None
            t.watermark_price = None
        elif trailing_stop_pct is not None:
            if not (0 < trailing_stop_pct < 1):
                return {"ok": False,
                        "message": "trailing_stop_pct must be 0..1 (e.g. 0.10 = 10%)"}
            t.trailing_stop_pct = trailing_stop_pct
            # Seed watermark at current mark.
            mark_val = _mark(s, t.ticker)
            if mark_val is not None:
                t.watermark_price = mark_val
        if "notes" in clear:
            t.notes = None
        elif notes is not None:
            t.notes = (notes or "")[:2000] or None
        s.add(t)
    return {"ok": True, "message": f"updated #{trade_id}",
            "trade_id": trade_id}


def open_trade_manual(
    *,
    fund_name: str,
    ticker: str,
    side: str,
    qty: float | None = None,
    notional: float | None = None,
    risk_pct: float | None = None,
    stop_price: float | None = None,
    note: str | None = None,
) -> dict:
    """User-initiated paper trade open. Three sizing modes (use one):

    - ``qty``: pass an absolute share count
    - ``notional``: pass a dollar amount; qty = notional / mark
    - ``risk_pct`` + ``stop_price``: classic "fixed-risk" sizing —
      qty = (equity × risk_pct) / |entry − stop|. Lets the user
      decide what % of equity they're willing to lose if the stop
      hits (e.g. 1% risk per trade across positions of varying
      volatility).

    Validates side, ticker, mark availability, cash budget. Records
    `open_reason="manual"` and an optional `note` (stored in the
    new `notes` slot for journal continuity). Publishes a `trade`
    SSE event so the bell + feed pick up immediately.

    Returns ``{"ok": bool, "message": str, "trade_id": int | None,
    "fill_price": float | None, "qty": float | None}``.
    """
    side = (side or "").lower().strip()
    if side not in ("long", "short"):
        return {"ok": False, "message": "side must be 'long' or 'short'",
                "trade_id": None, "fill_price": None, "qty": None}
    ticker = (ticker or "").upper().lstrip("$").strip()
    if not ticker:
        return {"ok": False, "message": "ticker required",
                "trade_id": None, "fill_price": None, "qty": None}
    name = (fund_name or "").strip().lower()
    sized_by = (
        "qty" if qty is not None
        else "notional" if notional is not None
        else "risk" if (risk_pct is not None and stop_price is not None)
        else None
    )
    if sized_by is None:
        return {"ok": False,
                "message": "specify qty OR notional OR (risk_pct + stop_price)",
                "trade_id": None, "fill_price": None, "qty": None}

    with session_scope() as s:
        fund = s.exec(select(Fund).where(Fund.name == name)).first()
        if fund is None:
            return {"ok": False, "message": f"unknown wallet '{name}'",
                    "trade_id": None, "fill_price": None, "qty": None}
        mark = _mark(s, ticker)
        if mark is None or mark <= 0:
            return {"ok": False,
                    "message": f"no live mark for {ticker} — try later",
                    "trade_id": None, "fill_price": None, "qty": None}

        opens = _open_trades(s, fund.id)
        equity = _equity(s, fund, opens)

        if sized_by == "qty":
            final_qty = float(qty)
        elif sized_by == "notional":
            final_qty = float(notional) / mark
        else:
            # Risk sizing: stop distance per share × qty = $ risk
            risk_per_share = abs(mark - float(stop_price))
            if risk_per_share <= 0:
                return {"ok": False,
                        "message": "stop_price too close to mark — zero risk",
                        "trade_id": None, "fill_price": None, "qty": None}
            risk_dollars = equity * float(risk_pct)
            final_qty = risk_dollars / risk_per_share

        if final_qty <= 0:
            return {"ok": False, "message": "computed qty is non-positive",
                    "trade_id": None, "fill_price": None, "qty": None}

        cost = final_qty * mark
        if not _within_budget(opens, equity, cost):
            return {"ok": False,
                    "message": (
                        "exceeds wallet budget — no leverage allowed; "
                        "size down or close another position first"
                    ),
                    "trade_id": None, "fill_price": None, "qty": None}

        now = datetime.now(timezone.utc)
        t = FundTrade(
            fund_id=fund.id,
            ticker=ticker,
            side=side,
            qty=final_qty,
            entry_price=mark,
            entry_at=now,
            open_reason=f"manual ({sized_by})",
            notes=(note or "")[:2000] or None,
            stop_price=float(stop_price) if (
                sized_by == "risk" and stop_price is not None
            ) else None,
        )
        if side == "long":
            fund.cash -= cost
        else:
            fund.cash += cost
        s.add(t)
        s.flush()
        trade_id = t.id
        s.add(fund)

    try:
        from . import events
        events.publish("trade", {
            "trade_id": trade_id,
            "ticker": ticker,
            "side": side,
            "fund": name,
            "summary": (
                f"opened {side} {ticker} ×{final_qty:.4f} @ {mark:.4f} "
                f"({sized_by} sizing)"
            ),
        })
    except Exception:
        pass

    return {
        "ok": True,
        "message": f"opened #{trade_id} ({side} {ticker})",
        "trade_id": trade_id,
        "fill_price": round(mark, 4),
        "qty": round(final_qty, 6),
    }


def close_trade_by_id(trade_id: int, reason: str = "manual") -> dict:
    """Close one open position at the current mark. Used by the
    dashboard book view. Returns ``{"ok": bool, "message": str,
    "trade_id": int, "realized_pnl": float | None}``."""
    with session_scope() as s:
        t = s.get(FundTrade, trade_id)
        if t is None:
            return {"ok": False, "message": f"no trade #{trade_id}",
                    "trade_id": trade_id, "realized_pnl": None}
        if t.status != "open":
            return {"ok": False,
                    "message": f"trade #{trade_id} is {t.status}",
                    "trade_id": trade_id, "realized_pnl": None}
        fund = s.get(Fund, t.fund_id)
        if fund is None:
            return {"ok": False, "message": "fund missing",
                    "trade_id": trade_id, "realized_pnl": None}
        mark_val = _mark(s, t.ticker)
        if mark_val is None:
            return {"ok": False,
                    "message": f"no mark for {t.ticker}; cannot close",
                    "trade_id": trade_id, "realized_pnl": None}
        _close(s, fund, t, mark_val, reason or "manual")
        pnl = t.realized_pnl
    # Publish event for live dashboard subscribers (best-effort).
    try:
        from . import events
        events.publish("trade", {
            "trade_id": trade_id,
            "ticker": t.ticker,
            "side": t.side,
            "realized_pnl": pnl,
            "fund": fund.name,
            "summary": f"closed {t.side} {t.ticker} · pnl {pnl:+.2f}",
        })
    except Exception:
        pass
    return {"ok": True, "message": f"closed #{trade_id}",
            "trade_id": trade_id, "realized_pnl": pnl}


def funds_brief() -> str:
    """One-liner for the synthesis snapshot — the bot seeing its own scoreboard."""
    rows = fund_standings()
    if not rows:
        return "funds not started"
    return "; ".join(f"{r['name']} {r['ret_pct']:+.1f}%" for r in rows)


# ── meta-analysis: is the edge real, and where? ─────────────────────────────

_MIN_EDGE_SAMPLE = 15  # combined closed trades before an edge is called


def _drawdown(equity_pts: list[float]) -> float:
    """Worst peak-to-trough % over an equity series (0.0 if never underwater)."""
    peak = None
    worst = 0.0
    for e in equity_pts:
        if peak is None or e > peak:
            peak = e
        if peak:
            worst = min(worst, (e - peak) / peak * 100)
    return round(worst, 2)


def _conv_bucket(conv: int) -> str:
    return "low" if conv <= 2 else "high" if conv >= 4 else "med"


def wallet_meta() -> dict:
    """Deterministic edge readout across every wallet — pure arithmetic over
    FundTrade / Fund / FundEquity joined to the triggering TradingCall. This
    is a measurement instrument: it never asks an LLM, it never guesses, and
    it refuses to call an edge real below _MIN_EDGE_SAMPLE closed trades."""
    with session_scope() as s:
        all_funds = s.exec(select(Fund).order_by(Fund.name)).all()
        per: list[dict] = []
        ret_by: dict[str, float] = {}
        n_by: dict[str, int] = {}
        all_closed: list[FundTrade] = []
        for fund in all_funds:
            opens = _open_trades(s, fund.id)
            closed = s.exec(
                select(FundTrade)
                .where(FundTrade.fund_id == fund.id)
                .where(FundTrade.status == "closed")
            ).all()
            all_closed.extend(closed)
            eq = _equity(s, fund, opens)
            ret = (eq - fund.starting_cash) / fund.starting_cash * 100
            n = len(closed)
            wins = [t for t in closed if (t.realized_pnl or 0.0) > 0]
            gross_w = sum(t.realized_pnl or 0.0 for t in wins)
            gross_l = -sum(
                t.realized_pnl or 0.0 for t in closed
                if (t.realized_pnl or 0.0) < 0
            )
            holds, by_reason = [], {}
            for t in closed:
                if t.entry_at and t.exit_at:
                    a = t.entry_at if t.entry_at.tzinfo else t.entry_at.replace(
                        tzinfo=timezone.utc)
                    b = t.exit_at if t.exit_at.tzinfo else t.exit_at.replace(
                        tzinfo=timezone.utc)
                    holds.append((b - a).days)
                k = t.close_reason or "?"
                by_reason[k] = round(
                    by_reason.get(k, 0.0) + (t.realized_pnl or 0.0), 2
                )
            eq_pts = [
                r.equity for r in s.exec(
                    select(FundEquity)
                    .where(FundEquity.fund_id == fund.id)
                    .order_by(FundEquity.ts)
                ).all()
            ]
            ret_by[fund.name] = round(ret, 2)
            n_by[fund.name] = n
            per.append({
                "name": fund.name,
                "ret_pct": round(ret, 2),
                "n_open": len(opens),
                "n_closed": n,
                "win_rate": round(len(wins) / n * 100, 1) if n else None,
                "expectancy": round(
                    sum(t.realized_pnl or 0.0 for t in closed) / n, 2
                ) if n else None,
                "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
                "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
                "max_drawdown_pct": _drawdown(eq_pts),
                "pnl_by_reason": by_reason,
            })
        per.sort(key=lambda d: d["ret_pct"], reverse=True)

        call_ids = {t.call_id for t in all_closed if t.call_id}
        calls = {
            c.id: c for c in s.exec(
                select(TradingCall).where(TradingCall.id.in_(call_ids))
            ).all()
        } if call_ids else {}

    def _bump(d: dict, key: str, r: float) -> None:
        a = d.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
        a["n"] += 1
        a["pnl"] = round(a["pnl"] + r, 2)
        a["wins"] += 1 if r > 0 else 0

    by_source, by_conv, by_asset = {}, {}, {}
    for t in all_closed:
        r = t.realized_pnl or 0.0
        c = calls.get(t.call_id) if t.call_id else None
        _bump(by_source, c.source if c else "?", r)
        _bump(by_conv, _conv_bucket(c.conviction) if c else "?", r)
        _bump(by_asset, asset_class_of(t.ticker) or "?", r)

    def _exp(a: str, b: str, kind: str) -> dict:
        ra, rb = ret_by.get(a, 0.0), ret_by.get(b, 0.0)
        n = n_by.get(a, 0) + n_by.get(b, 0)
        spread = round(ra - rb, 2)
        if n < _MIN_EDGE_SAMPLE:
            verdict = f"too early — {n} closed trades, need ≥{_MIN_EDGE_SAMPLE}"
        elif kind == "momentum":
            verdict = (
                f"momentum edge holds (+{spread}%)" if spread > 0
                else f"⚠️ MIRAGE — {b} beats {a} by {-spread}%" if spread < 0
                else "dead heat"
            )
        else:
            verdict = (
                f"crowd confirmation helps (+{spread}%)" if spread > 0
                else f"crowd confirmation hurts ({spread}%)" if spread < 0
                else "neutral"
            )
        return {"a": a, "b": b, "a_ret": ra, "b_ret": rb,
                "spread": spread, "n": n, "verdict": verdict}

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "funds": per,
        "by_source": by_source,
        "by_conviction": by_conv,
        "by_asset": by_asset,
        "experiments": {
            "momentum": _exp("degen", "contrarian", "momentum"),
            "crowd": _exp("hype", "degen", "crowd"),
        },
        # Live per-source risk multipliers — the active control loop
        # that's currently shaping every new position's size. Surfaced
        # here so the dashboard can render a small panel: which sources
        # are being pressed on, which are being faded, and why.
        "edge_multipliers": edge_multipliers(),
    }


def wallet_edge_brief() -> str:
    """Compact one-liner for the synthesis snapshot — the brain reading its
    own measured edge (observation, not auto-action)."""
    m = wallet_meta()
    if not m["funds"]:
        return "wallets not started"
    e = m["experiments"]
    return (
        f"momentum (degen vs contrarian): {e['momentum']['verdict']}; "
        f"crowd (hype vs degen): {e['crowd']['verdict']}"
    )


def _fmt_grp(d: dict, top: int = 6) -> str:
    rows = sorted(d.items(), key=lambda kv: abs(kv[1]["pnl"]), reverse=True)
    return " · ".join(
        f"{k} {v['pnl']:+,.0f} ({v['wins']}/{v['n']})"
        for k, v in rows[:top]
    ) or "—"


def meta_text(m: dict | None = None) -> str:
    m = m if m is not None else wallet_meta()
    if not m["funds"]:
        return "**🔬 Wallet meta**\nNo wallets seeded yet."
    em = m["experiments"]
    lines = [
        "**🔬 Wallet meta — is the edge real, and where?**",
        f"_as of {m['as_of'][:16]}Z_",
        "",
        "**Experiments**",
        f"• Momentum (degen vs contrarian): {em['momentum']['a_ret']:+.1f}% "
        f"vs {em['momentum']['b_ret']:+.1f}% — {em['momentum']['verdict']}",
        f"• Crowd (hype vs degen): {em['crowd']['a_ret']:+.1f}% "
        f"vs {em['crowd']['b_ret']:+.1f}% — {em['crowd']['verdict']}",
        "",
        f"**By signal source**  {_fmt_grp(m['by_source'])}",
        f"**By conviction**  {_fmt_grp(m['by_conviction'])}",
        f"**By asset**  {_fmt_grp(m['by_asset'])}",
        "",
        "**Wallets** _(ret · win · PF · maxDD · hold · closed/open)_",
    ]
    medal = ["🥇", "🥈", "🥉"]
    for i, f in enumerate(m["funds"]):
        wr = f"{f['win_rate']:.0f}%" if f["win_rate"] is not None else "—"
        pf = f"{f['profit_factor']}" if f["profit_factor"] is not None else "—"
        hold = f"{f['avg_hold_days']:.0f}d" if f["avg_hold_days"] is not None else "—"
        lines.append(
            f"{medal[i] if i < 3 else '·'} **{f['name']}** "
            f"{f['ret_pct']:+.1f}% · {wr} · PF {pf} · "
            f"DD {f['max_drawdown_pct']:.0f}% · {hold} · "
            f"{f['n_closed']}c/{f['n_open']}o"
        )
    return "\n".join(lines)[:4000]


def _meta_embed() -> discord.Embed | None:
    m = wallet_meta()
    if not m["funds"]:
        return None
    # meta_text() already leads with the bold header — no embed title, so the
    # scheduled post and the !meta command render identically.
    return discord.Embed(description=meta_text(m), color=ui.ACCENT)


async def run_funds_meta() -> None:
    try:
        embed = await asyncio.to_thread(_meta_embed)
    except Exception as e:
        logger.exception("run_funds_meta failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ funds meta error: {e}")
        except Exception:
            pass
        return
    chan = _funds_channel()
    if embed is None or not chan:
        return
    try:
        await discord_client.post_embed(chan, embed, importance=3)
    except Exception as e:
        logger.warning("funds meta post failed: {}", e)


async def run_funds_digest() -> None:
    try:
        import discord

        text = await asyncio.to_thread(standings_text)
        embed = discord.Embed(
            title="🏦 Fund standings", description=text, color=0x1ABC9C
        )
        await discord_client.post_embed(
            settings.DISCORD_DIGEST_CHANNEL_ID, embed, importance=3
        )
    except Exception as e:
        logger.exception("run_funds_digest failure: {}", e)
