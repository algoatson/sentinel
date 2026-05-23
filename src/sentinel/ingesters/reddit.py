"""Reddit ingester via public RSS (SPEC §7) — no API / OAuth.

This is the social arm feeding the convergence + social-pulse synthesis
("central brain"). Two fetch paths, tried in order per subreddit:

1. Direct `https://www.reddit.com/r/<sub>/new/.rss`. Full fidelity (post id,
   body, author, timestamp). Reddit hard-blocks this from datacenter IPs
   (HTTP 403) but it generally works from a residential connection — which is
   where this bot runs.
2. Fallback: Google News `site:reddit.com/r/<sub>` RSS. Not IP-blocked.
   Title-only (Google indexes the popular posts), no body/author, but enough
   to capture which tickers a community is buzzing about.

Limits handled: no score/comments in RSS → stored 0 (signal is mention
volume + which community, not karma); dead/private subs 404 → skipped.

Ticker extraction reuses utils.extract_tickers against the watchlist and is
crypto/futures-aware: `$BTC`→`BTC-USD`, `ES`→`ES=F`.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import random
import time
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import CONFIG_DIR, settings
from ..db import session_scope
from ..models import RedditMention, Watchlist
from ..utils import extract_tickers


_SUBS_PATH = CONFIG_DIR / "subreddits.yaml"
_DIRECT_TMPL = "https://www.reddit.com/r/{sub}/new/.rss?limit=25"
_GNEWS_TMPL = (
    "https://news.google.com/rss/search?"
    "q=site:reddit.com/r/{sub}&hl=en-US&gl=US&ceid=US:en"
)
_PER_FEED_SLEEP = 0.8  # politeness; runs off the event loop in a thread
_MAX_ENTRIES = 25

# Reddit's RSS block keys partly on a stale/datacenter-looking UA. Rotate a
# small pool of current desktop-browser strings so the fetcher doesn't look
# like one fixed scraper hammering 58 feeds on a fixed cadence.
_UA_POOL = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 "
    "Firefox/124.0",
)
_UA_CYCLE = itertools.cycle(_UA_POOL)


def _pick_ua() -> str:
    return settings.REDDIT_USER_AGENT or next(_UA_CYCLE)


# Circuit breaker: once direct fetches start 403-ing in bulk, stop hammering
# Reddit (which only deepens the IP/UA block) and serve the rest of the cycle
# from the gnews fallback. Probe direct again only after a cooldown.
_DIRECT_403_TRIP = 5          # consecutive 403s in a cycle → trip the breaker
_DIRECT_COOLDOWN = timedelta(minutes=20)
_direct_cooldown_until: datetime | None = None
_BROWSER_UA = _UA_POOL[0]     # gnews UA (gnews isn't IP-blocked; static is fine)


async def poll_reddit() -> None:
    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("poll_reddit top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ reddit poll error: {e}")
        except Exception:
            pass


def _load_subs() -> list[str]:
    if not _SUBS_PATH.exists():
        logger.warning("{} missing — no subreddits polled", _SUBS_PATH)
        return []
    cfg = yaml.safe_load(_SUBS_PATH.read_text()) or {}
    seen: set[str] = set()
    out: list[str] = []
    for s in cfg.get("subreddits") or []:
        s = str(s).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _build_alias_map() -> tuple[list[str], dict[str, str]]:
    """Return (match_tokens, alias→canonical).

    Equities match as-is. Crypto/futures store yfinance tickers (BTC-USD,
    ES=F) that never appear verbatim on Reddit, so we also accept the bare
    base symbol and map it back to the canonical watchlist ticker.
    """
    with session_scope() as s:
        rows = s.exec(
            select(Watchlist).where(Watchlist.ticker.is_not(None))
        ).all()
        pairs = [(r.ticker, r.asset_class or "equity") for r in rows if r.ticker]

    tokens: set[str] = set()
    alias: dict[str, str] = {}
    for ticker, cls in pairs:
        tokens.add(ticker.upper())
        if cls in ("crypto", "future"):
            base = ticker.split("-")[0].split("=")[0].upper()
            if base and base != ticker.upper():
                tokens.add(base)
                alias.setdefault(base, ticker)
    return sorted(tokens), alias


def _strip_html(html: str) -> str:
    try:
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    except Exception:
        return html


# ── top-comment enrichment (lazy, breaker-aware) ────────────────────────────
# The post is the question; the *answers* are often the signal. We do NOT
# fetch comments in the bulk poll (that would be 25×N keyless JSON hits →
# instant Reddit ban). Callers (reddit_feed) fetch lazily, only for the few
# already-notable candidates, and only while the direct breaker is closed.
_COMMENT_LIMIT = 5
_COMMENT_MAXLEN = 300
_JUNK_AUTHORS = {"automoderator", "[deleted]", "visualmod"}


def direct_blocked() -> bool:
    """True while the Reddit direct-fetch circuit breaker is open — callers
    must not attempt per-post comment JSON during a block (it only deepens
    the IP/UA penalty)."""
    return (
        _direct_cooldown_until is not None
        and datetime.now(timezone.utc) < _direct_cooldown_until
    )


def _parse_comments(payload, limit: int = _COMMENT_LIMIT) -> list[str]:
    """Pure: a Reddit `<permalink>.json` body → up to `limit` top real
    comments as `(+score) text` strings. Drops removed/deleted, bot and
    stickied noise, and trivially short replies. No network."""
    try:
        children = payload[1]["data"]["children"]
    except (TypeError, KeyError, IndexError):
        return []
    rows: list[tuple[int, str]] = []
    for ch in children:
        if not isinstance(ch, dict) or ch.get("kind") != "t1":
            continue
        d = ch.get("data") or {}
        body = (d.get("body") or "").strip()
        author = (d.get("author") or "").strip()
        if (
            not body
            or body in ("[deleted]", "[removed]")
            or d.get("stickied")
            or author.lower() in _JUNK_AUTHORS
            or len(body) < 25
        ):
            continue
        score = d.get("score")
        score = score if isinstance(score, int) else 0
        text = " ".join(body.split())
        if len(text) > _COMMENT_MAXLEN:
            text = text[:_COMMENT_MAXLEN].rstrip() + "…"
        rows.append((score, text))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [f"(+{s}) {t}" for s, t in rows[:limit]]


def fetch_top_comments(permalink: str, *, limit: int = _COMMENT_LIMIT) -> list[str]:
    """Best-effort top comments for one Reddit thread. Returns [] on anything
    that isn't a clean win (non-Reddit URL, breaker open, non-200, parse
    fail) — never raises. Honours the same UA rotation + circuit breaker as
    the bulk poller."""
    if not permalink or "reddit.com" not in permalink or "/comments/" not in permalink:
        return []  # gnews-sourced / non-thread permalink — no comments to get
    if direct_blocked():
        return []
    url = permalink.rstrip("/") + "/.json"
    try:
        r = httpx.get(
            url,
            params={"sort": "top", "limit": limit, "raw_json": 1},
            headers={"User-Agent": _pick_ua()},
            timeout=12.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return []
        return _parse_comments(r.json(), limit)
    except Exception as e:
        logger.debug("fetch_top_comments({}) failed: {}", permalink[:60], e)
        return []


def _published(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed"):
        val = entry.get(field)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return datetime.now(timezone.utc)


def _normalize(entry, *, via_gnews: bool) -> dict | None:
    """Flatten a feed entry into the fields RedditMention needs, or None."""
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None

    if via_gnews:
        raw_id = entry.get("id") or link
        post_id = "gn:" + hashlib.sha1(raw_id.encode()).hexdigest()[:16]
        body = ""
        author = "unknown"
    else:
        post_id = (entry.get("id") or "").strip()
        if not post_id:
            return None
        body_html = ""
        if entry.get("content"):
            body_html = entry["content"][0].get("value", "")
        elif entry.get("summary"):
            body_html = entry["summary"]
        body = _strip_html(body_html)
        author = (entry.get("author") or "").lstrip("/").strip() or "unknown"

    return {
        "post_id": post_id,
        "title": title,
        "body": body,
        "author": author[:100],
        "link": link,
        "created": _published(entry),
    }


def _fetch_entries(sub: str, *, direct_ok: bool = True) -> tuple[list[dict], str]:
    """Direct Reddit RSS first; Google-News site-scoped fallback on failure.
    Returns (normalized_entries, mode) where
    mode ∈ {"direct","gnews","none","403"}. "403" means direct was blocked
    *and* gnews didn't yield — the caller uses it to trip the breaker.
    `direct_ok=False` skips the direct attempt entirely (breaker open).
    """
    blocked = False
    if direct_ok:
        try:
            r = httpx.get(
                _DIRECT_TMPL.format(sub=sub),
                headers={"User-Agent": _pick_ua()},
                timeout=15.0,
                follow_redirects=True,
            )
            if r.status_code == 200:
                parsed = feedparser.parse(r.content)
                entries = [
                    e for e in (
                        _normalize(x, via_gnews=False)
                        for x in parsed.entries[:_MAX_ENTRIES]
                    ) if e
                ]
                if entries:
                    return entries, "direct"
            else:
                blocked = r.status_code in (403, 429)
                logger.debug("reddit r/{} direct HTTP {}", sub, r.status_code)
        except Exception as e:
            logger.debug("reddit r/{} direct fetch failed: {}", sub, e)

    try:
        r = httpx.get(
            _GNEWS_TMPL.format(sub=sub),
            headers={"User-Agent": _BROWSER_UA},
            timeout=20.0,
            follow_redirects=True,
        )
        if r.status_code == 200:
            parsed = feedparser.parse(r.content)
            entries = [
                e for e in (
                    _normalize(x, via_gnews=True)
                    for x in parsed.entries[:_MAX_ENTRIES]
                ) if e
            ]
            return entries, "gnews"
        logger.debug("reddit r/{} gnews HTTP {}", sub, r.status_code)
    except Exception as e:
        logger.debug("reddit r/{} gnews fetch failed: {}", sub, e)

    return [], ("403" if blocked else "none")


def _run() -> None:
    subs = _load_subs()
    if not subs:
        return

    match_tokens, alias = _build_alias_map()
    if not match_tokens:
        logger.info("reddit: empty watchlist, skipping")
        return

    global _direct_cooldown_until

    total_posts = 0
    total_rows = 0
    direct_feeds = 0
    gnews_feeds = 0

    now = datetime.now(timezone.utc)
    direct_ok = _direct_cooldown_until is None or now >= _direct_cooldown_until
    if not direct_ok:
        logger.debug(
            "reddit: direct breaker open until {} — gnews only this cycle",
            _direct_cooldown_until,
        )
    consecutive_403 = 0

    for sub in subs:
        entries, mode = _fetch_entries(sub, direct_ok=direct_ok)
        if mode == "direct":
            direct_feeds += 1
            consecutive_403 = 0
        elif mode == "gnews":
            gnews_feeds += 1
        elif mode == "403":
            consecutive_403 += 1
            if direct_ok and consecutive_403 >= _DIRECT_403_TRIP:
                direct_ok = False
                _direct_cooldown_until = (
                    datetime.now(timezone.utc) + _DIRECT_COOLDOWN
                )
                logger.info(
                    "reddit: direct blocked {}x — tripping breaker, gnews "
                    "only until {}",
                    consecutive_403,
                    _direct_cooldown_until,
                )

        for item in entries:
            total_posts += 1
            hits = extract_tickers(
                f"{item['title']}\n{item['body']}",
                match_tokens,
                title=item["title"],
            )
            if not hits:
                continue
            with session_scope() as session:
                for raw in hits:
                    ticker = alias.get(raw, raw)
                    exists = session.exec(
                        select(RedditMention)
                        .where(RedditMention.post_id == item["post_id"])
                        .where(RedditMention.ticker == ticker)
                    ).first()
                    if exists is not None:
                        continue
                    session.add(
                        RedditMention(
                            subreddit=sub,
                            post_id=item["post_id"],
                            comment_id=None,
                            ticker=ticker,
                            author=item["author"],
                            score=0,
                            num_comments=0,
                            created_at=item["created"],
                            title=item["title"][:500],
                            body_excerpt=item["body"][:500],
                            permalink=item["link"][:1000],
                        )
                    )
                    total_rows += 1

        # Jittered politeness delay — a fixed cadence is itself a scraper
        # fingerprint; skip the wait once the breaker is open (gnews only).
        if direct_ok:
            time.sleep(_PER_FEED_SLEEP + random.uniform(0, 0.6))
        else:
            time.sleep(0.2)

    logger.info(
        "reddit: {} subs ({} direct, {} gnews), {} posts scanned, "
        "{} mentions stored",
        len(subs),
        direct_feeds,
        gnews_feeds,
        total_posts,
        total_rows,
    )
