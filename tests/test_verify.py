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


# ── persistence + telemetry ──────────────────────────────────────────────────


def test_verify_text_persists_claimcheck(monkeypatch):
    from sqlmodel import select

    from sentinel.models import ClaimCheck

    _seed("AAPL", last_price=200.0)
    _patch_llm(
        monkeypatch, '[{"ticker":"AAPL","metric":"price","value":260,"raw":"260"}]'
    )
    verify.verify_text(
        "AAPL at 260", ["AAPL"], surface="call", source="synthesis"
    )
    with session_scope() as s:
        rows = s.exec(select(ClaimCheck)).all()
    assert len(rows) == 1
    assert rows[0].surface == "call"
    assert rows[0].source == "synthesis"
    assert rows[0].ticker == "AAPL"
    assert rows[0].n_contradicted == 1
    assert rows[0].grounded is False


def test_verify_text_unavailable_writes_no_row(monkeypatch):
    from sqlmodel import select

    from sentinel.models import ClaimCheck

    _seed("AAPL")
    _patch_llm(monkeypatch, RuntimeError("down"))
    verify.verify_text("AAPL at 260", ["AAPL"], surface="post", source="x")
    with session_scope() as s:
        assert s.exec(select(ClaimCheck)).all() == []


# ── health grounding detector ────────────────────────────────────────────────


def _seed_checks(n_checked, n_contradicted, *, age_hours=1.0):
    from sentinel.models import ClaimCheck

    with session_scope() as s:
        for i in range(n_checked):
            contradicted = i < n_contradicted
            s.add(
                ClaimCheck(
                    ts=datetime.now(UTC) - timedelta(hours=age_hours),
                    surface="post",
                    source="test",
                    ticker="AAPL",
                    n_claims=1,
                    n_contradicted=1 if contradicted else 0,
                    grounded=not contradicted,
                    note="AAPL price stated 260 vs actual 200" if contradicted else "",
                    sample="x",
                )
            )


def test_grounding_detector_flags_high_rate():
    from sentinel.health import _grounding_status

    _seed_checks(20, 5)  # 25% contradicted over a real sample
    with session_scope() as s:
        st = _grounding_status(s, datetime.now(UTC))
    assert st["checked_7d"] == 20
    assert st["contradicted_7d"] == 5
    assert st["warn"] is True
    assert st["worst"]  # a contradiction note surfaced


def test_grounding_detector_quiet_below_sample():
    from sentinel.health import _grounding_status

    _seed_checks(4, 4)  # 100% contradicted but sample too small to flag
    with session_scope() as s:
        st = _grounding_status(s, datetime.now(UTC))
    assert st["warn"] is False


def test_grounding_detector_quiet_below_rate():
    from sentinel.health import _grounding_status

    _seed_checks(20, 1)  # 5% — under the 10% threshold
    with session_scope() as s:
        st = _grounding_status(s, datetime.now(UTC))
    assert st["warn"] is False


def test_health_report_includes_grounding():
    from sentinel.health import health_report

    rep = health_report()
    assert "grounding" in rep
    assert rep["grounding"]["checked_7d"] == 0  # nothing checked yet


# ── migration idempotency ────────────────────────────────────────────────────


def test_tradingcall_verify_columns_migrate_idempotently():
    from sentinel.db import _migrate_add_columns

    cols = [("grounded", "BOOLEAN"), ("verify_note", "VARCHAR")]
    # Already present from create_all; re-applying must be a no-op, not an error.
    _migrate_add_columns("tradingcall", cols)
    _migrate_add_columns("tradingcall", cols)

    from datetime import datetime as _dt

    from sqlmodel import select

    from sentinel.models import TradingCall

    with session_scope() as s:
        s.add(
            TradingCall(
                ticker="AAPL",
                direction="long",
                conviction=3,
                source="test",
                thesis="t",
                created_at=_dt.now(UTC),
                grounded=False,
                verify_note="faded figure",
            )
        )
    with session_scope() as s:
        tc = s.exec(select(TradingCall)).first()
    assert tc.grounded is False
    assert tc.verify_note == "faded figure"
