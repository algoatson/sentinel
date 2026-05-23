"""book_risk contract — the copilot's most important and most dangerous
pipeline (a false ping annoys; a missed one costs money). The deterministic
gate + cooldown/escalation logic is pinned here; the LLM read on top is not
load-bearing and isn't tested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.db import session_scope
from sentinel.models import NarrativeEvent, PaperTrade, PriceContext
from sentinel.narrative import record_event
from sentinel.pipelines import book_risk

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _pos(ticker, side, entry, mark, *, qty=10):
    with session_scope() as s:
        s.add(PaperTrade(
            ticker=ticker, side=side, qty=qty, entry_price=entry,
            entry_at=_now() - timedelta(days=1), status="open",
            opened_by="manual",
        ))
        if mark is not None:
            s.add(PriceContext(
                ticker=ticker, last_price=mark, change_1d_pct=0.0,
                change_5d_pct=0.0, volume_vs_20d_avg=1.0, last_updated=_now(),
            ))


_NO_EARNINGS = lambda _t: None  # noqa: E731


# ── pure helpers ────────────────────────────────────────────────────────────


def test_dd_bucket_thresholds_and_none():
    assert book_risk._dd_bucket(None) == 0
    assert book_risk._dd_bucket(-5.0) == 0
    assert book_risk._dd_bucket(-8.0) == 1   # boundary inclusive
    assert book_risk._dd_bucket(-16.0) == 2
    assert book_risk._dd_bucket(-30.0) == 3
    assert book_risk._dd_bucket(-50.0) == 4


def test_sev_token_roundtrips_and_worse_logic():
    tok = book_risk._sev_token(2, {"drawdown", "earnings"})
    assert book_risk._parse_sev(tok) == (2, {"drawdown", "earnings"})
    assert book_risk._parse_sev("garbage") == (0, set())
    # deeper drawdown is worse; a new trigger kind is worse; same is not.
    assert book_risk._worse((3, {"drawdown"}), (1, {"drawdown"})) is True
    assert book_risk._worse((1, {"drawdown", "event"}), (1, {"drawdown"})) is True
    assert book_risk._worse((1, {"drawdown"}), (1, {"drawdown"})) is False
    assert book_risk._worse((1, {"drawdown"}), (2, {"drawdown"})) is False


# ── _assess: triggers ───────────────────────────────────────────────────────


def test_clean_book_is_silent():
    _pos("AAPL", "long", 100, 98)  # −2%, no earnings, no events
    assert book_risk._assess(_now(), earnings_of=_NO_EARNINGS) == []


def test_long_drawdown_is_flagged():
    _pos("NVDA", "long", 100, 84)  # −16% → bucket 2
    out = book_risk._assess(_now(), earnings_of=_NO_EARNINGS)
    assert len(out) == 1
    assert out[0]["ticker"] == "NVDA" and out[0]["dd"] == 2
    assert "drawdown" in out[0]["triggers"]


def test_short_drawdown_is_flagged_with_correct_sign():
    # Short @100, price ROSE to 120 → position is down; portfolio makes
    # pnl_pct negative, so the same threshold catches it.
    _pos("TSLA", "short", 100, 120)
    out = book_risk._assess(_now(), earnings_of=_NO_EARNINGS)
    assert len(out) == 1 and out[0]["dd"] == 2
    assert out[0]["pnl_pct"] < 0


def test_earnings_imminent_flags_an_otherwise_fine_position():
    _pos("MSFT", "long", 100, 101)  # P&L fine
    soon = (_now().date() + timedelta(days=2))
    out = book_risk._assess(_now(), earnings_of=lambda t: soon)
    assert len(out) == 1
    assert out[0]["triggers"].keys() == {"earnings"}


def test_far_earnings_does_not_flag():
    _pos("MSFT", "long", 100, 101)
    far = (_now().date() + timedelta(days=30))
    assert book_risk._assess(_now(), earnings_of=lambda t: far) == []


def test_fresh_material_event_flags_the_name():
    _pos("AMD", "long", 100, 99)  # P&L fine
    record_event("AMD", "why_moved", "AMD dumps on guidance cut", tier=2)
    out = book_risk._assess(_now(), earnings_of=_NO_EARNINGS)
    assert len(out) == 1 and "event" in out[0]["triggers"]


# ── _assess: cooldown / escalation ──────────────────────────────────────────


def test_cooldown_suppresses_a_non_worse_repeat():
    _pos("NVDA", "long", 100, 84)  # −16% → dd 2
    record_event(
        "NVDA", book_risk._BR_KIND, "risk flagged",
        tier=2, detail=book_risk._sev_token(2, {"drawdown"}),
    )
    # same severity, within cooldown → muted.
    assert book_risk._assess(_now(), earnings_of=_NO_EARNINGS) == []


def test_deeper_drawdown_breaks_cooldown():
    _pos("NVDA", "long", 100, 70)  # −30% → dd 3
    record_event(
        "NVDA", book_risk._BR_KIND, "risk flagged",
        tier=2, detail=book_risk._sev_token(1, {"drawdown"}),  # was shallower
    )
    out = book_risk._assess(_now(), earnings_of=_NO_EARNINGS)
    assert len(out) == 1 and out[0]["dd"] == 3


def test_new_trigger_kind_breaks_cooldown():
    _pos("NVDA", "long", 100, 91)  # −9% → dd 1
    record_event(
        "NVDA", book_risk._BR_KIND, "risk flagged",
        tier=2, detail=book_risk._sev_token(1, {"drawdown"}),
    )
    soon = _now().date() + timedelta(days=1)
    out = book_risk._assess(_now(), earnings_of=lambda t: soon)
    assert len(out) == 1
    assert set(out[0]["triggers"]) == {"drawdown", "earnings"}


def test_stale_prior_flag_outside_cooldown_re_alerts():
    _pos("NVDA", "long", 100, 84)
    with session_scope() as s:
        s.add(NarrativeEvent(
            ticker="NVDA", ts=_now() - book_risk._COOLDOWN - timedelta(hours=1),
            kind=book_risk._BR_KIND, tier=2,
            headline="old flag", detail=book_risk._sev_token(2, {"drawdown"}),
        ))
    # prior flag is older than the cooldown → a still-bad position re-alerts.
    out = book_risk._assess(_now(), earnings_of=_NO_EARNINGS)
    assert len(out) == 1 and out[0]["ticker"] == "NVDA"
