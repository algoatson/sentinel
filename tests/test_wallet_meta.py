"""wallet_meta contract — the edge-readout instrument.

It decides whether the bot thinks its own signal is real, so a silent math
error here would lie about exactly the thing the whole system exists to
measure. It must be pure arithmetic and must REFUSE to call an edge below
the sample floor. Pinned here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel import funds
from sentinel.db import session_scope
from sentinel.models import Fund, FundEquity, FundTrade, TradingCall

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _fund(name, *, starting=1000.0, cash=1000.0):
    with session_scope() as s:
        f = Fund(name=name, mandate="m", starting_cash=starting, cash=cash,
                 last_call_id=0, created_at=_now())
        s.add(f)
        s.flush()
        return f.id


def _closed(fid, *, pnl, reason="take", hold=2, call_id=None, ticker="ZZ"):
    with session_scope() as s:
        s.add(FundTrade(
            fund_id=fid, ticker=ticker, side="long", qty=1, entry_price=10,
            entry_at=_now() - timedelta(days=hold), status="closed",
            exit_price=10, exit_at=_now(), realized_pnl=pnl,
            close_reason=reason, call_id=call_id,
        ))


# ── pure helpers ────────────────────────────────────────────────────────────


def test_drawdown_math():
    assert funds._drawdown([]) == 0.0
    assert funds._drawdown([100, 110, 120]) == 0.0          # only up
    assert funds._drawdown([100, 120, 90, 110]) == -25.0    # 120→90
    assert funds._conv_bucket(1) == "low"
    assert funds._conv_bucket(3) == "med"
    assert funds._conv_bucket(5) == "high"


def test_unseeded_is_safe_and_silent():
    m = funds.wallet_meta()
    assert m["funds"] == []
    assert "No wallets seeded" in funds.meta_text(m)
    assert funds._meta_embed() is None
    assert funds.wallet_edge_brief() == "wallets not started"


# ── per-fund stats ──────────────────────────────────────────────────────────


def test_per_fund_stats_math():
    fid = _fund("t")
    _closed(fid, pnl=100.0, reason="take", hold=3, ticker="AAA")
    _closed(fid, pnl=-40.0, reason="stop", hold=1, ticker="BBB")
    _closed(fid, pnl=60.0, reason="take", hold=5, ticker="CCC")
    with session_scope() as s:
        for e in (1000.0, 1100.0, 900.0):  # peak 1100 → trough 900
            s.add(FundEquity(fund_id=fid, ts=_now(), equity=e))

    f = next(x for x in funds.wallet_meta()["funds"] if x["name"] == "t")
    assert f["n_closed"] == 3 and f["n_open"] == 0
    assert f["win_rate"] == 66.7                       # 2/3
    assert f["expectancy"] == 40.0                     # (100-40+60)/3
    assert f["profit_factor"] == 4.0                   # 160 / 40
    assert f["avg_hold_days"] == 3.0                   # (3+1+5)/3
    assert f["pnl_by_reason"] == {"take": 160.0, "stop": -40.0}
    assert f["max_drawdown_pct"] == round((900 - 1100) / 1100 * 100, 2)


def test_profit_factor_none_without_losses():
    fid = _fund("allwin")
    _closed(fid, pnl=10.0)
    _closed(fid, pnl=5.0)
    f = next(x for x in funds.wallet_meta()["funds"] if x["name"] == "allwin")
    assert f["profit_factor"] is None and f["win_rate"] == 100.0


# ── cross-cuts ──────────────────────────────────────────────────────────────


def test_cross_cut_by_source_conviction_asset():
    fid = _fund("x")
    with session_scope() as s:
        s.add(TradingCall(id=1, ticker="AAA", direction="long", conviction=4,
                           source="why_moved", thesis="t", created_at=_now()))
        s.add(TradingCall(id=2, ticker="BBB", direction="long", conviction=2,
                           source="convergence", thesis="t", created_at=_now()))
    _closed(fid, pnl=100.0, call_id=1, ticker="AAA")
    _closed(fid, pnl=-40.0, call_id=2, ticker="BBB")
    _closed(fid, pnl=60.0, call_id=None, ticker="CCC")   # unlinked → "?"

    m = funds.wallet_meta()
    assert m["by_source"]["why_moved"] == {"n": 1, "pnl": 100.0, "wins": 1}
    assert m["by_source"]["convergence"] == {"n": 1, "pnl": -40.0, "wins": 0}
    assert m["by_source"]["?"] == {"n": 1, "pnl": 60.0, "wins": 1}
    assert m["by_conviction"]["high"]["pnl"] == 100.0   # conv 4
    assert m["by_conviction"]["low"]["pnl"] == -40.0    # conv 2
    # asset bucket key depends on Watchlist (untracked → "?"); the contract
    # is that the split SUMS correctly regardless of how it's keyed.
    tot = {"n": 0, "pnl": 0.0, "wins": 0}
    for v in m["by_asset"].values():
        tot["n"] += v["n"]
        tot["pnl"] += v["pnl"]
        tot["wins"] += v["wins"]
    assert tot == {"n": 3, "pnl": 120.0, "wins": 2}


# ── the headline experiment + sample gating ─────────────────────────────────


def test_trend_filter_verdict_is_sample_gated():
    # leaders +5%, degen +2% → spread +3, but only 4 closed trades → too early
    _fund("leaders", cash=1050.0)
    dg = _fund("degen", cash=1020.0)
    for _ in range(4):
        _closed(dg, pnl=1.0)
    exp = funds.wallet_meta()["experiments"]["trend"]
    assert "too early" in exp["verdict"] and exp["spread"] == 3.0


def test_trend_filter_edge_real_vs_costly_once_enough_samples():
    _fund("leaders", cash=1050.0)           # +5% (a)
    dg = _fund("degen", cash=1020.0)         # +2% (b)
    for _ in range(16):                      # ≥ _MIN_EDGE_SAMPLE combined
        _closed(dg, pnl=1.0)
    exp = funds.wallet_meta()["experiments"]["trend"]
    assert exp["spread"] == 3.0 and "adds edge" in exp["verdict"]

    # flip it: raw degen now beats leaders → the trend filter is costing us
    with session_scope() as s:
        lead = s.exec(select(Fund).where(Fund.name == "leaders")).first()
        d = s.exec(select(Fund).where(Fund.name == "degen")).first()
        lead.cash, d.cash = 1010.0, 1080.0
        s.add(lead)
        s.add(d)
    exp = funds.wallet_meta()["experiments"]["trend"]
    assert "costs" in exp["verdict"] and exp["spread"] < 0


def test_wallet_edge_brief_is_compact_string():
    _fund("leaders", cash=1100.0)
    _fund("degen", cash=1000.0)
    b = funds.wallet_edge_brief()
    assert isinstance(b, str) and ";" in b and "trend" in b.lower()
