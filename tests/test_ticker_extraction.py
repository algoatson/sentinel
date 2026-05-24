"""Tests for sentinel.utils.extract_tickers per SPEC §7 rules."""

from sentinel.utils import extract_tickers


WATCHLIST = {"NVDA", "AAPL", "TSLA", "BRK-B", "DD", "IT"}
# DD and IT are intentionally in the watchlist to verify the blocklist
# overrides watchlist membership.


def test_cashtag_in_watchlist_accepted():
    assert extract_tickers("$NVDA looking strong", WATCHLIST) == {"NVDA"}


def test_cashtag_not_in_watchlist_rejected():
    assert extract_tickers("$ZZZZ moonshot", WATCHLIST) == set()


def test_bare_ticker_needs_two_occurrences():
    assert extract_tickers("watch NVDA", WATCHLIST) == set()
    assert extract_tickers("NVDA earnings — NVDA up big", WATCHLIST) == {"NVDA"}


def test_bare_ticker_accepted_when_flair_matches():
    assert extract_tickers("watch this", WATCHLIST, flair="NVDA") == set()
    assert extract_tickers("watch NVDA today", WATCHLIST, flair="NVDA") == {"NVDA"}


def test_bare_ticker_accepted_when_title_has_cashtag():
    assert extract_tickers(
        "good things happening", WATCHLIST, title="$NVDA breakout"
    ) == {"NVDA"}
    # Bare in body, cashtag in title — should accept on title-cashtag rule.
    assert extract_tickers(
        "NVDA is going up", WATCHLIST, title="$NVDA breakout"
    ) == {"NVDA"}


def test_blocklist_overrides_watchlist_for_cashtag():
    # DD is watchlisted in this test fixture but it's also in the blocklist.
    assert extract_tickers("$DD due diligence", WATCHLIST) == set()


def test_blocklist_overrides_watchlist_for_bare():
    assert extract_tickers("DD DD DD", WATCHLIST) == set()


def test_blocklist_rejects_common_english():
    assert extract_tickers("the IT department is great IT IT", WATCHLIST) == set()
    assert extract_tickers("CEO said so", WATCHLIST) == set()
    assert extract_tickers("IPO season", WATCHLIST) == set()


def test_lowercase_not_extracted():
    # Tickers are uppercase by convention; lowercase 'nvda' should not match.
    assert extract_tickers("nvda earnings", WATCHLIST) == set()


def test_multiple_tickers_in_one_text():
    assert extract_tickers("$NVDA and $AAPL both moved", WATCHLIST) == {"NVDA", "AAPL"}


def test_cashtag_and_bare_together_dedupes():
    # $NVDA satisfies cashtag rule; the bare NVDA is also there but already
    # accepted via cashtag — set returns a single entry.
    assert extract_tickers("$NVDA then NVDA again", WATCHLIST) == {"NVDA"}


def test_empty_text():
    assert extract_tickers("", WATCHLIST) == set()
    assert extract_tickers("no tickers here at all", WATCHLIST) == set()


# ── Rule 3: company-name resolution (Wave 2 addition) ────────────────────


def test_name_resolution_finds_ticker_when_no_cashtag():
    """Headline-grade news rarely uses cashtags — without name
    resolution, every $NVDA story written as "Nvidia announced…" would
    sail through extract_tickers with no ticker attached. Pin the fix
    against that regression."""
    body = (
        "Nvidia announced a new generation of datacenter GPUs targeting "
        "the AI training workload."
    )
    assert extract_tickers(body, WATCHLIST) == {"NVDA"}


def test_name_resolution_is_case_insensitive():
    assert extract_tickers("NVIDIA is up", WATCHLIST) == {"NVDA"}
    assert extract_tickers("nvidia is up", WATCHLIST) == {"NVDA"}
    assert extract_tickers("NvIdIa is up", WATCHLIST) == {"NVDA"}


def test_name_resolution_respects_watchlist():
    """A name like Microsoft maps to MSFT — but if MSFT isn't in the
    watchlist, we don't tag it. Watchlist remains the source of truth
    for what we track."""
    # MSFT not in WATCHLIST → not extracted
    assert "MSFT" not in extract_tickers(
        "Microsoft and Oracle both reported", WATCHLIST
    )


def test_name_resolution_word_boundary_does_not_overmatch():
    """`Apple` must NOT match inside `Snapple` / `Pineapple`. Word-
    boundary regex saves us from this class of false positive."""
    assert extract_tickers("Snapple sales rose", WATCHLIST) == set()
    assert extract_tickers("Pineapple Express", WATCHLIST) == set()


def test_name_aliases_resolve_to_same_canonical():
    """Google + Alphabet should both map to GOOGL — both are valid
    English-language references to the same company in news."""
    watchlist = WATCHLIST | {"GOOGL"}
    assert "GOOGL" in extract_tickers("Alphabet reported", watchlist)
    assert "GOOGL" in extract_tickers("Google announced", watchlist)


def test_name_resolution_combined_with_cashtags():
    """Mixed input — one ticker via cashtag, another via name —
    yields both."""
    watchlist = WATCHLIST | {"MSFT"}
    out = extract_tickers(
        "$AAPL and Microsoft both reported on Tuesday", watchlist
    )
    assert out == {"AAPL", "MSFT"}


def test_name_resolution_realistic_news_excerpt():
    """The exact user complaint: a news excerpt with 5 watchable
    names, none in cashtag form, must yield all five."""
    body = (
        "Industry observers said the move was likely to benefit IonQ, "
        "Rigetti, and D-Wave — the three publicly traded pure-plays in "
        "the space. Larger players including IBM and Alphabet also "
        "stand to gain through their existing research programmes."
    )
    watchlist = {"IONQ", "RGTI", "QBTS", "IBM", "GOOGL"}
    out = extract_tickers(body, watchlist)
    assert {"IONQ", "RGTI", "QBTS", "IBM", "GOOGL"} <= out


def test_blocklist_still_overrides_for_name_match():
    """If a name aliases to a blocklisted ticker, the blocklist wins
    (defence in depth)."""
    # Synthesise: pretend "DD" is in the alias map (it isn't shipped
    # because of the conflict, but the rule logic is unit-testable).
    from sentinel import utils
    import re
    saved = utils._NAME_PATTERNS
    utils._NAME_PATTERNS = saved + (
        (re.compile(r"\bdupontish\b", re.IGNORECASE), "DD"),
    )
    try:
        # WATCHLIST has DD; ext would otherwise match — blocklist saves us
        assert "DD" not in extract_tickers("dupontish things", WATCHLIST)
    finally:
        utils._NAME_PATTERNS = saved
