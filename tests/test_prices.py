"""Price-ingest integrity.

Two source-level guards, both load-bearing for every downstream P&L / mover:
- a bar with a non-positive or NaN close must NEVER enter PriceBar (Yahoo
  hands back all-zero rows for dead coins);
- price rows for a ticker no longer on the watchlist must not linger (they
  resurface as fabricated movers).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlmodel import select

from sentinel.db import session_scope
from sentinel.ingesters import prices
from sentinel.models import PriceBar, PriceContext, Watchlist

UTC = timezone.utc


def test_persist_bars_rejects_nonpositive_and_nan_close():
    idx = pd.to_datetime([
        "2026-05-18 14:30", "2026-05-18 14:31",
        "2026-05-18 14:32", "2026-05-18 14:33",
    ])
    df = pd.DataFrame(
        {
            "Open": [100.0, 0.0, 0.0, 100.0],
            "High": [101.0, 0.0, 0.0, 101.0],
            "Low": [99.0, 0.0, 0.0, 99.0],
            "Close": [100.5, 0.0, -3.0, float("nan")],  # only row 0 is valid
            "Volume": [1000, 5, 5, 1000],
        },
        index=idx,
    )
    inserted = prices._persist_bars("GUARD", df)
    assert inserted == 1
    with session_scope() as s:
        rows = s.exec(
            select(PriceBar).where(PriceBar.ticker == "GUARD")
        ).all()
    assert len(rows) == 1 and rows[0].close == 100.5


def test_persist_bars_is_idempotent_via_unique_constraint():
    """The bulk INSERT…ON CONFLICT DO NOTHING must dedup on (ticker, ts) —
    re-persisting the same window inserts nothing, no duplicate rows. This is
    what lets the lock be held for one fast statement instead of N round-trips.
    """
    idx = pd.to_datetime(["2026-05-18 14:30", "2026-05-18 14:31"])
    df = pd.DataFrame(
        {
            "Open": [1.0, 1.0], "High": [1.0, 1.0], "Low": [1.0, 1.0],
            "Close": [10.0, 11.0], "Volume": [5, 5],
        },
        index=idx,
    )
    n1 = prices._persist_bars("DUP", df)
    prices._persist_bars("DUP", df)  # same (ticker, ts) → all conflict
    with session_scope() as s:
        rows = s.exec(
            select(PriceBar).where(PriceBar.ticker == "DUP")
        ).all()
    assert n1 == 2
    assert len(rows) == 2  # second call added zero dupes — constraint held


def test_purge_orphans_drops_untracked_price_rows_only():
    now = datetime.now(UTC)
    with session_scope() as s:
        s.add(Watchlist(
            cik="0000000001", ticker="KEEP", source="index",
            asset_class="equity", added_at=now,
        ))
        for tk in ("KEEP", "GONE"):
            s.add(PriceContext(
                ticker=tk, last_price=10.0, change_1d_pct=0.0,
                change_5d_pct=0.0, volume_vs_20d_avg=1.0, last_updated=now,
            ))
            s.add(PriceBar(
                ticker=tk, ts=now, open=10, high=10, low=10, close=10,
                volume=1,
            ))

    prices._purge_orphans()

    with session_scope() as s:
        pcs = {p.ticker for p in s.exec(select(PriceContext)).all()}
        bars = {b.ticker for b in s.exec(select(PriceBar)).all()}
    assert pcs == {"KEEP"}          # untracked GONE removed
    assert bars == {"KEEP"}


def test_purge_orphans_noop_when_watchlist_empty():
    """Safety: an empty watchlist must NOT nuke every price row (that would
    happen if the guard were missing and is catastrophic)."""
    now = datetime.now(UTC)
    with session_scope() as s:
        s.add(PriceContext(
            ticker="SAFE", last_price=1.0, change_1d_pct=0.0,
            change_5d_pct=0.0, volume_vs_20d_avg=1.0, last_updated=now,
        ))
    prices._purge_orphans()
    with session_scope() as s:
        assert s.exec(select(PriceContext)).all()  # untouched
