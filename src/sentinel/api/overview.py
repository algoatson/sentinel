"""Overview page endpoints — KPI tiles, equity curve, realised P&L
curve, activity feed (calls + filings + news mixed)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from sqlmodel import select

from .. import funds as _funds
from .. import portfolio as _portfolio
from .. import scorecard
from ..db import session_scope
from ..llm import llm_stats
from ..models import Filing, NewsItem, TradingCall


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/overview/kpi")
def kpi_snapshot() -> dict[str, Any]:
    """One blocking gather of headline numbers — identical to the
    `_kpi_snapshot` the old NiceGUI ribbon used, but pure data so the
    frontend renders it however it wants."""
    out: dict[str, Any] = {}
    try:
        st = _funds.fund_standings()
        eq = sum(r["equity"] for r in st)
        start = sum(r["start"] for r in st)
        out["equity"] = eq
        out["return_pct"] = (
            ((eq - start) / start * 100) if start else None
        )
        out["wallets"] = len(st)
    except Exception:
        out.update(equity=None, return_pct=None, wallets=None)
    try:
        pos = _portfolio.open_positions()
        out["open_positions"] = len(pos)
        out["unrealized_pnl"] = sum(
            p["pnl"] for p in pos if p.get("pnl") is not None
        )
    except Exception:
        out["open_positions"] = None
        out["unrealized_pnl"] = None
    try:
        r = _portfolio.realized_summary()
        out["realized_pnl"] = r["realized_pnl"]
        out["wins"], out["closed"] = r["wins"], r["closed"]
    except Exception:
        out["realized_pnl"] = out["wins"] = out["closed"] = None
    try:
        tr = scorecard.track_record_summary()["overall"]
        out["calls_scored"] = tr["n"]
        out["hit_rate_pct"] = (
            (tr["hits"] / tr["n"] * 100) if tr["n"] else None
        )
        out["hits"] = tr["hits"]
    except Exception:
        out["calls_scored"] = out["hit_rate_pct"] = out["hits"] = None
    try:
        ls = llm_stats()
        out["llm_calls"] = ls["calls"]
        out["llm_errors"] = ls["errors"]
        out["llm_reliability_pct"] = (
            (1 - ls["errors"] / ls["calls"]) * 100
            if ls["calls"] else None
        )
    except Exception:
        out["llm_calls"] = out["llm_errors"] = out["llm_reliability_pct"] = None
    return out


@router.get("/overview/equity-curve")
def equity_curve(days: int = Query(30, ge=1, le=365)) -> list[dict]:
    """Multi-line equity curve, one entry per active fund."""
    return _funds.equity_curve(None, days)


@router.get("/overview/realized-curve")
def realized_curve() -> list[dict]:
    """Cumulative realised P&L points, one per closed trade."""
    return _portfolio.realized_curve()


@router.get("/overview/activity")
def activity_feed(hours: int = Query(48, ge=1, le=168)) -> list[dict]:
    """Mixed feed of recent calls + filings + news, newest first.
    Capped at 40 items; same shape as the old `_activity_panel` loader
    but as pure JSON."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_naive = cutoff.replace(tzinfo=None)
    items: list[dict] = []
    with session_scope() as s:
        for c in s.exec(
            select(TradingCall)
            .where(TradingCall.created_at >= cutoff_naive)
            .order_by(TradingCall.created_at.desc())
            .limit(25)
        ).all():
            items.append({
                "kind": "call", "id": c.id, "ticker": c.ticker,
                "ts": _aware_iso(c.created_at),
                "title": (c.thesis or "")[:200],
                "side": c.direction, "src": c.source,
                "conviction": c.conviction, "url": None,
            })
        for f in s.exec(
            select(Filing)
            .where(Filing.filed_at >= cutoff_naive)
            .order_by(Filing.filed_at.desc())
            .limit(25)
        ).all():
            items.append({
                "kind": "filing", "id": f.id, "ticker": f.ticker,
                "ts": _aware_iso(f.filed_at),
                "title": ((f.summary or f.form_type) or "")[:200],
                "form": f.form_type, "url": f.primary_doc_url,
                "materiality_score": f.materiality_score,
            })
        for n in s.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff_naive)
            .order_by(NewsItem.published_at.desc())
            .limit(25)
        ).all():
            items.append({
                "kind": "news", "id": n.id, "ticker": n.ticker,
                "ts": _aware_iso(n.published_at),
                "title": (n.title or "")[:200],
                "url": n.url, "src": n.source,
                "sentiment": n.sentiment,
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:40]
