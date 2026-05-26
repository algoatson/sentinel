"""Market-data tools the LLM can call mid-reasoning.

Each function in here is a thin adapter that lets the model fetch
something the bot already knows but isn't in the initial evidence
payload: a recent price window, ATR, peer-mover snapshot, news /
filings search, crypto microstructure, correlation between two
tickers, or the current open book.

Design notes:
  * Every tool returns a small JSON-friendly dict. Big return values
    waste model context.
  * Tools are *read-only*. Nothing here can open/close trades or
    write to the DB; the bot's autonomous trading path is separate.
  * Adding a new tool is one ``@TOOLS.tool(...)`` decorator + a Python
    function. The JSON schema goes in the decorator; the docstring
    becomes operator documentation (the model sees ``description``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlmodel import select

from .analytics import correlation as _corr
from .analytics import volatility as _vol
from .db import session_scope
from .llm_tools import ToolRegistry
from .models import (
    Filing,
    FundTrade,
    NewsItem,
    PriceBar,
    PriceContext,
    Watchlist,
)


TOOLS = ToolRegistry()


# ── string normalisers ────────────────────────────────────────────────


def _norm(t: str) -> str:
    return (t or "").upper().lstrip("$").strip()


# ── tools ─────────────────────────────────────────────────────────────


@TOOLS.tool(
    description=(
        "Recent daily OHLCV bars for a ticker. Use when you need to "
        "see actual price action — not just the 1d % move — e.g. to "
        "judge whether a move is a breakout, gap, or just noise. "
        "Bars are most-recent-first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Symbol like NVDA or BTC-USD"},
            "days":   {"type": "integer", "minimum": 1, "maximum": 90, "default": 14},
        },
        "required": ["ticker"],
    },
)
def get_ticker_chart(ticker: str, days: int = 14) -> dict:
    sym = _norm(ticker)
    days = max(1, min(int(days), 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days * 2 + 3)
    with session_scope() as s:
        bars = s.exec(
            select(PriceBar)
            .where(PriceBar.ticker == sym)
            .where(PriceBar.ts >= cutoff)
            .order_by(PriceBar.ts.desc())
        ).all()
    if not bars:
        return {"ticker": sym, "bars": [], "note": "no price data"}
    # Collapse intraday → one bar per UTC day so the model sees
    # actual daily candles.
    by_day: dict[str, dict] = {}
    for b in bars:
        d = b.ts.strftime("%Y-%m-%d")
        cur = by_day.get(d)
        if cur is None:
            by_day[d] = {
                "date": d, "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume,
            }
        else:
            cur["high"] = max(cur["high"], b.high)
            cur["low"] = min(cur["low"], b.low)
            cur["close"] = b.close
            cur["volume"] += b.volume
    out = sorted(by_day.values(), key=lambda r: r["date"], reverse=True)[:days]
    return {"ticker": sym, "bars": out, "count": len(out)}


@TOOLS.tool(
    description=(
        "Current quick-stats: last price, 1d/5d %, vol vs 20d avg. "
        "Use when you want a fast 'where is it now' check on any "
        "ticker the bot tracks."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
        },
        "required": ["ticker"],
    },
)
def get_ticker_stats(ticker: str) -> dict:
    sym = _norm(ticker)
    with session_scope() as s:
        pc = s.get(PriceContext, sym)
    if pc is None:
        return {"ticker": sym, "note": "not tracked"}
    return {
        "ticker": sym,
        "last_price": pc.last_price,
        "change_1d_pct": round((pc.change_1d_pct or 0) * 100, 2),
        "change_5d_pct": round((pc.change_5d_pct or 0) * 100, 2),
        "volume_vs_20d_avg": round(pc.volume_vs_20d_avg or 0, 2),
        "as_of": (
            pc.last_updated.replace(tzinfo=timezone.utc)
            if pc.last_updated and pc.last_updated.tzinfo is None
            else pc.last_updated
        ).isoformat() if pc.last_updated else None,
    }


@TOOLS.tool(
    description=(
        "Wilder ATR(14) and suggested 2×ATR stop on a ticker. Use "
        "when you need to judge how big today's move is vs the "
        "ticker's natural daily range."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "period": {"type": "integer", "minimum": 5, "maximum": 60, "default": 14},
        },
        "required": ["ticker"],
    },
)
def get_atr(ticker: str, period: int = 14) -> dict:
    return _vol.atr_for(_norm(ticker), period=int(period))


@TOOLS.tool(
    description=(
        "Top N movers in the same asset class today (by absolute "
        "1-day percent change). Use to tell whether the subject is "
        "alone or part of a cohort move."
    ),
    parameters={
        "type": "object",
        "properties": {
            "asset_class": {
                "type": "string",
                "enum": ["equity", "crypto", "future", "rate"],
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 20, "default": 6,
            },
        },
        "required": ["asset_class"],
    },
)
def peer_movers(asset_class: str, limit: int = 6) -> dict:
    cls = (asset_class or "").lower()
    limit = max(1, min(int(limit), 20))
    with session_scope() as s:
        rows = s.exec(
            select(PriceContext, Watchlist.asset_class)
            .join(Watchlist, Watchlist.ticker == PriceContext.ticker)
        ).all()
    out = []
    for pc, c in rows:
        if (c or "equity") != cls:
            continue
        chg = pc.change_1d_pct or 0.0
        out.append({
            "ticker": pc.ticker,
            "change_1d_pct": round(chg * 100, 2),
            "change_5d_pct": round((pc.change_5d_pct or 0) * 100, 2),
            "volume_vs_20d_avg": round(pc.volume_vs_20d_avg or 0, 2),
        })
    out.sort(key=lambda d: abs(d["change_1d_pct"]), reverse=True)
    return {"asset_class": cls, "movers": out[:limit]}


@TOOLS.tool(
    description=(
        "Recent news headlines mentioning a ticker. Returns title, "
        "source, timestamp, sentiment, and 1d impact %. Use to scan "
        "for a catalyst that wasn't in the initial evidence payload."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "hours":  {"type": "integer", "minimum": 1, "maximum": 168, "default": 48},
            "limit":  {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
        },
        "required": ["ticker"],
    },
)
def search_news(ticker: str, hours: int = 48, limit: int = 6) -> dict:
    sym = _norm(ticker)
    hours = max(1, min(int(hours), 168))
    limit = max(1, min(int(limit), 20))
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    with session_scope() as s:
        rows = s.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff_naive)
            .where(
                or_(
                    NewsItem.ticker == sym,
                    NewsItem.tickers_csv.contains(f",{sym},"),
                )
            )
            .order_by(NewsItem.published_at.desc())
            .limit(limit)
        ).all()
    return {
        "ticker": sym,
        "items": [
            {
                "title": n.title,
                "source": n.source,
                "ts": (
                    n.published_at.replace(tzinfo=timezone.utc)
                    if n.published_at.tzinfo is None
                    else n.published_at
                ).isoformat(),
                "sentiment": n.sentiment,
                "impact_1d_pct": n.impact_1d_pct,
                "summary": (n.summary or "")[:240] or None,
            }
            for n in rows
        ],
    }


@TOOLS.tool(
    description=(
        "Recent SEC filings for an equity ticker. Returns form type, "
        "filing date, materiality score, and a short summary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "days":   {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
            "limit":  {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["ticker"],
    },
)
def recent_filings(ticker: str, days: int = 30, limit: int = 5) -> dict:
    sym = _norm(ticker)
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 10))
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).replace(tzinfo=None)
    with session_scope() as s:
        rows = s.exec(
            select(Filing)
            .where(Filing.ticker == sym)
            .where(Filing.filed_at >= cutoff_naive)
            .order_by(Filing.filed_at.desc())
            .limit(limit)
        ).all()
    return {
        "ticker": sym,
        "filings": [
            {
                "form_type": f.form_type,
                "filed_at": (
                    f.filed_at.replace(tzinfo=timezone.utc)
                    if f.filed_at.tzinfo is None else f.filed_at
                ).isoformat(),
                "materiality_score": f.materiality_score,
                "summary": (f.summary or "")[:240] or None,
                "url": f.primary_doc_url,
            }
            for f in rows
        ],
    }


@TOOLS.tool(
    description=(
        "Pearson correlation on daily log returns between two "
        "tickers over the last N days. Use to test whether a move "
        "is likely driven by a common factor."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker_a": {"type": "string"},
            "ticker_b": {"type": "string"},
            "days":     {"type": "integer", "minimum": 5, "maximum": 180, "default": 30},
        },
        "required": ["ticker_a", "ticker_b"],
    },
)
def correlation(ticker_a: str, ticker_b: str, days: int = 30) -> dict:
    a = _norm(ticker_a)
    b = _norm(ticker_b)
    days = max(5, min(int(days), 180))
    out = _corr.correlation_matrix(tickers=[a, b], days=days)
    matrix = out.get("matrix") or []
    if len(matrix) >= 2 and len(matrix[0]) >= 2:
        return {
            "ticker_a": a, "ticker_b": b, "days": days,
            "correlation": matrix[0][1],
            "bars_used": (out.get("bars_used") or {}).get(a),
        }
    return {"ticker_a": a, "ticker_b": b, "days": days,
            "correlation": None, "note": "insufficient bars"}


@TOOLS.tool(
    description=(
        "Crypto microstructure for a coin: order-book imbalance, "
        "spread, last trades. Available for tracked coins only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Coin pair like BTC-USD"},
        },
        "required": ["ticker"],
    },
)
def crypto_micro(ticker: str) -> dict:
    from .ingesters.crypto_micro import micro_for

    sym = _norm(ticker)
    data = micro_for(sym)
    if not data:
        return {"ticker": sym, "note": "no microstructure data"}
    return {"ticker": sym, **data}


@TOOLS.tool(
    description=(
        "Current open positions across all wallets — what the bot "
        "is holding right now, with side and quantity. Use to know "
        "whether the subject ticker is already in the book."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Filter to a single ticker; omit to get all opens.",
            },
        },
    },
)
def get_holdings(ticker: str | None = None) -> dict:
    sym = _norm(ticker) if ticker else None
    with session_scope() as s:
        q = select(FundTrade).where(FundTrade.status == "open")
        if sym:
            q = q.where(FundTrade.ticker == sym)
        rows = s.exec(q).all()
    return {
        "filter": sym,
        "positions": [
            {
                "id": t.id, "fund_id": t.fund_id,
                "ticker": t.ticker, "side": t.side,
                "qty": t.qty, "entry": t.entry_price,
                "stop": t.stop_price, "target": t.target_price,
            }
            for t in rows
        ],
    }


def default_registry() -> ToolRegistry:
    """The registry every pipeline should use by default. Returning the
    module-level singleton — registries are read-only in practice."""
    return TOOLS


__all__ = ["TOOLS", "default_registry"]
