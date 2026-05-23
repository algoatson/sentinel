"""Hot-movers triage contract.

The `_is_hot` predicate and the market-hours/crypto gate decide what
reaches #hot — wrong thresholds turn the channel into either dead silence
or pure noise. These pin the exact behaviour so a future tweak doesn't
silently shift it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sentinel.pipelines import hot_movers as hm


def _pc(*, pct: float, vol: float):
    """A minimal PriceContext-shaped object — the predicate only reads two
    fields, so a SimpleNamespace stays test-fast without DB."""
    return SimpleNamespace(change_1d_pct=pct, volume_vs_20d_avg=vol)


def test_is_hot_large_move_passes_on_any_volume():
    # ≥4% absolute — no volume requirement
    assert hm._is_hot(_pc(pct=0.05, vol=0.0)) is True
    assert hm._is_hot(_pc(pct=-0.06, vol=1.0)) is True
    # under threshold, even with volume
    assert hm._is_hot(_pc(pct=0.03, vol=1.0)) is False


def test_is_hot_small_move_needs_volume_kicker():
    # 2–4% range requires ≥1.8× 20d avg volume
    assert hm._is_hot(_pc(pct=0.025, vol=2.0)) is True
    assert hm._is_hot(_pc(pct=-0.03, vol=1.9)) is True
    # 2-4% with weak volume → not hot
    assert hm._is_hot(_pc(pct=0.025, vol=1.0)) is False
    assert hm._is_hot(_pc(pct=0.03, vol=1.5)) is False


def test_is_hot_none_fields_are_safe():
    # PriceContext rows can have None numerics on a fresh row
    assert hm._is_hot(_pc(pct=None, vol=None)) is False
    assert hm._is_hot(_pc(pct=0.0, vol=None)) is False


def test_cooldown_blocks_within_window_and_allows_after():
    hm._LAST_POSTED.clear()
    now = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
    assert hm._cooldown_ok("NVDA", now) is True
    hm._LAST_POSTED["NVDA"] = now
    # within window
    assert hm._cooldown_ok("NVDA",
                           now + timedelta(hours=hm._COOLDOWN_HOURS - 1)
                           ) is False
    # at exactly the window — boundary should release
    assert hm._cooldown_ok("NVDA",
                           now + timedelta(hours=hm._COOLDOWN_HOURS)
                           ) is True


def test_market_open_now_gates_weekday_window():
    # Mon at 10:00 ET = 14:00 UTC → open
    open_mon = datetime(2026, 5, 18, 14, 0, tzinfo=timezone.utc)
    assert hm._market_open_now(open_mon) is True
    # Mon at 03:00 ET = 07:00 UTC → before open
    pre = datetime(2026, 5, 18, 7, 0, tzinfo=timezone.utc)
    assert hm._market_open_now(pre) is False
    # Sat at 14:00 ET = 18:00 UTC → weekend
    sat = datetime(2026, 5, 23, 18, 0, tzinfo=timezone.utc)
    assert hm._market_open_now(sat) is False


def test_line_renders_arrow_and_flags():
    # green up + filing flag
    out = hm._line({
        "ticker": "NVDA", "last_price": 250.0,
        "change_1d_pct": 4.2, "vol_ratio": 2.1,
        "had_filing": True, "had_news": False,
    })
    assert "🟢" in out and "$NVDA" in out and "📑" in out and "📰" not in out
    # red down + news flag
    out = hm._line({
        "ticker": "AAPL", "last_price": 175.0,
        "change_1d_pct": -3.5, "vol_ratio": 1.9,
        "had_filing": False, "had_news": True,
    })
    assert "🔴" in out and "📰" in out and "📑" not in out
