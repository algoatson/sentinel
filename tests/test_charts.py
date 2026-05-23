"""Chart-spec contracts.

Most of the spec builders are dictionary scaffolding (untested — the
output is consumed by ECharts client-side, so the only "wrong" thing
visible from Python is the structural shape). The SMA helper IS
stateful and easy to break in a refactor; pin its window/edge behavior.
"""

from __future__ import annotations

from sentinel.dashboard import charts


def test_sma_returns_none_until_window_fills():
    # Each candle is [open, close, low, high]; SMA reads close (idx 1).
    candles = [[100, c, 99, 101] for c in (1, 2, 3, 4, 5)]
    out = charts._sma(candles, 3)
    assert out[0] is None and out[1] is None  # need ≥ window samples
    # window of 3 starting at index 2: mean(1,2,3) = 2
    assert out[2] == 2.0
    assert out[3] == 3.0          # mean(2,3,4)
    assert out[4] == 4.0          # mean(3,4,5)


def test_sma_handles_window_larger_than_input():
    # Fewer bars than `window` → entire output is None (no partial mean)
    out = charts._sma([[100, 10, 99, 101]] * 3, window=20)
    assert all(v is None for v in out)


def test_candlestick_spec_includes_y_axis_zoom_with_filter_none():
    # The Y-zoom MUST have `filterMode: 'none'` — without it, zooming the
    # price range would HIDE bars outside the viewport (breaking the chart)
    # instead of just rescaling the axis. Regression pin.
    data = {
        "ticker": "X",
        "bars": [
            {"ts": "2026-05-20T00:00:00+00:00",
             "open": 100, "high": 105, "low": 99, "close": 103,
             "volume": 1000},
            {"ts": "2026-05-21T00:00:00+00:00",
             "open": 103, "high": 110, "low": 102, "close": 108,
             "volume": 1200},
        ],
        "open_position": None, "closed": [],
    }
    spec = charts.candlestick_spec(data)
    zooms = spec.get("dataZoom") or []
    y_zoom = next((z for z in zooms if "yAxisIndex" in z), None)
    assert y_zoom is not None, "Y-axis dataZoom missing"
    assert y_zoom.get("filterMode") == "none"


def test_candlestick_spec_adds_ma20_only_when_enough_bars():
    base = {"open_position": None, "closed": [], "ticker": "X"}
    # 5 bars → no MA20 series
    base["bars"] = [
        {"ts": f"2026-05-{20+i:02d}T00:00:00+00:00",
         "open": 100, "high": 105, "low": 99, "close": 100 + i,
         "volume": 1000}
        for i in range(5)
    ]
    spec = charts.candlestick_spec(base)
    names = [s.get("name") for s in spec["series"]]
    assert "MA20" not in names
    # 20 bars → MA20 present
    base["bars"] = [
        {"ts": f"2026-05-{i:02d}T00:00:00+00:00",
         "open": 100, "high": 105, "low": 99, "close": 100 + i,
         "volume": 1000}
        for i in range(1, 21)
    ]
    spec = charts.candlestick_spec(base)
    names = [s.get("name") for s in spec["series"]]
    assert "MA20" in names
