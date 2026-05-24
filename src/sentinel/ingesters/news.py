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
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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

# Cross-source dedup window. yfinance + RSS often republish the same
# underlying article URL (typically Reuters/CNBC/etc) under different
# `external_id`s, so the existing source+id unique constraint doesn't
# catch them. 24h is generous — most dups land within minutes of each
# other but news stories can re-syndicate over a day or two.
_DEDUP_WINDOW_HOURS = 24

# Query tracking-parameter blocklist for canonicalisation. Adapted
# from the consumer-grade trackers; conservative on what we drop so
# functional query strings (article_id=…, page=…) survive.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content",
    "utm_term", "utm_id", "utm_name", "utm_brand",
    "fbclid", "gclid", "msclkid", "yclid", "twclid", "dclid",
    "mc_cid", "mc_eid", "_ga", "_gl",
    "ref", "ref_src", "ref_url", "source", "via", "ncid",
    "s_cid", "yptr", "mod", "smid", "smtyp", "guccounter",
    "soc_src", "soc_trk", "__source", "__twitter_impression",
    "campaign_id",
})


def canonical_url(url: str) -> str:
    """Stable form of a URL for cross-source de-dup.

    - Lowercase scheme + host (case sensitivity on those is folklore).
    - Drop fragment (#section-id) — never disambiguates the article.
    - Drop tracking query params (utm_*, fbclid, etc.) but KEEP
      functional ones (article_id, page, etc.) so a legitimate
      "?id=42 vs ?id=43" stays distinguishable.
    - Strip trailing slash on the path (so /a and /a/ collapse).
    Returns the original string on any parse error — safer to keep a
    poorly-formed URL than to crash the ingester."""
    if not url:
        return url
    try:
        p = urlparse(url.strip())
    except Exception:
        return url
    if not p.scheme or not p.netloc:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    path = p.path
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((
        p.scheme.lower(),
        p.netloc.lower(),
        path,
        "",                                     # drop params (rarely used)
        urlencode(kept, doseq=True),
        "",                                     # drop fragment
    ))


def _recent_canonicals(session, hours: int = _DEDUP_WINDOW_HOURS) -> set[str]:
    """Set of canonical URLs ingested in the last `hours`. Used to
    skip cross-source dups. Built once per ingest cycle; per-item
    membership check is O(1)."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    rows = session.exec(
        select(NewsItem.url).where(NewsItem.fetched_at >= cutoff_naive)
    ).all()
    return {canonical_url(u) for u in rows if u}


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

    # Lazy-built set of canonical URLs ingested in the last 24h, used to
    # drop cross-source dups (a yfinance article from earlier this cycle
    # vs the same URL coming through Reuters RSS now). Built on first
    # need inside the loop's session_scope.
    seen_urls: set[str] | None = None
    skipped_dup = 0

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
                # Cross-source URL dedup — yfinance + RSS both republish
                # the same underlying Reuters/CNBC URL under different
                # `external_id`s, leaving the feed cluttered with
                # duplicates the user can't easily tell apart.
                # `_recent_canonicals` is rebuilt per session_scope so
                # we don't reuse a stale snapshot between iterations.
                if seen_urls is None:
                    seen_urls = _recent_canonicals(session)
                canon = canonical_url(link)
                if canon in seen_urls:
                    skipped_dup += 1
                    continue
                seen_urls.add(canon)
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

    logger.info(
        "news RSS: inserted {} new items across {} feeds (skipped {} url dups)",
        new_count, len(feeds), skipped_dup,
    )


def _poll_yfinance() -> None:
    """Per-ticker news via yfinance. Polled less frequently than RSS — it's
    one network call per ticker. We cap at the most-active tickers (those
    with recent watchlist updates) to stay polite.
    """
    # Same lazy cross-source dedup as `_poll_rss`. Built per-cycle so it
    # picks up anything `_poll_rss` (which ran first in `poll_news`) just
    # inserted — that's where 90% of the yfinance↔Reuters/CNBC overlap
    # actually shows up.
    seen_urls: set[str] | None = None
    skipped_dup = 0
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
                if seen_urls is None:
                    seen_urls = _recent_canonicals(session)
                canon = canonical_url(url)
                if canon in seen_urls:
                    skipped_dup += 1
                    continue
                seen_urls.add(canon)
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

    logger.info(
        "news yfinance: inserted {} new items across {} tickers (skipped {} url dups)",
        new_count, len(tickers), skipped_dup,
    )
