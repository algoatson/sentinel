"""Tests for sentinel.news_tickers.resolve_article_tickers — the LLM-authority
ticker tagger with watchlist validation and a heuristic fallback.

The LLM is stubbed throughout; these pin that the model can recover subjects
the keyword matcher missed AND drop false matches it flagged, that its output
is validated against the watchlist (no hallucination, scoped to tracked
names), and that the deterministic fallback still demotes a bad feed-ticker.
"""

import pytest

from sentinel import news_tickers
from sentinel.news_tickers import resolve_article_tickers

WATCHLIST = {"NVDA", "ARM", "RTX", "COIN", "SNOW", "AMZN", "AMD", "AAPL"}


class _StubLLM:
    def __init__(self, payload="", *, boom=False):
        self._payload = payload
        self._boom = boom

    def complete(self, *args, **kwargs):
        if self._boom:
            raise RuntimeError("llm down")
        return self._payload


@pytest.fixture
def stub_llm(monkeypatch):
    def _install(payload="", *, boom=False):
        monkeypatch.setattr(
            news_tickers, "get_llm", lambda: _StubLLM(payload, boom=boom)
        )
    return _install


# ── the reported failures ────────────────────────────────────────────────


def test_ai_recovers_missed_subject_and_drops_false_match(stub_llm):
    """The Arm/Nvidia/RTX case: the keyword matcher misses ARM (named in
    prose) and false-flags RTX (Nvidia's product brand → Raytheon). The model
    returns the real subjects; RTX is excluded, ARM is recovered as primary."""
    stub_llm('{"primary": "ARM", "tickers": ["ARM", "NVDA"]}')
    r = resolve_article_tickers(
        "Arm's stock may be the biggest beneficiary of Nvidia's new AI effort",
        "Nvidia's new RTX Spark PC chip uses Arm technology.",
        WATCHLIST,
        feed_ticker=None,
        allow_ai=True,
    )
    assert r.primary == "ARM"
    assert "ARM" in r.ranked and "NVDA" in r.ranked
    assert "RTX" not in r.ranked
    assert r.used_ai is True


def test_ai_recovers_subject_with_zero_keyword_candidates(stub_llm):
    """The Coinbase case: even if the keyword matcher found nothing, the model
    names the subject and (being watchlisted) it gets tagged."""
    stub_llm('{"primary": "COIN", "tickers": ["COIN"]}')
    r = resolve_article_tickers(
        "Some Exchange Launches Direct Rupee Rails",
        "The U.S. exchange established direct rupee trading for Indian users.",
        WATCHLIST,
        allow_ai=True,
    )
    assert r.primary == "COIN"
    assert r.ranked == ["COIN"]
    assert r.used_ai is True


# ── watchlist validation (anti-hallucination + scope) ────────────────────


def test_ai_output_validated_against_watchlist(stub_llm):
    stub_llm('{"primary": "COIN", "tickers": ["COIN", "ZZZZ"]}')
    r = resolve_article_tickers("Coinbase news", "", {"COIN", "NVDA"}, allow_ai=True)
    assert r.primary == "COIN"
    assert r.ranked == ["COIN"]  # ZZZZ not tracked → dropped


def test_ai_primary_outside_watchlist_falls_back_to_first_valid(stub_llm):
    stub_llm('{"primary": "TSLA", "tickers": ["TSLA", "NVDA"]}')
    r = resolve_article_tickers("x", "", {"NVDA"}, allow_ai=True)
    assert r.primary == "NVDA"
    assert r.ranked == ["NVDA"]


def test_ai_null_primary_yields_no_tag(stub_llm):
    """Private-company / macro story (OpenAI + Anthropic) → no ticker."""
    stub_llm('{"primary": null, "tickers": []}')
    r = resolve_article_tickers(
        "AI is crushing a generation of startups built before ChatGPT",
        "More than $250B has funneled into OpenAI and Anthropic.",
        WATCHLIST,
        allow_ai=True,
    )
    assert r.primary is None
    assert r.ranked == []
    assert r.used_ai is True


# ── heuristic fallback (no AI / AI failure) ──────────────────────────────


def test_fallback_demotes_unsupported_feed_ticker(stub_llm):
    """allow_ai=False: the deterministic path still keeps a yfinance
    feed-ticker (NVDA) from winning on a Snowflake/Amazon story."""
    r = resolve_article_tickers(
        "Snowflake's Partnership With Amazon", "",
        WATCHLIST, feed_ticker="NVDA", allow_ai=False,
    )
    assert "NVDA" not in r.ranked
    assert set(r.ranked) == {"SNOW", "AMZN"}
    assert r.used_ai is False


def test_fallback_trusts_feed_when_no_content(stub_llm):
    r = resolve_article_tickers(
        "An opaque headline with no recognizable company", "",
        WATCHLIST, feed_ticker="AMD", allow_ai=False,
    )
    assert r.primary == "AMD"
    assert r.ranked == ["AMD"]
    assert r.used_ai is False


def test_ai_failure_falls_back_to_heuristic_and_charges_budget(stub_llm):
    stub_llm(boom=True)
    r = resolve_article_tickers(
        "Nvidia and AMD both report earnings tonight", "",
        WATCHLIST, allow_ai=True,
    )
    assert r.ranked and set(r.ranked) <= {"NVDA", "AMD"}
    assert r.used_ai is True  # attempted → budget charged even on failure


def test_ai_garbage_output_falls_back(stub_llm):
    stub_llm("not json at all")
    r = resolve_article_tickers(
        "Nvidia and AMD both report earnings tonight", "",
        WATCHLIST, allow_ai=True,
    )
    assert set(r.ranked) <= {"NVDA", "AMD"} and r.ranked
