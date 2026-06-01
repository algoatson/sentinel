"""LLM dossiers for individual calls and news items — cached.

The dashboard surfaces these via click-to-open modals. Caching matters
because the user clicks rows constantly and the LLM call isn't free
(latency + tokens). Once generated, a dossier is stored in CallSummary /
NewsAnalysis and re-served from cache forever; the underlying TradingCall
or NewsItem is effectively immutable for this purpose (their thesis /
title / published_at don't change), so the dossier stays relevant.

Why a dedicated module rather than glue inside `chat.py`:
- `chat.answer_question` has a 500-char question cap and runs a generic
  context retriever; a dossier wants a *focused* context (just this
  call's ticker history / just this news item) without that ceremony.
- Cache logic lives next to the prompt, so a future cache-invalidation
  policy (e.g. regenerate on `ret_5d_pct` arrival) is a one-place change.

Public API:
- `call_dossier(call_id, *, refresh=False) -> str`
- `news_dossier(news_id, *, refresh=False) -> str`
- `ask_about_call(call_id, question) -> str`     # not cached
- `ask_about_news(news_id, question) -> str`     # not cached
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from string import Template

from loguru import logger
from sqlmodel import select

from .db import session_scope
from .llm import LLM_ERROR_SENTINEL, get_llm
from .models import (
    CallSummary,
    Filing,
    NewsAnalysis,
    NewsItem,
    PaperTrade,
    PriceContext,
    RedditAnalysis,
    RedditMention,
    TradingCall,
)


# ── prompt templates ──────────────────────────────────────────────────────


_CALL_DOSSIER_PROMPT = Template("""\
You're analysing one of the bot's trading CALLS. Below is the call + the
relevant context. Write a tight markdown dossier covering, IN THIS ORDER:

**TL;DR**: 1–2 lines — is the call working, broken, or too early?

**Conviction check**: does new info support, weaken, or invalidate the
original thesis?

**Target & timeline**: a realistic price target (a specific number, not a
range) and the expected hold horizon (days/weeks/months). State the
assumption that gets you there.

