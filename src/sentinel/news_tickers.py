"""Resolve which tickers a news article is actually ABOUT.

The keyword extractor (`utils.extract_tickers`) is fast but mid-tier: it
misses companies named in plain prose whose name isn't in its alias map
("Coinbase launched…" → no $COIN), and it false-positives when a word
collides with a ticker (Nvidia's "RTX" PC brand → Raytheon $RTX). It also
can't tell the subject of a story from a passing mention. yfinance's
per-ticker feed compounds this by handing us a feed-ticker the article
barely concerns.

`resolve_article_tickers` makes a cheap LLM call the AUTHORITY on tagging:
the model identifies the real subject companies from the headline + summary
using its own world knowledge — so it recovers names the keyword matcher
missed AND drops false matches it flagged — and we VALIDATE its output
against the watchlist (the allowlist), which both prevents hallucination and
keeps tagging scoped to names we actually track. The keyword extractor is
kept as a noisy hint to the model and as the deterministic FALLBACK when the
LLM is unavailable or over the per-poll budget.

The call is a reasoning-off JSON classifier (`json_mode=True`), ~tens of
tokens out, run once per ingested article after dedup. Cost is bounded by
the caller's `allow_ai` budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from loguru import logger

from .llm import get_llm, parse_json_response
from .prompts import get_prompt
from .utils import extract_tickers_ranked


@dataclass
class ResolvedTickers:
    """Outcome of resolution. `ranked` is primary-first and is what goes into
    `tickers_csv`; `primary` is the single-`ticker` column (None for a macro /
    private-company / unattributable story). `used_ai` lets the caller
    decrement its per-cycle LLM budget."""

    primary: Optional[str]
    ranked: list[str]
    used_ai: bool = False


def _fallback(
    content_cand: list[str], feed_ticker: Optional[str]
) -> tuple[Optional[str], list[str]]:
    """Deterministic resolution when the LLM isn't used. Heuristic content
    candidates, with a yfinance feed-ticker the content doesn't support demoted
    so it never silently wins as primary."""
    cand = content_cand
    feed_suspect = False
    if feed_ticker:
        fu = feed_ticker.upper()
        if fu in cand:
            pass                       # content backs the feed ticker
        elif not cand:
            cand = [fu]                # nothing else — trust the feed
        else:
            cand = cand + [fu]         # feed says fu, content says other
            feed_suspect = True
    if not cand:
        return None, []
    ranked = cand[:-1] if feed_suspect and len(cand) > 1 else cand
    return ranked[0], ranked


def _ai_resolve(
    title: str, summary: str, candidates: list[str]
) -> Optional[tuple[Optional[str], list[str]]]:
    """One light-model JSON call. Returns ``(primary, tickers)`` — uppercased,
    primary-first — or None on any failure. NOT constrained to `candidates`
    (the model may add a subject the keyword matcher missed); the caller
    validates against the watchlist. `candidates` is passed only as a hint."""
    try:
        tmpl = get_prompt("tag_article_tickers")
        rendered = tmpl.safe_substitute(
            title=title[:300],
            summary=summary[:800],
            candidates=", ".join(candidates) if candidates else "(none detected)",
        )
        raw = get_llm().complete(
            rendered, model="light", json_mode=True, max_tokens=120,
            grounded=False,
        )
    except Exception as e:
        logger.debug("tag_article_tickers LLM call failed: {}", e)
        return None
    parsed = parse_json_response(raw, expect=dict)
    if parsed is None:
        return None

    tickers: list[str] = []
    for t in parsed.get("tickers") or []:
        if not isinstance(t, str):
            continue
        tu = t.strip().upper()
        if tu and tu not in tickers:
            tickers.append(tu)
    primary = parsed.get("primary")
    primary = primary.strip().upper() if isinstance(primary, str) else None

    if primary:
        if primary in tickers:
            tickers.remove(primary)
        tickers.insert(0, primary)
    elif tickers:
        primary = tickers[0]
    return primary, tickers


def resolve_article_tickers(
    title: str,
    summary: str,
    watchlist: Iterable[str],
    *,
    feed_ticker: Optional[str] = None,
    allow_ai: bool = True,
) -> ResolvedTickers:
    """Decide the primary + relevant tickers for one article.

    `feed_ticker` is the symbol whose yfinance feed surfaced the article (None
    for RSS) — used only in the heuristic fallback, since the LLM judges the
    subject from content directly. `allow_ai` is the caller's per-cycle LLM
    budget gate; when False, resolution is purely heuristic.
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    watch_set = {t.upper() for t in watchlist}
    content_cand = extract_tickers_ranked(
        f"{title} {summary}", watch_set, title=title
    )

    if allow_ai:
        ai = _ai_resolve(title, summary, content_cand)
        if ai is not None:
            primary, tickers = ai
            # Validate against the watchlist allowlist: drops anything we
            # don't track (and any hallucinated / malformed symbol), keeping
            # tagging scoped and safe. Order (primary-first) preserved.
            ranked = [t for t in tickers if t in watch_set]
            if primary and primary in watch_set:
                if primary in ranked:
                    ranked.remove(primary)
                ranked.insert(0, primary)
            else:
                primary = ranked[0] if ranked else None
            return ResolvedTickers(primary=primary, ranked=ranked, used_ai=True)
        # AI attempted but failed — fall back to the heuristic, and still count
        # the attempt against the caller's budget so an LLM outage can't make
        # us retry (and re-bill) every article in the cycle.
        primary, ranked = _fallback(content_cand, feed_ticker)
        return ResolvedTickers(primary=primary, ranked=ranked, used_ai=True)

    # No AI (over budget / disabled) — pure heuristic.
    primary, ranked = _fallback(content_cand, feed_ticker)
    return ResolvedTickers(primary=primary, ranked=ranked, used_ai=False)
