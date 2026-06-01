"""Tests for sentinel.news_tickers.resolve_article_tickers — the precision
layer that decides which ticker a news item is actually ABOUT.

The LLM is stubbed throughout; these pin the heuristic gating, the
feed-ticker demotion, and the candidate-constraint on the model's output.
"""

import pytest

from sentinel import news_tickers
from sentinel.news_tickers import resolve_article_tickers

WATCHLIST = {"NVDA", "SNOW", "AMZN", "AMD", "AAPL", "MSFT"}


class _StubLLM:
    """Returns a canned `complete()` payload; raises if `boom` is set."""

    def __init__(self, payload="", *, boom=False):
        self._payload = payload
        self._boom = boom

    def complete(self, *args, **kwargs):
        if self._boom:
            raise RuntimeError("llm down")
        return self._payload


@pytest.fixture
def stub_llm(monkeypatch):
    """Patch news_tickers.get_llm to return a configurable stub."""

    def _install(payload="", *, boom=False):
        monkeypatch.setattr(
            news_tickers, "get_llm", lambda: _StubLLM(payload, boom=boom)
        )

    return _install


# ── the reported bug: yfinance NVDA feed carries a SNOW/AMZN story ───────


def test_yfinance_feed_ticker_demoted_without_ai(stub_llm):
    """allow_ai=False: even with no LLM, an unsupported feed-ticker must NOT
    win. Content says Snowflake + Amazon; NVDA came only from the feed."""
    r = resolve_article_tickers(
        "What to Know About Snowflake's Partnership With Amazon",
        "",
        WATCHLIST,
        feed_ticker="NVDA",
        allow_ai=False,
    )
    assert "NVDA" not in r.ranked
    assert r.primary in {"SNOW", "AMZN"}
    assert set(r.ranked) == {"SNOW", "AMZN"}
    assert r.used_ai is False


def test_yfinance_feed_ticker_resolved_by_ai(stub_llm):
    """allow_ai=True + ambiguous: the model picks the real subject and the
    spurious feed-ticker is dropped."""
    stub_llm('{"primary": "SNOW", "tickers": ["SNOW", "AMZN"]}')
    r = resolve_article_tickers(
        "What to Know About Snowflake's Partnership With Amazon",
        "Snowflake deepens its tie-up with Amazon's AWS.",
        WATCHLIST,
        feed_ticker="NVDA",
        allow_ai=True,
    )
    assert r.primary == "SNOW"
    assert r.ranked == ["SNOW", "AMZN"]
    assert "NVDA" not in r.ranked
    assert r.used_ai is True


# ── feed-ticker that the content DOES back is kept, no LLM spent ─────────


def test_supported_feed_ticker_kept_without_ai(stub_llm):
    stub_llm('{"primary": "WRONG", "tickers": ["WRONG"]}')  # must be ignored
    r = resolve_article_tickers(
        "Nvidia earnings beat expectations",
        "",
        WATCHLIST,
        feed_ticker="NVDA",
        allow_ai=True,
    )
    # Single supported candidate → not ambiguous → no LLM call.
    assert r.primary == "NVDA"
    assert r.ranked == ["NVDA"]
    assert r.used_ai is False


# ── candidate constraint: the model can never introduce a new ticker ─────


def test_ai_output_constrained_to_candidates(stub_llm):
    """Model hallucinates TSLA (not a candidate) — it must be dropped, and a
    primary outside the candidate set falls back to the surviving subject."""
    stub_llm('{"primary": "TSLA", "tickers": ["TSLA", "SNOW"]}')
    r = resolve_article_tickers(
        "Snowflake and Amazon expand partnership",
        "",
        WATCHLIST,
        feed_ticker="NVDA",
        allow_ai=True,
    )
    assert "TSLA" not in r.ranked
    assert r.primary == "SNOW"
    assert r.ranked == ["SNOW"]


def test_ai_failure_falls_back_to_demoted_heuristic(stub_llm):
    """LLM raises → heuristic fallback, still demoting the feed-ticker."""
    stub_llm(boom=True)
    r = resolve_article_tickers(
        "Snowflake teams up with Amazon",
        "",
        WATCHLIST,
        feed_ticker="NVDA",
        allow_ai=True,
    )
    assert "NVDA" not in r.ranked
    assert set(r.ranked) == {"SNOW", "AMZN"}
    assert r.used_ai is True  # we attempted it; budget should be charged


def test_ai_garbage_output_falls_back(stub_llm):
    """Unparseable model output → heuristic fallback."""
    stub_llm("not json at all")
    r = resolve_article_tickers(
        "Snowflake teams up with Amazon",
        "",
        WATCHLIST,
        feed_ticker="NVDA",
        allow_ai=True,
    )
    assert set(r.ranked) == {"SNOW", "AMZN"}


# ── degenerate / macro cases ─────────────────────────────────────────────


def test_no_candidates_returns_empty(stub_llm):
    r = resolve_article_tickers(
        "Fed holds interest rates steady amid inflation watch",
        "",
        WATCHLIST,
        feed_ticker=None,
        allow_ai=True,
    )
    assert r.primary is None
    assert r.ranked == []
    assert r.used_ai is False


def test_feed_ticker_only_signal_is_trusted(stub_llm):
    """yfinance feed-ticker with NO content match and nothing else to go on —
    trust the feed rather than drop the article entirely."""
    r = resolve_article_tickers(
        "An opaque headline with no recognizable company",
        "",
        WATCHLIST,
        feed_ticker="AMD",
        allow_ai=True,
    )
    assert r.primary == "AMD"
    assert r.ranked == ["AMD"]
    assert r.used_ai is False  # single candidate, not ambiguous


def test_rss_single_cashtag_no_ai(stub_llm):
    """RSS path (no feed-ticker): one clear cashtag subject, no LLM spent."""
    stub_llm('{"primary": "WRONG", "tickers": ["WRONG"]}')
    r = resolve_article_tickers(
        "$TSLA recalls 2 million vehicles over autopilot",
        "",
        WATCHLIST | {"TSLA"},
        feed_ticker=None,
        allow_ai=True,
    )
    assert r.primary == "TSLA"
    assert r.used_ai is False


def test_multi_ticker_rss_uses_ai(stub_llm):
    """Two genuine subjects in an RSS item → ambiguous → LLM picks primary."""
    stub_llm('{"primary": "AMD", "tickers": ["AMD", "NVDA"]}')
    r = resolve_article_tickers(
        "AMD launches MI400 to challenge Nvidia in AI accelerators",
        "",
        WATCHLIST,
        feed_ticker=None,
        allow_ai=True,
    )
    assert r.primary == "AMD"
    assert r.ranked == ["AMD", "NVDA"]
    assert r.used_ai is True
