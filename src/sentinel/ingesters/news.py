"""News ingester — RSS feeds + yfinance per-ticker stories.

Two sources, one table (NewsItem):

1. Macro RSS feeds (config/news_feeds.yaml) — Fed, geopolitics, sector policy.
   These supply geopolitical context that filings/HN/Reddit don't.
2. yfinance.Ticker(t).news — per-watchlist-ticker headlines, free.

Ticker extraction from RSS titles uses the watchlist allowlist (same rules
as Reddit/HN). Macro feeds are tagged is_macro=True regardless of ticker
match so the macro_themes pipeline can pull them cleanly.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import feedparser
import yaml
import yfinance as yf
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import CONFIG_DIR
from ..db import session_scope
from ..models import NewsItem, Watchlist
from ..utils import extract_tickers


_FEEDS_PATH = CONFIG_DIR / "news_feeds.yaml"


def _stable_id(source: str, identifier: str) -> str:
    h = hashlib.sha256(f"{source}:{identifier}".encode()).hexdigest()[:32]
    return f"{source}:{h}"


def _parse_published(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed"):
        val = entry.get(field)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return datetime.now(timezone.utc)


async def poll_news() -> None:
    try:
        await asyncio.to_thread(_poll_rss)
        await asyncio.to_thread(_poll_yfinance)
    except Exception as e:
        logger.exception("poll_news top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ news poll error: {e}")
        except Exception:
            pass


def _poll_rss() -> None:
    if not _FEEDS_PATH.exists():
        logger.warning("config/news_feeds.yaml missing — skipping macro feeds")
        return
    cfg = yaml.safe_load(_FEEDS_PATH.read_text()) or {}
    feeds = cfg.get("feeds") or []
    if not feeds:
        return

    with session_scope() as session:
        watch_tickers = sorted({
            r.ticker
            for r in session.exec(
                select(Watchlist).where(Watchlist.ticker.is_not(None))
            ).all()
            if r.ticker
        })

    new_count = 0
    for feed_cfg in feeds:
        name = feed_cfg.get("name") or "rss"
        url = feed_cfg.get("url")
        is_macro = bool(feed_cfg.get("macro", False))
        if not url:
            continue
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            logger.warning("RSS fetch failed for {}: {}", name, e)
            continue

        source = f"rss:{name}"
        for entry in parsed.entries[:50]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            summary = (entry.get("summary") or "")[:1000]
            published_at = _parse_published(entry)
            ext_id = _stable_id(source, entry.get("id") or link)

            tickers_in_title = extract_tickers(
                f"{title} {summary}", watch_tickers, title=title
            )
            ticker = next(iter(tickers_in_title)) if tickers_in_title else None

            with session_scope() as session:
                existing = session.exec(
                    select(NewsItem).where(NewsItem.external_id == ext_id)
                ).first()
                if existing is not None:
                    continue
                session.add(
                    NewsItem(
                        source=source,
                        external_id=ext_id,
                        title=title[:500],
                        url=link[:1000],
                        summary=summary,
                        ticker=ticker,
                        is_macro=is_macro and ticker is None,
                        published_at=published_at,
                        fetched_at=datetime.now(timezone.utc),
                    )
                )
                new_count += 1

    logger.info("news RSS: inserted {} new items across {} feeds", new_count, len(feeds))


def _poll_yfinance() -> None:
    """Per-ticker news via yfinance. Polled less frequently than RSS — it's
    one network call per ticker. We cap at the most-active tickers (those
    with recent watchlist updates) to stay polite.
    """
    with session_scope() as session:
        rows = session.exec(
            select(Watchlist)
            .where(Watchlist.ticker.is_not(None))
            .order_by(Watchlist.added_at.desc())
            .limit(200)
        ).all()
        tickers = sorted({r.ticker for r in rows if r.ticker})

    new_count = 0
    for ticker in tickers:
        # yfinance uses dashes for class shares (BRK-B); watchlist stores dots.
        yf_ticker = ticker.replace(".", "-")
        try:
            yt = yf.Ticker(yf_ticker)
            news_items = getattr(yt, "news", None) or []
        except Exception as e:
            logger.debug("yfinance news failed for {}: {}", ticker, e)
            continue

        for item in news_items[:10]:
            # yfinance changed shape over versions — try both styles.
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, dict):
                title = content.get("title") or ""
                url = (content.get("canonicalUrl") or {}).get("url") or content.get("clickThroughUrl", {}).get("url", "")
                summary = content.get("summary") or content.get("description") or ""
                pub = content.get("pubDate") or content.get("displayTime")
                external_id = content.get("id") or item.get("uuid") or url
            else:
                title = item.get("title") or ""
                url = item.get("link") or item.get("url") or ""
                summary = item.get("summary") or ""
                pub = item.get("providerPublishTime")
                external_id = item.get("uuid") or item.get("id") or url

            if not title or not url:
                continue
            try:
                if isinstance(pub, (int, float)):
                    published_at = datetime.fromtimestamp(int(pub), tz=timezone.utc)
                elif isinstance(pub, str):
                    published_at = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                else:
                    published_at = datetime.now(timezone.utc)
            except (TypeError, ValueError):
                published_at = datetime.now(timezone.utc)

            ext_id = _stable_id("yfinance", str(external_id))
            with session_scope() as session:
                existing = session.exec(
                    select(NewsItem).where(NewsItem.external_id == ext_id)
                ).first()
                if existing is not None:
                    continue
                session.add(
                    NewsItem(
                        source="yfinance",
                        external_id=ext_id,
                        title=title[:500],
                        url=url[:1000],
                        summary=(summary or "")[:1000],
                        ticker=ticker,
                        is_macro=False,
                        published_at=published_at,
                        fetched_at=datetime.now(timezone.utc),
                    )
                )
                new_count += 1

    logger.info("news yfinance: inserted {} new items across {} tickers", new_count, len(tickers))
