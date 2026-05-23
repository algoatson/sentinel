"""Hacker News ingester per SPEC §7.

Queries Algolia's HN search API (no auth) for each watchlist ticker plus the
issuer's company name. Filters out false-positive hits where neither term
appears in the title, then upserts into HnMention dedupe-by-hn_id.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..db import session_scope
from ..edgar.client import EdgarClient
from ..models import HnMention, Watchlist
from ..utils import TICKER_BLOCKLIST


_ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
_HN_LOOKBACK_HOURS = 6


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": "sentinel/0.1"},
        timeout=20.0,
        follow_redirects=True,
    )


def _term_in_title(term: str, title: str) -> bool:
    """Whole-word match, case-insensitive — avoids `AI` matching `chain`."""
    if not term:
        return False
    return re.search(rf"\b{re.escape(term)}\b", title, flags=re.IGNORECASE) is not None


def _ticker_is_searchable(ticker: str) -> bool:
    """Decide if a ticker symbol is specific enough to use as an Algolia query.

    Single- and double-letter tickers (T, F, V, ON, MA, GE, AI, …) match in
    too many unrelated headlines — searching for "T" returns everything.
    For those we only search by company name.
    """
    if not ticker or len(ticker) < 3:
        return False
    if ticker.upper() in TICKER_BLOCKLIST:
        return False
    return True


def _accept_for_ticker(ticker: str, company: Optional[str], title: str) -> bool:
    """Decide if an HN headline genuinely refers to the given watchlist ticker.

    Rules:
    - Short tickers (<3 char) OR blocklist tickers: REQUIRE the company name
      to appear whole-word in the title. Symbol-only mention is too noisy.
    - Long tickers: accept either the ticker as a whole word OR the company
      name in the title.
    """
    if company and _term_in_title(company, title):
        return True
    if _ticker_is_searchable(ticker) and _term_in_title(ticker, title):
        return True
    return False


def _search(http: httpx.Client, query: str, since_ts: int) -> list[dict]:
    params = {
        "tags": "story",
        "query": query,
        "numericFilters": f"created_at_i>{since_ts}",
        "hitsPerPage": 50,
    }
    try:
        r = http.get(_ALGOLIA, params=params)
        r.raise_for_status()
    except Exception as e:
        logger.warning("HN search '{}' failed: {}", query, e)
        return []
    return r.json().get("hits", []) or []


async def poll_hackernews() -> None:
    try:
        # _poll loops over hundreds of HTTP calls + uses sync httpx.Client.
        # Run in a thread so the event loop stays free for sibling jobs
        # (and the Discord heartbeat).
        await asyncio.to_thread(_poll)
    except Exception as e:
        logger.exception("poll_hackernews top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ HN poll error: {e}")
        except Exception:
            pass


def _poll() -> None:
    edgar = EdgarClient()
    since_ts = int(time.time()) - _HN_LOOKBACK_HOURS * 3600

    with session_scope() as session:
        rows = session.exec(
            select(Watchlist).where(Watchlist.ticker.is_not(None))
        ).all()
        watch_pairs = [(r.ticker, r.cik) for r in rows if r.ticker]

    if not watch_pairs:
        logger.info("HN poll: no tickers on watchlist, skipping")
        return

    new_count = 0
    http = _client()
    try:
        for ticker, cik in watch_pairs:
            # Synthetic crypto/macro CIKs ("X…") have no EDGAR record — skip
            # the lookup entirely instead of 404-ing every cycle.
            company = edgar.get_company_name(cik) if cik.isdigit() else None

            # Build the query list with discriminating power in mind.
            # - 3+ char ticker (not blocklisted) → searchable on its own
            # - 1-2 char or blocklisted ticker → company-name only
            queries: list[tuple[str, str]] = []
            if _ticker_is_searchable(ticker):
                queries.append((ticker, "ticker"))
            if company:
                queries.append((company, "name"))
            if not queries:
                # Nothing safe to search for — skip this ticker.
                continue

            seen_in_batch: set[str] = set()
            for query, _kind in queries:
                hits = _search(http, query, since_ts)
                for hit in hits:
                    hn_id = str(hit.get("objectID") or "")
                    if not hn_id or hn_id in seen_in_batch:
                        continue
                    seen_in_batch.add(hn_id)

                    title = hit.get("title") or ""
                    if not title:
                        continue
                    # Unified acceptance rule — regardless of which query
                    # matched, the title must genuinely refer to this ticker.
                    if not _accept_for_ticker(ticker, company, title):
                        continue

                    created_i = hit.get("created_at_i")
                    if created_i is None:
                        continue
                    created_at = datetime.fromtimestamp(int(created_i), tz=timezone.utc)

                    with session_scope() as session:
                        existing = session.exec(
                            select(HnMention).where(HnMention.hn_id == hn_id)
                        ).first()
                        if existing is not None:
                            continue
                        session.add(
                            HnMention(
                                ticker=ticker,
                                hn_id=hn_id,
                                title=title[:500],
                                url=hit.get("url") or f"https://news.ycombinator.com/item?id={hn_id}",
                                points=int(hit.get("points") or 0),
                                num_comments=int(hit.get("num_comments") or 0),
                                author=hit.get("author") or "",
                                created_at=created_at,
                            )
                        )
                        new_count += 1
    finally:
        http.close()

    logger.info("HN poll: inserted {} new mentions", new_count)
