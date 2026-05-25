"""News duplicate-event clustering.

The bot's news ingester already deduplicates by canonical URL — but
Reuters/CNBC/AP often syndicate the SAME story with different titles
and URLs. The hit count for "NVDA earnings beat" then looks bigger
than it really is and the dossier treats them as independent
evidence.

This module clusters recent NewsItems by a lightweight token-set
fingerprint: lowercase, drop stopwords, sort the top-N most-
discriminating tokens, take a hash. Two articles with the same
fingerprint are "the same event"; the dashboard can render a
"+N dupes" badge instead of N separate cards.

Heuristic on purpose — no embeddings, no LLM. Fast (string ops
only) and explainable; if it misses a dup the user can see what the
bot saw."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlmodel import select

from ..db import session_scope
from ..models import NewsItem


_STOPWORDS = {
    "a", "an", "the", "of", "and", "or", "to", "in", "on", "for",
    "with", "by", "as", "is", "was", "are", "were", "be", "been",
    "being", "this", "that", "these", "those", "it", "its", "from",
    "at", "but", "if", "then", "than", "into", "after", "before",
    "over", "under", "up", "down", "out", "about", "more", "less",
    "new", "old", "vs", "amid", "near", "high", "low", "says", "said",
    "report", "reports", "today", "yesterday", "year", "month", "week",
}


def _tokens(title: str) -> list[str]:
    """Normalize a title into a discriminating token list."""
    # Lowercase, drop punctuation, then split.
    cleaned = re.sub(r"[^a-zA-Z0-9 ]+", " ", title.lower())
    parts = [p for p in cleaned.split() if len(p) > 2 and p not in _STOPWORDS]
    return parts


def fingerprint(title: str) -> str:
    """A canonical "event id" for a headline. Same fingerprint means
    we consider the stories to be reports of the same event."""
    toks = _tokens(title)
    if not toks:
        return ""
    # Take the top-8 unique tokens sorted alphabetically. Sorting
    # collapses word-order variants ("NVDA beats earnings" /
    # "Earnings beat for NVDA"); truncation lets us tolerate small
    # extra adjectives.
    uniq = sorted(set(toks))[:8]
    return "-".join(uniq)


def cluster_recent(hours: int = 24) -> dict[str, list[int]]:
    """Group recent news by fingerprint. Returns
    ``{fingerprint: [news_id, ...]}``, sorted within each cluster
    so the oldest item is the "canonical" one."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    buckets: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
    with session_scope() as s:
        rows = s.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff_naive)
        ).all()
        for n in rows:
            fp = fingerprint(n.title or "")
            if fp:
                buckets[fp].append((n.id, n.published_at))
    out: dict[str, list[int]] = {}
    for fp, items in buckets.items():
        if len(items) < 2:
            continue  # only surface actual clusters
        items.sort(key=lambda x: x[1])  # oldest first
        out[fp] = [iid for iid, _ in items]
    return out


def for_news_ids(news_ids: Iterable[int], hours: int = 48) -> dict[int, dict]:
    """For each given news_id, return the cluster it belongs to (if
    any). Used by the API to overlay duplicate counts on the news
    feed."""
    target = set(news_ids)
    clusters = cluster_recent(hours)
    by_id: dict[int, list[int]] = {}
    for member_ids in clusters.values():
        for mid in member_ids:
            if mid in target:
                by_id[mid] = member_ids
    out: dict[int, dict] = {}
    for mid, member_ids in by_id.items():
        sibling_ids = [x for x in member_ids if x != mid]
        out[mid] = {
            "size": len(member_ids),
            "sibling_ids": sibling_ids,
            "is_canonical": mid == member_ids[0],
        }
    return out
