"""Upcoming earnings reports for currently-held tickers.

Earnings prints are the single largest source of binary risk on a
small-cap swing book — an in-line beat-and-raise on Tuesday close
can mean a 12% gap, in either direction, that no stop loss will
help you with. The trader who knows what's about to print can
size down, hedge, or close — the trader who doesn't, can't.

Joins `EarningsDate` (populated by the catalyst ingest pipeline)
with the current open book to surface "you have these positions
reporting this week". Read-only; no new queries beyond the open
positions read and a single EarningsDate lookup.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlmodel import select

from .. import funds as _funds
from ..db import session_scope
from ..models import EarningsDate


def earnings_exposure(window_days: int = 30) -> dict:
    """For every open position, look up its next earnings date and
    return rows sorted by days_until_report. Tickers without a
    known date fall through to `unknown` (so the UI can nudge the
    trader to research them)."""
    rows = _funds.open_positions_all()
    if not rows:
        return {
            "window_days": window_days,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "upcoming": [],
            "this_week": 0,
            "this_month": 0,
            "unknown": [],
        }

    tickers = sorted({r["ticker"].upper() for r in rows})
    with session_scope() as s:
        dates = {
            ed.ticker.upper(): ed
            for ed in s.exec(
                select(EarningsDate).where(EarningsDate.ticker.in_(tickers))
            ).all()
        }

    today = date.today()
    upcoming: list[dict] = []
    unknown: list[dict] = []
    this_week = 0
    this_month = 0

    # Group positions by ticker so a fund holding the same name in
    # multiple wallets shows up as one earnings row with the combined
    # notional.
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"].upper(), []).append(r)

    for ticker, positions in by_ticker.items():
        ed = dates.get(ticker)
        notional = round(sum(p.get("notional", 0.0) for p in positions), 2)
        upnl = round(sum(p.get("upnl", 0.0) for p in positions), 2)
        funds_csv = ", ".join(sorted({p["fund"] for p in positions}))
        if ed is None:
            unknown.append({
                "ticker": ticker,
                "funds": funds_csv,
                "notional": notional,
                "upnl": upnl,
                "n_positions": len(positions),
            })
            continue
        days = (ed.report_date - today).days
        if days < 0:
            continue
        if days <= window_days:
            if days <= 7:
                this_week += 1
            if days <= 30:
                this_month += 1
            upcoming.append({
                "ticker": ticker,
                "report_date": ed.report_date.isoformat(),
                "days_until": days,
                "funds": funds_csv,
                "notional": notional,
                "upnl": upnl,
                "n_positions": len(positions),
                "fetched_at": (
                    ed.fetched_at.isoformat()
                    if isinstance(ed.fetched_at, datetime) else None
                ),
            })

    upcoming.sort(key=lambda r: r["days_until"])
    unknown.sort(key=lambda r: -r["notional"])

    return {
        "window_days": window_days,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "upcoming": upcoming,
        "this_week": this_week,
        "this_month": this_month,
        "unknown": unknown,
    }
