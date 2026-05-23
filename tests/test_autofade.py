"""Auto-fade control loop.

`_fade_conviction` turns the scorecard from a passive report into a control
signal: a measurably weak source gets dampened at the `record_call`
chokepoint, which propagates everywhere. The discipline is the contract —
fade-only, sample-gated, never below 1, never inflate — so it's pinned here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel import scorecard
from sentinel.db import session_scope
from sentinel.models import TradingCall

UTC = timezone.utc


# ── pure _fade_conviction ───────────────────────────────────────────────────


def test_no_history_or_below_sample_is_untouched():
    assert scorecard._fade_conviction("x", 4, {}) == (4, None)
    # below the sample floor — never act on noise, however bad it looks
    assert scorecard._fade_conviction(
        "x", 4, {"x": {"hits": 0, "n": 5}}
    ) == (4, None)


def test_good_or_marginal_source_is_never_touched():
    assert scorecard._fade_conviction(
        "x", 5, {"x": {"hits": 15, "n": 20}}
    ) == (5, None)                                   # 75% — fade-only
    assert scorecard._fade_conviction(
        "x", 4, {"x": {"hits": 46, "n": 100}}
    ) == (4, None)                                   # 46% — in the noise band


def test_weak_source_is_dampened_proportionally():
    a, n1 = scorecard._fade_conviction("x", 4, {"x": {"hits": 42, "n": 100}})
    assert a == 3 and "faded x 42/100 (42%)" in n1   # mild → -1
    a, _ = scorecard._fade_conviction("x", 4, {"x": {"hits": 36, "n": 100}})
    assert a == 2                                    # worse → -2


def test_catastrophic_source_floors_to_one_regardless():
    a, note = scorecard._fade_conviction("x", 5, {"x": {"hits": 20, "n": 100}})
    assert a == 1 and note is not None               # 20% → floor, not 0
    # already-minimal conviction: no change, no spurious note
    assert scorecard._fade_conviction(
        "x", 1, {"x": {"hits": 20, "n": 100}}
    ) == (1, None)


# ── end-to-end through record_call (the chokepoint) ─────────────────────────


def test_record_call_fades_a_measured_loser_and_spares_a_clean_source():
    now = datetime.now(UTC)
    with session_scope() as s:
        for i in range(14):  # 14 scored losing longs from why_moved
            s.add(TradingCall(
                ticker=f"OLD{i}", direction="long", source="why_moved",
                thesis="t", conviction=4, price_at_call=100.0,
                ret_5d_pct=-5.0, settled=True,
                created_at=now - timedelta(days=3),
            ))

    scorecard.record_call("NEWT", "long", "why_moved", "fresh idea", 4)
    scorecard.record_call("CLEANX", "long", "synthesis", "untracked", 4)

    with session_scope() as s:
        faded = s.exec(
            select(TradingCall).where(TradingCall.ticker == "NEWT")
        ).first()
        clean = s.exec(
            select(TradingCall).where(TradingCall.ticker == "CLEANX")
        ).first()
    # why_moved measured 0/14 → floored, tagged; still recorded (recoverable)
    assert faded is not None and faded.conviction == 1
    assert "⚖︎ faded why_moved" in faded.thesis
    # synthesis has no measured history → completely untouched
    assert clean is not None and clean.conviction == 4
    assert "⚖︎" not in clean.thesis
