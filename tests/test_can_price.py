"""`prices.can_price` contract.

This is the gate that stops `crypto_trending` from spamming the #crypto
channel with names yfinance can't actually price (PENGU, VVV, HYPE,
etc — real CoinGecko-trending tokens that have no Yahoo data). Strict
contract:

- True for tickers that yfinance returns ANY data for (cache hit
  forever after the first probe).
- False for tickers that return nothing (cache hit for 7d after probe,
  re-probes after the negative TTL so a freshly-listed coin gets a
  second chance without us paying daily).
- False on probe error (raised exception, network blip, weird shape) —
  optimistic-on-failure leaves us in the broken state we're trying
  to fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.ingesters import prices


class _FakeHistory:
    def __init__(self, empty: bool) -> None:
        self.empty = empty


class _FakeTicker:
    def __init__(self, *, empty: bool, raise_exc: bool = False):
        self._empty = empty
        self._raise = raise_exc

    def history(self, period: str = "5d", interval: str = "1d"):
        if self._raise:
            raise RuntimeError("synthetic yfinance failure")
        return _FakeHistory(empty=self._empty)


def _stub_yf(monkeypatch, *, empty: bool, raise_exc: bool = False) -> None:
    """Replace `yfinance.Ticker` so tests don't touch the network."""
    import yfinance as yf
    monkeypatch.setattr(
        yf, "Ticker",
        lambda sym: _FakeTicker(empty=empty, raise_exc=raise_exc),
    )


def _clear_cache() -> None:
    prices._CAN_PRICE_CACHE.clear()


def test_can_price_returns_true_for_non_empty_history(monkeypatch):
    _clear_cache()
    _stub_yf(monkeypatch, empty=False)
    assert prices.can_price("BTC-USD", "crypto") is True


def test_can_price_returns_false_for_empty_history(monkeypatch):
    _clear_cache()
    _stub_yf(monkeypatch, empty=True)
    assert prices.can_price("PENGU-USD", "crypto") is False


def test_can_price_treats_exception_as_false(monkeypatch):
    """Network blip, rate-limit, schema drift — must not raise; must
    be conservative (no admit). Optimism here means polluting the
    watchlist with un-priceable noise."""
    _clear_cache()
    _stub_yf(monkeypatch, empty=False, raise_exc=True)
    assert prices.can_price("ANYTHING", "crypto") is False


def test_positive_result_is_cached_forever_within_process(monkeypatch):
    _clear_cache()
    _stub_yf(monkeypatch, empty=False)
    assert prices.can_price("BTC-USD", "crypto") is True
    # Re-probe must NOT happen on cache hit — flip yfinance to raise
    # and confirm we still get True from cache.
    _stub_yf(monkeypatch, empty=False, raise_exc=True)
    assert prices.can_price("BTC-USD", "crypto") is True


def test_negative_result_is_cached_within_ttl(monkeypatch):
    _clear_cache()
    _stub_yf(monkeypatch, empty=True)
    assert prices.can_price("DEAD-USD", "crypto") is False
    # Sanity check: cache contains the False entry
    value, ts = prices._CAN_PRICE_CACHE["DEAD-USD"]
    assert value is False
    assert (datetime.now(timezone.utc) - ts) < timedelta(seconds=5)
    # Even if yfinance "comes back" with data, we honour the negative
    # cache for the TTL — daily polls don't re-probe a dead token.
    _stub_yf(monkeypatch, empty=False)
    assert prices.can_price("DEAD-USD", "crypto") is False


def test_negative_result_reprobes_after_ttl(monkeypatch):
    """A token listed AFTER the bot last probed it should get a second
    chance once the negative TTL elapses — we don't blacklist forever."""
    _clear_cache()
    _stub_yf(monkeypatch, empty=True)
    assert prices.can_price("LATER-USD", "crypto") is False
    # Backdate the cache entry past the TTL
    stale = datetime.now(timezone.utc) - prices._NEGATIVE_PROBE_TTL - timedelta(seconds=1)
    prices._CAN_PRICE_CACHE["LATER-USD"] = (False, stale)
    # Now yfinance has data; can_price must re-probe and update
    _stub_yf(monkeypatch, empty=False)
    assert prices.can_price("LATER-USD", "crypto") is True
    # And the cache reflects the new positive result
    assert prices._CAN_PRICE_CACHE["LATER-USD"][0] is True


def test_crypto_trending_skips_un_priceable_tokens(monkeypatch):
    """End-to-end gate: the trending promoter must NOT add a token that
    `can_price` rejects. Regression pin for the "channel spammed with
    PENGU/VVV/HYPE" bug."""
    from sentinel.ingesters import crypto_trending
    from sentinel.db import session_scope
    from sentinel.models import Watchlist
    from sqlmodel import select

    # Stub the CoinGecko fetch with a tiny payload mixing one priceable
    # (BTC, mapped → BTC-USD) with one we'll mark un-priceable.
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"coins": [
                {"item": {"symbol": "BTC"}},
                {"item": {"symbol": "ZZZBAD"}},
            ]}
    monkeypatch.setattr(
        crypto_trending.httpx, "get", lambda *a, **k: _Resp(),
    )
    _clear_cache()
    # BTC-USD priceable, ZZZBAD-USD not
    real_can_price = prices.can_price
    def _fake_can(t, ac):
        return t == "BTC-USD"
    monkeypatch.setattr(prices, "can_price", _fake_can)
    try:
        newly = crypto_trending._run()
    finally:
        monkeypatch.setattr(prices, "can_price", real_can_price)
    assert "BTC" in newly
    assert "ZZZBAD" not in newly
    # And the watchlist row exists for BTC-USD but NOT ZZZBAD-USD
    with session_scope() as s:
        btc = s.exec(
            select(Watchlist).where(Watchlist.ticker == "BTC-USD")
            .where(Watchlist.source == "crypto_trending")
        ).first()
        bad = s.exec(
            select(Watchlist).where(Watchlist.ticker == "ZZZBAD-USD")
        ).first()
        assert btc is not None
        assert bad is None
