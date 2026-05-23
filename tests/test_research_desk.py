"""Research Desk — guardrail contracts.

These are NOT about the LLM's quality; that's measured by the scorecard
over time. These pin the *safety rails*:

- Conviction floor: `execute` refuses below the floor even if the user
  clicks (the wallet enforces discipline, not the user).
- Rate limit: 3 executions per UTC day, hard cap. Counts EXECUTIONS, not
  proposals — the user can ask freely.
- Verdict parsing: TRADE without a ticker / direction → degrades to PASS
  rather than executing junk.
- Validation: size_pct is clamped; bad tickers / directions invalidate.
- Idempotence: re-running the same prompt within the dedup window
  returns the cached task id (no double-spend on tokens).
- One-shot execute: a task can only be executed once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel import funds, research_desk
from sentinel.db import session_scope
from sentinel.models import (
    Fund,
    FundTrade,
    PriceContext,
    ResearchTask,
    Watchlist,
)


def _seed_research_wallet_and_priced_ticker(ticker: str = "NVDA",
                                            price: float = 250.0) -> None:
    funds.seed_funds()
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        if not s.exec(
            select(Watchlist).where(Watchlist.ticker == ticker)
        ).first():
            s.add(Watchlist(
                cik="x", ticker=ticker, source="manual",
                asset_class="equity", added_at=now,
            ))
        pc = s.get(PriceContext, ticker)
        if pc is None:
            s.add(PriceContext(
                ticker=ticker, last_price=price,
                change_1d_pct=0.0, change_5d_pct=0.0,
                volume_vs_20d_avg=1.0, last_updated=now,
            ))


def _seed_task(*, verdict="TRADE", conviction=4, ticker="NVDA",
               direction="long", size_pct=5.0,
               executed_at=None) -> int:
    """Plant a finished ResearchTask in the DB so we can call
    `execute()` against it without round-tripping the LLM."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        t = ResearchTask(
            prompt="seeded for test",
            created_at=now,
            dossier="seeded",
            dossier_at=now,
            verdict=verdict,
            rec_ticker=ticker,
            rec_direction=direction,
            rec_conviction=conviction,
            rec_size_pct=size_pct,
            rec_thesis="t",
            rec_risks="r",
            model="stub",
            executed_at=executed_at,
        )
        s.add(t)
        s.flush()
        return t.id


# ── _parse_verdict / _validate_recommendation ──────────────────────────────


def test_parse_verdict_tolerates_fence_and_leading_text():
    raw = '```json\nhere is the answer:\n{"verdict":"PASS","ticker":null}```'
    out = research_desk._parse_verdict(raw)
    assert out is not None and out["verdict"] == "PASS"


def test_parse_verdict_handles_trailing_garbage():
    raw = '{"verdict":"TRADE","ticker":"NVDA"}\n\nThanks for asking!'
    out = research_desk._parse_verdict(raw)
    assert out is not None and out["ticker"] == "NVDA"


def test_validate_trade_with_bad_ticker_demotes_to_pass():
    d = {"verdict": "TRADE", "ticker": "?!@#", "direction": "long",
         "conviction": 4, "size_pct": 5.0}
    out = research_desk._validate_recommendation(d)
    assert out["verdict"] == "PASS"
    assert "_error" in out


def test_validate_trade_with_bad_direction_demotes_to_pass():
    d = {"verdict": "TRADE", "ticker": "NVDA", "direction": "sideways",
         "conviction": 4, "size_pct": 5.0}
    out = research_desk._validate_recommendation(d)
    assert out["verdict"] == "PASS"


def test_validate_clamps_size_pct_to_envelope():
    d = {"verdict": "TRADE", "ticker": "NVDA", "direction": "long",
         "conviction": 4, "size_pct": 50.0}
    out = research_desk._validate_recommendation(d)
    assert out["size_pct"] == research_desk._MAX_SIZE_PCT


def test_validate_clears_action_fields_on_watchlist():
    d = {"verdict": "WATCHLIST", "ticker": "NVDA", "direction": "long",
         "conviction": 4, "size_pct": 5.0}
    out = research_desk._validate_recommendation(d)
    assert out["ticker"] is None and out["direction"] is None
    assert out["conviction"] is None and out["size_pct"] is None


# ── execute() guardrails ──────────────────────────────────────────────────


