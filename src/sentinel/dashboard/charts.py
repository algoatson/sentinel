"""ECharts spec builders for the dashboard.

The dashboard layer stays presentational — these helpers take the
*structured* output of `portfolio.position_chart`, `funds.equity_curve`,
`portfolio.realized_curve` and translate it to an Apache ECharts option
dict. Colours come from the same palette as the rest of the dashboard
(near-black bg, low-alpha white borders, --primary blue, --good/--bad)
so the charts feel native rather than embedded.

Each builder returns a `dict` ready for `ui.echart(spec)`. An empty/no-
data input doesn't blow up — it returns a chart that just shows a
centred placeholder string, so a fresh DB still renders cleanly.
"""

from __future__ import annotations

# ── shared palette tokens (must stay aligned with _THEME_CSS in app.py) ─
_BG = "transparent"
_AXIS = "rgba(255,255,255,.085)"
_LABEL = "rgba(255,255,255,.62)"
_GRID = "rgba(255,255,255,.05)"
_TOOLTIP_BG = "rgba(19,20,22,.95)"
_TOOLTIP_BORDER = "rgba(255,255,255,.10)"
_TEXT = "#f0f0f1"
_UP = "#3ddc97"
_DOWN = "#ff6b6b"
_PRIMARY = "#6699ff"


def _empty_spec(message: str) -> dict:
    """Centred-text chart for the no-data state — preserves layout so the
    section doesn't collapse on fresh DBs."""
    return {
        "backgroundColor": _BG,
        "title": {
            "text": message,
            "left": "center",
            "top": "center",
            "textStyle": {
                "color": "rgba(255,255,255,.42)",
                "fontSize": 13,
                "fontWeight": "normal",
            },
        },
    }


def _tooltip() -> dict:
    return {
        "trigger": "axis",
        "axisPointer": {"type": "cross",
                        "lineStyle": {"color": _AXIS}},
        "backgroundColor": _TOOLTIP_BG,
        "borderColor": _TOOLTIP_BORDER,
        "borderWidth": 1,
        "padding": 8,
        "textStyle": {"color": _TEXT, "fontSize": 12},
    }


def _axis_x(data: list[str], *, grid_index: int = 0,
            show_label: bool = True) -> dict:
    return {
        "type": "category",
        "data": data,
        "gridIndex": grid_index,
        "axisLine": {"lineStyle": {"color": _AXIS}},
        "axisLabel": ({"color": _LABEL, "fontSize": 10}
                      if show_label else {"show": False}),
        "splitLine": {"show": False},
        "axisTick": {"show": False},
    }


def _axis_y(*, grid_index: int = 0, show_label: bool = True) -> dict:
    return {
        "scale": True,
        "gridIndex": grid_index,
        "axisLine": {"lineStyle": {"color": _AXIS}},
        "axisLabel": ({"color": _LABEL, "fontSize": 10}
                      if show_label else {"show": False}),
        "splitLine": {"lineStyle": {"color": _GRID}},
    }


# ── candlestick + volume + position markers ───────────────────────────────

