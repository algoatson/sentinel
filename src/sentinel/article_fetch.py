"""Fetch + extract the body text of a news article URL — cached.

Why this exists: the news ingester stores `NewsItem.title` and a usually-
thin `summary` from the RSS feed. When the LLM is asked to "summarise
this news for trading", it sees the title + a 100-char marketing blurb
and confabulates. The user's complaint was exactly this — "the AI isn't
really aware of the article in question, it's aware of the title and
that's about it".

Strategy (in order):

1. **Direct fetch + heuristic extraction**: `httpx` with a browser UA,
   BeautifulSoup to strip script/style/nav/header/footer/aside, then
   pick the largest body element (`<article>`, `<main>`, or the longest
   `<div>` by text content). Works for ~70% of our news sources, zero
   new deps.

2. **Jina Reader fallback**: when extraction yields < ~600 chars (likely
   a paywall / JS-only page), we try `https://r.jina.ai/<url>` — a free,
   keyless service that handles the harder cases (paywalls, dynamic
   content). Adds latency but boosts coverage to ~90%+ of sources.

Results are persisted in `ArticleBody` keyed by URL. Successful fetches
are de-facto permanent (article URLs don't typically change content),
"stub" entries (we tried, got nothing) hang around so we don't waste
time re-fetching on every dossier open — pass `force=True` to retry.

No external API key. ~50ms on cache hit, ~500-2000ms on cache miss.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from . import source_tags
from .db import session_scope
from .models import ArticleBody
from .utils import format_tickers_csv, parse_tickers_csv


# Conservative timeouts. Real-world news sites can be slow, but we're
# in a UX-blocking path (the dossier modal is waiting on this), so we
# bound the total wait. The Jina fallback is even slower, so it gets
# more headroom.
_DIRECT_TIMEOUT: Final = 6.0
_JINA_TIMEOUT: Final = 12.0

# Below this many characters of extracted text, we treat the direct
# fetch as a "stub" (paywall, JS-only, or our heuristic missed) and
# fall through to Jina. ~600 chars is roughly a single news paragraph;
# anything shorter isn't worth giving the LLM as "the article".
_STUB_THRESHOLD_CHARS: Final = 600

# Hard cap on stored body — keeps the DB sane and the LLM context budget
# manageable. ~6k chars ≈ ~1500 tokens of source for a typical dossier.
_MAX_BODY_CHARS: Final = 6000

# Browser-ish UA. Some publishers serve JS-only stubs to obvious bots
# (curl/python-requests). Pretending to be a current Chrome on macOS
# gets through most aggressive bot-walls without anything sketchier.
_UA: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Tag families we strip wholesale before measuring body content —
# they are NEVER article body, so leaving them in only confuses the
# largest-element heuristic.
_STRIP_TAGS: Final = (
    "script", "style", "noscript", "iframe",
    "nav", "header", "footer", "aside",
    "form", "button", "svg", "input",
)


def fetch_article_text(url: str, *, force: bool = False) -> str | None:
    """Fetch + extract the body of a news article. Returns the text on
    success, None on total failure. Cache-hit on second+ call.

    `force=True` bypasses the cache and re-fetches — useful for the
    dossier modal's "regenerate" button when the cached body is a
    known stub."""
    if not url:
        return None
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return None

    if not force:
        cached = _cache_get(url)
        if cached is not None:
            return cached

    # Direct attempt. `tags` are scraped from the SAME html as the body (no
    # extra request) and cached on every branch below — even a paywall stub
    # often still carries the ticker-tag module in its <head>.
    text, tags, source = _try_direct(url)
    if text and len(text) >= _STUB_THRESHOLD_CHARS:
        body = text[:_MAX_BODY_CHARS]
        _cache_put(url, body, source, tags)
        return body

    # Jina fallback — preserves whatever the direct fetch got as a
    # context bonus to the model (sometimes the page DOES have the
    # title + dek that Jina misses).
    jina_text = _try_jina(url)
    if jina_text:
        body = jina_text[:_MAX_BODY_CHARS]
        _cache_put(url, body, "jina", tags)
        return body

    # Neither worked. Persist a stub row so we don't burn 6+ seconds
    # on the same URL every time the dossier opens.
    if text:
        _cache_put(url, text[:_MAX_BODY_CHARS], "stub", tags)
        return text
    _cache_put(url, "", "stub", tags)
    return None


def fetch_article_tags(url: str, *, force: bool = False) -> list[str]:
    """Curated ticker tags scraped off the article PAGE, cached alongside the
    body (`source_tags.from_html`). Triggers a single fetch when not yet
    extracted (NULL `tags_csv`), then returns the normalised set. Fail-open →
    `[]`. The retag job uses this to upgrade tag-poor NewsItems."""
    if not url:
        return []
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return []
    row = _cache_get_row(url)
    if force or row is None or row.tags_csv is None:
        # Body + tags come from one fetch; force it so tags get computed even
        # when a pre-feature body row is already cached.
        fetch_article_text(url, force=True)
        row = _cache_get_row(url)
    return parse_tickers_csv(row.tags_csv) if (row and row.tags_csv) else []


# ── direct fetch + heuristic extraction ──────────────────────────────────


def _try_direct(url: str) -> tuple[str, list[str], str]:
    """Return `(text, tags, source)` from a direct httpx + bs4 extraction.
    `tags` are the curated page ticker tags pulled from the SAME html (empty
    list when none / on failure). Empty text on any failure; `source` is
    "direct" on any attempt."""
    try:
        with httpx.Client(timeout=_DIRECT_TIMEOUT, follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": _UA})
        if r.status_code >= 400:
            logger.debug("article_fetch direct {} → HTTP {}", url, r.status_code)
            return ("", [], "direct")
        text = _extract_body(r.text)
        tags = source_tags.from_html(r.text)
    except Exception as e:
        logger.debug("article_fetch direct {} raised: {}", url, e)
        return ("", [], "direct")
    return (text, tags, "direct")


def _extract_body(html: str) -> str:
    """Strip boilerplate then pick the largest text-bearing element.

    The heuristic order matters: <article> wins outright when present;
    otherwise we look at <main> or the biggest <div>. Most modern news
    sites have ONE of these, so this gets us close to readability-lxml
    quality without the dep. Falls back to whole-body text on weird
    pages so we always emit something.
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml parser bombed on malformed HTML — try the stdlib fallback.
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return ""

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    # Preferred semantic containers first.
    for selector in ("article", "main", "[role='main']"):
        candidate = soup.select_one(selector)
        if candidate is not None:
            text = _clean(candidate.get_text("\n", strip=True))
            if len(text) >= _STUB_THRESHOLD_CHARS:
                return text

    # Largest content `<div>` by text length — works for sites that
    # don't use semantic tags. Cap the candidate set so we don't iterate
    # every div on a 5MB page.
    best = ""
    for div in soup.find_all("div", limit=2000):
        candidate_text = div.get_text("\n", strip=True)
        if len(candidate_text) > len(best):
            best = candidate_text
    cleaned = _clean(best)
    if len(cleaned) >= _STUB_THRESHOLD_CHARS:
        return cleaned

    # Last-ditch: full body. Better something than nothing — Jina will
    # likely supersede this on the fallback step.
    body = soup.body
    if body is None:
        return _clean(soup.get_text("\n", strip=True))
    return _clean(body.get_text("\n", strip=True))


