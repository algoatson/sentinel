"""`portfolio.open_paper_position` — the single chokepoint for opening a
paper position, shared by Discord !buy/!short and the dashboard form. The
contract is pinned here so behaviour can't drift between surfaces.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel import portfolio
from sentinel.db import session_scope
from sentinel.models import PaperTrade, PriceContext


UTC = timezone.utc


def _seed_price(ticker: str, price: float, *, ago_min: int = 1) -> None:
    with session_scope() as s:
        s.add(PriceContext(
            ticker=ticker, last_price=price,
            change_1d_pct=0.0, change_5d_pct=0.0,
            volume_vs_20d_avg=1.0,
            last_updated=datetime.now(UTC) - timedelta(minutes=ago_min),
        ))


def _open_trades_count(ticker: str) -> int:
    with session_scope() as s:
        return len(s.exec(
            select(PaperTrade).where(PaperTrade.ticker == ticker)
            .where(PaperTrade.status == "open")
        ).all())


def test_happy_long_with_explicit_price():
    r = portfolio.open_paper_position("nvda", "long", 10, price=120.5)
    assert r["ok"]
    assert r["ticker"] == "NVDA" and r["side"] == "long"
    assert r["qty"] == 10 and r["price"] == 120.5
    assert "long 10 $NVDA @ 120.5" in r["message"]
    assert _open_trades_count("NVDA") == 1


def test_happy_short_falls_back_to_price_context():
    _seed_price("AAA", 50.0)
    r = portfolio.open_paper_position("AAA", "short", 5)
    assert r["ok"]
    assert r["price"] == 50.0
    assert r["side"] == "short"


def test_reject_when_already_holding():
    _seed_price("BBB", 10.0)
    assert portfolio.open_paper_position("BBB", "long", 2)["ok"]
    r = portfolio.open_paper_position("BBB", "long", 2)
    assert not r["ok"]
    assert "already holding" in r["message"]


def test_reject_bad_side():
    r = portfolio.open_paper_position("CCC", "yolo", 1, price=10)
    assert not r["ok"] and "unknown side" in r["message"]


def test_reject_empty_ticker_or_nonpositive_qty():
    assert not portfolio.open_paper_position(
        "", "long", 1, price=10
    )["ok"]
    assert not portfolio.open_paper_position(
        "DDD", "long", 0, price=10
    )["ok"]
    assert not portfolio.open_paper_position(
        "DDD", "long", -1, price=10
    )["ok"]


def test_reject_when_no_price_available():
    # no PriceContext seeded, no price param → can't infer a mark, refuse
    r = portfolio.open_paper_position("EEE", "long", 1)
    assert not r["ok"]
    assert "no usable price" in r["message"]
