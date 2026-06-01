"""The /news feed must window on BOTH clocks (published_at OR fetched_at).

Regression for: news visible in the Overview live feed (which fires on
INGESTION) never appeared under /intel, because the list filtered on
`published_at` alone. yfinance/RSS routinely hand us articles published days
ago but ingested now — keying on publish date silently dropped ~80% of fresh
arrivals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from sentinel.api.news import list_recent
from sentinel.db import session_scope
from sentinel.models import NewsItem


def _purge() -> None:
    with session_scope() as s:
        for row in s.exec(select(NewsItem)).all():
            s.delete(row)


def _seed(tag: str, *, published_hours_ago: float, fetched_hours_ago: float) -> int:
    now = datetime.now(timezone.utc)
    pub = (now - timedelta(hours=published_hours_ago)).replace(tzinfo=None)
    fet = (now - timedelta(hours=fetched_hours_ago)).replace(tzinfo=None)
    with session_scope() as s:
        item = NewsItem(
            source="rss:test",
            external_id=f"test:{tag}",
            title=tag, url=f"https://ex.com/{tag}", summary="",
            ticker=None, is_macro=False,
            published_at=pub, fetched_at=fet,
        )
        s.add(item)
        s.flush()
        return item.id


def test_feed_includes_freshly_fetched_but_old_article():
    """The reported bug: published 40h ago, ingested 1h ago. The live feed
    showed it on arrival; the feed list must include it too (via fetched_at),
    while a genuinely-stale item (both clocks old) stays out of a 24h view."""
    _purge()
    fresh = _seed("fresh", published_hours_ago=1, fetched_hours_ago=1)
    backfill = _seed("backfill", published_hours_ago=40, fetched_hours_ago=1)
    stale = _seed("stale", published_hours_ago=40, fetched_hours_ago=40)

    rows = list_recent(hours=24, ticker=None, limit=60, dedupe=False)
    ids = {r["id"] for r in rows}

    assert fresh in ids, "recently-published item must show"
    assert backfill in ids, "freshly-ingested-but-old item must show (the bug)"
    assert stale not in ids, "item old on both clocks stays out of a 24h window"


def test_feed_orders_by_publish_date_desc():
    """Displayed timestamps stay monotonic: even though both arrived together,
    the more-recently-PUBLISHED story leads so the feed reads newest-first."""
    _purge()
    older_pub = _seed("older", published_hours_ago=10, fetched_hours_ago=1)
    newer_pub = _seed("newer", published_hours_ago=2, fetched_hours_ago=1)

    rows = list_recent(hours=24, ticker=None, limit=60, dedupe=False)
    ordered = [r["id"] for r in rows if r["id"] in {older_pub, newer_pub}]
    assert ordered == [newer_pub, older_pub]
