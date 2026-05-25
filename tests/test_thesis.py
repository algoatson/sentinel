"""Thesis engine contract.

Pins the cross-pollination loop end-to-end:

- Accessors round-trip (list_active / list_recent_closed / get_thesis /
  close_thesis).
- Linker: news on a watched ticker matches active theses, dedupes
  repeat links, and the sentiment-aligned impact rule is correct.
- Review cycle transitions: target hit → validated; challenge ratio
  → invalidated; horizon elapsed → matured.
- Cache aggregates on Thesis (supporting_events / challenging_events
  / last_event_at) increment correctly.

The LLM-driven `generate_cycle` is NOT exercised against a real model
here — only its validate-and-coerce path (`_validate_open`) and the
JSON parser (`_parse_generator_output`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel import thesis
from sentinel.db import session_scope
from sentinel.models import (
    Filing,
    NewsItem,
    PriceContext,
    Thesis,
    ThesisEvent,
)


# ── fixtures ─────────────────────────────────────────────────────────────


def _purge() -> None:
    with session_scope() as s:
        for row in s.exec(select(ThesisEvent)).all():
            s.delete(row)
        for row in s.exec(select(Thesis)).all():
            s.delete(row)


def _seed_active(*, ticker="NVDA", direction="long", conviction=4,
                 target_price=None, horizon_days=None,
                 supporting=0, challenging=0,
                 created_days_ago=0) -> int:
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=created_days_ago)
    with session_scope() as s:
        t = Thesis(
            ticker=ticker, direction=direction,
            title=f"{ticker} test thesis",
            body="test body",
            invalidation_criteria="something specific breaks",
            conviction=conviction,
            target_price=target_price,
            horizon_days=horizon_days,
            state="active",
            model="stub",
            created_at=created,
            updated_at=created,
            supporting_events=supporting,
            challenging_events=challenging,
        )
        s.add(t)
        s.flush()
        return t.id


def _seed_news(ticker="NVDA", sentiment=1, *, url_suffix=""):
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        n = NewsItem(
            source="rss:test",
            external_id=f"test-{ticker}-{url_suffix or sentiment}",
            title=f"Some {ticker} news",
            url=f"https://x.com/{ticker}/{url_suffix or sentiment}",
            summary="",
            ticker=ticker, is_macro=False,
            sentiment=sentiment,
            published_at=now, fetched_at=now,
        )
        s.add(n)
        s.flush()
        return n.id


def _seed_filing(ticker="NVDA", form_type="8-K", materiality=7):
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        f = Filing(
            cik="0001045810", ticker=ticker, form_type=form_type,
            accession_number=f"acc-{ticker}-{form_type}-{materiality}",
            filed_at=now, primary_doc_url="https://sec.gov/test",
            summary="test summary",
            materiality_score=materiality,
        )
        s.add(f)
        s.flush()
        return f.id


def _set_price(ticker: str, last: float) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        pc = s.get(PriceContext, ticker)
        if pc is None:
            s.add(PriceContext(
                ticker=ticker, last_price=last,
                change_1d_pct=0.0, change_5d_pct=0.0,
                volume_vs_20d_avg=1.0, last_updated=now,
            ))
        else:
            pc.last_price = last
            pc.last_updated = now
            s.add(pc)


# ── accessors ────────────────────────────────────────────────────────────


def test_list_active_returns_only_active():
    _purge()
    a = _seed_active(ticker="NVDA")
    b = _seed_active(ticker="AAPL")
    # Close b
    thesis.close_thesis(b, state="closed", reason="manual")
    out = thesis.list_active()
    ids = {t["id"] for t in out}
    assert a in ids
    assert b not in ids


def test_get_thesis_includes_events_chronological():
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long")
    nid = _seed_news("NVDA", sentiment=1)
    thesis.link_news(nid)
    d = thesis.get_thesis(tid)
    assert d is not None
    assert d["id"] == tid
    assert len(d["events"]) == 1
    assert d["events"][0]["impact"] == "supports"


def test_close_thesis_rejects_invalid_state_or_double_close():
    _purge()
    tid = _seed_active()
    # invalid state
    assert thesis.close_thesis(tid, state="nope", reason="x") is False
    # valid close
    assert thesis.close_thesis(tid, state="closed", reason="ok") is True
    # double close → False (already non-active)
    assert thesis.close_thesis(tid, state="invalidated", reason="x") is False


# ── linker: impact rule ──────────────────────────────────────────────────


def test_link_news_supports_on_long_bullish():
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long")
    nid = _seed_news("NVDA", sentiment=1)
    n = thesis.link_news(nid)
    assert n == 1
    with session_scope() as s:
        events = s.exec(
            select(ThesisEvent).where(ThesisEvent.thesis_id == tid)
        ).all()
        assert len(events) == 1
        assert events[0].impact == "supports"
        # Aggregate counter bumped
        t = s.get(Thesis, tid)
        assert t.supporting_events == 1


def test_link_news_challenges_on_long_bearish():
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long")
    nid = _seed_news("NVDA", sentiment=-1)
    thesis.link_news(nid)
    with session_scope() as s:
        t = s.get(Thesis, tid)
        assert t.challenging_events == 1
        assert t.supporting_events == 0


def test_link_news_inverts_for_short_direction():
    _purge()
    tid = _seed_active(ticker="NVDA", direction="short")
    nid = _seed_news("NVDA", sentiment=1)  # bullish news, short thesis
    thesis.link_news(nid)
    with session_scope() as s:
        t = s.get(Thesis, tid)
        assert t.challenging_events == 1


def test_link_news_neutral_when_sentiment_missing():
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long")
    nid = _seed_news("NVDA", sentiment=None)
    thesis.link_news(nid)
    with session_scope() as s:
        t = s.get(Thesis, tid)
        assert t.supporting_events == 0
        assert t.challenging_events == 0


def test_link_news_dedupes_repeat_links():
    """Re-running link_news on the same item must NOT create a second
    ThesisEvent or double-bump the aggregates."""
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long")
    nid = _seed_news("NVDA", sentiment=1)
    thesis.link_news(nid)
    thesis.link_news(nid)
    thesis.link_news(nid)
    with session_scope() as s:
        events = s.exec(
            select(ThesisEvent).where(ThesisEvent.thesis_id == tid)
        ).all()
        assert len(events) == 1
        t = s.get(Thesis, tid)
        assert t.supporting_events == 1


def test_link_news_ignores_unrelated_ticker():
    _purge()
    _seed_active(ticker="NVDA", direction="long")
    nid = _seed_news("AAPL", sentiment=1)
    n = thesis.link_news(nid)
    assert n == 0


def test_link_filing_records_neutral_for_now():
    """V1 of the filing linker tags everything as neutral (the LLM-
    classified version is a future upgrade). What matters: it still
    appears in the timeline so users can read the linked filing."""
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long")
    fid = _seed_filing("NVDA", form_type="8-K", materiality=8)
    n = thesis.link_filing(fid)
    assert n == 1
    with session_scope() as s:
        events = s.exec(
            select(ThesisEvent).where(ThesisEvent.thesis_id == tid)
        ).all()
        assert len(events) == 1
        assert events[0].impact == "neutral"
        # Description mentions the form type or summary
        assert events[0].description  # not empty


# ── review cycle transitions ─────────────────────────────────────────────


def test_review_validates_on_target_hit_long():
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long", target_price=250.0)
    _set_price("NVDA", 260.0)  # past target
    out = thesis.review_cycle()
    assert out["validated"] == 1
    with session_scope() as s:
        t = s.get(Thesis, tid)
        assert t.state == "validated"
        assert "target" in (t.close_reason or "").lower()


def test_review_validates_on_target_hit_short():
    _purge()
    _seed_active(ticker="NVDA", direction="short", target_price=200.0)
    _set_price("NVDA", 190.0)
    out = thesis.review_cycle()
    assert out["validated"] == 1


def test_review_does_not_validate_when_short_of_target():
    _purge()
    tid = _seed_active(ticker="NVDA", direction="long", target_price=300.0)
    _set_price("NVDA", 250.0)
    out = thesis.review_cycle()
    assert out["validated"] == 0
    with session_scope() as s:
        t = s.get(Thesis, tid)
        assert t.state == "active"


def test_review_invalidates_on_challenge_ratio():
    _purge()
    # 4 challenges, 1 support → ratio = 4, ≥ floor + min events met
    tid = _seed_active(
        ticker="NVDA", direction="long",
        supporting=1, challenging=4,
    )
    out = thesis.review_cycle()
    assert out["invalidated"] == 1
    with session_scope() as s:
        t = s.get(Thesis, tid)
        assert t.state == "invalidated"


def test_review_does_not_invalidate_with_balanced_events():
    _purge()
    _seed_active(
        ticker="NVDA", direction="long",
        supporting=3, challenging=2,
    )
    out = thesis.review_cycle()
    assert out["invalidated"] == 0


def test_review_matures_on_horizon_elapsed():
    _purge()
    tid = _seed_active(
        ticker="NVDA", direction="long", horizon_days=7,
        created_days_ago=10,
    )
    out = thesis.review_cycle()
    assert out["matured"] == 1
    with session_scope() as s:
        t = s.get(Thesis, tid)
        assert t.state == "matured"
        assert "horizon" in (t.close_reason or "").lower()


# ── generator validation ─────────────────────────────────────────────────


def test_validate_open_clamps_bad_fields():
    raw = {
        "ticker": "nvda",  # lowercase ok, gets uppercased
        "direction": "long",
        "title": "ok",
        "body": "body",
        "invalidation_criteria": "specific thing breaks",
        "conviction": 99,        # out of range, clamps to 5
        "target_price": "-5",    # negative → None
        "horizon_days": "30",    # str ok, coerces
    }
    out = thesis._validate_open(raw)
    assert out is not None
    assert out["ticker"] == "NVDA"
    assert out["conviction"] == 5
    assert out["target_price"] is None
    assert out["horizon_days"] == 30


def test_validate_open_returns_none_on_missing_fields():
    assert thesis._validate_open({}) is None
    assert thesis._validate_open(
        {"ticker": "NVDA", "direction": "sideways"}
    ) is None  # bad direction
    assert thesis._validate_open(
        {"ticker": "NVDA", "direction": "long", "title": "ok"}
    ) is None  # missing invalidation


def test_parse_generator_tolerates_fences_and_garbage_prose():
    raw = (
        "Here is the analysis:\n"
        "```json\n"
        '{"open": [], "close": [{"id": 7, "reason": "no longer valid"}]}'
        "\n```\nThanks!"
    )
    d = thesis._parse_generator_output(raw)
    assert d is not None
    assert d.get("close") == [{"id": 7, "reason": "no longer valid"}]
