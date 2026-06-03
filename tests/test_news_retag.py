"""Tests for the Phase-2 news_retag upgrade job.

Pins the load-bearing contract: a recent, tag-poor, Yahoo-page NewsItem is
upgraded from the curated page ticker set (additively — never losing a tag),
stamped tag_source='html+ai'; items where the page adds nothing skip the LLM
entirely; and a page-fetch failure is fail-open per item (no crash, no change).

The page-tag fetch and the LLM are both stubbed — deterministic, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from sentinel import news_tickers
from sentinel.db import session_scope
from sentinel.models import NewsItem, Watchlist
from sentinel.pipelines import news_retag

WATCH = ["H", "BTC-USD", "BNB-USD", "NVDA"]


class _StubLLM:
    def __init__(self, payload="", *, boom=False):
        self._payload, self._boom = payload, boom

    def complete(self, *a, **k):
        if self._boom:
            raise RuntimeError("llm down")
        return self._payload


def _purge() -> None:
    with session_scope() as s:
        for row in s.exec(select(NewsItem)).all():
            s.delete(row)
        for row in s.exec(select(Watchlist)).all():
            s.delete(row)


def _seed_watch() -> None:
    with session_scope() as s:
        for t in WATCH:
            s.add(Watchlist(cik=f"x{t}", ticker=t, source="test",
                            added_at=datetime.now(timezone.utc)))


def _seed_news(*, ticker, tickers_csv, tag_source="ai",
               url="https://finance.yahoo.com/news/story-1",
               hours_ago=2) -> int:
    pub = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None)
    with session_scope() as s:
        item = NewsItem(
            source="yfinance", external_id=f"yf:{url}",
            title="Crypto names rally as treasury buyers step in", url=url,
            summary="", ticker=ticker, tickers_csv=tickers_csv,
            tag_source=tag_source, is_macro=False,
            published_at=pub, fetched_at=pub,
        )
        s.add(item)
        s.flush()
        return item.id


@pytest.fixture
def stub_llm(monkeypatch):
    def _install(payload="", *, boom=False):
        monkeypatch.setattr(news_tickers, "get_llm", lambda: _StubLLM(payload, boom=boom))
    return _install


def test_retag_upgrades_tag_poor_item_from_page_tags(monkeypatch, stub_llm):
    """A single-ticker item gets the page's fuller set (additive superset),
    stamped html+ai."""
    _purge()
    _seed_watch()
    nid = _seed_news(ticker="H", tickers_csv=",H,")
    monkeypatch.setattr(
        news_retag.article_fetch, "fetch_article_tags",
        lambda url, **k: ["H", "BTC-USD", "BNB-USD"],
    )
    stub_llm('{"primary": "H", "tickers": ["H", "BTC-USD", "BNB-USD"]}')

    news_retag._run()

    with session_scope() as s:
        item = s.get(NewsItem, nid)
        tags = (item.tickers_csv or "").strip(",").split(",")
        assert set(tags) == {"H", "BTC-USD", "BNB-USD"}
        assert item.tag_source == "html+ai"
        assert item.ticker == "H"


def test_retag_skips_when_page_adds_nothing(monkeypatch, stub_llm):
    """Page tags ⊆ current set → no upgrade, and the LLM is never called."""
    _purge()
    _seed_watch()
    nid = _seed_news(ticker="H", tickers_csv=",H,BTC-USD,")  # already 2 tickers
    monkeypatch.setattr(
        news_retag.article_fetch, "fetch_article_tags",
        lambda url, **k: ["H"],
    )
    # Boom LLM proves we short-circuit before any model call (item has ≥2
    # tickers so it isn't even a candidate; belt-and-suspenders).
    stub_llm(boom=True)

    news_retag._run()

    with session_scope() as s:
        item = s.get(NewsItem, nid)
        assert item.tag_source == "ai"          # unchanged
        assert set((item.tickers_csv or "").strip(",").split(",")) == {"H", "BTC-USD"}


def test_retag_skips_non_superset(monkeypatch, stub_llm):
    """If the resolver (thin text) doesn't strictly expand the current set, we
    leave the item alone — retag never regresses a tag."""
    _purge()
    _seed_watch()
    nid = _seed_news(ticker="NVDA", tickers_csv=",NVDA,")
    monkeypatch.setattr(
        news_retag.article_fetch, "fetch_article_tags",
        lambda url, **k: ["BTC-USD"],          # page suggests a different name
    )
    # LLM returns only BTC-USD (drops NVDA) → not a superset of {NVDA}.
    stub_llm('{"primary": "BTC-USD", "tickers": ["BTC-USD"]}')

    news_retag._run()

    with session_scope() as s:
        item = s.get(NewsItem, nid)
        assert item.tag_source == "ai"          # unchanged — no regression
        assert item.ticker == "NVDA"


def test_retag_fail_open_on_fetch_error(monkeypatch, stub_llm):
    _purge()
    _seed_watch()
    nid = _seed_news(ticker=None, tickers_csv=None)

    def _boom(url, **k):
        raise RuntimeError("fetch down")
    monkeypatch.setattr(news_retag.article_fetch, "fetch_article_tags", _boom)
    stub_llm('{"primary": "H", "tickers": ["H"]}')

    news_retag._run()   # must not raise

    with session_scope() as s:
        item = s.get(NewsItem, nid)
        assert item.tag_source == "ai"          # untouched


def test_retag_ignores_non_yahoo_urls(monkeypatch, stub_llm):
    _purge()
    _seed_watch()
    nid = _seed_news(ticker="H", tickers_csv=",H,",
                     url="https://www.cnbc.com/news/story")
    called = {"n": 0}

    def _tags(url, **k):
        called["n"] += 1
        return ["H", "BTC-USD"]
    monkeypatch.setattr(news_retag.article_fetch, "fetch_article_tags", _tags)
    stub_llm('{"primary": "H", "tickers": ["H", "BTC-USD"]}')

    news_retag._run()

    assert called["n"] == 0                     # non-Yahoo never fetched
    with session_scope() as s:
        assert s.get(NewsItem, nid).tag_source == "ai"
