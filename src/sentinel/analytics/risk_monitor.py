"""Book-level risk monitor — what's the book *actually* exposed to right now.

Reuses `funds.open_positions_all()` (same query the /book page uses,
already enriched with distance_to_stop/target + r_multiple) and
distills it into one summary suitable for the Overview "Risk" card.

The aim is to surface the questions a trader checks first thing every
morning — without opening every position drawer:

  * How many positions are within striking distance of a stop?
  * How many are within striking distance of a target?
  * What is the total $ at risk if every stop hits today?
  * Of the book, how much equity is "in jeopardy" vs running freely?

No new ingest, no extra queries beyond the already-batched one.
"""

from __future__ import annotations

from .. import funds as _funds


# A position is "near" its stop/target when the current mark is
# within this percentage of it. 1.5% is a tradition for swing
# trading — wide enough to catch the wobble, tight enough to mean
# "watch this one today".
_NEAR_PCT = 1.5


def risk_snapshot() -> dict:
    rows = _funds.open_positions_all()
    if not rows:
        return {
            "n_open": 0,
            "n_with_stop": 0,
            "n_with_target": 0,
            "n_near_stop": 0,
            "n_near_target": 0,
            "n_underwater": 0,
            "n_in_profit": 0,
            "dollar_at_risk": 0.0,
            "pct_book_at_risk": 0.0,
            "avg_r_multiple": None,
            "median_dist_to_stop_pct": None,
            "biggest_winner": None,
            "biggest_loser": None,
            "near_stop": [],
            "near_target": [],
            "naked": [],   # open positions without a stop set
            "near_pct": _NEAR_PCT,
        }

    n_open = len(rows)
    n_with_stop = sum(1 for r in rows if r.get("stop_price"))
    n_with_target = sum(1 for r in rows if r.get("target_price"))
    n_near_stop = sum(
        1 for r in rows
        if (d := r.get("dist_to_stop_pct")) is not None and 0 <= d <= _NEAR_PCT
    )
    n_near_target = sum(
        1 for r in rows
        if (d := r.get("dist_to_target_pct")) is not None and 0 <= d <= _NEAR_PCT
    )
    n_underwater = sum(1 for r in rows if (r.get("upnl") or 0) < 0)
    n_in_profit = sum(1 for r in rows if (r.get("upnl") or 0) > 0)

    # Total $ that would be lost if every stop triggered exactly
    # at its price. Positions without a stop contribute 0 here — we
    # surface them separately as "naked" so they're obviously
    # missing risk management, not silently inflating the at-risk
    # number.
    dollar_at_risk = 0.0
    for r in rows:
        stop = r.get("stop_price")
        if not stop:
            continue
        d = 1 if r["side"] == "long" else -1
        # Per-share loss if filled at the stop, times qty.
        loss = (r["entry"] - stop) * d * r["qty"]
        # Only count if it represents an actual loss (a stop above
        # entry on a long means the trade is now risk-free).
        if loss > 0:
            dollar_at_risk += loss

    # Total book notional — denominator for "what % of capital is
    # currently exposed if everything blows up today". Use entry
    # cost rather than current mark; consistent with how the
    # `pct_of_equity` column was computed upstream.
    notional = sum(r.get("notional", 0.0) for r in rows) or 1.0
    pct_book_at_risk = round(dollar_at_risk / notional * 100, 1)

    rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    avg_r = round(sum(rs) / len(rs), 2) if rs else None

    dists = sorted(
        r["dist_to_stop_pct"] for r in rows
        if r.get("dist_to_stop_pct") is not None
    )
    med_dist = (
        round(dists[len(dists) // 2], 2)
        if dists else None
    )

    by_upnl = sorted(rows, key=lambda r: r.get("upnl") or 0.0)
    biggest_loser = by_upnl[0] if by_upnl and (by_upnl[0].get("upnl") or 0) < 0 else None
    biggest_winner = by_upnl[-1] if by_upnl and (by_upnl[-1].get("upnl") or 0) > 0 else None

    def _slim(r: dict) -> dict:
        return {
            "id": r["id"],
            "fund": r["fund"],
            "ticker": r["ticker"],
            "side": r["side"],
            "upnl": r.get("upnl"),
            "upnl_pct": r.get("upnl_pct"),
            "mark": r.get("mark"),
            "stop_price": r.get("stop_price"),
            "target_price": r.get("target_price"),
            "dist_to_stop_pct": r.get("dist_to_stop_pct"),
            "dist_to_target_pct": r.get("dist_to_target_pct"),
            "r_multiple": r.get("r_multiple"),
        }

    near_stop = sorted(
        (r for r in rows
         if (d := r.get("dist_to_stop_pct")) is not None and 0 <= d <= _NEAR_PCT),
        key=lambda r: r["dist_to_stop_pct"],
    )
    near_target = sorted(
        (r for r in rows
         if (d := r.get("dist_to_target_pct")) is not None and 0 <= d <= _NEAR_PCT),
        key=lambda r: r["dist_to_target_pct"],
    )
    naked = [r for r in rows if not r.get("stop_price")]

    return {
        "n_open": n_open,
        "n_with_stop": n_with_stop,
        "n_with_target": n_with_target,
        "n_near_stop": n_near_stop,
        "n_near_target": n_near_target,
        "n_underwater": n_underwater,
        "n_in_profit": n_in_profit,
        "dollar_at_risk": round(dollar_at_risk, 2),
        "pct_book_at_risk": pct_book_at_risk,
        "avg_r_multiple": avg_r,
        "median_dist_to_stop_pct": med_dist,
        "biggest_winner": _slim(biggest_winner) if biggest_winner else None,
        "biggest_loser": _slim(biggest_loser) if biggest_loser else None,
        "near_stop": [_slim(r) for r in near_stop[:6]],
        "near_target": [_slim(r) for r in near_target[:6]],
        "naked": [_slim(r) for r in naked[:6]],
        "near_pct": _NEAR_PCT,
    }
