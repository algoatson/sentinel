"""Upgrade tag-poor news items using the curated tickers on the article PAGE.

RSS items (and search-sourced yfinance items with a thin/empty summary) often
land under-tagged: the resolver only saw a headline. The Yahoo article page,
though, carries a curated tight ticker set (the `stockTickers` blob /
ticker-tag-module anchors / `$cashtag` meta). This job, for recent tag-poor
items whose URL is a Yahoo page, fetches that set via the `article_fetch` cache
(`source_tags.from_html` — no extra request beyond the one body fetch), re-runs
the SAME LLM resolver with the page tags as an anchored candidate set, and
upgrades `ticker` / `tickers_csv` / `tag_source='html+ai'` when the page
surfaces watchlisted names the item was missing.

Conservative + additive: an upgrade only fires when the new set is a strict
SUPERSET of the current tags (we never drop a tag via retag — the page tags
thin text might mislead, so we only add), and the same watchlist gate +
fail-open + per-cycle LLM budget as the live ingester apply. On a real change
we re-link active theses and re-publish the live event, exactly like ingest.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from loguru import logger
from sqlmodel import select

from .. import article_fetch, discord_client
from ..db import session_scope
from ..ingesters.news import _safe_link_news, _safe_publish_news
from ..models import NewsItem, Watchlist
from ..news_tickers import resolve_article_tickers
from ..utils import format_tickers_csv, parse_tickers_csv

# Only look back a few days — older items are unlikely to be re-read and the
# article page may have rotated. Keeps the candidate set + fetch volume bounded.
_RETAG_WINDOW_DAYS = 3
# Hard cap on candidates examined per run (each may cost one page fetch).
_RETAG_LIMIT = 60
# Per-run LLM budget — mirrors the ingester's per-poll cap. One light JSON call
# per genuinely-upgradeable item; beyond it we stop (next run continues).
_AI_BUDGET_PER_RUN = 40


def _is_yahoo(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == "yahoo.com" or host.endswith(".yahoo.com")


async def run_news_retag() -> None:
    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("run_news_retag top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ news retag error: {e}")
        except Exception:
            pass


def _run() -> None:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_RETAG_WINDOW_DAYS)
    ).replace(tzinfo=None)

    with session_scope() as session:
        watch = sorted({
            r.ticker
            for r in session.exec(
                select(Watchlist).where(Watchlist.ticker.is_not(None))
            ).all()
            if r.ticker
        })
    watch_set = set(watch)

    # Recent Yahoo-page items not already page-tagged. Tag-poverty (fewer than
    # two tickers) is refined in Python — counting CSV entries in SQL is fiddly.
    with session_scope() as session:
        candidates = session.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff)
            .where(NewsItem.url.like("%yahoo.com%"))
            .where(
                (NewsItem.tag_source.is_(None))
                | (NewsItem.tag_source != "html+ai")
            )
            .order_by(NewsItem.published_at.desc())
            .limit(_RETAG_LIMIT)
        ).all()
        # Detach the plain values we need so we can work outside the txn.
        rows = [
            (c.id, c.title or "", c.summary or "", c.url or "", c.ticker,
             c.tickers_csv, c.source)
            for c in candidates
            if _is_yahoo(c.url or "")
            and len(parse_tickers_csv(c.tickers_csv)) < 2
        ]

    upgraded = 0
    ai_budget = _AI_BUDGET_PER_RUN
    for news_id, title, summary, url, cur_ticker, cur_csv, src in rows:
        try:
            html_tags = article_fetch.fetch_article_tags(url)
        except Exception as e:
            logger.debug("news_retag fetch_article_tags failed for {}: {}", news_id, e)
            continue
        # Watchlisted page tags only — if the page added nothing trackable
        # beyond what we already have, there's nothing to upgrade (skip BEFORE
        # spending an LLM call).
        page_watch = [t for t in html_tags if t in watch_set]
        cur_set = set(parse_tickers_csv(cur_csv))
        if cur_ticker:
            cur_set.add(cur_ticker)
        if not page_watch or set(page_watch) <= cur_set:
            continue

        resolved = resolve_article_tickers(
            title, summary, watch,
            source_tickers=html_tags, feed_ticker=cur_ticker,
            source_label="html", allow_ai=ai_budget > 0,
        )
        if resolved.used_ai:
            ai_budget -= 1

        new_set = set(resolved.ranked)
        # Additive only: fire just when the page strictly EXPANDS the tag set.
        # A superset guarantees no existing tag is lost; a non-superset means
        # the (thin-text) resolver disagreed — leave the item as-is.
        if not (new_set > cur_set):
            continue

        new_csv = format_tickers_csv(resolved.ranked)
        with session_scope() as session:
            item = session.get(NewsItem, news_id)
            if item is None:
                continue
            item.ticker = resolved.primary
            item.tickers_csv = new_csv
            item.tag_source = "html+ai"
            session.add(item)
        upgraded += 1
        # Re-link active theses on the now-fuller ticker set + re-publish so the
        # live dashboard reflects the change — same funnels as ingest.
        _safe_link_news(news_id)
        _safe_publish_news(news_id, resolved.primary, title, src or "yfinance", None)

    logger.info(
        "news retag: upgraded {} of {} candidate items", upgraded, len(rows)
    )
