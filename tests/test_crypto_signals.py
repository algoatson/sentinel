"""Crypto leg: BTC market-regime gate + funding-squeeze entry filters.

Pins the three improvements that hardened the weak crypto leg:
  1. market_regime classifies the tape from BTC; blocks_entry refuses
     counter-trend entries (no longs in risk_off, no shorts in risk_on).
  2. funding_squeeze._evaluate suppresses counter-regime setups and ones the
     resting orderbook contradicts.
  3. stale microstructure never fires a signal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.crypto_regime import Regime, blocks_entry, market_regime
from sentinel.db import session_scope
from sentinel.models import CryptoMicro, PriceContext
from sentinel.pipelines.funding_squeeze import _evaluate

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _seed_price(ticker: str, *, d1: float, d5: float = 0.0, age_min: float = 0.0) -> None:
    """d1/d5 are FRACTIONS (0.08 = +8%), matching PriceContext storage."""
    with session_scope() as s:
        s.merge(PriceContext(
            ticker=ticker, last_price=100.0,
            change_1d_pct=d1, change_5d_pct=d5, volume_vs_20d_avg=1.0,
            last_updated=_now() - timedelta(minutes=age_min),
        ))


def _seed_micro(ticker: str, *, funding=None, oi_chg=None, imbalance=None,
                age_min: float = 0.0) -> None:
    with session_scope() as s:
        s.merge(CryptoMicro(
            ticker=ticker, venue="test",
            funding_rate=funding, open_interest=None,
            oi_change_24h_pct=oi_chg, orderbook_imbalance=imbalance,
            updated_at=_now() - timedelta(minutes=age_min),
        ))


# ── market_regime ────────────────────────────────────────────────────────


def test_regime_risk_off_on_sharp_btc_drop():
    _seed_price("BTC-USD", d1=-0.04)  # −4% 1d
    with session_scope() as s:
        assert market_regime(s).state == "risk_off"


def test_regime_risk_on_on_sharp_btc_rip():
    _seed_price("BTC-USD", d1=0.04)
    with session_scope() as s:
        assert market_regime(s).state == "risk_on"


def test_regime_soft_band_needs_trend_agreement():
    # −2% 1d alone is neutral; with −10% 5d it's risk_off.
    _seed_price("BTC-USD", d1=-0.02, d5=0.0)
    with session_scope() as s:
        assert market_regime(s).state == "neutral"
    _seed_price("BTC-USD", d1=-0.02, d5=-0.10)
    with session_scope() as s:
        assert market_regime(s).state == "risk_off"


def test_regime_fails_open_when_btc_missing_or_stale():
    with session_scope() as s:
        assert market_regime(s).state == "neutral"  # no BTC row
    _seed_price("BTC-USD", d1=-0.04, age_min=120)   # sharp but stale
    with session_scope() as s:
        assert market_regime(s).state == "neutral"


# ── blocks_entry ───────────────────────────────────────────────────────────


def test_blocks_entry_directionality():
    off = Regime("risk_off", -4.0, 0.0, "")
    on = Regime("risk_on", 4.0, 0.0, "")
    neu = Regime("neutral", 0.0, 0.0, "")
    assert blocks_entry(off, "long", "SOL-USD") is True
    assert blocks_entry(off, "short", "SOL-USD") is False
    assert blocks_entry(on, "short", "SOL-USD") is True
    assert blocks_entry(on, "long", "SOL-USD") is False
    assert blocks_entry(neu, "long", "SOL-USD") is False
    # BTC is the proxy — never gated against its own regime.
    assert blocks_entry(off, "long", "BTC-USD") is False


# ── funding_squeeze._evaluate gates ──────────────────────────────────────

_NEUTRAL = Regime("neutral", 0.0, 0.0, "neutral")
_RISK_OFF = Regime("risk_off", -4.0, 0.0, "BTC -4.0% 1d")
_RISK_ON = Regime("risk_on", 4.0, 0.0, "BTC +4.0% 1d")


def _eval(ticker: str, regime: Regime):
    with session_scope() as s:
        return _evaluate(s, ticker, _now(), regime)


def test_clean_squeeze_fires_in_neutral():
    _seed_price("SQZ-USD", d1=0.08)              # +8% 24h
    _seed_micro("SQZ-USD", funding=-0.0008, imbalance=0.1)  # −0.08%/8h funding
    f = _eval("SQZ-USD", _NEUTRAL)
    assert f is not None and f["kind"] == "squeeze_long" and f["direction"] == "long"


def test_squeeze_long_suppressed_in_risk_off():
    _seed_price("SQZ-USD", d1=0.08)
    _seed_micro("SQZ-USD", funding=-0.0008, imbalance=0.1)
    assert _eval("SQZ-USD", _RISK_OFF) is None


def test_squeeze_long_suppressed_by_ask_heavy_book():
    _seed_price("SQZ-USD", d1=0.08)
    _seed_micro("SQZ-USD", funding=-0.0008, imbalance=-0.5)  # heavy sell wall
    assert _eval("SQZ-USD", _NEUTRAL) is None


def test_funding_fade_fires_then_blocked_in_risk_on():
    _seed_price("FADE-USD", d1=0.005)            # flat
    _seed_micro("FADE-USD", funding=0.0010, imbalance=0.0)  # +0.10%/8h crowded long
    f = _eval("FADE-USD", _NEUTRAL)
    assert f is not None and f["kind"] == "funding_fade" and f["direction"] == "short"
    # Same setup while the market is ripping → don't fade a bull tape.
    assert _eval("FADE-USD", _RISK_ON) is None


def test_stale_micro_never_fires():
    _seed_price("OLD-USD", d1=0.08)
    _seed_micro("OLD-USD", funding=-0.0008, imbalance=0.1, age_min=200)
    assert _eval("OLD-USD", _NEUTRAL) is None