def _clean(text: str) -> str:
    """Collapse runs of whitespace, drop empty lines. Bs4's
    `get_text("\\n", strip=True)` already strips per-line; this just
    collapses the multi-newlines that nested boilerplate leaves behind."""
    out_lines: list[str] = []
    last_blank = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if not last_blank:
                out_lines.append("")
            last_blank = True
        else:
            out_lines.append(line)
            last_blank = False
    return "\n".join(out_lines).strip()


# ── Jina Reader fallback ─────────────────────────────────────────────────


def _try_jina(url: str) -> str | None:
    """Try `https://r.jina.ai/<url>` — a free reader API that handles
    paywalls / JS-only sites the direct path can't. Markdown output;
    we keep it as-is since the LLM consumes markdown well.

    No API key required at low volume. If they ever rate-limit us, the
    fallback simply returns None and the dossier falls back to title-
    only — which is what we have today, so the only downside is "no
    improvement", not regression."""
    proxied = f"https://r.jina.ai/{url}"
    try:
        with httpx.Client(timeout=_JINA_TIMEOUT, follow_redirects=True) as c:
            r = c.get(proxied, headers={"User-Agent": _UA, "Accept": "text/plain"})
        if r.status_code >= 400:
            logger.debug("article_fetch jina {} → HTTP {}", url, r.status_code)
            return None
        text = (r.text or "").strip()
        if not text:
            return None
        # Jina prepends a small header like "Title: ...\nURL Source: ...\n\n";
        # leave it — it's useful context for the LLM and we cap by
        # `_MAX_BODY_CHARS` anyway.
        return text
    except Exception as e:
        logger.debug("article_fetch jina {} raised: {}", url, e)
        return None