def test_execute_refuses_below_conviction_floor():
    _seed_research_wallet_and_priced_ticker()
    tid = _seed_task(conviction=research_desk._CONVICTION_FLOOR - 1)
    res = research_desk.execute(tid)
    assert not res["ok"]
    assert "floor" in res["message"].lower()
    # never wrote a trade
    with session_scope() as s:
        t = s.get(ResearchTask, tid)
        assert t.executed_at is None


def test_execute_refuses_when_verdict_not_trade():
    _seed_research_wallet_and_priced_ticker()
    tid = _seed_task(verdict="WATCHLIST", conviction=None,
                     ticker=None, direction=None, size_pct=None)
    res = research_desk.execute(tid)
    assert not res["ok"]
    assert "verdict" in res["message"].lower()


def test_execute_creates_fund_trade_on_research_wallet():
    _seed_research_wallet_and_priced_ticker(ticker="MSFT", price=400.0)
    tid = _seed_task(ticker="MSFT", conviction=4, size_pct=5.0)
    before_remaining = research_desk.executions_remaining_today()
    res = research_desk.execute(tid)
    assert res["ok"], res["message"]
    assert res["trade_id"] is not None
    # the FundTrade is on the research wallet
    with session_scope() as s:
        fund = s.exec(
            select(Fund).where(Fund.name == funds.RESEARCH_WALLET_NAME)
        ).first()
        trade = s.get(FundTrade, res["trade_id"])
        assert trade.fund_id == fund.id
        assert trade.status == "open"
        assert trade.ticker == "MSFT"
    # rate-limit counter ticked
    assert (research_desk.executions_remaining_today()
            == before_remaining - 1)


def test_execute_is_one_shot():
    _seed_research_wallet_and_priced_ticker(ticker="AAPL", price=180.0)
    tid = _seed_task(ticker="AAPL")
    res1 = research_desk.execute(tid)
    assert res1["ok"]
    res2 = research_desk.execute(tid)
    assert not res2["ok"]
    assert "already" in res2["message"].lower()


def test_execute_rate_limit_blocks_after_cap(monkeypatch):
    # Pretend we've already burned the daily budget.
    monkeypatch.setattr(
        research_desk, "_executions_today",
        lambda: research_desk._RATE_LIMIT_PER_DAY,
    )
    _seed_research_wallet_and_priced_ticker()
    tid = _seed_task()
    res = research_desk.execute(tid)
    assert not res["ok"]
    assert "daily cap" in res["message"].lower()


def test_execute_handles_missing_price_context():
    _seed_research_wallet_and_priced_ticker()
    # task references a ticker with no PriceContext row
    tid = _seed_task(ticker="UNKWN")
    res = research_desk.execute(tid)
    assert not res["ok"]
    assert "mark" in res["message"].lower()


# ── duplicate prompt dedup ────────────────────────────────────────────────


def test_recent_duplicate_finder_returns_cached_id():
    # Plant a task within the dedup window; `_find_recent_duplicate`
    # should find it (so `run_research` is idempotent).
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        t = ResearchTask(
            prompt="dedup-me",
            created_at=now,
            dossier="x", dossier_at=now,
            verdict="PASS", model="x",
        )
        s.add(t)
        s.flush()
        tid = t.id
    found = research_desk._find_recent_duplicate("dedup-me")
    assert found is not None and found.id == tid


def test_recent_duplicate_finder_ignores_stale_rows():
    long_ago = datetime.now(timezone.utc) - timedelta(days=2)
    long_ago_naive = long_ago.replace(tzinfo=None)
    with session_scope() as s:
        t = ResearchTask(
            prompt="stale-prompt",
            created_at=long_ago_naive,
            dossier="x", dossier_at=long_ago_naive,
            verdict="PASS", model="x",
        )
        s.add(t)
    assert research_desk._find_recent_duplicate("stale-prompt") is None


# ── seeding ──────────────────────────────────────────────────────────────


def test_seed_funds_creates_research_wallet_without_policy():
    funds.seed_funds()
    with session_scope() as s:
        wallet = s.exec(
            select(Fund).where(Fund.name == funds.RESEARCH_WALLET_NAME)
        ).first()
        assert wallet is not None
        assert wallet.starting_cash > 0
        assert wallet.cash > 0
    # And critically: it is NOT in `_POLICIES`, so the autonomous cycle
    # will skip it (verified by the `_run` guard `if pol is None: continue`).
    assert funds.RESEARCH_WALLET_NAME not in funds._POLICIES
