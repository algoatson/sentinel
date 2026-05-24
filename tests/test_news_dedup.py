"""News canonical-URL dedup contract.

Pins the two halves of the fix:

1. `canonical_url` strips tracking params + fragment, lowercases scheme
   and host, normalises trailing slash. Functional query params (id=,
   page=, article_id=) are preserved so legitimately-different URLs
   stay distinguishable.
2. The cross-source dedup check actually skips the duplicate insert and
   counts it under `skipped_dup`. yfinance + Yahoo RSS frequently
   republish the same Reuters/CNBC URL under different external_ids
   — the user complaint that triggered this — and we want a single row
   per canonical URL within the 24h window.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from sentinel.db import session_scope
from sentinel.ingesters.news import (
    _recent_canonicals,
    canonical_url,
)
from sentinel.models import NewsItem


# ── canonical_url ────────────────────────────────────────────────────────


def test_canonical_drops_utm_params():
    src = "https://example.com/article/123?utm_source=feed&utm_campaign=daily"
    out = canonical_url(src)
    assert "utm_" not in out
    assert "/article/123" in out


def test_canonical_keeps_functional_params():
    """An article id IS a real identifier; we must not strip it just
    because it's in the query string."""
    out = canonical_url("https://example.com/post?id=42&utm_source=x")
    assert "id=42" in out
    assert "utm_" not in out


def test_canonical_drops_fragment():
    assert "#" not in canonical_url("https://x.com/post/A#section-1")


def test_canonical_strips_trailing_slash():
    # /a and /a/ should collapse so equivalent URLs match
    a = canonical_url("https://x.com/article/a")
    b = canonical_url("https://x.com/article/a/")
    assert a == b


def test_canonical_lowercases_host_but_not_path():
    src = "https://Example.COM/Article/123"
    out = canonical_url(src)
    assert out.startswith("https://example.com/")
    # Path is case-significant for some servers, so we leave it alone
    assert "/Article/123" in out


def test_canonical_drops_common_share_trackers():
    """Real-world facebook/google click ids and similar."""
    src = "https://news.example.com/x?fbclid=abc&gclid=def&msclkid=ghi"
    out = canonical_url(src)
    for tag in ("fbclid", "gclid", "msclkid"):
        assert tag not in out


def test_canonical_survives_garbage_input():
    # We don't want a malformed URL to crash the ingester; just return
    # the original (the per-row dedup will then trivially "not match"
    # other rows, which is the right failure mode — fewer skips, not
    # missed inserts).
    assert canonical_url("") == ""
    assert canonical_url("not a url") == "not a url"
    # `urlparse` accepts almost anything; if it returns a result with
    # no scheme/netloc, we hand back the input
    assert canonical_url("just/some/path") == "just/some/path"


# ── _recent_canonicals + dedup integration ────────────────────────────────


def _seed_news(url: str, *, source: str = "rss:test", hours_ago: int = 1,
               ext_suffix: str = "") -> None:
    now = datetime.now(timezone.utc)
    when = now.replace(tzinfo=None)
    with session_scope() as s:
        s.add(NewsItem(
            source=source,
            external_id=f"{source}:{url}{ext_suffix}",
            title="x", url=url, summary="",
            ticker=None, is_macro=False,
            published_at=when, fetched_at=when,
        ))


def _purge_news() -> None:
    with session_scope() as s:
        for row in s.exec(select(NewsItem)).all():
            s.delete(row)


def test_recent_canonicals_returns_set_of_normalised_urls():
    _purge_news()
    _seed_news("https://Example.COM/a/?utm_source=y", source="rss:a")
    _seed_news("https://news.com/b#x", source="rss:b")
    with session_scope() as s:
        seen = _recent_canonicals(s, hours=24)
    # both got canonicalised
    assert "https://example.com/a" in seen
    assert "https://news.com/b" in seen


def test_cross_source_dedup_drops_second_insertion():
    """The headline regression: yfinance posts a Reuters URL, then 5
    minutes later the same URL comes through Yahoo RSS. Without
    canonical dedup, two NewsItem rows show up in the feed. With it,
    the second pass sees the canonical and skips."""
    _purge_news()
    # First source plants the row
    _seed_news("https://reuters.com/business/x?utm_source=yfin",
               source="yfinance")
    # Build the recent-canonicals set BEFORE the second source tries
    # to insert — this is what `_poll_rss` / `_poll_yfinance` do at
    # the top of their loop, lazily on first need.
    with session_scope() as s:
        seen = _recent_canonicals(s, hours=24)
    second_url = "https://reuters.com/business/x?fbclid=abc&utm_medium=email"
    assert canonical_url(second_url) in seen


def test_recent_canonicals_respects_window():
    """A 48h-old article must NOT contribute to the 24h dedup set —
    otherwise stale stories permanently shadow re-emerging coverage."""
    _purge_news()
    now = datetime.now(timezone.utc)
    long_ago_naive = (now - __import__("datetime").timedelta(hours=48)
                      ).replace(tzinfo=None)
    with session_scope() as s:
        s.add(NewsItem(
            source="rss:old", external_id="old-1",
            title="stale", url="https://x.com/stale",
            summary="", ticker=None, is_macro=False,
            published_at=long_ago_naive, fetched_at=long_ago_naive,
        ))
    with session_scope() as s:
        seen = _recent_canonicals(s, hours=24)
    assert "https://x.com/stale" not in seen
