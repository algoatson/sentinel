"""Money-math coverage: the load-bearing deterministic logic behind funds
P&L, the scorecard, paper-portfolio P&L, and TradingView symbols.

These have no LLM and no network — pure arithmetic that decides displayed
P&L and the bot's own track record, so a silent sign error here would
quietly corrupt every fund standing and the calibration synthesis trusts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel import chat, funds, portfolio, scorecard, tradingview
from sentinel.config import settings
from sentinel.db import session_scope
from sentinel.models import (
    EarningsDate,
    Fund,
    FundEquity,
    FundTrade,
    Holding,
    PaperTrade,
    PriceBar,
    PriceContext,
    RedditMention,
    TradingCall,
)

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _pc(ticker: str, price: float) -> PriceContext:
    return PriceContext(
        ticker=ticker,
        last_price=price,
        change_1d_pct=0.0,
        change_5d_pct=0.0,
        volume_vs_20d_avg=1.0,
        last_updated=_now(),
    )


# ─────────────────────────── tradingview ────────────────────────────────────


def test_tv_symbol_equity_passthrough():
    assert tradingview.tv_symbol("aapl", "equity") == "AAPL"
    assert tradingview.tv_symbol("BRK.B", "equity") == "BRK.B"


def test_tv_symbol_crypto_futures_index():
    assert tradingview.tv_symbol("BTC-USD", "crypto") == "CRYPTO:BTCUSD"
    assert tradingview.tv_symbol("PEPE-USD") == "CRYPTO:PEPEUSD"  # suffix-detected
    assert tradingview.tv_symbol("ES=F") == "ES1!"
    assert tradingview.tv_symbol("^TNX") == "TVC:TNX"
    assert tradingview.tv_symbol("") == ""


def test_chart_url_encodes_symbol():
    url = tradingview.chart_url("BTC-USD", "crypto")
    assert url.startswith("https://www.tradingview.com/chart/?symbol=")
    assert "CRYPTO%3ABTCUSD" in url  # ':' url-encoded


def test_watchlist_export_dedupes_keeps_order_skips_empty():
    out = tradingview.watchlist_export(["AAPL", "BTC-USD", "AAPL", "", "ES=F"])
    assert out.splitlines() == ["AAPL", "CRYPTO:BTCUSD", "ES1!"]


# ─────────────────────────── portfolio ──────────────────────────────────────


def test_position_pnl_long_short_directions():
    long = PaperTrade(
        ticker="X", side="long", qty=10, entry_price=100, entry_at=_now()
    )
    short = PaperTrade(
        ticker="Y", side="short", qty=10, entry_price=100, entry_at=_now()
    )
    # Long: profit when mark rises.
    assert portfolio.position_pnl(long, 110) == 100
    assert portfolio.position_pnl(long, 90) == -100
    # Short: profit when mark FALLS — the sign that must never flip.
    assert portfolio.position_pnl(short, 90) == 100
    assert portfolio.position_pnl(short, 110) == -100
    # No mark on an open position → unknown, not zero.
    assert portfolio.position_pnl(long, None) is None


def test_position_pnl_closed_uses_realized():
    p = PaperTrade(
        ticker="X", side="long", qty=10, entry_price=100, entry_at=_now(),
        status="closed", realized_pnl=42.0,
    )
    # Even given a bogus mark, a closed trade reports its locked realized P&L.
    assert portfolio.position_pnl(p, 999) == 42.0


def test_close_position_realizes_and_marks_closed():
    with session_scope() as s:
        s.add(PaperTrade(
            ticker="NVDA", side="long", qty=5, entry_price=100, entry_at=_now()
        ))
    closed = portfolio.close_position("NVDA", mark=120)
    assert closed is not None
    assert closed.status == "closed"
    assert closed.exit_price == 120
    assert closed.realized_pnl == 100  # (120-100)*5
    assert portfolio.close_position("NVDA", mark=120) is None  # nothing open now


def test_open_positions_and_realized_summary():
    with session_scope() as s:
        s.add(_pc("AAA", 110))
        s.add(PaperTrade(
            ticker="AAA", side="long", qty=10, entry_price=100, entry_at=_now()
        ))
        s.add(PaperTrade(
            ticker="BBB", side="short", qty=2, entry_price=50, entry_at=_now(),
            status="closed", realized_pnl=-20.0,
        ))
    pos = portfolio.open_positions()
    assert len(pos) == 1
    row = pos[0]
    assert row["ticker"] == "AAA"
    assert row["pnl"] == 100  # (110-100)*10
    assert row["pnl_pct"] == 10.0  # 100 / (100*10) * 100
    summ = portfolio.realized_summary()
    assert summ == {"closed": 1, "wins": 0, "realized_pnl": -20.0}


def test_held_tickers_union_holdings_and_open_trades():
    with session_scope() as s:
        s.add(Holding(ticker="HOLD", added_at=_now()))
        s.add(PaperTrade(
            ticker="OPEN", side="long", qty=1, entry_price=1, entry_at=_now()
        ))
        s.add(PaperTrade(
            ticker="GONE", side="long", qty=1, entry_price=1, entry_at=_now(),
            status="closed", realized_pnl=0.0,
        ))
    held = portfolio.held_tickers()
    assert held == {"HOLD", "OPEN"}
    assert portfolio.is_held("OPEN") and not portfolio.is_held("GONE")


# ─────────────────────────── scorecard ──────────────────────────────────────


def test_hit_direction_logic():
    assert scorecard._hit("long", 1.0) and not scorecard._hit("long", -1.0)
    assert scorecard._hit("short", -1.0) and not scorecard._hit("short", 1.0)
    assert not scorecard._hit("long", 0.0)  # flat is not a hit


def test_record_call_dedupes_unsettled_same_signal():
    with session_scope() as s:
        s.add(_pc("ABC", 50))
    scorecard.record_call("abc", "long", "convergence", "thesis", conviction=4)
    scorecard.record_call("abc", "long", "convergence", "again", conviction=2)
    with session_scope() as s:
        rows = s.exec(select(TradingCall).where(TradingCall.ticker == "ABC")).all()
    assert len(rows) == 1
    assert rows[0].price_at_call == 50  # mark captured at call time
    assert rows[0].conviction == 4  # first one wins, not overwritten


def test_record_call_rejects_bad_input():
    scorecard.record_call("", "long", "src", "t")
    scorecard.record_call("X", "sideways", "src", "t")
    with session_scope() as s:
        assert s.exec(select(TradingCall)).all() == []


def test_track_record_summary_buckets_and_sources():
    now = _now()
    with session_scope() as s:
        # 2 convergence longs: one hit (5d +3%), one miss (5d -2%), high conv.
        s.add(TradingCall(
            ticker="A", direction="long", conviction=5, source="convergence",
            thesis="t", price_at_call=10, created_at=now, ret_5d_pct=3.0,
        ))
        s.add(TradingCall(
            ticker="B", direction="long", conviction=5, source="convergence",
            thesis="t", price_at_call=10, created_at=now, ret_5d_pct=-2.0,
        ))
        # low-conv short, hit (5d -1%).
        s.add(TradingCall(
            ticker="C", direction="short", conviction=1, source="why_moved",
            thesis="t", price_at_call=10, created_at=now, ret_5d_pct=-1.0,
        ))
        # unmarked — must be ignored (no ret).
        s.add(TradingCall(
            ticker="D", direction="long", conviction=3, source="synthesis",
            thesis="t", price_at_call=10, created_at=now,
        ))
    tr = scorecard.track_record_summary()
    assert tr["overall"] == {"hits": 2, "n": 3}
    assert tr["by_source"]["convergence"] == {"hits": 1, "n": 2}
    assert tr["by_conviction"]["high"] == {"hits": 1, "n": 2}
    assert tr["by_conviction"]["low"] == {"hits": 1, "n": 1}


def test_calibration_note_flags_overconfidence():
    # high: 1/5 (20%), low: 4/5 (80%) → high underperforms low by >10pts.
    by_conv = {
        "high": {"hits": 1, "n": 5},
        "low": {"hits": 4, "n": 5},
        "med": {"hits": 0, "n": 0},
    }
    note = scorecard._calibration_note(by_conv)
    assert note is not None and "OVERCONFIDENT" in note
    # Too few samples → no note (don't cry wolf on noise).
    assert scorecard._calibration_note(
        {"high": {"hits": 0, "n": 2}, "low": {"hits": 2, "n": 2}}
    ) is None


def test_mark_open_calls_fills_horizon_and_retires_unscoreable():
    now = _now()
    created = now - timedelta(days=2)
    with session_scope() as s:
        s.add(TradingCall(
            ticker="MK", direction="long", conviction=3, source="convergence",
            thesis="t", price_at_call=100, created_at=created,
        ))
        # A bar at the 1d horizon (created+1d) priced 110 → +10%.
        s.add(PriceBar(
            ticker="MK", ts=(created + timedelta(days=1)).replace(tzinfo=None),
            open=110, high=110, low=110, close=110, volume=1,
        ))
        # Unscoreable call (no price at call) must be retired, not scored.
        s.add(TradingCall(
            ticker="NO", direction="long", conviction=3, source="convergence",
            thesis="t", price_at_call=None, created_at=created,
        ))
    scorecard.mark_open_calls()
    with session_scope() as s:
        mk = s.exec(select(TradingCall).where(TradingCall.ticker == "MK")).first()
        no = s.exec(select(TradingCall).where(TradingCall.ticker == "NO")).first()
    assert mk.ret_1d_pct == 10.0
    assert mk.settled is False  # 5d/20d still pending
    assert no.settled is True and no.ret_1d_pct is None


def test_mark_open_calls_rejects_stale_far_horizon_no_fabricated_grade():
    """A dead-feed ticker must NOT get a 20d return computed off a weeks-old
    close. Near horizons (within the staleness window) still mark; the far
    one stays None and the call retires unscored at give-up."""
    now = _now()
    created = now - timedelta(days=26)  # 20d matured AND past give-up (25d)
    with session_scope() as s:
        s.add(TradingCall(
            ticker="DF", direction="long", conviction=3, source="convergence",
            thesis="t", price_at_call=100, created_at=created,
        ))
        # Only ONE bar, at created+1d. 1d gap≈0 (mark), 5d gap=4d (≤7, mark),
        # 20d gap=19d (>7, must be rejected — feed is dead by then).
        s.add(PriceBar(
            ticker="DF", ts=(created + timedelta(days=1)).replace(tzinfo=None),
            open=110, high=110, low=110, close=110, volume=1,
        ))
    scorecard.mark_open_calls()
    with session_scope() as s:
        c = s.exec(select(TradingCall).where(TradingCall.ticker == "DF")).first()
    assert c.ret_1d_pct == 10.0      # fresh enough
    assert c.ret_5d_pct == 10.0      # 4d gap tolerated (weekend-like)
    assert c.ret_20d_pct is None     # 19d-stale close NOT fabricated
    assert c.settled is True         # retired unscored on the 20d leg


# ─────────────────────────── funds ──────────────────────────────────────────


def test_fund_close_cash_convention_long_and_short():
    """The sign-error trap: long close adds cash & gains when mark rises;
    short close subtracts cash & gains when mark FALLS."""
    with session_scope() as s:
        f = Fund(
            name="t", mandate="m", starting_cash=1000, cash=1000,
            last_call_id=0, created_at=_now(),
        )
        s.add(f)
        s.flush()
        lng = FundTrade(
            fund_id=f.id, ticker="L", side="long", qty=10, entry_price=100,
            entry_at=_now(),
        )
        sht = FundTrade(
            fund_id=f.id, ticker="S", side="short", qty=10, entry_price=100,
            entry_at=_now(),
        )
        s.add(lng)
        s.add(sht)
        s.flush()
        funds._close(s, f, lng, 120, "take")   # +20*10 = +200
        funds._close(s, f, sht, 80, "take")    # short win: entry>mark → +200
        assert lng.realized_pnl == 200
        assert sht.realized_pnl == 200
        # cash: +qty*mark (long close) then -qty*mark (short close)
        # 1000 + 10*120 - 10*80 = 1400
        assert f.cash == 1400


def test_fund_run_force_closes_stale_priceless_position_at_max_hold():
    """Regression: a position whose ticker stops pricing must still be
    force-closed once it exceeds max_hold (at entry, realized ≈ 0) instead
    of becoming an immortal position that distorts standings forever."""
    funds.seed_funds()
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        # degen max_hold_days = 5; this is 30d old with NO PriceContext.
        s.add(FundTrade(
            fund_id=degen.id, ticker="DEAD", side="long", qty=5,
            entry_price=20, entry_at=_now() - timedelta(days=30),
        ))
    funds._run()
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        t = s.exec(select(FundTrade).where(FundTrade.ticker == "DEAD")).first()
        assert t.status == "closed"
        assert t.close_reason == "max_hold_stale"
        assert t.realized_pnl == 0  # closed at entry, no invented P&L
        assert funds._open_trades(s, degen.id) == []  # slot freed


def test_fund_equity_marks_to_market():
    with session_scope() as s:
        s.add(_pc("L", 130))
        s.add(_pc("S", 70))
        f = Fund(
            name="e", mandate="m", starting_cash=1000, cash=500,
            last_call_id=0, created_at=_now(),
        )
        s.add(f)
        s.flush()
        opens = [
            FundTrade(fund_id=f.id, ticker="L", side="long", qty=10,
                      entry_price=100, entry_at=_now()),
            FundTrade(fund_id=f.id, ticker="S", side="short", qty=10,
                      entry_price=100, entry_at=_now()),
        ]
        for t in opens:
            s.add(t)
        # equity = cash + long qty*mark - short qty*mark
        #        = 500 + 10*130 - 10*70 = 1100
        assert funds._equity(s, f, opens) == 1100


def test_fund_run_opens_long_with_correct_cash_then_marks_up():
    """End-to-end: seed funds (cursor=0), a convergence call → degen opens a
    long, cash debited by notional, equity snapshot recorded; a later mark-up
    raises equity."""
    funds.seed_funds()  # 3 funds, cursor 0 (no calls yet)
    with session_scope() as s:
        s.add(_pc("ZZ", 100))
        s.add(TradingCall(
            ticker="ZZ", direction="long", conviction=5, source="convergence",
            thesis="t", price_at_call=100, created_at=_now(),
        ))
    funds._run()

    start = settings.FUND_STARTING_CASH
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        trades = s.exec(
            select(FundTrade).where(FundTrade.fund_id == degen.id)
        ).all()
        assert len(trades) == 1
        t = trades[0]
        assert t.ticker == "ZZ" and t.side == "long"
        # Sizing is fixed-risk (not flat notional): qty risks
        # _BASE_RISK_PCT × equity between entry and stop. Conviction 5 →
        # conv_bias 1.0; no drawdown + no attribution history → dd_scale and
        # edge_mult both 1.0; degen size_pct 0.20 → notional-scale 1.0. So
        # qty = (start × 0.008) / |entry − stop|, derived here from the
        # trade's own stop so the assertion tracks the formula, not a literal.
        per_share_risk = abs(t.entry_price - t.stop_price)
        expected_qty = (start * funds._BASE_RISK_PCT) / per_share_risk
        assert abs(t.qty - expected_qty) < 1e-6
        notional = t.qty * t.entry_price  # well under the 0.20 notional ceiling
        assert abs(degen.cash - (start - notional)) < 1e-6
        eq_rows = s.exec(
            select(FundEquity).where(FundEquity.fund_id == degen.id)
        ).all()
        assert eq_rows and abs(eq_rows[-1].equity - start) < 1e-6  # mark==entry

    # Price doubles; no new calls → equity should rise by the notional.
    with session_scope() as s:
        pc = s.get(PriceContext, "ZZ")
        pc.last_price = 200
        s.add(pc)
    funds._run()
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        opens = funds._open_trades(s, degen.id)
        # if still open, equity = cash + qty*200; if a take-profit fired it
        # realized the gain into cash. Either way equity ≈ start + notional.
        assert abs(funds._equity(s, degen, opens) - (start + notional)) < 1.0


def test_fund_run_does_not_fabricate_loss_on_zero_price_tick():
    """Regression: a bad 0.0 price tick must NOT read as −100% and
    fake-liquidate a position. Below max_hold it's left untouched; at/over
    max_hold it force-closes at ENTRY (realized 0), never at a fabricated 0."""
    funds.seed_funds()
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        s.add(_pc("BADTICK", 0.0))  # garbage feed value
        s.add(FundTrade(
            fund_id=degen.id, ticker="BADTICK", side="long", qty=5,
            entry_price=20, entry_at=_now() - timedelta(days=1),  # < 5d
        ))
    funds._run()
    with session_scope() as s:
        t = s.exec(
            select(FundTrade).where(FundTrade.ticker == "BADTICK")
        ).first()
        assert t.status == "open"          # NOT fake-stopped at 0
        assert t.realized_pnl is None

    # Age it past degen's 5d max_hold — now it closes, but at ENTRY not 0.
    with session_scope() as s:
        t = s.exec(
            select(FundTrade).where(FundTrade.ticker == "BADTICK")
        ).first()
        t.entry_at = _now() - timedelta(days=9)
        s.add(t)
    funds._run()
    with session_scope() as s:
        t = s.exec(
            select(FundTrade).where(FundTrade.ticker == "BADTICK")
        ).first()
        assert t.status == "closed"
        assert t.close_reason == "max_hold_stale"
        assert t.realized_pnl == 0  # NOT -5*20 = -100 fabricated loss


def test_policy_dict_integrity():
    """Every wallet must have the keys the engine reads; invert is opt-in and
    only where intended."""
    required = {
        "mandate", "sources", "min_conviction", "asset_classes",
        "size_pct", "max_positions", "stop_pct", "take_pct", "max_hold_days",
    }
    assert set(funds._POLICIES) == {
        "degen", "catalyst", "macro", "crypto", "sniper", "contrarian",
        "hype",
    }
    for name, pol in funds._POLICIES.items():
        assert required <= set(pol), f"{name} missing {required - set(pol)}"
        assert isinstance(pol["sources"], set) and pol["sources"]
    inverted = {n for n, p in funds._POLICIES.items() if p.get("invert")}
    assert inverted == {"contrarian"}
    surge = {n for n, p in funds._POLICIES.items() if p.get("require_social_surge")}
    assert surge == {"hype"}


def test_funds_earnings_blackout_blocks_fresh_opens():
    """No fund initiates a position when the name reports within the
    blackout window; a far date or an unknown name trades normally."""
    funds.seed_funds()
    today = _now().date()
    with session_scope() as s:
        for tk in ("ERNSOON", "ERNFAR", "ERNNONE"):
            s.add(_pc(tk, 100))
        s.add(EarningsDate(ticker="ERNSOON",
              report_date=today + timedelta(days=1), fetched_at=_now()))
        s.add(EarningsDate(ticker="ERNFAR",
              report_date=today + timedelta(days=30), fetched_at=_now()))
        for tk in ("ERNSOON", "ERNFAR", "ERNNONE"):
            s.add(TradingCall(
                ticker=tk, direction="long", conviction=5,
                source="convergence", thesis="t", price_at_call=100,
                created_at=_now()))
    funds._run()
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        held = {t.ticker for t in funds._open_trades(s, degen.id)}
    assert "ERNSOON" not in held              # prints in 1d → blacked out
    assert {"ERNFAR", "ERNNONE"} <= held      # far date / unknown → traded


def test_hype_wallet_requires_social_corroboration():
    """hype only opens when the crowd is ALSO surging (≥4 r/ posts/18h);
    degen has no such gate and opens regardless."""
    funds.seed_funds()
    with session_scope() as s:
        s.add(_pc("QUIET", 100))
        s.add(_pc("LOUD", 100))
        for i in range(5):  # LOUD: 5 distinct posts in-window
            s.add(RedditMention(
                subreddit="wallstreetbets", post_id=f"L{i}", comment_id=None,
                ticker="LOUD", author="u", score=0, num_comments=0,
                created_at=_now(), title="t", body_excerpt="", permalink="u"))
        s.add(RedditMention(
            subreddit="stocks", post_id="Q1", comment_id=None, ticker="QUIET",
            author="u", score=0, num_comments=0, created_at=_now(),
            title="t", body_excerpt="", permalink="u"))
        for tk in ("QUIET", "LOUD"):
            s.add(TradingCall(
                ticker=tk, direction="long", conviction=4,
                source="why_moved", thesis="t", price_at_call=100,
                created_at=_now()))
    funds._run()
    with session_scope() as s:
        hype = s.exec(select(Fund).where(Fund.name == "hype")).first()
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        hype_held = {t.ticker for t in funds._open_trades(s, hype.id)}
        degen_held = {t.ticker for t in funds._open_trades(s, degen.id)}
    assert hype_held == {"LOUD"}              # only crowd-corroborated
    assert {"QUIET", "LOUD"} <= degen_held    # degen ungated


def test_contrarian_fades_a_long_call_into_a_short():
    """A why_moved LONG → degen goes long (control), contrarian goes SHORT."""
    funds.seed_funds()
    with session_scope() as s:
        s.add(_pc("QQ", 100))
        s.add(TradingCall(
            ticker="QQ", direction="long", conviction=4, source="why_moved",
            thesis="fade test thesis", price_at_call=100, created_at=_now(),
        ))
    moves = funds._run()

    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        contra = s.exec(select(Fund).where(Fund.name == "contrarian")).first()
        dt = s.exec(select(FundTrade).where(FundTrade.fund_id == degen.id)).first()
        ct = s.exec(
            select(FundTrade).where(FundTrade.fund_id == contra.id)
        ).first()
    assert dt.side == "long"                       # control: normal fund
    assert ct.side == "short"                      # contrarian faded it
    assert ct.open_reason.startswith("fade why_moved")
    # the return surfaces the reasoning, including the verbatim thesis
    opens = {m["fund"]: m for m in moves if m["kind"] == "open"}
    assert opens["contrarian"]["side"] == "short"
    assert opens["contrarian"]["invert"] is True
    assert opens["contrarian"]["thesis"] == "fade test thesis"
    assert opens["degen"]["invert"] is False


def test_inverted_flip_alignment_and_no_pyramiding():
    funds.seed_funds()
    with session_scope() as s:
        s.add(_pc("QQ", 100))
        s.add(TradingCall(
            ticker="QQ", direction="long", conviction=4, source="why_moved",
            thesis="t1", price_at_call=100, created_at=_now(),
        ))
    funds._run()  # contrarian now SHORT QQ

    # Another LONG call → contrarian still wants SHORT == held → no pyramid.
    with session_scope() as s:
        s.add(TradingCall(
            ticker="QQ", direction="long", conviction=4, source="why_moved",
            thesis="t2", price_at_call=100, created_at=_now(),
        ))
    funds._run()
    with session_scope() as s:
        contra = s.exec(select(Fund).where(Fund.name == "contrarian")).first()
        opens = funds._open_trades(s, contra.id)
        assert len(opens) == 1 and opens[0].side == "short"

    # A SHORT call → contrarian wants LONG ≠ held short → flip.
    with session_scope() as s:
        s.add(TradingCall(
            ticker="QQ", direction="short", conviction=4, source="why_moved",
            thesis="t3", price_at_call=100, created_at=_now(),
        ))
    funds._run()
    with session_scope() as s:
        contra = s.exec(select(Fund).where(Fund.name == "contrarian")).first()
        opens = funds._open_trades(s, contra.id)
        flipped = s.exec(
            select(FundTrade)
            .where(FundTrade.fund_id == contra.id)
            .where(FundTrade.status == "closed")
        ).all()
    assert len(opens) == 1 and opens[0].side == "long"   # faded the short
    assert any(t.close_reason == "flip" for t in flipped)


def test_moves_embed_renders_reasoning_and_handles_empty():
    assert funds._moves_embed([]) is None
    e = funds._moves_embed([
        {"fund": "contrarian", "kind": "open", "ticker": "QQ",
         "side": "short", "qty": 5.0, "price": 100.0, "source": "why_moved",
         "conviction": 4, "thesis": "momentum looks exhausted", "invert": True},
        {"fund": "degen", "kind": "close", "ticker": "ZZ", "side": "long",
         "qty": 3.0, "entry": 10.0, "exit": 12.0, "realized": -0.0,
         "reason": "flip", "held_days": 2, "open_reason": "why_moved c4"},
    ])
    assert e is not None
    body = e.description
    assert "$QQ" in body and "FADE" in body
    assert "momentum looks exhausted" in body          # verbatim thesis
    assert "closed LONG" in body and "flip" in body
    assert "-0" not in body                            # negative-zero safe


def test_fund_detail_surfaces_mark_freshness():
    """The recurring 'is it the weekend or is the feed stuck?' question is
    answered in the view itself: fresh marks say 'live', stale ones warn the
    P&L is frozen at entry."""
    funds.seed_funds()
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        s.add(FundTrade(
            fund_id=degen.id, ticker="FRSH", side="long", qty=2,
            entry_price=50.0, entry_at=_now(),
        ))
        s.add(PriceContext(
            ticker="FRSH", last_price=50.0, change_1d_pct=0.0,
            change_5d_pct=0.0, volume_vs_20d_avg=1.0, last_updated=_now(),
        ))
    assert "marks live" in funds.fund_detail_text("degen")

    with session_scope() as s:
        pc = s.get(PriceContext, "FRSH")
        pc.last_updated = _now() - timedelta(days=2)
        s.add(pc)
    txt = funds.fund_detail_text("degen")
    assert "market likely closed" in txt and "frozen at entry" in txt


def test_flat_short_does_not_render_negative_zero():
    """A short with mark==entry has unrealized 0.0*-1 = -0.0; it must display
    as +0, not a misleading -0 (every weekend short hit this)."""
    funds.seed_funds()
    with session_scope() as s:
        degen = s.exec(select(Fund).where(Fund.name == "degen")).first()
        s.add(_pc("FLAT", 50.0))
        s.add(FundTrade(
            fund_id=degen.id, ticker="FLAT", side="short", qty=10,
            entry_price=50.0, entry_at=_now(),
        ))
    text = funds.fund_detail_text("degen")
    assert "(-0)" not in text          # no negative-zero anywhere
    assert "$FLAT" in text and "(+0)" in text
    rows = funds.fund_standings()
    drow = next(r for r in rows if r["name"] == "degen")
    assert str(drow["upnl"]) != "-0.0"  # aggregate normalized too


def test_within_budget_is_symmetric_no_leverage():
    """The funds are only a clean comparison if every one risks at most its
    own bankroll. _within_budget must gate total committed notional vs equity
    identically for longs and shorts (it doesn't even see direction) — closing
    the old hole where short proceeds back-doored leverage into longs."""
    eq = 10_000.0
    assert funds._within_budget([], eq, 2_000.0) is True
    assert funds._within_budget([], eq, 10_000.0) is True      # exactly all-in
    assert funds._within_budget([], eq, 10_000.01) is False     # no leverage

    def _t(side: str) -> FundTrade:
        # 80 * 100 = 8_000 committed notional, regardless of side.
        return FundTrade(
            fund_id=1, ticker="X", side=side, qty=80, entry_price=100,
            entry_at=_now(),
        )

    for side in ("short", "long"):
        opens = [_t(side)]  # 8_000 already committed
        assert funds._within_budget(opens, eq, 2_000.0) is True   # 8k+2k = eq
        assert funds._within_budget(opens, eq, 2_500.0) is False   # > eq


# ──────────────────── manual paper-book command guards ──────────────────────


def test_cmd_open_happy_path_creates_one_position():
    """Sanity: the new guards don't break a normal open (price from mark,
    note parsing, opened_by)."""
    with session_scope() as s:
        s.add(_pc("OK", 25))
    chat._cmd_open("OK 4 earnings play", "long")
    with session_scope() as s:
        rows = s.exec(
            select(PaperTrade).where(PaperTrade.ticker == "OK")
        ).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.side == "long" and r.qty == 4 and r.entry_price == 25
    assert r.note == "earnings play" and r.opened_by == "manual"


def test_cmd_open_rejects_duplicate_or_opposing_open_on_same_ticker():
    """A second open on a ticker that already has one would make !close
    nondeterministic and let long+short coexist — reject it."""
    with session_scope() as s:
        s.add(_pc("DUP", 50))
    chat._cmd_open("DUP 10", "long")
    chat._cmd_open("DUP 3", "short")  # opposing second open — must be refused
    with session_scope() as s:
        rows = s.exec(
            select(PaperTrade).where(PaperTrade.ticker == "DUP")
        ).all()
    assert len(rows) == 1
    assert rows[0].side == "long" and rows[0].qty == 10


def test_cmd_open_rejects_nonpositive_or_missing_price():
    """price 0 / negative / unavailable must never open a position (it would
    fabricate P&L off a zero cost basis)."""
    chat._cmd_open("ZP 10 0", "long")    # explicit zero
    chat._cmd_open("ZN 10 -5", "long")   # explicit negative
    chat._cmd_open("NOPX 10", "long")    # no mark, no price given
    with session_scope() as s:
        assert s.exec(select(PaperTrade)).all() == []


def test_macro_desk_idea_is_logged_to_accountability_spine():
    """A news-desk read must enter the same TradingCall stream as
    synthesis/why_moved so scorecard / call_review / wallet_meta track it."""
    scorecard.record_call("XOM", "long", "macro_themes", "hormuz squeeze", 4)
    with session_scope() as s:
        c = s.exec(
            select(TradingCall).where(TradingCall.source == "macro_themes")
        ).first()
    assert c is not None
    assert c.ticker == "XOM" and c.direction == "long" and c.conviction == 4


def test_macro_desk_usable_gate():
    from sentinel.llm import LLM_ERROR_SENTINEL
    from sentinel.pipelines.macro_themes import _usable

    good = (
        "**Hormuz risk**: strikes lift tanker premiums.\n"
        "Chain: shipping risk → crude → energy bid.\n"
        "Read: lean long $XOM, conviction 3.\nExposed: $XOM $XLE\n"
    ) * 2
    assert _usable(good) is True
    assert _usable("") is False
    assert _usable(LLM_ERROR_SENTINEL) is False
    assert _usable("too short, no structure") is False
