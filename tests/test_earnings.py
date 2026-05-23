"""Earnings-date cache contract.

This is the one source funds (entry blackout) and synthesis (binary-risk
awareness) read so neither trades nor reasons blind into a print. The
load-bearing rule: a stale or missing entry must read as *unknown*, never
as fact — pinned here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlmodel import select

from sentinel import earnings
from sentinel.db import session_scope
from sentinel.models import EarningsDate, Holding

UTC = timezone.utc


def test_absent_reads_as_unknown():
    assert earnings.next_earnings("NOPE") is None
    assert earnings.next_earnings("") is None
    assert earnings.days_until_earnings("NOPE") is None


def test_upsert_inserts_then_updates_idempotently():
    earnings.upsert_earnings([{"ticker": "abc", "date": "2026-06-01"}])
    assert earnings.next_earnings("ABC") == date(2026, 6, 1)  # case-normalised

    earnings.upsert_earnings([{"ticker": "ABC", "date": "2026-06-15"}])
    assert earnings.next_earnings("ABC") == date(2026, 6, 15)  # updated

    with session_scope() as s:
        rows = s.exec(select(EarningsDate)).all()
    assert len(rows) == 1  # upsert, not duplicate

    # malformed rows are skipped, never fatal
    assert earnings.upsert_earnings([{"ticker": "X"}, {"bad": 1}]) == 0


def test_stale_entry_reads_as_unknown_not_fact():
    with session_scope() as s:
        s.add(EarningsDate(
            ticker="OLD", report_date=date(2030, 1, 1),
            fetched_at=datetime.now(UTC) - timedelta(days=30),
        ))
    assert earnings.next_earnings("OLD") is None
    assert earnings.days_until_earnings("OLD") is None


def test_days_until_earnings_math_and_past_is_none():
    today = date(2026, 5, 18)
    with session_scope() as s:
        s.add(EarningsDate(ticker="SOON",
              report_date=today + timedelta(days=3), fetched_at=datetime.now(UTC)))
        s.add(EarningsDate(ticker="PAST",
              report_date=today - timedelta(days=2), fetched_at=datetime.now(UTC)))
    assert earnings.days_until_earnings("SOON", today) == 3
    assert earnings.days_until_earnings("PAST", today) is None  # already past


def test_synthesis_snapshot_surfaces_earnings_window():
    """The brain must actually receive the dates (focus-name in window)."""
    from sentinel.pipelines import synthesis

    today = date.today()
    with session_scope() as s:
        s.add(Holding(ticker="ERN", quantity=1, added_at=datetime.now(UTC)))
        s.add(EarningsDate(ticker="ERN",
              report_date=today + timedelta(days=3), fetched_at=datetime.now(UTC)))
    snap = synthesis._build_snapshot()
    ew = {r["ticker"]: r for r in snap["earnings_window"]}
    assert "ERN" in ew and ew["ERN"]["in_days"] == 3
