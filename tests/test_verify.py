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

from sentinel import verify
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


# ── extraction + verify_text orchestration (fake LLM) ───────────────────────


class _FakeLLM:
    def __init__(self, out):
        self._out = out

    def complete(self, *a, **k):
        if isinstance(self._out, Exception):
            raise self._out
        return self._out


def _patch_llm(monkeypatch, out):
    import sentinel.llm as _llm

    monkeypatch.setattr(_llm, "get_llm", lambda: _FakeLLM(out))


def test_extract_filters_universe_and_metric(monkeypatch):
    _patch_llm(
        monkeypatch,
        '[{"ticker":"AAPL","metric":"price","value":201,"raw":"at 201"},'
        '{"ticker":"ZZZZ","metric":"price","value":5},'             # off-universe
        '{"ticker":"AAPL","metric":"pe_ratio","value":30}]',        # bad metric
    )
    claims = verify.extract_claims("AAPL trading at 201", ["AAPL"])
    assert len(claims) == 1
    assert claims[0].ticker == "AAPL" and claims[0].metric == "price"
    assert claims[0].value == 201.0


def test_extract_coerces_string_values(monkeypatch):
    _patch_llm(
        monkeypatch,
        '[{"ticker":"AAPL","metric":"change_1d_pct","value":"+11.6%"},'
        '{"ticker":"AAPL","metric":"vol_mult","value":"2.3x"}]',
    )
    claims = verify.extract_claims("x", ["AAPL"])
    by_metric = {c.metric: c.value for c in claims}
    assert by_metric["change_1d_pct"] == 11.6
    assert by_metric["vol_mult"] == 2.3


def test_verify_text_contradiction_end_to_end(monkeypatch):
    _seed("AAPL", last_price=200.0)
    _patch_llm(
        monkeypatch,
        '[{"ticker":"AAPL","metric":"price","value":260,"raw":"at 260"}]',
    )
    r = verify.verify_text(
        "AAPL is trading at 260", ["AAPL"], surface="post", source="test"
    )
    assert r.ok is True            # extraction ran
    assert r.grounded is False     # but the figure is wrong
    assert r.n_contradicted == 1


def test_verify_text_failopen_on_extractor_error(monkeypatch):
    _seed("AAPL")
    _patch_llm(monkeypatch, RuntimeError("model down"))
    r = verify.verify_text("AAPL at 260", ["AAPL"], surface="call", source="test")
    assert r.ok is False           # extraction unavailable → unverified
    assert r.grounded is True      # nothing contradicted (nothing checked)
    assert r.n_checked == 0


def test_verify_text_llm_error_sentinel_is_unavailable(monkeypatch):
    from sentinel.llm import LLM_ERROR_SENTINEL

    _seed("AAPL")
    _patch_llm(monkeypatch, LLM_ERROR_SENTINEL)
    r = verify.verify_text("AAPL at 260", ["AAPL"], surface="post", source="test")
    assert r.ok is False