# ── cache (ArticleBody table) ────────────────────────────────────────────


def _cache_get(url: str) -> str | None:
    """Return cached body for URL or None. "stub" rows return their
    (possibly empty) body so callers can decide whether to `force` a
    retry; that's a UX call that doesn't belong here."""
    try:
        with session_scope() as s:
            row = s.get(ArticleBody, url)
            if row is None:
                return None
            return row.body or None
    except Exception as e:
        logger.debug("article_fetch cache_get failed for {}: {}", url, e)
        return None


def _cache_get_row(url: str) -> ArticleBody | None:
    """Return the full cache row (detached; `expire_on_commit=False`) or None.
    Used by `fetch_article_tags` to read `tags_csv` + decide on re-extraction."""
    try:
        with session_scope() as s:
            return s.get(ArticleBody, url)
    except Exception as e:
        logger.debug("article_fetch cache_get_row failed for {}: {}", url, e)
        return None


def _cache_put(url: str, body: str, source: str, tags: list[str] | None = None) -> None:
    """Upsert the cache row. Best-effort — a failed write is logged but
    doesn't bubble (next call just re-fetches). `tags_csv` records the page
    ticker tags: a packed ",X,Y," set, or "" to mark "extracted, none found"
    (distinct from NULL = never extracted), so we don't re-fetch a tagless
    page forever."""
    try:
        now = datetime.now(timezone.utc)
        tags_csv = (format_tickers_csv(tags) or "") if tags is not None else None
        with session_scope() as s:
            row = s.get(ArticleBody, url)
            if row is None:
                s.add(ArticleBody(
                    url=url, body=body, source=source,
                    fetched_at=now, char_count=len(body), tags_csv=tags_csv,
                ))
            else:
                # Never let a re-fetch DOWNGRADE a good body to a stub — a
                # tags-only re-fetch (fetch_article_tags) or a transient
                # paywall/timeout shouldn't wipe a body the dossier already
                # had. Tags still update either way; stub→good is allowed.
                downgrade = (
                    source == "stub"
                    and row.source != "stub"
                    and row.char_count >= _STUB_THRESHOLD_CHARS
                )
                if not downgrade:
                    row.body = body
                    row.source = source
                    row.fetched_at = now
                    row.char_count = len(body)
                if tags_csv is not None:
                    row.tags_csv = tags_csv
                s.add(row)
    except Exception as e:
        logger.debug("article_fetch cache_put failed for {}: {}", url, e)


# ── inspection helper for the dashboard ───────────────────────────────────


def cache_meta(url: str) -> dict | None:
    """Return {"source", "fetched_at", "char_count"} for a URL's cache
    entry, or None if not cached. Lets the dossier modal show "fetched
    via Jina, 4321 chars" without reloading the body."""
    try:
        with session_scope() as s:
            row = s.get(ArticleBody, url)
            if row is None:
                return None
            return {
                "source": row.source,
                "fetched_at": (
                    row.fetched_at if row.fetched_at.tzinfo
                    else row.fetched_at.replace(tzinfo=timezone.utc)
                ).isoformat(),
                "char_count": row.char_count,
            }
    except Exception as e:
        logger.debug("article_fetch cache_meta failed for {}: {}", url, e)
        return None
