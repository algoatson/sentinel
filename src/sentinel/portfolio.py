"""Paper-portfolio helpers.

`Holding` = a lightweight watch (relevance tagging only). `PaperTrade` = a
real paper position with entry/size and live-marked P&L. "The book" for
tagging/relevance is the union of both; P&L logic only touches PaperTrade.
Personal paper-trading tool — opinions/positions are the point, no broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from .db import session_scope
from .models import Holding, PaperTrade, PriceBar, PriceContext


def held_tickers() -> set[str]:
    """Canonical tickers in the book — Holdings + open paper positions.
    Cheap; recomputed per call so it's never stale after !hold/!buy."""
    with session_scope() as s:
        out = {h.ticker for h in s.exec(select(Holding)).all() if h.ticker}
        out |= {
            p.ticker
            for p in s.exec(
                select(PaperTrade).where(PaperTrade.status == "open")
            ).all()
            if p.ticker
        }
    return out


def is_held(ticker: str | None) -> bool:
    return bool(ticker) and ticker in held_tickers()


def _mark_price(session, ticker: str) -> float | None:
    pc = session.get(PriceContext, ticker)
    return pc.last_price if pc is not None else None


def position_pnl(p: PaperTrade, mark: float | None) -> float | None:
    """Unrealized (open) or realized (closed) P&L in quote currency."""
    if p.status == "closed" and p.realized_pnl is not None:
        return p.realized_pnl
    if mark is None:
        return None
    direction = 1.0 if p.side == "long" else -1.0
    return (mark - p.entry_price) * p.qty * direction


def open_positions() -> list[dict]:
    """Open positions with live mark + unrealized P&L, for the synthesis
    snapshot and !positions."""
    with session_scope() as s:
        rows = s.exec(
            select(PaperTrade).where(PaperTrade.status == "open")
        ).all()
        out = []
        for p in rows:
            mark = _mark_price(s, p.ticker)
            pnl = position_pnl(p, mark)
            cost = p.entry_price * p.qty
            out.append(
                {
                    "ticker": p.ticker,
                    "side": p.side,
                    "qty": p.qty,
                    "entry": p.entry_price,
                    "mark": mark,
                    "pnl": pnl,
                    "pnl_pct": (
                        round(pnl / cost * 100, 2)
                        if pnl is not None and cost
                        else None
                    ),
                }
            )
    return out


def realized_summary() -> dict:
    """Closed-trade tally: total realized P&L, win count, total count."""
    with session_scope() as s:
        closed = s.exec(
            select(PaperTrade).where(PaperTrade.status == "closed")
        ).all()
    wins = sum(1 for p in closed if (p.realized_pnl or 0) > 0)
    total = sum(p.realized_pnl or 0.0 for p in closed)
    return {"closed": len(closed), "wins": wins, "realized_pnl": round(total, 2)}


def _iso(t: datetime) -> str:
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


def realized_curve() -> list[dict]:
    """Cumulative realized P&L over time — one point per closed trade in
    `exit_at` order. Empty list when nothing's been closed yet.

    Each point: ``{"ts": iso, "ticker": str, "side": str, "pnl": float,
    "cumulative": float}``. The chart consumes the cumulative, the row
    breakdown is there for tooltips."""
    with session_scope() as s:
        rows = s.exec(
            select(PaperTrade)
            .where(PaperTrade.status == "closed")
            .order_by(PaperTrade.exit_at)
        ).all()
    out: list[dict] = []
    cum = 0.0
    for p in rows:
        if p.exit_at is None or p.realized_pnl is None:
            continue
        cum += p.realized_pnl
        out.append({
            "ts": _iso(p.exit_at),
            "ticker": p.ticker,
            "side": p.side,
            "pnl": round(p.realized_pnl, 2),
            "cumulative": round(cum, 2),
        })
    return out


