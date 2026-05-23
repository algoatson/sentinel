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
