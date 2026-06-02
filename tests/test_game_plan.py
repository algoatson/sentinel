"""Deterministic Morning Game Plan assembler (`game_plan.build_inputs`).

Pins the contract that matters: the bundle is built from real numbers via the
existing accessors, surfaces the at-risk position + earnings exposure, ranks
the higher conviction×edge idea first, dedupes ideas against the open book, and
exposes recent narrative keys so the LLM stage can dedupe against them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlmodel import select

from sentinel import funds, narrative
from sentinel.db import session_scope
from sentinel.models import EarningsDate, Fund, FundTrade, TradingCall
from sentinel.pipelines import game_plan

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _seed_book():
    """A live fund position in NVDA sitting 1% above its stop, with NVDA
    reporting in 2 days."""
    funds.seed_funds()
    with session_scope() as s:
        f = s.exec(select(Fund).where(Fund.name == "degen")).first()
        s.add(FundTrade(
            fund_id=f.id, ticker="NVDA", side="long", qty=10,
            entry_price=100.0, stop_price=99.0, entry_at=_now(), status="open",
        ))
        s.add(EarningsDate(
            ticker="NVDA", report_date=date.today() + timedelta(days=2), fetched_at=_now(),
        ))


def _seed_calls():
    """Two fresh, grounded, NOT-held ideas of differing conviction/source."""
    with session_scope() as s:
        s.add(TradingCall(
            ticker="AAPL", direction="long", conviction=5, source="synthesis",
            thesis="strong setup", price_at_call=200.0, created_at=_now(),
        ))
        s.add(TradingCall(
            ticker="AMD", direction="long", conviction=2, source="why_moved",
            thesis="weak setup", price_at_call=150.0, created_at=_now(),
        ))


def test_build_inputs_never_raises_on_empty_db():
    b = game_plan.build_inputs()
    assert set(b) >= {"et_date", "book_risk", "maturing", "catalysts", "fresh_ideas", "prior"}
    assert b["fresh_ideas"] == []
    assert b["book_risk"]["near_stop"] == []


def test_surfaces_at_risk_position_and_earnings():
    _seed_book()
    b = game_plan.build_inputs()
    near = b["book_risk"]["near_stop"]
    assert any(r["ticker"] == "NVDA" for r in near), "at-risk NVDA should surface near_stop"
    assert all(r.get("trigger") == "near_stop" for r in near)
    soon = b["book_risk"]["earnings_soon"]
    assert any(e["ticker"] == "NVDA" and e["days_until"] == 2 for e in soon)
    assert "NVDA" in b["held_tickers"]


def test_fresh_ideas_ranked_by_conviction_times_edge_and_deduped():
    _seed_book()   # NVDA held
    _seed_calls()  # AAPL conv5, AMD conv2
    b = game_plan.build_inputs()
    ideas = b["fresh_ideas"]
    tickers = [i["ticker"] for i in ideas]
    # higher conviction × edge first (edge mult is 1.0 with no closed history)
    assert tickers[0] == "AAPL"
    assert "AMD" in tickers
    # deduped vs the open book — NVDA is held, must not appear as a fresh idea
    assert "NVDA" not in tickers
    assert ideas[0]["score"] >= ideas[1]["score"]


def test_prior_flags_already_narrated_idea():
    _seed_calls()
    narrative.record_event("AAPL", "call", "synthesis went long AAPL")
    b = game_plan.build_inputs()
    keys = b["prior"]["recent_narrative"]
    assert any(k.startswith("AAPL:") for k in keys), "narrated AAPL should appear for dedup"


def test_contradicted_call_excluded_from_ideas():
    with session_scope() as s:
        s.add(TradingCall(
            ticker="TSLA", direction="long", conviction=5, source="synthesis",
            thesis="bad number", price_at_call=250.0, created_at=_now(), grounded=False,
        ))
    b = game_plan.build_inputs()
    assert all(i["ticker"] != "TSLA" for i in b["fresh_ideas"])