def candlestick_spec(data: dict) -> dict:
    """OHLC candlestick + volume sub-grid + entry/exit markers from the
    open/closed paper positions on the ticker. `data` comes straight from
    `portfolio.position_chart(ticker)`."""
    bars = data.get("bars", [])
    ticker = data.get("ticker") or ""
    if not bars:
        return _empty_spec(
            f"No price history for ${ticker}" if ticker
            else "Pick a ticker to load a chart"
        )

    # ECharts candlestick wants [open, close, low, high] per bar.
    dates = [b["ts"][:10] for b in bars]
    candles = [[b["open"], b["close"], b["low"], b["high"]] for b in bars]
    volumes = [
        {
            "value": b["volume"],
            "itemStyle": {
                "color": ("rgba(61,220,151,.45)" if b["close"] >= b["open"]
                          else "rgba(255,107,107,.45)"),
            },
        }
        for b in bars
    ]

    # markers — open position (big chip) + closed entries/exits (small dots)
    markers: list[dict] = []
    op = data.get("open_position")
    if op and op.get("entry_at"):
        side = op["side"]
        markers.append({
            "name": "entry",
            "coord": [op["entry_at"][:10], op["entry"]],
            "value": f'{"▲" if side == "long" else "▼"} {side.upper()}',
            "symbol": "pin",
            "symbolSize": 52,
            "itemStyle": {"color": _UP if side == "long" else _DOWN},
            "label": {"color": "#0d0e10", "fontSize": 9,
                      "fontWeight": "bold"},
        })
    for cl in data.get("closed") or []:
        if cl.get("entry_at"):
            markers.append({
                "name": "in",
                "coord": [cl["entry_at"][:10], cl["entry"]],
                "value": "in",
                "symbol": "circle",
                "symbolSize": 12,
                "itemStyle": {"color": "rgba(255,255,255,.55)"},
                "label": {"show": False},
            })
        if cl.get("exit_at") and cl.get("exit") is not None:
            pnl = cl.get("pnl") or 0
            markers.append({
                "name": "out",
                "coord": [cl["exit_at"][:10], cl["exit"]],
                "value": "out",
                "symbol": "circle",
                "symbolSize": 12,
                "itemStyle": {
                    "color": _UP if pnl >= 0 else _DOWN,
                    "opacity": 0.8,
                },
                "label": {"show": False},
            })

    candle_series: dict = {
        "name": "OHLC",
        "type": "candlestick",
        "data": candles,
        "itemStyle": {
            "color": _UP, "color0": _DOWN,
            "borderColor": _UP, "borderColor0": _DOWN,
        },
    }
    if markers:
        candle_series["markPoint"] = {
            "label": {"color": "#fff", "fontSize": 9},
            "data": markers,
        }

    return {
        "backgroundColor": _BG,
        "animation": False,
        "tooltip": _tooltip(),
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "grid": [
            {"left": 56, "right": 14, "top": 14, "height": "60%"},
            {"left": 56, "right": 14, "top": "76%", "height": "18%"},
        ],
        "xAxis": [
            _axis_x(dates, grid_index=0),
            _axis_x(dates, grid_index=1, show_label=False),
        ],
        "yAxis": [
            _axis_y(grid_index=0),
            _axis_y(grid_index=1, show_label=False),
        ],
        "dataZoom": [
            {"type": "inside", "xAxisIndex": [0, 1], "start": 55, "end": 100},
            {
                "show": True,
                "xAxisIndex": [0, 1],
                "type": "slider",
                "bottom": 4,
                "height": 16,
                "borderColor": _AXIS,
                "backgroundColor": "rgba(255,255,255,.014)",
                "fillerColor": "rgba(102,153,255,.18)",
                "handleStyle": {"color": "rgba(255,255,255,.5)"},
                "moveHandleStyle": {"color": "rgba(255,255,255,.18)"},
                "textStyle": {"color": _LABEL, "fontSize": 9},
                "start": 55, "end": 100,
            },
        ],
        "series": [
            candle_series,
            {
                "name": "Volume",
                "type": "bar",
                "xAxisIndex": 1,
                "yAxisIndex": 1,
                "data": volumes,
                "barWidth": "70%",
            },
        ],
    }


# ── multi-line equity curve (one line per fund) ───────────────────────────

# Stable per-fund colour assignment so the legend doesn't shuffle between
# refreshes. Order picked to be readable on dark and distinct from each
# other (avoiding green/red collision with up/down semantics).
_FUND_COLOURS = (
    "#6699ff",   # primary blue
    "#f59e0b",   # amber
    "#a78bfa",   # violet
    "#22d3ee",   # cyan
    "#fbbf24",   # warm yellow
    "#ec4899",   # pink
    "#84cc16",   # lime
    "#fb7185",   # rose
)


