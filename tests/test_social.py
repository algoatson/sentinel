"""Social (Reddit) API contract — the Intel → Reddit tab.

Pins the two bugs the redesign fixed: the author handle must come back as a
BARE handle (so the UI's single `u/` prefix doesn't double up to `u/u/name`),
and the permalink must be served verbatim (it's already an absolute URL — the
UI must not prepend a reddit base, which produced the duped-URL bug).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from sentinel.api import social
from sentinel.db import session_scope
from sentinel.models import RedditMention


def _seed(author: str, *, ticker="NVDA") -> int:
    with session_scope() as s:
        m = RedditMention(
            subreddit="stocks", post_id=f"p-{author}", ticker=ticker,
            author=author, score=10, num_comments=3,
            created_at=datetime.now(timezone.utc),
            title="t", body_excerpt="body here",
            permalink="https://www.reddit.com/r/stocks/comments/abc/x/",
            sentiment=1, is_thesis=True,
        )
        s.add(m)
        s.flush()
        return m.id


def test_author_normalisation_strips_u_prefix():
    # All the shapes feedparser / the ingester have produced over time.
    assert social._author("u/Chilly5") == "Chilly5"
    assert social._author("/u/Chilly5") == "Chilly5"
    assert social._author("U/Chilly5") == "Chilly5"
    assert social._author("Chilly5") == "Chilly5"
    assert social._author(None) == ""


def test_recent_returns_bare_author_and_absolute_permalink():
    _seed("u/andix3")
    rows = social.recent(hours=720, ticker=None, limit=10)
    assert rows, "expected the seeded mention"
    r = rows[0]
    assert r["author"] == "andix3"                       # single u/ owned by UI
    assert r["permalink"].startswith("https://www.reddit.com/")  # absolute, verbatim
    assert r["body_excerpt"] == "body here"
    assert r["is_thesis"] is True


def test_get_one_round_trips_and_404s():
    mid = _seed("u/zzz")
    one = social.get_one(mid)
    assert one["id"] == mid and one["author"] == "zzz"
    with pytest.raises(HTTPException):
        social.get_one(999_999)
