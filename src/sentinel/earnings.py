"""Earnings-date cache — one restart-safe source of "when does X report".

The catalyst pipeline already fetches upcoming earnings (yfinance) for the
relevant universe daily. This persists what it computes so the *decision*
paths can read it cheaply, off-network, and consistently:

  - funds: don't OPEN a fresh position into an imminent print (binary risk).
  - synthesis: the brain forms its read knowing a name reports in N days.

Readers reject rows older than `_STALE_DAYS` — a stale cache must read as
"unknown" (no blackout, no false awareness), never as fact. yfinance shifts
estimated dates, and the catalyst job refreshes daily, so this stays current.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlmodel import select

from .db import session_scope
from .models import EarningsDate

_STALE_DAYS = 8  # a cache entry older than this is treated as unknown


def upsert_earnings(rows: list[dict]) -> int:
    """Persist [{'ticker': str, 'date': 'YYYY-MM-DD'}]. Idempotent upsert
    (one row per ticker). Returns the number written. Best-effort: a single
    bad row is skipped, never fatal."""
    now = datetime.now(timezone.utc)
    n = 0
    with session_scope() as s:
        for r in rows:
            try:
                ticker = (r.get("ticker") or "").strip().upper()
                rd = r.get("date")
                rd = rd if isinstance(rd, date) else date.fromisoformat(str(rd))
            except (ValueError, TypeError):
                continue
            if not ticker:
                continue
            row = s.get(EarningsDate, ticker)
            if row is None:
                s.add(EarningsDate(ticker=ticker, report_date=rd, fetched_at=now))
            else:
                # When the report_date is rolling forward (the print
                # just happened and the catalyst pipeline now sees the
                # NEXT quarter's date), preserve the prior date so the
                # post-earnings entry blackout still has something to
                # check. Only move it when we're actually rolling
                # *past* the old date — not when the upstream simply
                # republishes the same date or pulls in an earlier
                # one (rare, but happens on guidance changes).
                today_d = date.today()
                if row.report_date < today_d and rd > row.report_date:
                    row.last_report_date = row.report_date
                row.report_date = rd
                row.fetched_at = now
                s.add(row)
            n += 1
    if n:
        logger.info("earnings: cached {} report dates", n)
    return n


def next_earnings(ticker: str) -> date | None:
    """Next report date for `ticker`, or None if unknown or the cache entry
    is stale. Never raises — a missing/old row simply reads as 'unknown'."""
    if not ticker:
        return None
    try:
        with session_scope() as s:
            row = s.get(EarningsDate, ticker.upper())
            if row is None:
                return None
            fetched = row.fetched_at
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched > timedelta(days=_STALE_DAYS):
                return None  # stale → unknown, not fact
            return row.report_date
    except Exception as e:
        logger.debug("next_earnings({}) failed: {}", ticker, e)
        return None


def upcoming(days: int = 14) -> list[dict]:
    """All cached earnings within the next `days` days, oldest-first.

    Stale rows (older than `_STALE_DAYS` since last fetch) are excluded —
    same conservatism as `next_earnings`: better to omit than mis-report
    a date the upstream source has since shifted. Returns:
    ``[{"ticker": str, "report_date": "YYYY-MM-DD", "days_out": int}, ...]``.
    """
    today_d = date.today()
    horizon = today_d + timedelta(days=days)
    out: list[dict] = []
    try:
        with session_scope() as s:
            rows = s.exec(
                select(EarningsDate)
                .where(EarningsDate.report_date >= today_d)
                .where(EarningsDate.report_date <= horizon)
                .order_by(EarningsDate.report_date)
            ).all()
            now = datetime.now(timezone.utc)
            for r in rows:
                fetched = r.fetched_at
                if fetched.tzinfo is None:
                    fetched = fetched.replace(tzinfo=timezone.utc)
                if now - fetched > timedelta(days=_STALE_DAYS):
                    continue
                out.append({
                    "ticker": r.ticker,
                    "report_date": r.report_date.isoformat(),
                    "days_out": (r.report_date - today_d).days,
                })
    except Exception as e:
        logger.debug("earnings.upcoming({}) failed: {}", days, e)
    return out


def days_until_earnings(ticker: str, today: date | None = None) -> int | None:
    """Whole days until the next report (>=0), or None if unknown/stale/past."""
    rd = next_earnings(ticker)
    if rd is None:
        return None
    delta = (rd - (today or date.today())).days
    return delta if delta >= 0 else None


def days_since_last_earnings(
    ticker: str, today: date | None = None,
) -> int | None:
    """Days since the immediately-prior earnings print (>=0), or None
    if we have no prior on file.

    Powers the funds._post_earnings_blackout — the post-print
    gap/drift volatility cool-down that the engine uses to skip
    entries for N days after a report. Without this helper the
    blackout was structurally dead (the next-quarter upsert wipes
    the past date and days_until_earnings filters out negatives).
    """
    if not ticker:
        return None
    try:
        with session_scope() as s:
            row = s.get(EarningsDate, ticker.upper())
            if row is None or row.last_report_date is None:
                return None
            delta = ((today or date.today()) - row.last_report_date).days
            return delta if delta >= 0 else None
    except Exception as e:
        logger.debug("days_since_last_earnings({}) failed: {}", ticker, e)
        return None