def equity_curve_spec(funds_data: list[dict]) -> dict:
    """Multi-line equity curve: one line per fund. Data from
    `funds.equity_curve()`. Empty/all-empty input → placeholder."""
    if not funds_data or all(not f.get("points") for f in funds_data):
        return _empty_spec("No equity history yet — funds mark each cycle")

    # Union of timestamps so all series share the x-axis; we fill missing
    # points with `None` (ECharts gaps them rather than connecting through).
    all_ts = sorted({
        p["ts"] for f in funds_data for p in f.get("points", [])
    })
    labels = [t[:16].replace("T", " ") for t in all_ts]

    series: list[dict] = []
    for i, f in enumerate(funds_data):
        if not f.get("points"):
            continue
        m = {p["ts"]: p["equity"] for p in f["points"]}
        series.append({
            "name": f["fund"],
            "type": "line",
            "smooth": True,
            "showSymbol": False,
            "data": [m.get(t) for t in all_ts],
            "lineStyle": {"width": 1.7,
                          "color": _FUND_COLOURS[i % len(_FUND_COLOURS)]},
            "itemStyle": {"color": _FUND_COLOURS[i % len(_FUND_COLOURS)]},
        })
    return {
        "backgroundColor": _BG,
        "animation": False,
        "tooltip": _tooltip(),
        "legend": {
            "data": [s["name"] for s in series],
            "textStyle": {"color": _LABEL, "fontSize": 11},
            "top": 0,
            "icon": "roundRect",
        },
        "grid": {"left": 56, "right": 14, "top": 32, "bottom": 30},
        "xAxis": _axis_x(labels),
        "yAxis": _axis_y(),
        "series": series,
    }


# ── cumulative realised P&L line ──────────────────────────────────────────

def realized_curve_spec(points: list[dict]) -> dict:
    """Cumulative realised P&L line — one point per closed trade."""
    if not points:
        return _empty_spec("No closed trades yet")
    labels = [p["ts"][:10] for p in points]
    values = [p["cumulative"] for p in points]
    last = values[-1] if values else 0
    line_colour = _UP if last >= 0 else _DOWN
    return {
        "backgroundColor": _BG,
        "animation": False,
        "tooltip": _tooltip(),
        "grid": {"left": 56, "right": 14, "top": 14, "bottom": 28},
        "xAxis": _axis_x(labels),
        "yAxis": _axis_y(),
        "series": [{
            "name": "Realized P&L",
            "type": "line",
            "smooth": True,
            "showSymbol": False,
            "areaStyle": {"opacity": 0.20, "color": line_colour},
            "lineStyle": {"width": 2, "color": line_colour},
            "itemStyle": {"color": line_colour},
            "markLine": {
                "symbol": "none",
                "lineStyle": {"color": "rgba(255,255,255,.18)",
                              "type": "dashed", "width": 1},
                "data": [{"yAxis": 0}],
                "label": {"show": False},
            },
            "data": values,
        }],
    }


# ── small inline sparkline (for watchlist rows, position rows, etc.) ──────

def sparkline_spec(values: list[float]) -> dict:
    """Tiny sparkline — no axes, no tooltips, just a line. Colour follows
    the sign of the last-vs-first move."""
    if not values or len(values) < 2:
        return _empty_spec("")
    colour = _UP if values[-1] >= values[0] else _DOWN
    return {
        "backgroundColor": _BG,
        "animation": False,
        "grid": {"left": 2, "right": 2, "top": 2, "bottom": 2},
        "xAxis": {"type": "category", "show": False,
                  "data": list(range(len(values)))},
        "yAxis": {"type": "value", "show": False, "scale": True},
        "series": [{
            "type": "line",
            "data": values,
            "showSymbol": False,
            "smooth": True,
            "lineStyle": {"width": 1.3, "color": colour},
            "areaStyle": {"opacity": 0.12, "color": colour},
        }],
    }
