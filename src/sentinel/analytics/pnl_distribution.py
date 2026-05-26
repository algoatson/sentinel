"""PnL distribution — histogram of realized return % per closed trade.

Trader's classic chart for spotting *the shape of the edge*: a long
fat-tail of small losses with a sharp positive tail (good — let
winners run, cut losers) versus a tall mode at small positives and
a sparse but huge left tail (bad — taking profits too quick, letting
losers blow out).

Reuses funds.closed_trades_recent() so no new query.
"""

from __future__ import annotations

import math

from .. import funds as _funds


def pnl_distribution(limit: int = 500) -> dict:
    """Bucket the realized_pct of closed trades into symmetric bins
    centred around 0%. Bin width auto-picks between 1% / 2% / 5% / 10%
    so the chart stays readable across timeframes."""
    rows = _funds.closed_trades_recent(limit=limit)
    if not rows:
        return {
            "n": 0,
            "bin_width_pct": 2,
            "bins": [],
            "mean_pct": None,
            "median_pct": None,
            "stdev_pct": None,
            "skew": None,
            "p10": None,
            "p90": None,
        }

    pcts = sorted(r["realized_pct"] for r in rows if r.get("realized_pct") is not None)
    if not pcts:
        return {"n": 0, "bin_width_pct": 2, "bins": []}

    max_abs = max(abs(min(pcts)), abs(max(pcts)))
    # Auto bin width: keep ~10-30 bins.
    if max_abs <= 15:
        bw = 1
    elif max_abs <= 40:
        bw = 2
    elif max_abs <= 100:
        bw = 5
    else:
        bw = 10
    # Symmetric range around 0 so the chart isn't visually skewed
    # off-centre when one side is bigger.
    n_side = max(1, math.ceil(max_abs / bw))
    range_max = n_side * bw

    bins: list[dict] = []
    for i in range(-n_side, n_side):
        lo = i * bw
        hi = lo + bw
        count = sum(1 for p in pcts if lo <= p < hi)
        # Closing bucket includes the rightmost value (i = n_side - 1).
        if i == n_side - 1:
            count += sum(1 for p in pcts if p == hi)
        bins.append({
            "lo": lo,
            "hi": hi,
            "count": count,
        })

    n = len(pcts)
    mean = sum(pcts) / n
    median = pcts[n // 2] if n % 2 else (pcts[n // 2 - 1] + pcts[n // 2]) / 2
    var = sum((p - mean) ** 2 for p in pcts) / n if n > 0 else 0.0
    stdev = math.sqrt(var)
    # Pearson moment-based skewness, classic g1.
    skew: float | None = None
    if n >= 3 and stdev > 0:
        m3 = sum((p - mean) ** 3 for p in pcts) / n
        skew = m3 / (stdev ** 3)
    p10_i = max(0, int(0.1 * (n - 1)))
    p90_i = max(0, int(0.9 * (n - 1)))

    return {
        "n": n,
        "bin_width_pct": bw,
        "range_max": range_max,
        "bins": bins,
        "mean_pct": round(mean, 2),
        "median_pct": round(median, 2),
        "stdev_pct": round(stdev, 2),
        "skew": round(skew, 2) if skew is not None else None,
        "p10": round(pcts[p10_i], 2),
        "p90": round(pcts[p90_i], 2),
        "best": round(pcts[-1], 2),
        "worst": round(pcts[0], 2),
    }
