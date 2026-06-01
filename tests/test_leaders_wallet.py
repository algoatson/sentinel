"""Leaders wallet — the trend / relative-strength gate that replaced the
retired `contrarian` wallet.

The gate is the whole point of the wallet: only trade WITH a name's
established trend, never fade it. These pin the directional logic, the
thin-history fail-closed, the over-extension guard, the optional
relative-strength overlay, and the one-time contrarian→leaders DB rename.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel import funds
from sentinel.db import session_scope
from sentinel.models import Fund, FundTrade, PriceBar, PriceContext

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _seed_bars(ticker: str, closes: list[float]) -> None:
    """Write one daily PriceBar per close, oldest→newest ending today."""
    n = len(closes)
    with session_scope() as s:
        prev = closes[0]
        for i, c in enumerate(closes):
            ts = _now() - timedelta(days=n - 1 - i)
            s.add(PriceBar(
                ticker=ticker, ts=ts,
                open=prev, high=max(prev, c) * 1.005,
                low=min(prev, c) * 0.995, close=c, volume=1000,
            ))
            prev = c


def _q(fn):
    """Run a callable inside a session and return its result."""
    with session_scope() as s:
        return fn(s)


# ── policy wiring ────────────────────────────────────────────────────────────


def test_leaders_replaced_contrarian_in_policies():
    assert "leaders" in funds._POLICIES
    assert "contrarian" not in funds._POLICIES
    pol = funds._POLICIES["leaders"]
    assert pol.get("require_trend_align") is True
    assert pol.get("invert") is not True          # leaders follows, never fades
    # patient profile: wider stop / longer hold / higher take than degen
    assert pol["max_hold_days"] >= funds._POLICIES["degen"]["max_hold_days"]


# ── directional gate ─────────────────────────────────────────────────────────


def test_uptrend_confirms_long_blocks_short():
    _seed_bars("UP", [100.0 + i for i in range(40)])          # steady rise
    ok_long, _ = _q(lambda s: funds._trend_aligned(s, "UP", "long"))
    ok_short, why = _q(lambda s: funds._trend_aligned(s, "UP", "short"))
    assert ok_long is True
    assert ok_short is False and "downtrend" in why


def test_downtrend_confirms_short_blocks_long():
    _seed_bars("DN", [140.0 - i for i in range(40)])          # steady fall
    ok_short, _ = _q(lambda s: funds._trend_aligned(s, "DN", "short"))
    ok_long, why = _q(lambda s: funds._trend_aligned(s, "DN", "long"))
    assert ok_short is True
    assert ok_long is False and "uptrend" in why


def test_flat_blocks_both_sides():
    _seed_bars("FLAT", [100.0] * 40)
    assert _q(lambda s: funds._trend_aligned(s, "FLAT", "long"))[0] is False
    assert _q(lambda s: funds._trend_aligned(s, "FLAT", "short"))[0] is False


def test_thin_history_fails_closed():
    _seed_bars("NEW", [100.0 + i for i in range(10)])         # < _TREND_MIN_BARS
    ok, why = _q(lambda s: funds._trend_aligned(s, "NEW", "long"))
    assert ok is False and "thin history" in why


def test_over_extension_guard_blocks_blowoff():
    # Clean uptrend, then a vertical spike far above the fast MA.
    closes = [100.0 + i for i in range(39)] + [300.0]
    _seed_bars("BLOW", closes)
    ok, why = _q(lambda s: funds._trend_aligned(s, "BLOW", "long", atr=2.0))
    assert ok is False and "over-extended" in why
    # Without an ATR the extension guard can't fire → trend still confirms.
    assert _q(lambda s: funds._trend_aligned(s, "BLOW", "long"))[0] is True


# ── relative-strength overlay ────────────────────────────────────────────────


def _set_benchmark_5d(ticker: str, pct_frac: float) -> None:
    with session_scope() as s:
        s.add(PriceContext(
            ticker=ticker, last_price=1.0, change_1d_pct=0.0,
            change_5d_pct=pct_frac, volume_vs_20d_avg=1.0, last_updated=_now(),
        ))


def test_rs_overlay_blocks_laggards_passes_leaders():
    # Name's own 5d ≈ +3.7% (closes 100..139). Crypto → benchmark BTC-USD.
    _seed_bars("ALT", [100.0 + i for i in range(40)])
    # Benchmark up +6% over 5d → name lags it → long blocked.
    _set_benchmark_5d("BTC-USD", 0.06)
    ok, why = _q(lambda s: funds._trend_aligned(
        s, "ALT", "long", asset_class="crypto"))
    assert ok is False and "lagging benchmark" in why


def test_rs_overlay_noop_when_benchmark_absent():
    # No SPY PriceContext → equity overlay is skipped, absolute trend wins.
    _seed_bars("LEAD", [100.0 + i for i in range(40)])
    ok, _ = _q(lambda s: funds._trend_aligned(
        s, "LEAD", "long", asset_class="equity"))
    assert ok is True


# ── one-time contrarian → leaders rename ─────────────────────────────────────


def test_seed_migration_renames_contrarian_keeping_equity_and_positions():
    with session_scope() as s:
        f = Fund(name="contrarian", mandate="old", starting_cash=10_000.0,
                 cash=8_500.0, last_call_id=0, created_at=_now())
        s.add(f)
        s.flush()
        old_id = f.id
        s.add(FundTrade(
            fund_id=old_id, ticker="ZZ", side="short", qty=3,
            entry_price=10.0, entry_at=_now(), status="open",
        ))

    funds.seed_funds()

    with session_scope() as s:
        assert s.exec(select(Fund).where(Fund.name == "contrarian")).first() is None
        lead = s.exec(select(Fund).where(Fund.name == "leaders")).first()
        assert lead is not None
        assert lead.id == old_id                      # renamed in place
        assert lead.cash == 8_500.0                    # equity carried over
        assert lead.mandate == funds._POLICIES["leaders"]["mandate"]
        # the inherited open position still hangs off the same fund row
        trades = s.exec(
            select(FundTrade).where(FundTrade.fund_id == old_id)
        ).all()
        assert len(trades) == 1 and trades[0].ticker == "ZZ"


def test_seed_migration_is_idempotent():
    funds.seed_funds()
    funds.seed_funds()  # second run must not double-seed or crash
    with session_scope() as s:
        leaders = s.exec(select(Fund).where(Fund.name == "leaders")).all()
        assert len(leaders) == 1
