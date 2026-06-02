"""Tests for sentinel.prompts."""

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from sentinel.db import session_scope
from sentinel.models import PromptVersion
from sentinel.prompts import ALL_PROMPTS, get_prompt, seed_prompts


EXPECTED_PROMPT_NAMES = {
    "summarize_8k",
    "summarize_form4",
    "summarize_10q",
    "summarize_10k",
    "summarize_13f",
    "summarize_offering",
    "summarize_proxy",
    "summarize_generic",
    "materiality",
    "tag_sentiment",
    "social_pulse",
    "daily_digest",
    "tuning_suggest",
    "synthesis",
    "lounge",
    "reddit_curate",
    "book_risk",
    "macro_themes",
    "tag_article_tickers",
    "extract_claims",
    "game_plan",
}


def test_every_spec_prompt_is_registered():
    assert set(ALL_PROMPTS.keys()) == EXPECTED_PROMPT_NAMES


def test_get_prompt_falls_back_to_constant_when_db_empty():
    tmpl = get_prompt("summarize_8k")
    assert "8-K SEC filing" in tmpl.template


def test_get_prompt_returns_db_version_when_active():
    custom = "$text custom override"
    with session_scope() as s:
        s.add(
            PromptVersion(
                prompt_name="summarize_8k",
                content=custom,
                created_at=datetime.now(timezone.utc),
                active=True,
            )
        )
    tmpl = get_prompt("summarize_8k")
    assert tmpl.template == custom


def test_get_prompt_unknown_raises():
    with pytest.raises(KeyError):
        get_prompt("nonexistent_prompt_xyz")


def test_seed_prompts_idempotent():
    seed_prompts()
    seed_prompts()
    with session_scope() as s:
        rows = s.exec(select(PromptVersion)).all()
        assert len(rows) == len(ALL_PROMPTS)


def test_every_prompt_substitutes_cleanly():
    """Every prompt template should fully render with our standard placeholder set."""
    dummy = {
        key: "X"
        for key in [
            "text",
            "tickers",
            "bundle",
            "narrative",
            "form_type",
            "ticker",
            "title",
            "summary",
            "enrichment_json",
            "numbered_items",
            "spike_data_json",
            "input_json",
            "feedback_data_json",
            "snapshot_json",
            "macro_news",
            "movers",
            "featured",
            "community",
            "previous_lounge",
            "candidates",
            "max_keep",
            "positions",
            "book",
            "watchlist_sample",
            "headlines_json",
        ]
    }
    for name, tmpl in ALL_PROMPTS.items():
        try:
            tmpl.substitute(dummy)
        except (KeyError, ValueError) as e:
            pytest.fail(f"prompt {name!r} failed to substitute: {e}")


def test_materiality_prompt_renders_literal_dollar_amounts():
    """The materiality prompt contains literal '$1M' / '$500k' which must
    survive substitution as literal dollar signs (escaped as $$ in source)."""
    tmpl = ALL_PROMPTS["materiality"]
    out = tmpl.substitute(
        form_type="8-K", ticker="NVDA", summary="x", enrichment_json="{}"
    )
    assert "$1M" in out
    assert "$500k" in out
    assert "$5M" in out


def test_macro_themes_renders_literal_tickers_and_fills_vars():
    out = ALL_PROMPTS["macro_themes"].substitute(
        book="$NVDA", watchlist_sample="$XOM, $AMD", headlines_json="[]"
    )
    assert "$XOM" in out and "CALL: $TICKER" in out  # literals survived
    assert out.rstrip().endswith("[]")               # headlines var filled


def test_reddit_curate_renders_literal_symbol_examples():
    """The curator prompt cites '$OPEN' / '$ALL' as spurious-match examples;
    those literals (escaped $$ in source) must survive substitution, and the
    real placeholders must fill."""
    out = ALL_PROMPTS["reddit_curate"].substitute(max_keep=5, candidates="C")
    assert "$OPEN" in out and "$ALL" in out
    assert "at most 5" in out and out.rstrip().endswith("C")


def test_daily_digest_renders_literal_ticker_token():
    """daily_digest tells the model to use '$TICKER' form — that literal token
    must survive substitution."""
    out = ALL_PROMPTS["daily_digest"].substitute(input_json="{}")
    assert "$TICKER" in out
