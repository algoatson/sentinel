"""`portfolio.add_hold` / `remove_hold` / `list_holds` — single chokepoint
shared by Discord !hold/!unhold/!holdings and the dashboard Holds panel.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel import portfolio
from sentinel.db import session_scope
from sentinel.models import PriceContext

UTC = timezone.utc


def _seed_pc(ticker, price=10.0):
    with session_scope() as s:
        s.add(PriceContext(
            ticker=ticker, last_price=price,
            change_1d_pct=0.012, change_5d_pct=-0.04,
            volume_vs_20d_avg=1.0,
            last_updated=datetime.now(UTC) - timedelta(minutes=1),
        ))


def test_add_normalises_ticker_and_returns_created_true():
    r = portfolio.add_hold("$nvda")
    assert r["ok"] and r["ticker"] == "NVDA"
    assert r["created"] is True
    assert r["qty"] is None
    assert "added $NVDA" in r["message"]


def test_add_is_idempotent_and_updates_qty():
    a = portfolio.add_hold("AAA", 3)
    assert a["ok"] and a["created"] is True and a["qty"] == 3.0
    b = portfolio.add_hold("AAA", 5)
    assert b["ok"] and b["created"] is False
    assert b["qty"] == 5.0


def test_reject_empty_or_bad_qty():
    assert not portfolio.add_hold("")["ok"]
    assert not portfolio.add_hold("XYZ", "lots")["ok"]
    assert not portfolio.add_hold("XYZ", -1)["ok"]


def test_remove_happy_and_unknown():
    portfolio.add_hold("BBB")
    ok = portfolio.remove_hold("BBB")
    assert ok["ok"] and "removed $BBB" in ok["message"]
    again = portfolio.remove_hold("BBB")
    assert not again["ok"]


def test_list_carries_price_context_when_available():
    _seed_pc("CCC", 12.34)
    portfolio.add_hold("CCC", 2)
    portfolio.add_hold("NOPRICE")  # no PC seeded
    rows = {r["ticker"]: r for r in portfolio.list_holds()}
    assert rows["CCC"]["price"] == 12.34
    assert round(rows["CCC"]["change_1d_pct"], 2) == 1.2  # 0.012 → 1.2%
    assert rows["NOPRICE"]["price"] is None
    assert rows["NOPRICE"]["change_1d_pct"] is None
