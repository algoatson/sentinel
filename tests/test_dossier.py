"""Dossier cache contract.

The whole point of `dossier.{call,news}_dossier` is "don't regen the LLM
text on every click". This pins:
- First call generates → second call returns cached body
- `refresh=True` forces regeneration even with a cached row
- Missing source row returns a clean sentinel (no crash, no LLM call)

The contextual chat (`ask_about_call` / `ask_about_news`) is NOT cached
by design — pinned too so a future "let's cache chat too" change doesn't
break the iterative-Q UX.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sentinel import dossier
from sentinel.db import session_scope
from sentinel.models import CallSummary, NewsItem, TradingCall


class _CountingLLM:
    """Minimal LLM stub — counts calls so we can assert cache behaviour."""

    def __init__(self, body: str = "dossier body") -> None:
        self.body = body
        self.calls = 0

    def complete(self, prompt, *, model=None, max_tokens=None):
        self.calls += 1
        return self.body


def _seed_call(ticker="NVDA", direction="long") -> int:
    with session_scope() as s:
        c = TradingCall(
            ticker=ticker, direction=direction, conviction=4,
            source="dashboard", thesis="thesis body",
            price_at_call=250.0,
            created_at=datetime.now(timezone.utc),
        )
        s.add(c)
        s.flush()
        return c.id


def _seed_news() -> int:
    with session_scope() as s:
        n = NewsItem(
            source="rss:test", external_id="t-1",
            title="Test headline", url="https://example.com/x",
            ticker="NVDA",
            published_at=datetime.now(timezone.utc),
            fetched_at=datetime.now(timezone.utc),
        )
        s.add(n)
        s.flush()
        return n.id


def test_call_dossier_caches_after_first_generation(monkeypatch):
    cid = _seed_call()
    stub = _CountingLLM("**TL;DR**: looks fine.")
    monkeypatch.setattr(dossier, "get_llm", lambda: stub)

    first = dossier.call_dossier(cid)
    assert "TL;DR" in first
    assert stub.calls == 1

    # second call returns from cache, no LLM hit
    second = dossier.call_dossier(cid)
    assert second == first
    assert stub.calls == 1


def test_call_dossier_refresh_true_regenerates(monkeypatch):
    cid = _seed_call(ticker="AAPL")
    stub = _CountingLLM("v1 body")
    monkeypatch.setattr(dossier, "get_llm", lambda: stub)
    dossier.call_dossier(cid)
    assert stub.calls == 1

    stub.body = "v2 body"
    out = dossier.call_dossier(cid, refresh=True)
    assert out == "v2 body"
    assert stub.calls == 2
    # cache now holds v2
    with session_scope() as s:
        row = s.get(CallSummary, cid)
        assert row is not None and row.summary == "v2 body"


def test_call_dossier_missing_call_returns_sentinel_without_llm(monkeypatch):
    boom = _CountingLLM()
    monkeypatch.setattr(dossier, "get_llm", lambda: boom)
    out = dossier.call_dossier(999_999)
    assert "not found" in out.lower()
    assert boom.calls == 0


def test_news_dossier_caches_after_first_generation(monkeypatch):
    nid = _seed_news()
    stub = _CountingLLM("**TL;DR**: read.")
    monkeypatch.setattr(dossier, "get_llm", lambda: stub)

    a = dossier.news_dossier(nid)
    b = dossier.news_dossier(nid)
    assert a == b
    assert stub.calls == 1


def test_ask_about_call_is_not_cached(monkeypatch):
    """The follow-up chat MUST hit the LLM each time — caching it would
    freeze the user's iterative refinements on the first answer."""
    cid = _seed_call(ticker="MSFT")
    stub = _CountingLLM("Answer.")
    monkeypatch.setattr(dossier, "get_llm", lambda: stub)

    dossier.ask_about_call(cid, "what's the target?")
    dossier.ask_about_call(cid, "and the hold horizon?")
    assert stub.calls == 2


def test_call_summary_meta_returns_none_when_missing():
    assert dossier.call_summary_meta(987_654_321) is None


# ── reddit + filing (Intel overhaul) ──────────────────────────────────────────


def _seed_reddit(ticker="NVDA") -> int:
    from sentinel.models import RedditMention
    with session_scope() as s:
        m = RedditMention(
            subreddit="stocks", post_id="p1", ticker=ticker, author="someuser",
            score=42, num_comments=7, created_at=datetime.now(timezone.utc),
            title="Is NVDA a buy here?", body_excerpt="discussion body",
            permalink="https://www.reddit.com/r/stocks/comments/p1/x/",
        )
        s.add(m)
        s.flush()
        return m.id


def _seed_filing(ticker="NVDA") -> int:
    from sentinel.models import Filing
    with session_scope() as s:
        f = Filing(
            cik="0001045810", ticker=ticker, form_type="8-K",
            accession_number="acc-1", filed_at=datetime.now(timezone.utc),
            primary_doc_url="https://sec.gov/x", summary="a summary",
            materiality_score=6, materiality_reason="why",
        )
        s.add(f)
        s.flush()
        return f.id


def test_reddit_dossier_caches_after_first_generation(monkeypatch):
    mid = _seed_reddit()
    stub = _CountingLLM("**TL;DR**: noise.")
    monkeypatch.setattr(dossier, "get_llm", lambda: stub)
    # Don't hit the network for top comments in tests.
    import sentinel.ingesters.reddit as _r
    monkeypatch.setattr(_r, "fetch_top_comments", lambda *a, **k: [])

    a = dossier.reddit_dossier(mid)
    b = dossier.reddit_dossier(mid)
    assert a == b and "TL;DR" in a
    assert stub.calls == 1  # second served from cache


def test_reddit_dossier_missing_returns_sentinel_without_llm(monkeypatch):
    boom = _CountingLLM()
    monkeypatch.setattr(dossier, "get_llm", lambda: boom)
    out = dossier.reddit_dossier(999_999)
    assert "not found" in out.lower() and boom.calls == 0


def test_ask_about_reddit_and_filing_missing_are_clean(monkeypatch):
    boom = _CountingLLM()
    monkeypatch.setattr(dossier, "get_llm", lambda: boom)
    assert "not found" in dossier.ask_about_reddit(999_999, "q").lower()
    assert "not found" in dossier.ask_about_filing(999_999, "q").lower()
    assert boom.calls == 0


def test_ask_about_filing_hits_llm(monkeypatch):
    fid = _seed_filing()
    stub = _CountingLLM("Answer about the filing.")
    monkeypatch.setattr(dossier, "get_llm", lambda: stub)
    out = dossier.ask_about_filing(fid, "what does this mean?")
    assert out == "Answer about the filing." and stub.calls == 1
