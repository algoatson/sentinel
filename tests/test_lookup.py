"""`chat.lookup` — the dashboard's Lookup-panel dispatcher.

It must (a) reuse the same internals as `!cmd`, never grow a parallel
formatter; (b) return a clean sentinel when a required arg is missing
rather than calling the underlying handler with empty input.
"""

from __future__ import annotations

import discord

from sentinel import chat


def test_missing_arg_returns_sentinel_for_kinds_that_need_one():
    for kind in ("ticker", "filing", "timeline"):
        out = chat.lookup(kind, "")
        assert isinstance(out, str) and out
        # the sentinel is the centralised version, not a generic stack trace
        assert "_enter" in out


def test_unknown_kind_returns_clean_sentinel():
    out = chat.lookup("totally-not-real")
    assert "unknown lookup kind" in out
    assert "totally-not-real" in out


def test_noarg_kinds_dispatch_without_raising():
    # fresh-DB minimums — these helpers are designed to render an empty-
    # state embed/text when there's nothing yet, not blow up
    for kind in ("catalysts", "status", "news", "recent"):
        out = chat.lookup(kind, "")
        assert isinstance(out, str)


def test_parse_news_arg_prefers_watchlist_ticker_over_digit_heuristic():
    # if a (hypothetical) all-digit ticker is in the watchlist, treat it as
    # the ticker — don't let `isdigit()` win. HK 7203 is the canonical case.
    from sentinel.db import session_scope
    from sentinel.models import Watchlist
    from datetime import datetime, timezone
    with session_scope() as s:
        s.add(Watchlist(
            cik="x", ticker="7203", source="manual",
            asset_class="equity",
            added_at=datetime.now(timezone.utc),
        ))
    t, n = chat._parse_news_arg("7203")
    assert t == "7203" and n == chat._NEWS_DEFAULT_N
    # token NOT in watchlist + all-digit → count
    t, n = chat._parse_news_arg("99999")
    assert t is None and n == chat._NEWS_MAX_N  # clamped


def test_parse_news_arg_picks_count_vs_ticker_from_free_form():
    # nothing → macro feed default
    assert chat._parse_news_arg("") == (None, chat._NEWS_DEFAULT_N)
    # digits only → bump count, ticker stays None
    assert chat._parse_news_arg("50") == (None, 50)
    # alpha only → ticker; count default
    assert chat._parse_news_arg("NVDA")[0] == "NVDA"
    assert chat._parse_news_arg("NVDA")[1] == chat._NEWS_DEFAULT_N
    # both, either order
    t, n = chat._parse_news_arg("NVDA 30")
    assert t == "NVDA" and n == 30
    t, n = chat._parse_news_arg("30 NVDA")
    assert t == "NVDA" and n == 30
    # count is clamped (defensive — long results blow embed limits)
    assert chat._parse_news_arg("9999")[1] == chat._NEWS_MAX_N
    assert chat._parse_news_arg("0")[1] == 1


def test_join_within_never_slices_mid_line():
    # the "broken last entry" bug was [:limit] cutting inside a markdown
    # URL — _join_within must drop trailing lines whole instead
    lines = [
        "short one",
        "[title](https://news.google.com/rss/articles/very-long-url-" + "x" * 200 + ")",
        "after the link",
    ]
    out = chat._join_within(lines, limit=60)
    # nothing partial: every retained line is intact
    for line in out.split("\n"):
        if line == "…":
            continue
        assert line in lines
    # the long markdown link must not have been chopped in half
    assert "](https://" not in out or "[title](" in out


def test_join_within_appends_ellipsis_when_lines_dropped():
    out = chat._join_within(["aaaa", "bbbb", "cccc"], limit=10)
    assert out.endswith("…")


def test_join_within_no_change_when_fits():
    out = chat._join_within(["x", "y", "z"], limit=100)
    assert out == "x\ny\nz"


def test_md_link_wraps_when_url_present_and_escapes_brackets():
    # url present → markdown link; brackets in label can't break the syntax
    assert chat._md_link("Title", "https://x.co/a") == "[Title](https://x.co/a)"
    assert chat._md_link("a [b] c", "u") == "[a (b) c](u)"
    # url absent → bare label, no `[]()` noise added
    assert chat._md_link("Title", None) == "Title"
    assert chat._md_link("Title", "") == "Title"


def test_md_hardbreaks_turns_single_newlines_into_real_breaks():
    # CommonMark collapses a bare "\n" to a space — `chat.lookup` outputs
    # one line per source-line, which would otherwise render as a wall of
    # text in the Lookup panel. The hard-break marker is two spaces + \n.
    out = chat._md_hardbreaks("line one\nline two\n\nnew para\nstill new")
    parts = out.split("\n")
    assert parts[0].endswith("  ")          # "line one" got a hard break
    assert parts[1].endswith("  ")          # "line two" got a hard break
    assert parts[2] == ""                   # paragraph break preserved
    assert parts[3].endswith("  ")
    # empty input is a no-op (not a crash)
    assert chat._md_hardbreaks("") == ""
    assert chat._md_hardbreaks(None) is None


def test_lookup_strings_round_trip_through_hardbreaks():
    # any lookup result the dashboard renders must already have its
    # newlines turned into hard breaks — pin via a no-arg kind that
    # exercises the full lookup → render path
    out = chat.lookup("status", "")
    if "\n" in out:
        # at least one non-blank line ends with the hard-break marker
        assert any(
            line.endswith("  ")
            for line in out.split("\n") if line.strip()
        )


def test_embed_to_text_flattens_title_description_and_fields():
    e = discord.Embed(title="Hello", description="body line")
    e.add_field(name="F1", value="v1", inline=False)
    e.add_field(name="F2", value="v2", inline=False)
    out = chat._embed_to_text(e)
    # title is bolded, description preserved, each field becomes its own
    # bold-name + body block — readable as markdown
    assert "**Hello**" in out
    assert "body line" in out
    assert "**F1**" in out and "v1" in out
    assert "**F2**" in out and "v2" in out
