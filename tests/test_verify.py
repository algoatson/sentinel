"""Deterministic fact-verifier core (`verify.check_claims`).

The discipline IS the contract: only ground-truthable ticker-bound numbers
are ever checked, tolerances are exact and configurable, and the layer errs
toward 'unverifiable' over a false 'contradicted'. Every tolerance edge,
the %-move unit conversion (DB stores fractions, the model states pp), the
direction sign rule, and the fail-soft cases (no row / stale / unknown
metric / empty) are pinned here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.config import settings
from sentinel.db import session_scope
from sentinel.models import PriceContext
from sentinel.verify import Claim, check_claims

UTC = timezone.utc


def _seed(
    ticker="AAPL",
    *,
    last_price=200.0,
    change_1d_pct=0.10,
    change_5d_pct=0.05,
    volume_vs_20d_avg=2.0,
    age_hours=1.0,
):
    """Insert one fresh PriceContext row. change_*_pct are FRACTIONS."""
    with session_scope() as s:
        s.add(
            PriceContext(
                ticker=ticker,
                last_price=last_price,
                change_1d_pct=change_1d_pct,
                change_5d_pct=change_5d_pct,
                volume_vs_20d_avg=volume_vs_20d_avg,
                last_updated=datetime.now(UTC).replace(tzinfo=None)
                - timedelta(hours=age_hours),
            )
        )


# ── empty / structural ──────────────────────────────────────────────────────


def test_empty_claims_grounded_ok():
    r = check_claims([])
    assert r.grounded is True
    assert r.ok is True
    assert r.n_checked == 0


def test_ticker_absent_is_unverifiable():
    r = check_claims([Claim(ticker="ZZZZ", metric="price", value=100.0)])
    assert r.n_unverifiable == 1
    assert r.n_contradicted == 0
    assert r.grounded is True  # nothing contradicted → still grounded


def test_unknown_metric_is_unverifiable():
    _seed()
    r = check_claims([Claim(ticker="AAPL", metric="market_cap", value=3e12)])
    assert r.verdicts[0].status == "unverifiable"
    assert r.grounded is True


def test_stale_context_is_unverifiable():
    _seed(age_hours=settings.VERIFY_CONTEXT_STALE_HOURS + 5)
    r = check_claims([Claim(ticker="AAPL", metric="price", value=200.0)])
    assert r.verdicts[0].status == "unverifiable"
    assert "stale" in r.verdicts[0].detail


# ── price tolerance edges ────────────────────────────────────────────────────


def test_price_just_inside_tol_supported():
    _seed(last_price=200.0)  # 2% tol → 196..204
    r = check_claims([Claim(ticker="AAPL", metric="price", value=203.9)])
    assert r.verdicts[0].status == "supported"
    assert r.grounded is True


def test_price_just_outside_tol_contradicted():
    _seed(last_price=200.0)
    r = check_claims([Claim(ticker="AAPL", metric="price", value=205.0)])
    assert r.verdicts[0].status == "contradicted"
    assert r.grounded is False
    assert r.note  # worst-contradiction summary populated


def test_price_exactly_at_tol_edge_supported():
    _seed(last_price=200.0)  # exactly 2% = 204.0
    r = check_claims([Claim(ticker="AAPL", metric="price", value=204.0)])
    assert r.verdicts[0].status == "supported"


# ── %-move: absolute pp + relative edges + unit conversion ──────────────────


def test_pct_move_absolute_pp_edge():
    # actual 1d = +10.00pp; tol = 1.5pp → 11.5 supported, 11.6 needs relative
    _seed(change_1d_pct=0.10)
    assert (
        check_claims([Claim(ticker="AAPL", metric="change_1d_pct", value=11.5)])
        .verdicts[0].status == "supported"
    )


def test_pct_move_relative_band_rescues_large_move():
    # actual = +10pp, stated 12pp: 2pp > 1.5pp absolute, but 2 <= 25%*10=2.5
    _seed(change_1d_pct=0.10)
    r = check_claims([Claim(ticker="AAPL", metric="change_1d_pct", value=12.0)])
    assert r.verdicts[0].status == "supported"


def test_pct_move_outside_both_bands_contradicted():
    # actual = +10pp, stated 13pp: 3pp > 1.5pp and 3 > 2.5 relative
    _seed(change_1d_pct=0.10)
    r = check_claims([Claim(ticker="AAPL", metric="change_1d_pct", value=13.0)])
    assert r.verdicts[0].status == "contradicted"


def test_pct_small_move_uses_absolute_band():
    # actual = +0.40pp; 25% relative is only 0.1pp, but 1.5pp absolute rescues
    _seed(change_5d_pct=0.004)
    r = check_claims([Claim(ticker="AAPL", metric="change_5d_pct", value=1.5)])
    assert r.verdicts[0].status == "supported"


# ── volume multiple edge ─────────────────────────────────────────────────────


def test_vol_mult_inside_band_supported():
    _seed(volume_vs_20d_avg=2.0)  # tol 0.5 → 1.5..2.5
    assert (
        check_claims([Claim(ticker="AAPL", metric="vol_mult", value=2.5)])
        .verdicts[0].status == "supported"
    )


def test_vol_mult_outside_band_contradicted():
    _seed(volume_vs_20d_avg=2.0)
    assert (
        check_claims([Claim(ticker="AAPL", metric="vol_mult", value=3.1)])
        .verdicts[0].status == "contradicted"
    )


# ── direction sign ───────────────────────────────────────────────────────────


def test_direction_match_supported():
    _seed(change_1d_pct=0.03)
    r = check_claims([Claim(ticker="AAPL", metric="direction", direction_word="up")])
    assert r.verdicts[0].status == "supported"


def test_direction_mismatch_always_contradicted():
    _seed(change_1d_pct=0.03)  # up day
    r = check_claims([Claim(ticker="AAPL", metric="direction", direction_word="down")])
    assert r.verdicts[0].status == "contradicted"
    assert r.grounded is False


def test_direction_flat_is_unverifiable():
    _seed(change_1d_pct=0.0)
    r = check_claims([Claim(ticker="AAPL", metric="direction", direction_word="up")])
    assert r.verdicts[0].status == "unverifiable"


def test_direction_missing_word_is_unverifiable():
    _seed(change_1d_pct=0.03)
    r = check_claims([Claim(ticker="AAPL", metric="direction", direction_word=None)])
    assert r.verdicts[0].status == "unverifiable"


# ── aggregation ──────────────────────────────────────────────────────────────


def test_mixed_batch_aggregates_and_flags():
    _seed("AAPL", last_price=200.0, change_1d_pct=0.10)
    r = check_claims(
        [
            Claim(ticker="AAPL", metric="price", value=201.0),        # supported
            Claim(ticker="AAPL", metric="price", value=260.0),        # contradicted
            Claim(ticker="ZZZZ", metric="price", value=10.0),         # unverifiable
        ]
    )
    assert (r.n_supported, r.n_contradicted, r.n_unverifiable) == (1, 1, 1)
    assert r.n_checked == 3
    assert r.grounded is False
