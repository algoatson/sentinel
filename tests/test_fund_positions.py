"""`fund_positions` — the structured snapshot the cockpit drill-in renders.

What's pinned: returns None for unknown / not-yet-seeded; computes uPnL with
the correct sign per side; survives a missing PriceContext (falls back to
entry, marks the position non-live).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel import funds
from sentinel.db import session_scope
from sentinel.models import Fund, FundTrade, PriceContext

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _seed_fund(name="degen", *, cash=1000.0, starting=1000.0):
    with session_scope() as s:
        f = Fund(
            name=name, mandate="🦍 Degen — test", starting_cash=starting,
            cash=cash, last_call_id=0, created_at=_now(),
        )
        s.add(f)
        s.flush()
        return f.id


def _open(fid, *, ticker, side, qty, entry, days_ago=1):
    with session_scope() as s:
        s.add(FundTrade(
            fund_id=fid, ticker=ticker, side=side, qty=qty,
            entry_price=entry,
            entry_at=_now() - timedelta(days=days_ago),
            status="open",
        ))


def _mark_price(ticker, price, *, ago_min=2):
    with session_scope() as s:
        s.add(PriceContext(
            ticker=ticker, last_price=price,
            change_1d_pct=0.0, change_5d_pct=0.0,
            volume_vs_20d_avg=1.0,
            last_updated=_now() - timedelta(minutes=ago_min),
        ))


def test_unknown_or_unseeded_returns_none():
    assert funds.fund_positions("does-not-exist") is None
    assert funds.fund_positions("") is None
    # known policy name but fund row not seeded yet → still None (clean
    # empty state for the dashboard, no fake zeroes)
    assert funds.fund_positions("degen") is None


def test_positions_carry_per_side_upnl_and_live_marks():
    fid = _seed_fund("degen")
    # long: bought 10 @ $50 → marked at $60 → +$100 unrealized (+20%)
    _open(fid, ticker="AAA", side="long", qty=10, entry=50.0)
    _mark_price("AAA", 60.0)
    # short: shorted 5 @ $80 → marked at $70 → +$50 unrealized (+12.5%)
    _open(fid, ticker="BBB", side="short", qty=5, entry=80.0)
    _mark_price("BBB", 70.0)
    # position with NO mark → falls back to entry → 0 uPnL, not live
    _open(fid, ticker="CCC", side="long", qty=2, entry=100.0)

    d = funds.fund_positions("degen")
    assert d is not None
    assert d["name"] == "degen"
    pos = {p["ticker"]: p for p in d["positions"]}

    assert pos["AAA"]["upnl"] == 100.0
    assert pos["AAA"]["upnl_pct"] == 20.0
    assert pos["AAA"]["mark_live"] is True

    assert pos["BBB"]["upnl"] == 50.0
    assert pos["BBB"]["upnl_pct"] == 12.5
    assert pos["BBB"]["side"] == "short"

    assert pos["CCC"]["upnl"] == 0.0
    assert pos["CCC"]["mark"] == 100.0  # fell back to entry
    assert pos["CCC"]["mark_live"] is False

    # aggregate uPnL is the sum and never -0.0 on a flat short
    assert d["open_upnl"] == 150.0
