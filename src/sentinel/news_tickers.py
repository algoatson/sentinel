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
    decrement its per-cycle LLM budget. `tag_source` records HOW the tags were
    decided (search+ai / html+ai / ai / heuristic) so a NewsItem can carry its
    provenance for the dashboard + the news_retag upgrade job."""

    primary: Optional[str]
    ranked: list[str]
    used_ai: bool = False
    tag_source: Optional[str] = None


def _fallback(
    source_cand: list[str],
    heuristic: list[str],
    feed_ticker: Optional[str],
) -> tuple[Optional[str], list[str]]:
    """Deterministic resolution when the LLM isn't used. Union of the
    (already watchlist-gated) structured search candidates and the heuristic
    content candidates — heuristic first so a title-backed name stays primary.

    The feed/query ticker is special: it's ALWAYS present in `source_cand`
    (the search was keyed on it), so "is it in the candidate set" can't tell us
    whether the story is actually about it. We judge "backed" against the
    CONTENT heuristic only; an unbacked feed/query ticker is the contamination
    we're fixing, so it's dropped entirely (never primary, never a tag) unless
    nothing else surfaced. Primary = title rank | source_cand[0] | feed."""
    fu = feed_ticker.upper() if feed_ticker else None
    backed = fu is not None and fu in heuristic
    ranked: list[str] = []
    for t in [*heuristic, *source_cand]:
        if t == fu and not backed:
            continue                   # unbacked feed/query contamination
        if t not in ranked:
            ranked.append(t)
    if fu and not ranked:
        ranked = [fu]                  # nothing else surfaced — trust the feed
    if not ranked:
        return None, []
    return ranked[0], ranked


# Cap on the tracked-universe sample handed to the model. Bounds prompt token
# cost on this high-volume per-article call. The sample is purely illustrative
# (the prompt says so) and the FULL watchlist is the real gate in code — so a
# name's absence from the sample must never make the model drop it.
_WATCH_CONTEXT_CAP = 60


def _watch_context(watch_set: set[str]) -> str:
    """A short, deterministic, alphabetically-SPREAD sample of the watchlist for
    the prompt. When the watchlist exceeds the cap we stride-sample across the
    sorted set (not just the first N) so the sample spans A–Z rather than the
    alphabet's head — a head-only slice could nudge a light model into treating
    a mid-alphabet name as 'untracked'. Deterministic (stride by length); the
    prompt frames it as a partial, non-authoritative hint."""
    if not watch_set:
        return ""
    ordered = sorted(watch_set)
    if len(ordered) <= _WATCH_CONTEXT_CAP:
        return ", ".join(ordered)
    step = len(ordered) / _WATCH_CONTEXT_CAP
    sample = [ordered[int(i * step)] for i in range(_WATCH_CONTEXT_CAP)]
    return ", ".join(sample) + ", …"


def _ai_resolve(
    title: str, summary: str, candidates: list[str], watch_context: str
) -> Optional[tuple[Optional[str], list[str]]]:
    """One light-model JSON call. Returns ``(primary, tickers)`` — uppercased,
    primary-first — or None on any failure. NOT constrained to `candidates`
    (the model may add a subject the keyword matcher missed); the caller
    validates against the watchlist. `candidates` is the anchored hint set;
    `watch_context` is a short sample of the tracked universe."""
    try:
        tmpl = get_prompt("tag_article_tickers")
        rendered = tmpl.safe_substitute(
            title=title[:300],
            summary=summary[:800],
            candidates=", ".join(candidates) if candidates else "(none detected)",
            watchlist_sample=watch_context or "(unavailable)",
        )
        raw = get_llm().complete(
            # 160 (was 120): the prompt now invites listing every materially-
            # affected name, so a multi-ticker story's JSON can run a little
            # longer; the extra headroom avoids mid-array truncation (which is
            # fail-open to the heuristic, but loses the AI's richer tagging).
            rendered, model="light", json_mode=True, max_tokens=160,
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
    source_tickers: Optional[list[str]] = None,
    feed_ticker: Optional[str] = None,
    source_label: str = "search",
    allow_ai: bool = True,
) -> ResolvedTickers:
    """Decide the primary + relevant tickers for one article.

    `source_tickers` is a structured, already-normalised, subject-first
    candidate set (Yahoo search `relatedTickers` for the yfinance path, article
    HTML tags for the retag job; None for RSS). It's intersected with the
    watchlist and unioned with the keyword heuristic to form the ANCHORED hint
    set the LLM reasons from — the model still reads title+summary and may add
    an affected name beyond the hints OR drop a query-contaminated one. The
    final set is always the LLM output ∩ watchlist, so an untracked ticker is
    never stored.

    `feed_ticker` is the symbol whose feed/search surfaced the article — used
    to demote query contamination in the fallback (and the model is told to
    drop it when the text doesn't back it). `source_label` tags the provenance
    string ("search"/"html") when structured candidates drove resolution.
    `allow_ai` is the caller's per-cycle LLM budget gate; when False (or on AI
    failure) resolution is the deterministic union fallback.
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    watch_set = {t.upper() for t in watchlist}

    # Structured candidates, watchlist-gated, order (subject-first) preserved.
    source_cand: list[str] = []
    for t in source_tickers or []:
        tu = t.upper()
        if tu in watch_set and tu not in source_cand:
            source_cand.append(tu)

    heuristic = extract_tickers_ranked(
        f"{title} {summary}", watch_set, title=title
    )

    # Union hints for the model: structured priors first (the search subject is
    # a strong prior), then any heuristic names it missed.
    hints: list[str] = []
    for t in [*source_cand, *heuristic]:
        if t not in hints:
            hints.append(t)

    if allow_ai:
        ai = _ai_resolve(title, summary, hints, _watch_context(watch_set))
        if ai is not None:
            primary, tickers = ai
            # Validate against the watchlist allowlist: drops anything we
            # don't track (and any hallucinated / malformed symbol), keeping
            # tagging scoped and safe. Order (primary-first) preserved. The LLM
            # MAY have added an affected name absent from the hints — that's
            # allowed as long as it's watchlisted.
            ranked = [t for t in tickers if t in watch_set]
            if primary and primary in watch_set:
                if primary in ranked:
                    ranked.remove(primary)
                ranked.insert(0, primary)
            else:
                primary = ranked[0] if ranked else None
            tag_source = f"{source_label}+ai" if source_cand else "ai"
            return ResolvedTickers(
                primary=primary, ranked=ranked, used_ai=True,
                tag_source=tag_source,
            )
        # AI attempted but failed — fall back to the union heuristic, and still
        # count the attempt against the caller's budget so an LLM outage can't
        # make us retry (and re-bill) every article in the cycle.
        primary, ranked = _fallback(source_cand, heuristic, feed_ticker)
        return ResolvedTickers(
            primary=primary, ranked=ranked, used_ai=True, tag_source="heuristic",
        )

    # No AI (over budget / disabled) — deterministic union fallback.
    primary, ranked = _fallback(source_cand, heuristic, feed_ticker)
    return ResolvedTickers(
        primary=primary, ranked=ranked, used_ai=False, tag_source="heuristic",
    )
