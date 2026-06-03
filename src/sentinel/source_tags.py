"""Structured ticker tags from Yahoo — the high-recall candidate set the LLM
ticker resolver reasons FROM.

The tickerless yfinance feed (`Ticker(t).news`) drops the structured tickers,
but the Yahoo v1 *search* endpoint still carries them on each news item as
`relatedTickers` — the "might be affected" set, subject-first, with the query
ticker injected (search NVDA → a Walmart story comes back ``['WMT','NVDA',
'COST','TGT']``). We use that as an ANCHORED shortlist for
`news_tickers.resolve_article_tickers`: the LLM still reads the headline and
decides, this just seeds it with structured priors and lets the caller demote
the query contamination.

Two sources live here:

- `related_tickers_for(query)` — one `httpx` GET to the v1 search API, returns
  per-article `{title, url, uuid, pub, related}` with `related` already
  normalised (Phase 1, the per-poll source).
- `from_html(html)` — parse the curated tight ticker set off a fetched Yahoo
  article page (Phase 2, used by the `news_retag` upgrade job).

Everything is deterministic except the one network GET, and every entry point
is fail-open: a fetch / parse error yields an empty list, never an exception,
so a news poll degrades to the heuristic+LLM path instead of crashing.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx
from loguru import logger

from .utils import normalize_symbol

# Yahoo's public search endpoint. `quotesCount=0` keeps the payload to just the
# news[] array we want; `newsCount` bounds it. query2 is the more stable of the
# two CDNs per the project's experience.
_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_SEARCH_TIMEOUT = 8.0

# Browser-ish UA — Yahoo's JSON endpoints 403 obvious bot UAs (python-httpx/…).
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def normalize(sym: str) -> Optional[str]:
    """Canonicalize one raw symbol to the watchlist's storage form, or None if
    it's a foreign/private/index token we never track. Thin delegate to the
    shared `utils.normalize_symbol` so normalization lives in exactly one place."""
    return normalize_symbol(sym)


def related_tickers_for(query: str, *, news_count: int = 10) -> list[dict]:
    """Hit the v1 search API for ``query`` and return its news items as
    ``[{title, url, uuid, pub, related}]`` — order preserved, ``related`` the
    normalised `relatedTickers` (deduped, subject-first, query ticker KEPT so
    the caller can demote it via ``feed_ticker``).

    Fail-open: any network / HTTP / parse error → ``[]``."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        r = httpx.get(
            _SEARCH_URL,
            params={"q": q, "newsCount": news_count, "quotesCount": 0},
            headers={"User-Agent": _UA},
            timeout=_SEARCH_TIMEOUT,
            follow_redirects=True,
        )
        if r.status_code >= 400:
            logger.debug("source_tags search {} → HTTP {}", q, r.status_code)
            return []
        data = r.json()
    except Exception as e:
        logger.debug("source_tags search {} failed: {}", q, e)
        return []

    out = _parse_search_news(data)
    # Coverage telemetry for the Pi: if the API answered but carried no
    # structured tickers at all, the anchoring is silently doing nothing — log
    # it once per poll-ticker so a Yahoo schema change stays observable.
    if out and not any(item["related"] for item in out):
        logger.info("source_tags: search for {} returned no relatedTickers", q)
    return out


def _parse_search_news(data: object) -> list[dict]:
    """Pull + normalise the news[] array out of a v1 search response. Pure;
    tolerant of any missing/odd field (returns what it can, never raises)."""
    news = data.get("news") if isinstance(data, dict) else None
    if not isinstance(news, list):
        return []
    out: list[dict] = []
    for item in news:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("link") or "").strip()
        if not title or not url:
            continue
        related: list[str] = []
        for sym in item.get("relatedTickers") or []:
            if not isinstance(sym, str):
                continue
            n = normalize(sym)
            if n and n not in related:
                related.append(n)
        out.append({
            "title": title,
            "url": url,
            "uuid": item.get("uuid") or "",
            "pub": item.get("providerPublishTime"),
            "related": related,
        })
    return out


# ── article-page HTML tags (Phase 2) ─────────────────────────────────────────
# A fetched Yahoo article page carries a curated, tight ticker set in three
# spots — high precision, no extra network call once the page is in the
# `article_fetch` cache:
#   1. an inline ``"stockTickers":[{"symbol":"H"},…]`` JSON blob
#   2. ``data-symbol="BTC-USD"`` ticker-tag-module anchors
#   3. a ``$bnb-usd;$h;$btc-usd`` meta hashtag string
# We union all three, normalise, and dedupe (first-seen order). The result is a
# hint set for the same LLM resolver — never stored unvalidated.
_STOCK_TICKERS_RE = re.compile(r'"stockTickers"\s*:\s*(\[.*?\])', re.DOTALL)
_DATA_SYMBOL_RE = re.compile(r'data-symbol="([^"]+)"')
_HASHTAG_RE = re.compile(r"\$([A-Za-z0-9.\-]{1,12})(?=[;\s\"'<]|$)")


def from_html(html: str) -> list[str]:
    """Extract the curated ticker set from a Yahoo article page, normalised +
    deduped (first-seen order). Returns ``[]`` on empty/garbage input — pure
    and fail-open."""
    if not html:
        return []
    out: list[str] = []

    def _add(raw: str) -> None:
        n = normalize(raw)
        if n and n not in out:
            out.append(n)

    # 1. stockTickers JSON blob — symbols live under "symbol".
    try:
        for blob in _STOCK_TICKERS_RE.findall(html):
            parsed = json.loads(blob)
            if isinstance(parsed, list):
                for entry in parsed:
                    if isinstance(entry, dict) and entry.get("symbol"):
                        _add(str(entry["symbol"]))
                    elif isinstance(entry, str):
                        _add(entry)
    except Exception as e:
        logger.debug("source_tags.from_html stockTickers parse failed: {}", e)

    # 2. data-symbol anchors (ticker-tag-module).
    for sym in _DATA_SYMBOL_RE.findall(html):
        _add(sym)

    # 3. $cashtag meta hashtag string.
    for sym in _HASHTAG_RE.findall(html):
        _add(sym)

    return out
