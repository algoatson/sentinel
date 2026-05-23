"""LLM output-parsing contracts.

`parse_calls` / `parse_trailing_importance` / `parse_json_response` are pure,
regex-based, and load-bearing: parse_calls feeds record_call → funds +
scorecard; parse_trailing_importance drives the importance badge;
parse_json_response gates sentiment/tuning/watches. A silent miss here
corrupts scoring or triage with no error, so the contract is pinned here.
"""

from __future__ import annotations

from sentinel.llm import (
    LLM_ERROR_SENTINEL,
    parse_calls,
    parse_json_response,
    parse_trailing_importance,
)

# ─────────────────────── parse_json_response ────────────────────────────────


def test_json_response_failure_modes_return_none():
    assert parse_json_response("") is None
    assert parse_json_response(None) is None
    assert parse_json_response(LLM_ERROR_SENTINEL) is None
    assert parse_json_response("not json at all") is None
    assert parse_json_response("[1, 2]", expect=dict) is None  # wrong top type


def test_json_response_happy_and_fenced():
    assert parse_json_response('{"a": 1}') == {"a": 1}
    assert parse_json_response("[1, 2]", expect=list) == [1, 2]
    assert parse_json_response('```json\n{"x": 2}\n```') == {"x": 2}
    assert parse_json_response("```\n[1]\n```", expect=list) == [1]


def test_json_response_salvages_dict_when_list_expected():
    # Small models routinely answer an array task with one object.
    assert parse_json_response('{"a": 1}', expect=list) == [{"a": 1}]


# ─────────────────────── parse_trailing_importance ──────────────────────────


def test_importance_absent_returns_text_and_none():
    assert parse_trailing_importance("") == ("", None, "")
    clean, lvl, reason = parse_trailing_importance("  just a body  ")
    assert clean == "just a body" and lvl is None and reason == ""


def test_importance_extracted_and_stripped_from_body():
    clean, lvl, reason = parse_trailing_importance(
        "the thesis body\nIMPORTANCE: 4 — strong setup"
    )
    assert clean == "the thesis body"
    assert lvl == 4
    assert reason == "strong setup"


def test_importance_separators_and_case_and_last_wins():
    assert parse_trailing_importance("x\nIMPORTANCE: 3")[1:] == (3, "")
    assert parse_trailing_importance("x\nIMPORTANCE: 5 - drop it")[2] == "drop it"
    assert parse_trailing_importance("x\nimportance: 2 context")[1] == 2
    # Multiple → the last one is authoritative.
    clean, lvl, _ = parse_trailing_importance(
        "a\nIMPORTANCE: 1 early\nb\nIMPORTANCE: 5 final"
    )
    assert lvl == 5 and "IMPORTANCE: 5" not in clean


# ─────────────────────── parse_calls ────────────────────────────────────────


def test_calls_basic_extraction_and_normalisation():
    clean, calls = parse_calls(
        "Here's the read.\nCALL: $aapl long 5\nCALL: NVDA SHORT 2\nrest"
    )
    assert calls == [
        {"ticker": "AAPL", "direction": "long", "conviction": 5},
        {"ticker": "NVDA", "direction": "short", "conviction": 2},
    ]
    assert "CALL:" not in clean and "Here's the read." in clean


def test_calls_default_conviction_and_exotic_tickers():
    _, calls = parse_calls("CALL: $BTC-USD LONG\nCALL: $ES=F short\nCALL: ^TNX LONG")
    assert calls[0] == {"ticker": "BTC-USD", "direction": "long", "conviction": 3}
    assert calls[1] == {"ticker": "ES=F", "direction": "short", "conviction": 3}
    assert calls[2] == {"ticker": "^TNX", "direction": "long", "conviction": 3}


def test_calls_flat_and_none_are_dropped():
    _, calls = parse_calls("CALL: $X FLAT 4\nCALL: $Y NONE\nCALL: $Z LONG 3")
    assert calls == [{"ticker": "Z", "direction": "long", "conviction": 3}]


def test_calls_no_false_positive_on_longer_word():
    # "LONGER" must not parse as a LONG call (word-boundary guard).
    _, calls = parse_calls("CALL: $AAPL LONGER horizon")
    assert calls == []


def test_calls_conviction_does_not_bleed_across_newline():
    """Regression: a conviction-less call followed by prose starting with a
    digit must default to 3, NOT swallow the digit off the next line."""
    _, calls = parse_calls("CALL: $AAPL LONG\n5 reasons it rips")
    assert calls == [{"ticker": "AAPL", "direction": "long", "conviction": 3}]

    _, calls = parse_calls("CALL: $TSLA SHORT")  # trailing, EOF
    assert calls == [{"ticker": "TSLA", "direction": "short", "conviction": 3}]


def test_calls_empty_input():
    assert parse_calls("") == ("", [])
    assert parse_calls("no calls here at all") == ("no calls here at all", [])
