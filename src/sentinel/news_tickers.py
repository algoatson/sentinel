"""Resolve which tickers a news article is actually ABOUT.

The keyword extractor (`utils.extract_tickers`) is generous by design — it
tags any watchlist name/cashtag it sees so headline-grade news written
without cashtags ("Nvidia announced…") still gets linked. That generosity,
plus yfinance's per-ticker feed (which surfaces loosely-related / syndicated
stories under a ticker the article barely mentions), produces the wrong
*primary* ticker: e.g. "Snowflake's Partnership With Amazon" arriving in
NVDA's yfinance feed and getting stamped ``ticker=NVDA`` even though the body
never names Nvidia.

`resolve_article_tickers` is the precision layer on top of the heuristic. It:

1. Generates CANDIDATES heuristically (free, deterministic — the existing
   `extract_tickers_ranked`).
2. Treats a yfinance feed-ticker the *content* doesn't support as a SUSPECT
   candidate rather than a guaranteed primary, and demotes it.
3. For genuinely ambiguous items (≥2 candidates, or a suspect feed-ticker)
   asks the light model — CONSTRAINED to the candidate set, so it can never
   invent a ticker — which one is the subject and which mentions are real.
   Clear single-subject items skip the LLM entirely (the cheap common path).
4. Falls back to the heuristic ranking whenever the LLM is unavailable, over
   budget, or returns nothing usable.

Cost is bounded by the caller via `allow_ai` (a per-cycle budget) plus the
ambiguity gate, so steady-state news polling stays cheap. The call is a
reasoning-off JSON classifier (`json_mode=True`), ~tens of tokens out.
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
    `tickers_csv`; `primary` is the single-`ticker` column (may be None for a
    macro / unattributable story). `used_ai` lets the caller decrement its
    per-cycle LLM budget."""

    primary: Optional[str]
    ranked: list[str]
    used_ai: bool = False


def _heuristic(
    title: str, summary: str, watchlist: Iterable[str], feed_ticker: Optional[str]
) -> tuple[list[str], bool]:
    """Candidate generation + feed-ticker reconciliation.

    Returns ``(candidates_primary_first, feed_suspect)``. `feed_suspect` is
    True when a yfinance feed-ticker is present but the article's own text
    points at *other* tickers instead — the case that produced the wrong
    primary before.
    """
    cand = extract_tickers_ranked(f"{title} {summary}", watchlist, title=title)
    feed_suspect = False
    if feed_ticker:
        fu = feed_ticker.upper()
        if fu in cand:
            pass  # content backs the feed ticker — trust it, no suspicion
        elif not cand:
            cand = [fu]  # nothing else to go on — trust the feed
        else:
            # The feed says `fu`, but the content is about other names. Keep
            # `fu` as a last-resort candidate so the LLM can still vindicate
            # it, but flag the conflict so the feed never silently wins.
            cand = cand + [fu]
            feed_suspect = True
    return cand, feed_suspect


def _ai_resolve(
    title: str, summary: str, candidates: list[str]
) -> Optional[tuple[Optional[str], list[str]]]:
    """One constrained light-model JSON call. Returns ``(primary, ranked)`` or
    None on any failure. The result is intersected with `candidates` so the
    model can never introduce a ticker that wasn't already detected."""
    cand_set = {c.upper() for c in candidates}
    try:
        tmpl = get_prompt("tag_article_tickers")
        rendered = tmpl.safe_substitute(
            title=title[:300],
            summary=summary[:800],
            candidates=", ".join(candidates),
        )
        # Pure classifier: no date/world grounding needed, tiny output.
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

    ranked: list[str] = []
    for t in parsed.get("tickers") or []:
        if not isinstance(t, str):
            continue
        tu = t.strip().upper()
        if tu in cand_set and tu not in ranked:
            ranked.append(tu)

    primary = parsed.get("primary")
    primary = primary.strip().upper() if isinstance(primary, str) else None
    if primary is not None and primary not in cand_set:
        primary = None  # hallucinated / out-of-set primary → discard

    # Reconcile: primary must lead `ranked`.
    if primary:
        if primary in ranked:
            ranked.remove(primary)
        ranked.insert(0, primary)
    elif ranked:
        primary = ranked[0]
    return primary, ranked


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
    for RSS). `allow_ai` is the caller's per-cycle LLM budget gate — when
    False, resolution is purely heuristic (still applying the feed-trust fix).
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    cand, feed_suspect = _heuristic(title, summary, watchlist, feed_ticker)

    if not cand:
        return ResolvedTickers(primary=None, ranked=[])

    # Only spend an LLM call when the answer is genuinely ambiguous: several
    # candidates, or a feed-ticker the body doesn't support. A lone, clearly
    # supported candidate is taken as-is (the cheap steady-state path).
    ambiguous = len(cand) >= 2 or feed_suspect
    if not (allow_ai and ambiguous):
        # Heuristic-only. Still demote an unsupported feed ticker — content
        # beats the feed even when we don't pay for the LLM.
        ranked = cand[:-1] if feed_suspect and len(cand) > 1 else cand
        return ResolvedTickers(primary=ranked[0], ranked=ranked)

    ai = _ai_resolve(title, summary, cand)
    if ai is None:
        # LLM unavailable / unparseable — fall back to the demoted heuristic.
        ranked = cand[:-1] if feed_suspect and len(cand) > 1 else cand
        return ResolvedTickers(primary=ranked[0], ranked=ranked, used_ai=True)

    primary, ranked = ai
    return ResolvedTickers(primary=primary, ranked=ranked, used_ai=True)