def position_chart(ticker: str, days: int = 60) -> dict:
    """Everything the dashboard needs to plot a chart for `ticker`: OHLC
    bars from PriceBar, the open paper position (if any) for the entry
    marker, and recent closed trades in the same window. Empty bars list
    is fine — a name with no PriceBar history yet renders as "no data".

    Returns ``{"ticker", "bars": [{ts, open, high, low, close, volume}],
    "open_position": {...}|None, "closed": [{...}], "context": {...}|None}``.
    """
    ticker = (ticker or "").upper().lstrip("$").strip()
    if not ticker:
        return {"ticker": "", "bars": [], "open_position": None,
                "closed": [], "context": None}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as s:
        bars = s.exec(
            select(PriceBar)
            .where(PriceBar.ticker == ticker)
            .where(PriceBar.ts >= cutoff)
            .order_by(PriceBar.ts)
        ).all()
        open_p = s.exec(
            select(PaperTrade)
            .where(PaperTrade.ticker == ticker)
            .where(PaperTrade.status == "open")
        ).first()
        closed_rows = s.exec(
            select(PaperTrade)
            .where(PaperTrade.ticker == ticker)
            .where(PaperTrade.status == "closed")
            .where(PaperTrade.exit_at >= cutoff)
            .order_by(PaperTrade.exit_at)
        ).all()
        pc = s.get(PriceContext, ticker)

    open_pos = None
    if open_p is not None:
        mark = pc.last_price if pc is not None else None
        pnl = position_pnl(open_p, mark)
        cost = open_p.entry_price * open_p.qty
        open_pos = {
            "side": open_p.side,
            "qty": open_p.qty,
            "entry": open_p.entry_price,
            "entry_at": _iso(open_p.entry_at),
            "mark": mark,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_pct": (
                round(pnl / cost * 100, 2)
                if pnl is not None and cost else None
            ),
        }

    return {
        "ticker": ticker,
        "bars": [
            {"ts": _iso(b.ts), "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume}
            for b in bars
        ],
        "open_position": open_pos,
        "closed": [
            {"side": p.side, "qty": p.qty,
             "entry": p.entry_price, "entry_at": _iso(p.entry_at),
             "exit": p.exit_price,
             "exit_at": _iso(p.exit_at) if p.exit_at else None,
             "pnl": (
                 round(p.realized_pnl, 2)
                 if p.realized_pnl is not None else None
             )}
            for p in closed_rows
        ],
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


def add_hold(ticker: str, qty: float | None = None) -> dict:
    """Add or update a hold (tagging-only — not a paper position). Single
    chokepoint shared by Discord !hold and the dashboard's Holds panel.

    Returns ``{"ok": bool, "message": str, "ticker": str, "qty": float |
    None, "created": bool}``. Idempotent: calling on an existing ticker
    updates its qty (if a qty is given) rather than duplicating.
    """
    ticker = (ticker or "").strip().upper().lstrip("$")
    if not ticker:
        return {"ok": False, "message": "ticker required",
                "ticker": "", "qty": None, "created": False}
    qty_v: float | None = None
    if qty is not None and qty != "":
        try:
            qty_v = float(qty)
        except (TypeError, ValueError):
            return {"ok": False,
                    "message": f"couldn't parse qty `{qty}`",
                    "ticker": ticker, "qty": None, "created": False}
        if qty_v < 0:
            return {"ok": False, "message": "qty must be ≥ 0",
                    "ticker": ticker, "qty": qty_v, "created": False}
    with session_scope() as s:
        row = s.exec(
            select(Holding).where(Holding.ticker == ticker)
        ).first()
        created = row is None
        if created:
            s.add(Holding(
                ticker=ticker, quantity=qty_v,
                added_at=datetime.now(timezone.utc),
            ))
        else:
            if qty_v is not None:
                row.quantity = qty_v
            s.add(row)
    verb = "added" if created else "updated"
    qstr = f" ×{qty_v:g}" if qty_v is not None else ""
    return {"ok": True, "message": f"{verb} ${ticker}{qstr}",
            "ticker": ticker, "qty": qty_v, "created": created}


def remove_hold(ticker: str) -> dict:
    """Remove a hold by ticker. Same return shape as add_hold (minus
    ``created``)."""
    ticker = (ticker or "").strip().upper().lstrip("$")
    if not ticker:
        return {"ok": False, "message": "ticker required", "ticker": ""}
    with session_scope() as s:
        row = s.exec(
            select(Holding).where(Holding.ticker == ticker)
        ).first()
        if row is None:
            return {"ok": False,
                    "message": f"${ticker} isn't in your book",
                    "ticker": ticker}
        s.delete(row)
    return {"ok": True, "message": f"removed ${ticker}",
            "ticker": ticker}


def list_holds() -> list[dict]:
    """Holds + live price context, oldest-first (matches !holdings)."""
    out: list[dict] = []
    with session_scope() as s:
        for h in s.exec(select(Holding).order_by(Holding.added_at)).all():
            pc = s.get(PriceContext, h.ticker)
            out.append({
                "ticker": h.ticker,
                "qty": h.quantity,
                "added_at": h.added_at,
                "price": pc.last_price if pc else None,
                "change_1d_pct": (
                    pc.change_1d_pct * 100 if pc else None
                ),
                "change_5d_pct": (
                    pc.change_5d_pct * 100 if pc else None
                ),
            })
    return out


def open_paper_position(
    ticker: str,
    side: str,
    qty: float,
    *,
    price: float | None = None,
    note: str | None = None,
    opened_by: str = "manual",
) -> dict:
    """Open a paper position. Single chokepoint shared by Discord
    !buy/!short and the dashboard's Open form, so behaviour can't drift
    between the two surfaces.

    Returns a result dict — always shaped the same so callers don't
    branch on missing keys: ``{"ok": bool, "message": str, "ticker":
    str, "side": str, "qty": float, "price": float | None}``.

    One open position per ticker (keeps the book unambiguous: otherwise
    `!close` would close an arbitrary lot, and long+short could coexist).
    """
    ticker = (ticker or "").strip().upper().lstrip("$")
    side = (side or "").strip().lower()

    def _fail(msg: str, p: float | None = None) -> dict:
        return {"ok": False, "message": msg, "ticker": ticker,
                "side": side, "qty": qty, "price": p}

    if side not in ("long", "short"):
        return _fail(f"unknown side `{side}` — use long or short")
    if not ticker:
        return _fail("ticker required")
    try:
        qty_f = float(qty)
    except (TypeError, ValueError):
        return _fail(f"couldn't parse qty `{qty}` — give a number")
    if qty_f <= 0:
        return _fail("qty must be positive")
    note_out = (note or "").strip()[:200] or None

    with session_scope() as s:
        existing = s.exec(
            select(PaperTrade)
            .where(PaperTrade.ticker == ticker)
            .where(PaperTrade.status == "open")
        ).first()
        if existing is not None:
            return _fail(
                f"already holding {existing.side} {existing.qty:g} "
                f"${ticker} @ {existing.entry_price:.4g} — "
                f"close it first to resize or flip"
            )
        if price is None:
            price = _mark_price(s, ticker)
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        if price_f is None or price_f <= 0:
            return _fail(
                f"no usable price for ${ticker} — pass a positive one"
            )
        s.add(PaperTrade(
            ticker=ticker, side=side, qty=qty_f, entry_price=price_f,
            entry_at=datetime.now(timezone.utc),
            note=note_out, opened_by=opened_by,
        ))
    return {
        "ok": True,
        "message": (
            f"opened {side} {qty_f:g} ${ticker} @ {price_f:.4g}"
            + (f" — {note_out}" if note_out else "")
        ),
        "ticker": ticker, "side": side, "qty": qty_f, "price": price_f,
    }


def close_position(ticker: str, mark: float | None) -> PaperTrade | None:
    """Close the open position on `ticker` at `mark`. Returns the closed row
    (detached, expire_on_commit=False) or None if nothing was open."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        p = s.exec(
            select(PaperTrade)
            .where(PaperTrade.ticker == ticker)
            .where(PaperTrade.status == "open")
        ).first()
        if p is None:
            return None
        if mark is None:
            mark = _mark_price(s, ticker) or p.entry_price
        p.status = "closed"
        p.exit_price = mark
        p.exit_at = now
        p.realized_pnl = position_pnl(p, mark)
        s.add(p)
        return p