**Risk**: the single biggest thing that would invalidate this thesis.
Specific, not generic ("supply chain risk" is generic; "China export
controls on $X tighten further" is specific).

**Action**: hold / add / cut / close. One word + one sentence.

Be precise. No hedge language ("could potentially"). No generic finance
filler. If the data is too thin to support a target, say so plainly.

CALL:
$call_json

RECENT CONTEXT:
$context_json
""")


_NEWS_DOSSIER_PROMPT = Template("""\
You're analysing one news item for a trading bot's user. Read the FULL
article body in the context below — do NOT reason from the headline alone.

Write a tight markdown dossier covering, IN THIS ORDER:

**TL;DR**: 1 line — what's the story, in plain language? Use details
from the article body, not just the title.

**Read**: 2–4 lines — what does this *actually* mean for the named
ticker(s), if any? Connect dots the headline doesn't. If macro/no
specific ticker, identify the sectors most affected. Cite specifics
from the article body where possible (numbers, named entities, dates).

**Watchlist impact**: if any name in the watchlist context below is
directly affected, name them and say how (positive / negative / neutral
+ one-line reason). Also identify tickers mentioned in the article body
that are NOT yet in the watchlist but matter for this story.

**Tradeable angle**: is there a setup here a paper-trader would act on,
or is it noise? If tradeable, name the direction and rough timeframe.

Be concrete. Don't pad. If the article body in the context is empty or
a stub (typical of paywalled sources), say so plainly and reason from
just the headline + summary — but flag the limitation in the TL;DR.

NEWS:
$news_json

CONTEXT:
$context_json
""")


_CALL_CHAT_PROMPT = Template("""\
The user is asking a follow-up question about one of the bot's trading
calls. Answer it directly. Use only the call + context provided. If the
question can't be answered from this, say so — don't invent.

CALL:
$call_json

CONTEXT:
$context_json

QUESTION:
$question
""")


_NEWS_CHAT_PROMPT = Template("""\
The user is asking a follow-up question about a news item. Answer it
directly. Use only the news + context provided. If the question can't
be answered from this, say so — don't invent.

NEWS:
$news_json

CONTEXT:
$context_json

QUESTION:
$question
""")


_REDDIT_DOSSIER_PROMPT = Template("""\
You're triaging a single Reddit thread for a trading bot's user. Reddit is
mostly noise — your job is to tell signal from hype. Use the post + its top
comments in the context below.

Write a tight markdown dossier covering, IN THIS ORDER:

**TL;DR**: 1 line — what's the thread actually about, in plain language?

**Signal vs noise**: is this a substantive take (data, a real catalyst, a
specific thesis) or just hype / a meme / a vent? Say which, and why. Weigh
the score + comment count as a popularity signal, not a correctness one.

**Crowd read**: what's the prevailing sentiment in the post + comments
toward the named ticker — and is the crowd early, late, or piling into a
move that already happened? Contrarian flags welcome.

**Tradeable angle**: is there anything here a disciplined paper-trader
would act on, or is it purely sentiment? If tradeable, name the direction
and rough timeframe; if not, say "no edge — sentiment only".

Be skeptical and concrete. No finance filler. If the thread is thin (no
comments, vague title), say so plainly and keep it to the TL;DR.

REDDIT THREAD:
$reddit_json

CONTEXT:
$context_json
""")


_REDDIT_CHAT_PROMPT = Template("""\
The user is asking a follow-up question about a Reddit thread. Answer it
directly using only the thread + context provided. Reddit is noisy — don't
over-weight a single loud comment. If it can't be answered from this, say
so — don't invent.

REDDIT THREAD:
$reddit_json

CONTEXT:
$context_json

QUESTION:
$question
""")


_FILING_CHAT_PROMPT = Template("""\
The user is asking a follow-up question about an SEC filing. Answer it
directly using only the filing metadata + summary + context provided. If
the answer isn't in what you've been given (e.g. it's buried in the full
document, which you don't have), say so plainly — don't invent figures.

FILING:
$filing_json

CONTEXT:
$context_json

QUESTION:
$question
""")


# ── context builders ──────────────────────────────────────────────────────


def _ctx_call(session, call: TradingCall) -> dict:
    """Build the LLM context for a call dossier — PriceContext, recent
    filings (14d), recent news (7d), and any open paper position on the
    same ticker. Short list caps keep tokens reasonable."""
    ticker = call.ticker
    cutoff_news = (datetime.now(timezone.utc) - timedelta(days=7))
    cutoff_filings = (datetime.now(timezone.utc) - timedelta(days=14))
    cutoff_news_naive = cutoff_news.replace(tzinfo=None)
    cutoff_filings_naive = cutoff_filings.replace(tzinfo=None)

    pc = session.get(PriceContext, ticker)
    news = session.exec(
        select(NewsItem)
        .where(NewsItem.ticker == ticker)
        .where(NewsItem.published_at >= cutoff_news_naive)
        .order_by(NewsItem.published_at.desc())
        .limit(8)
    ).all()
    filings = session.exec(
        select(Filing)
        .where(Filing.ticker == ticker)
        .where(Filing.filed_at >= cutoff_filings_naive)
        .order_by(Filing.filed_at.desc())
        .limit(5)
    ).all()
    open_p = session.exec(
        select(PaperTrade)
        .where(PaperTrade.ticker == ticker)
        .where(PaperTrade.status == "open")
    ).first()

    return {
        "ticker": ticker,
        "price_context": (
            {
                "last_price": pc.last_price,
                "change_1d_pct": pc.change_1d_pct,
                "change_5d_pct": pc.change_5d_pct,
                "volume_vs_20d_avg": pc.volume_vs_20d_avg,
            } if pc else None
        ),
        "recent_news": [
            {"title": n.title[:160], "source": n.source,
             "sentiment": n.sentiment, "impact_1d_pct": n.impact_1d_pct,
             "ts": n.published_at.isoformat()}
            for n in news
        ],
        "recent_filings": [
            {"form": f.form_type, "summary": (f.summary or "")[:200],
             "materiality_score": f.materiality_score,
             "ts": f.filed_at.isoformat()}
            for f in filings
        ],
        "open_position": (
            {"side": open_p.side, "qty": open_p.qty,
             "entry": open_p.entry_price,
             "entry_at": open_p.entry_at.isoformat()}
            if open_p else None
        ),
    }


def _ctx_news(session, item: NewsItem) -> dict:
    """Context for a news dossier — the item's ticker context (if any),
    current open paper positions to highlight watchlist-impact, AND the
    fetched article body so the LLM has actual content to reason about
    instead of just the headline.

    The body fetch is cached in `ArticleBody`, so the second-and-later
    open of the same news dossier is a single DB read — no re-fetching."""
    from . import article_fetch
    pc = None
    if item.ticker:
        pc = session.get(PriceContext, item.ticker)
    opens = session.exec(
        select(PaperTrade).where(PaperTrade.status == "open")
    ).all()
    body = article_fetch.fetch_article_text(item.url) if item.url else None
    body_meta = article_fetch.cache_meta(item.url) if item.url else None
    return {
        "article_body": (body or "")[:5500] if body else None,
        "article_body_source": (body_meta or {}).get("source"),
        "article_body_chars": (body_meta or {}).get("char_count"),
        "price_context": (
            {
                "ticker": item.ticker,
                "last_price": pc.last_price,
                "change_1d_pct": pc.change_1d_pct,
                "change_5d_pct": pc.change_5d_pct,
            } if pc else None
        ),
        "open_positions": [
            {"ticker": p.ticker, "side": p.side, "qty": p.qty,
             "entry": p.entry_price}
            for p in opens
        ],
    }


def _call_to_dict(c: TradingCall) -> dict:
    return {
        "ticker": c.ticker,
        "direction": c.direction,
        "conviction": c.conviction,
        "source": c.source,
        "thesis": c.thesis,
        "price_at_call": c.price_at_call,
        "created_at": c.created_at.isoformat(),
        "ret_1d_pct": c.ret_1d_pct,
        "ret_5d_pct": c.ret_5d_pct,
        "ret_20d_pct": c.ret_20d_pct,
        "settled": c.settled,
    }


def _news_to_dict(n: NewsItem) -> dict:
    return {
        "title": n.title,
        "url": n.url,
        "ticker": n.ticker,
        "is_macro": n.is_macro,
        "source": n.source,
        "summary": (n.summary or "")[:600],
        "sentiment": n.sentiment,
        "published_at": n.published_at.isoformat(),
        "impact_1h_pct": n.impact_1h_pct,
        "impact_1d_pct": n.impact_1d_pct,
    }


def _reddit_to_dict(m: RedditMention) -> dict:
    return {
        "subreddit": m.subreddit,
        "ticker": m.ticker,
        "author": (m.author or "").removeprefix("u/"),
        "title": m.title,
        "body_excerpt": (m.body_excerpt or "")[:500],
        "score": m.score,
        "num_comments": m.num_comments,
        "sentiment": m.sentiment,
        "created_at": m.created_at.isoformat(),
        "permalink": m.permalink,
    }


def _ctx_reddit(session, mention: RedditMention) -> dict:
    """Context for a Reddit dossier: the ticker's price snapshot, how loud
    the wider crowd is on that name right now (sibling mentions in the last
    48h), and the thread's live top comments so the LLM judges the actual
    discussion, not just the title."""
    from .ingesters.reddit import fetch_top_comments
    pc = session.get(PriceContext, mention.ticker) if mention.ticker else None
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).replace(tzinfo=None)
    siblings = session.exec(
        select(RedditMention)
        .where(RedditMention.ticker == mention.ticker)
        .where(RedditMention.created_at >= cutoff)
    ).all() if mention.ticker else []
    sib_sent = [m.sentiment for m in siblings if m.sentiment is not None]
    try:
        comments = fetch_top_comments(mention.permalink)
    except Exception as e:  # network/parse — degrade to title-only
        logger.debug("reddit dossier comments({}): {}", mention.id, e)
        comments = []
    return {
        "top_comments": comments[:8],
        "crowd": {
            "ticker": mention.ticker,
            "mentions_48h": len(siblings),
            "avg_sentiment_48h": (
                round(sum(sib_sent) / len(sib_sent), 2) if sib_sent else None
            ),
        },
        "price_context": (
            {
                "ticker": mention.ticker,
                "last_price": pc.last_price,
                "change_1d_pct": pc.change_1d_pct,
                "change_5d_pct": pc.change_5d_pct,
            } if pc else None
        ),
    }


def _filing_to_dict(f: Filing) -> dict:
    return {
        "form_type": f.form_type,
        "ticker": f.ticker,
        "cik": f.cik,
        "summary": (f.summary or "")[:1200],
        "materiality_score": f.materiality_score,
        "materiality_reason": (f.materiality_reason or "")[:600],
        "filed_at": f.filed_at.isoformat(),
        "primary_doc_url": f.primary_doc_url,
    }


def _ctx_filing(session, f: Filing) -> dict:
    """Context for a filing follow-up: the issuer's price snapshot + any
    open paper position on it, so 'what does this mean for my book' is
    answerable."""
    pc = session.get(PriceContext, f.ticker) if f.ticker else None
    opens = session.exec(
        select(PaperTrade).where(PaperTrade.status == "open")
    ).all()
    return {
        "price_context": (
            {
                "ticker": f.ticker,
                "last_price": pc.last_price,
                "change_1d_pct": pc.change_1d_pct,
                "change_5d_pct": pc.change_5d_pct,
            } if pc else None
        ),
        "open_positions": [
            {"ticker": p.ticker, "side": p.side, "qty": p.qty,
             "entry": p.entry_price}
            for p in opens if not f.ticker or p.ticker == f.ticker
        ],
    }


# ── LLM glue ──────────────────────────────────────────────────────────────


def _complete(prompt: str, *, max_tokens: int = 1100) -> str:
    """Single LLM call wrapper — keeps the model + max_tokens choice
    centralised so a future swap (e.g. heavy for dossiers) is one edit."""
    llm = get_llm()
    out = llm.complete(prompt, model="heavy", max_tokens=max_tokens)
    if not out or out == LLM_ERROR_SENTINEL:
        return ""
    return out.strip()


def _model_name() -> str:
    """Best-effort current heavy-model identifier for the cache row.
    Future cache-invalidation could rebuild dossiers whose `model` doesn't
    match the active one — but for now we just record it for forensics."""
    from .config import settings
    return (
        settings.LLM_API_MODEL_HEAVY
        or settings.HEAVY_LLM_API_MODEL
        or settings.LLM_MODEL_HEAVY
        or "unknown"
    )[:120]


# ── public API: dossiers (cached) ─────────────────────────────────────────


def call_dossier(call_id: int, *, refresh: bool = False) -> str:
    """Return the markdown dossier for a call, generating + caching on
    first access. `refresh=True` regenerates regardless of cache."""
    with session_scope() as s:
        if not refresh:
            cached = s.get(CallSummary, call_id)
            if cached:
                return cached.summary
        call = s.get(TradingCall, call_id)
        if call is None:
            return "_Call not found._"
        ctx = _ctx_call(s, call)
        call_d = _call_to_dict(call)

    rendered = _CALL_DOSSIER_PROMPT.safe_substitute(
        call_json=json.dumps(call_d, default=str),
        context_json=json.dumps(ctx, default=str),
    )
    body = _complete(rendered)
    if not body:
        return "_LLM unreachable — try again in a moment._"

    now = datetime.now(timezone.utc)
    model = _model_name()
    with session_scope() as s:
        row = s.get(CallSummary, call_id)
        if row is None:
            s.add(CallSummary(
                call_id=call_id, summary=body, created_at=now, model=model,
            ))
        else:
            row.summary = body
            row.created_at = now
            row.model = model
            s.add(row)
    return body


def news_dossier(news_id: int, *, refresh: bool = False) -> str:
    """Same as `call_dossier` but for a NewsItem."""
    with session_scope() as s:
        if not refresh:
            cached = s.get(NewsAnalysis, news_id)
            if cached:
                return cached.summary
        item = s.get(NewsItem, news_id)
        if item is None:
            return "_News item not found._"
        ctx = _ctx_news(s, item)
        news_d = _news_to_dict(item)

    rendered = _NEWS_DOSSIER_PROMPT.safe_substitute(
        news_json=json.dumps(news_d, default=str),
        context_json=json.dumps(ctx, default=str),
    )
    body = _complete(rendered)
    if not body:
        return "_LLM unreachable — try again in a moment._"

    now = datetime.now(timezone.utc)
    model = _model_name()
    with session_scope() as s:
        row = s.get(NewsAnalysis, news_id)
        if row is None:
            s.add(NewsAnalysis(
                news_id=news_id, summary=body, created_at=now, model=model,
            ))
        else:
            row.summary = body
            row.created_at = now
            row.model = model
            s.add(row)
    return body


def reddit_dossier(mention_id: int, *, refresh: bool = False) -> str:
    """Cached LLM read on one Reddit thread — signal-vs-noise triage of the
    post + its top comments. Same caching philosophy as `news_dossier`."""
    with session_scope() as s:
        if not refresh:
            cached = s.get(RedditAnalysis, mention_id)
            if cached:
                return cached.summary
        m = s.get(RedditMention, mention_id)
        if m is None:
            return "_Reddit thread not found._"
        ctx = _ctx_reddit(s, m)
        reddit_d = _reddit_to_dict(m)

    rendered = _REDDIT_DOSSIER_PROMPT.safe_substitute(
        reddit_json=json.dumps(reddit_d, default=str),
        context_json=json.dumps(ctx, default=str),
    )
    body = _complete(rendered, max_tokens=900)
    if not body:
        return "_LLM unreachable — try again in a moment._"

    now = datetime.now(timezone.utc)
    model = _model_name()
    with session_scope() as s:
        row = s.get(RedditAnalysis, mention_id)
        if row is None:
            s.add(RedditAnalysis(
                mention_id=mention_id, summary=body, created_at=now, model=model,
            ))
        else:
            row.summary = body
            row.created_at = now
            row.model = model
            s.add(row)
    return body


# ── public API: contextual chat (NOT cached) ──────────────────────────────


def ask_about_call(call_id: int, question: str) -> str:
    """Follow-up Q on a call. Not cached — each Q gets a fresh LLM call
    so the user can iterate. Returns markdown."""
    q = (question or "").strip()
    if not q:
        return ""
    with session_scope() as s:
        call = s.get(TradingCall, call_id)
        if call is None:
            return "_Call not found._"
        ctx = _ctx_call(s, call)
        call_d = _call_to_dict(call)
    rendered = _CALL_CHAT_PROMPT.safe_substitute(
        call_json=json.dumps(call_d, default=str),
        context_json=json.dumps(ctx, default=str),
        question=q[:600],
    )
    body = _complete(rendered, max_tokens=900)
    return body or "_LLM unreachable — try again._"


def ask_about_news(news_id: int, question: str) -> str:
    """Same as `ask_about_call` but for a NewsItem."""
    q = (question or "").strip()
    if not q:
        return ""
    with session_scope() as s:
        item = s.get(NewsItem, news_id)
        if item is None:
            return "_News item not found._"
        ctx = _ctx_news(s, item)
        news_d = _news_to_dict(item)
    rendered = _NEWS_CHAT_PROMPT.safe_substitute(
        news_json=json.dumps(news_d, default=str),
        context_json=json.dumps(ctx, default=str),
        question=q[:600],
    )
    body = _complete(rendered, max_tokens=900)
    return body or "_LLM unreachable — try again._"


def ask_about_reddit(mention_id: int, question: str) -> str:
    """Follow-up Q about a Reddit thread. Not cached."""
    q = (question or "").strip()
    if not q:
        return ""
    with session_scope() as s:
        m = s.get(RedditMention, mention_id)
        if m is None:
            return "_Reddit thread not found._"
        ctx = _ctx_reddit(s, m)
        reddit_d = _reddit_to_dict(m)
    rendered = _REDDIT_CHAT_PROMPT.safe_substitute(
        reddit_json=json.dumps(reddit_d, default=str),
        context_json=json.dumps(ctx, default=str),
        question=q[:600],
    )
    body = _complete(rendered, max_tokens=900)
    return body or "_LLM unreachable — try again._"


def ask_about_filing(filing_id: int, question: str) -> str:
    """Follow-up Q about an SEC filing. Not cached. Reasons from the
    filing's stored summary + materiality read + price context — it does
    NOT have the full document text, and the prompt tells it to say so."""
    q = (question or "").strip()
    if not q:
        return ""
    with session_scope() as s:
        f = s.get(Filing, filing_id)
        if f is None:
            return "_Filing not found._"
        ctx = _ctx_filing(s, f)
        filing_d = _filing_to_dict(f)
    rendered = _FILING_CHAT_PROMPT.safe_substitute(
        filing_json=json.dumps(filing_d, default=str),
        context_json=json.dumps(ctx, default=str),
        question=q[:600],
    )
    body = _complete(rendered, max_tokens=900)
    return body or "_LLM unreachable — try again._"


# ── tiny cache-state helpers used by the UI badges ────────────────────────


def call_summary_meta(call_id: int) -> dict | None:
    """Return `{"created_at": iso, "model": str}` if a cached summary
    exists, else None — lets the modal show "cached on X" without
    loading the full body up front."""
    try:
        with session_scope() as s:
            row = s.get(CallSummary, call_id)
            if row is None:
                return None
            return {
                "created_at": (
                    row.created_at if row.created_at.tzinfo
                    else row.created_at.replace(tzinfo=timezone.utc)
                ).isoformat(),
                "model": row.model,
            }
    except Exception as e:
        logger.debug("call_summary_meta({}): {}", call_id, e)
        return None


def news_analysis_meta(news_id: int) -> dict | None:
    try:
        with session_scope() as s:
            row = s.get(NewsAnalysis, news_id)
            if row is None:
                return None
            return {
                "created_at": (
                    row.created_at if row.created_at.tzinfo
                    else row.created_at.replace(tzinfo=timezone.utc)
                ).isoformat(),
                "model": row.model,
            }
    except Exception as e:
        logger.debug("news_analysis_meta({}): {}", news_id, e)
        return None


def reddit_analysis_meta(mention_id: int) -> dict | None:
    try:
        with session_scope() as s:
            row = s.get(RedditAnalysis, mention_id)
            if row is None:
                return None
            return {
                "created_at": (
                    row.created_at if row.created_at.tzinfo
                    else row.created_at.replace(tzinfo=timezone.utc)
                ).isoformat(),
                "model": row.model,
            }
    except Exception as e:
        logger.debug("reddit_analysis_meta({}): {}", mention_id, e)
        return None
