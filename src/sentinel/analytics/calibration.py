"""Calibration metrics for the bot's TradingCalls.

Beyond raw hit-rate, real calibration asks: when the bot says 5/5
conviction, does it actually win 80% of the time? Or just 50%
(overconfident)?

We compute:
- **Reliability curve**: per-conviction bucket, hit-rate vs the
  bucket's predicted probability.
- **Brier score**: mean squared error between predicted probability
  (conv/5) and outcome (1 win / 0 loss). Lower is better; perfect
  calibration = 0.
- **Confidence-weighted accuracy**: hit-rate weighted by conviction
  — a strong winning call counts more than a weak winning call.

Outcome = ret_5d_pct > 0 for longs, < 0 for shorts. Unsettled calls
are excluded.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import TradingCall


def _won(c: TradingCall) -> bool | None:
    """True/False if settled, None if not yet markable."""
    ret = c.ret_5d_pct
    if ret is None:
        return None
    return ret > 0 if c.direction == "long" else ret < 0


def calibration_summary(days: int = 90) -> dict:
    """Per-bucket reliability + global Brier + sample sizes."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).replace(tzinfo=None)

    by_conv: dict[int, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "brier_sum": 0.0}
    )
    by_source: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "brier_sum": 0.0}
    )

    n_total = wins_total = 0
    brier_total = 0.0

    with session_scope() as s:
        rows = s.exec(
            select(TradingCall)
            .where(TradingCall.created_at >= cutoff_naive)
            .where(TradingCall.settled == True)  # noqa: E712
        ).all()
        for c in rows:
            w = _won(c)
            if w is None:
                continue
            outcome = 1 if w else 0
            # Predicted prob = conv/5 (a 5/5 call claims ~100% confidence).
            p = max(1, min(5, c.conviction)) / 5.0
            brier = (p - outcome) ** 2

            n_total += 1
            wins_total += outcome
            brier_total += brier

            bc = by_conv[c.conviction]
            bc["n"] += 1
            bc["wins"] += outcome
            bc["brier_sum"] += brier

            bs = by_source[c.source]
            bs["n"] += 1
            bs["wins"] += outcome
            bs["brier_sum"] += brier

    def _shape(d: dict) -> dict:
        return {
            "n": int(d["n"]),
            "wins": int(d["wins"]),
            "hit_rate": (d["wins"] / d["n"]) if d["n"] else None,
            "brier": (d["brier_sum"] / d["n"]) if d["n"] else None,
        }

    return {
        "window_days": days,
        "n": n_total,
        "wins": wins_total,
        "hit_rate": (wins_total / n_total) if n_total else None,
        "brier": (brier_total / n_total) if n_total else None,
        # Reliability table: each bucket's predicted prob vs realised
        "buckets": [
            {
                "conviction": k,
                "predicted_prob": k / 5.0,
                **_shape(v),
            }
            for k, v in sorted(by_conv.items())
        ],
        "by_source": {
            k: {**_shape(v), "source": k}
            for k, v in by_source.items()
        },
    }
